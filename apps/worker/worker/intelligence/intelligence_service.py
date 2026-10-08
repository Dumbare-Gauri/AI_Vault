import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from vault_shared import (
    AIUnavailableError,
    DependencyUnavailableError,
    UnauthorizedError,
    get_logger,
)
from vault_shared.ai_gateway import AIGateway
from vault_shared.ai_gateway.org_completion_provider import resolve_org_completion_provider
from vault_shared.db.models import File, IntelligenceJob, IntelligenceStatus
from vault_shared.db.repositories import (
    FileClassificationRepository,
    FileExtractionRepository,
    FileIntelligenceRepository,
    FileRepository,
    IntelligenceEventRepository,
    IntelligenceJobRepository,
    IntelligenceProgressRepository,
    StorageConnectorRepository,
)
from worker.intelligence.parsing import IntelligenceParseError, parse_response
from worker.intelligence.prompt import build_messages

logger = get_logger("worker.intelligence.intelligence_service")

# `ExtractiveCompletionProvider.name` — checked, not imported, so this
# service depends only on the `AIGateway` boundary, same as every other
# caller of `AIGateway.complete()`.
_STUB_PROVIDER_NAME = "extractive_fallback"


class IntelligenceCancelled(Exception):
    """Mirrors `EnrichmentCancelled`/`EmbeddingCancelled` — unwinds `run()`
    once cooperative cancellation has been observed, without treating it
    as a failure."""


class IntelligenceService:
    """Phase 2's AI File Intelligence pipeline — analyzes every file whose
    content extraction succeeded and isn't already analyzed at the
    currently-configured provider/model, producing a `FileIntelligence`
    row per file (summary, entities, structured metadata, topics).

    Structurally mirrors `EnrichmentService`, not `EmbeddingService` — this
    service, unlike Embedding, calls a real hosted LLM
    (`OpenAICompatibleCompletionProvider`) and so has a genuine external-
    connectivity failure mode (`DependencyUnavailableError`) that must
    leave the job RUNNING for the Celery task to retry, exactly like
    Enrichment's Drive-connectivity handling."""

    def __init__(self, db: Session, *, ai_gateway: AIGateway) -> None:
        self._db = db
        self._ai_gateway = ai_gateway
        self._connectors = StorageConnectorRepository(db)
        self._files = FileRepository(db)
        self._extractions = FileExtractionRepository(db)
        self._classifications = FileClassificationRepository(db)
        self._intelligence = FileIntelligenceRepository(db)
        self._jobs = IntelligenceJobRepository(db)
        self._progress = IntelligenceProgressRepository(db)
        self._events = IntelligenceEventRepository(db)

    def run(self, intelligence_job_id: uuid.UUID) -> None:
        job = self._jobs.get_by_id(intelligence_job_id)
        if job is None:
            logger.warning(
                "intelligence_job_not_found",
                extra={"intelligence_job_id": str(intelligence_job_id)},
            )
            return

        connector = self._connectors.get_by_id(job.connector_id)
        if connector is None:
            self._jobs.mark_failed(job, error="Connector no longer exists.")
            self._db.commit()
            return

        self._jobs.mark_running(job)
        if self._progress.get_for_job(job.id) is None:
            self._progress.create_for_job(job.id)
        self._events.record(intelligence_job_id=job.id, event_type="intelligence_started")
        self._db.commit()

        # An org's own AI provider config (if any) takes over for this
        # job's completion calls — resolved before the stub-check below so
        # the gate reflects the provider actually in use, not the
        # process-wide default. One `IntelligenceService` instance serves
        # exactly one `run()` call (a fresh instance per Celery task), so
        # reassigning `self._ai_gateway` here is safe.
        org_completion_provider = resolve_org_completion_provider(
            self._db, connector.organization_id
        )
        if org_completion_provider is not None:
            self._ai_gateway = self._ai_gateway.with_completion_provider(org_completion_provider)

        if self._ai_gateway.completion_provider_name == _STUB_PROVIDER_NAME:
            # Fail the job cleanly rather than running every pending file
            # through a provider that can't return usable JSON — see the
            # module docstring / plan for why this is a job-level check,
            # not per-file noise.
            self._jobs.mark_failed(
                job,
                error="No completion provider configured — set COMPLETION_PROVIDER and "
                "COMPLETION_API_KEY.",
            )
            self._events.record(
                intelligence_job_id=job.id,
                event_type="intelligence_completion_provider_not_configured",
            )
            self._db.commit()
            return

        try:
            self._check_cancelled(job.id)
            pending_files = self._files.list_pending_intelligence_for_connector(
                job.connector_id,
                provider=self._ai_gateway.completion_provider_name,
                model_name=self._ai_gateway.completion_model_name,
            )
            self._progress.set_files_pending(job.id, len(pending_files))
            self._db.commit()

            for file in pending_files:
                self._check_cancelled(job.id)
                self._progress.set_current_file(job.id, file.name)
                self._db.commit()
                self._process_one_file(job, file)
                self._db.commit()
        except IntelligenceCancelled:
            self._jobs.mark_cancelled(job)
            self._events.record(intelligence_job_id=job.id, event_type="intelligence_cancelled")
            self._db.commit()
            return
        except AIUnavailableError as exc:
            if exc.retryable:
                logger.warning(
                    "intelligence_job_dependency_unavailable",
                    extra={"intelligence_job_id": str(job.id), "reason": exc.reason},
                )
                self._db.rollback()
                raise
            # A bad key, exhausted credits, or an unusable response won't fix
            # itself on retry — fail the job now with a clear reason instead
            # of burning the whole retry budget.
            logger.warning(
                "intelligence_job_failed_non_retryable",
                extra={"intelligence_job_id": str(job.id), "reason": exc.reason},
            )
            self._db.rollback()
            self._jobs.mark_failed(job, error=f"AI provider error: {exc.reason}")
            self._events.record(
                intelligence_job_id=job.id,
                event_type="intelligence_failed",
                message=f"AI provider error: {exc.reason}",
            )
            self._db.commit()
            return
        except DependencyUnavailableError:
            # Left RUNNING, not FAILED — `worker.tasks.intelligence.run_intelligence`
            # retries the whole job on this specific error, same pattern as
            # enrichment's Drive-connectivity retry.
            logger.warning(
                "intelligence_job_dependency_unavailable",
                extra={"intelligence_job_id": str(job.id)},
            )
            self._db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - job execution boundary must never crash the worker
            logger.exception(
                "intelligence_job_failed", extra={"intelligence_job_id": str(job.id)}
            )
            self._db.rollback()
            self._jobs.mark_failed(job, error=str(exc))
            self._events.record(
                intelligence_job_id=job.id, event_type="intelligence_failed", message=str(exc)
            )
            self._db.commit()
            return

        self._jobs.mark_completed(job)
        self._events.record(intelligence_job_id=job.id, event_type="intelligence_completed")
        self._db.commit()

    def _process_one_file(self, job: IntelligenceJob, file: File) -> None:
        try:
            self._analyze_file(file)
            self._progress.increment_processed(job.id)
        except AIUnavailableError as exc:
            # An unusable response to *this file's* prompt is a per-file
            # problem; every other AI failure (auth, quota, rate limit,
            # unreachable) affects the whole job identically.
            if exc.reason != AIUnavailableError.INVALID_RESPONSE:
                raise
            self._record_file_failure(job, file, exc)
        except (DependencyUnavailableError, UnauthorizedError):
            # A provider-connectivity or auth problem affects every
            # remaining file identically — a whole-job condition.
            raise
        except Exception as exc:  # noqa: BLE001 - one file's failure must not stop the job
            self._record_file_failure(job, file, exc)

    def _record_file_failure(self, job: IntelligenceJob, file: File, exc: Exception) -> None:
        logger.exception(
            "file_intelligence_failed",
            extra={"file_id": str(file.id), "job_id": str(job.id)},
        )
        self._db.rollback()
        self._progress.increment_failed(job.id)
        self._events.record(
            intelligence_job_id=job.id,
            event_type="file_intelligence_failed",
            message=str(exc),
            metadata={"file_id": str(file.id), "file_name": file.name},
        )

    def _analyze_file(self, file: File) -> None:
        extraction = self._extractions.get_by_file_id(file.id)
        now = datetime.now(UTC)
        if extraction is None or not extraction.extracted_text:
            # The "pending" query already filters to successfully-extracted
            # files; this is just defensive against a race with a
            # concurrent re-extraction clearing the text mid-job. Never
            # call the LLM with nothing to work with.
            self._intelligence.upsert(
                file_id=file.id,
                status=IntelligenceStatus.UNSUPPORTED,
                document_type=None,
                summary=None,
                entities=[],
                structured_metadata={},
                topics=[],
                confidence=None,
                provider=self._ai_gateway.completion_provider_name,
                model_name=self._ai_gateway.completion_model_name,
                error=None,
                processed_at=now,
            )
            return

        classification = self._classifications.get_by_file_id(file.id)
        messages = build_messages(
            file=file, classification=classification, extracted_text=extraction.extracted_text
        )

        try:
            result = self._ai_gateway.complete(messages=messages, context=None, max_tokens=1024)
            parsed = parse_response(result.text)
        except IntelligenceParseError as exc:
            # An unusable response is an anticipated, structured outcome —
            # same treatment `ContentExtractionService` gives an unreadable
            # PDF (`FileExtraction.status = FAILED`, not a raised
            # exception): recorded here and counted as *processed*, not
            # `progress.files_failed`. `progress.files_failed` (see
            # `_process_one_file`'s except block) stays reserved for a
            # genuinely unexpected crash — the provider itself erroring,
            # not the provider successfully responding with junk.
            self._intelligence.upsert(
                file_id=file.id,
                status=IntelligenceStatus.FAILED,
                document_type=None,
                summary=None,
                entities=[],
                structured_metadata={},
                topics=[],
                confidence=None,
                provider=self._ai_gateway.completion_provider_name,
                model_name=self._ai_gateway.completion_model_name,
                error=str(exc)[:2048],
                processed_at=now,
            )
            return

        self._intelligence.upsert(
            file_id=file.id,
            status=IntelligenceStatus.SUCCESS,
            document_type=parsed.document_type,
            summary=parsed.summary,
            entities=parsed.entities,
            structured_metadata=parsed.structured_metadata,
            topics=parsed.topics,
            confidence=parsed.confidence,
            provider=result.provider,
            model_name=result.model_name,
            error=None,
            processed_at=now,
        )

    def _check_cancelled(self, intelligence_job_id: uuid.UUID) -> None:
        if self._jobs.is_cancel_requested(intelligence_job_id):
            raise IntelligenceCancelled()
