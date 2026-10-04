import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from vault_shared import DependencyUnavailableError, UnauthorizedError, get_logger
from vault_shared.db.models import EnrichmentJob, File, RelationshipType, StorageConnector
from vault_shared.db.repositories import (
    EnrichmentEventRepository,
    EnrichmentJobRepository,
    EnrichmentProgressRepository,
    FileClassificationRepository,
    FileExtractionRepository,
    FileMetadataRepository,
    FileRelationshipRepository,
    FileRepository,
    KnowledgeAttributeRepository,
    StorageConnectorRepository,
)
from vault_shared.storage import StorageAdapterProvider
from worker.enrichment.extraction import ContentExtractionService
from worker.enrichment.processors import DEFAULT_PIPELINE, EnrichmentContext, FileProcessor
from worker.enrichment.relationships import RelationshipDiscoveryService

logger = get_logger("worker.enrichment.enrichment_service")

# A default `FileMetadata` contribution for every field a per-file processor
# might set — filled in by whichever processors actually run, so `upsert`
# always receives a complete set of keyword arguments regardless of which
# processors are enabled (Phase 5 spec's "allow individual processors to be
# enabled or disabled"). `duplicate_group_key` is deliberately excluded:
# it's written only by the later relationship-discovery pass, never by a
# per-file processor, so it must never be reset to None here on a rerun.
_METADATA_DEFAULTS: dict = {
    "normalized_extension": None,
    "mime_type_validated": True,
    "mime_mismatch_reason": None,
    "naming_pattern": None,
    "version_label": None,
    "owner_summary": None,
    "sharing_summary": None,
    "language": None,
}


class EnrichmentCancelled(Exception):
    """Mirrors `ScanCancelled` — unwinds `run()` once cooperative
    cancellation has been observed, without treating it as a failure."""


class EnrichmentService:
    """The Metadata Engine + Knowledge Builder (Handbook §8.3/§8.4) —
    processes every file a connector's Storage Scanner has discovered that
    isn't yet enriched (or has since been rescanned), running the
    deterministic processor pipeline per file, extracting content where a
    supported format exists, then discovering cross-file relationships in
    a bounded second pass. Never calls the AI Gateway (Phase 5 spec:
    "prioritize deterministic processing... over AI usage") and never
    modifies `File`/`Folder` rows — only ever adds enrichment records
    alongside them (Design Principles: "never overwrite raw scan data")."""

    def __init__(
        self,
        db: Session,
        *,
        storage: StorageAdapterProvider,
        pipeline: list[FileProcessor] | None = None,
    ) -> None:
        self._db = db
        self._storage = storage
        self._connectors = StorageConnectorRepository(db)
        self._files = FileRepository(db)
        self._file_metadata = FileMetadataRepository(db)
        self._classifications = FileClassificationRepository(db)
        self._extractions = FileExtractionRepository(db)
        self._knowledge_attributes = KnowledgeAttributeRepository(db)
        self._relationships = FileRelationshipRepository(db)
        self._jobs = EnrichmentJobRepository(db)
        self._progress = EnrichmentProgressRepository(db)
        self._events = EnrichmentEventRepository(db)
        self._extraction_service = ContentExtractionService()
        self._relationship_service = RelationshipDiscoveryService()
        self._pipeline = pipeline if pipeline is not None else DEFAULT_PIPELINE

    def run(self, enrichment_job_id: uuid.UUID) -> None:
        job = self._jobs.get_by_id(enrichment_job_id)
        if job is None:
            logger.warning(
                "enrichment_job_not_found", extra={"enrichment_job_id": str(enrichment_job_id)}
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
        self._events.record(enrichment_job_id=job.id, event_type="enrichment_started")
        self._db.commit()

        try:
            self._check_cancelled(job.id)
            pending_files = self._files.list_pending_enrichment_for_connector(job.connector_id)
            self._progress.set_files_pending(job.id, len(pending_files))
            self._db.commit()

            for file in pending_files:
                self._check_cancelled(job.id)
                self._progress.set_current_file(job.id, file.name)
                self._db.commit()
                self._process_one_file(job, connector, file)
                self._db.commit()

            self._check_cancelled(job.id)
            self._discover_relationships(job, connector)
            self._db.commit()
        except EnrichmentCancelled:
            self._jobs.mark_cancelled(job)
            self._events.record(enrichment_job_id=job.id, event_type="enrichment_cancelled")
            self._db.commit()
            return
        except DependencyUnavailableError:
            # Left RUNNING, not FAILED — `worker.tasks.enrichment.run_enrichment`
            # retries the whole job on this specific error (same pattern as
            # ADR-016's scanner retry).
            logger.warning(
                "enrichment_job_dependency_unavailable", extra={"enrichment_job_id": str(job.id)}
            )
            self._db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - job execution boundary must never crash the worker
            logger.exception("enrichment_job_failed", extra={"enrichment_job_id": str(job.id)})
            self._db.rollback()
            self._jobs.mark_failed(job, error=str(exc))
            self._events.record(
                enrichment_job_id=job.id, event_type="enrichment_failed", message=str(exc)
            )
            self._db.commit()
            return

        self._jobs.mark_completed(job)
        self._events.record(enrichment_job_id=job.id, event_type="enrichment_completed")
        self._db.commit()

    def _process_one_file(
        self, job: EnrichmentJob, connector: StorageConnector, file: File
    ) -> None:
        try:
            self._enrich_file(connector, file)
            self._progress.increment_processed(job.id)
        except (DependencyUnavailableError, UnauthorizedError):
            # A Drive-connectivity or token problem affects every remaining
            # file identically — a whole-job condition, not a per-file one.
            raise
        except Exception as exc:  # noqa: BLE001 - one file's failure must not stop the job
            logger.exception(
                "file_enrichment_failed", extra={"file_id": str(file.id), "job_id": str(job.id)}
            )
            self._db.rollback()
            self._progress.increment_failed(job.id)
            self._events.record(
                enrichment_job_id=job.id,
                event_type="file_enrichment_failed",
                message=str(exc),
                metadata={"file_id": str(file.id), "file_name": file.name},
            )

    def _enrich_file(self, connector: StorageConnector, file: File) -> None:
        adapter = self._storage.adapter_for(connector)
        outcome = self._extraction_service.extract(storage=adapter, file=file)

        now = datetime.now(UTC)
        self._extractions.upsert(
            file_id=file.id,
            status=outcome.status,
            extractor_name=outcome.extractor_name,
            extracted_text=outcome.text,
            char_count=outcome.char_count,
            error=outcome.error,
            extracted_at=now,
        )

        context = EnrichmentContext(file=file, extraction=outcome)
        metadata = dict(_METADATA_DEFAULTS)
        classification: tuple[str, float, str] | None = None
        knowledge_attributes: list[dict] = []

        for processor in self._pipeline:
            try:
                result = processor.run(context)
            except Exception as exc:  # noqa: BLE001 - one processor's bug must not skip the rest
                logger.warning(
                    "metadata_processor_failed",
                    extra={"processor": processor.name, "file_id": str(file.id), "error": str(exc)},
                )
                continue

            metadata.update(result.metadata)
            if result.classification is not None and (
                classification is None or result.classification[1] > classification[1]
            ):
                classification = result.classification
            knowledge_attributes.extend(result.knowledge_attributes)

        self._file_metadata.upsert(file_id=file.id, enriched_at=now, **metadata)

        if classification is not None:
            document_type, confidence, method = classification
            self._classifications.upsert(
                file_id=file.id,
                document_type=document_type,
                confidence=confidence,
                method=method,
                classified_at=now,
            )

        self._knowledge_attributes.replace_for_file(file.id, knowledge_attributes)

    def _discover_relationships(self, job: EnrichmentJob, connector: StorageConnector) -> None:
        all_files = self._files.list_all_for_connector(job.connector_id)
        files_by_id = {file.id: file for file in all_files}
        discovered = self._relationship_service.discover(all_files)

        now = datetime.now(UTC)
        for relationship in discovered:
            self._relationships.upsert(
                connector_id=connector.id,
                file_id=relationship.file_id,
                related_file_id=relationship.related_file_id,
                relationship_type=relationship.relationship_type,
                confidence=relationship.confidence,
                metadata=relationship.metadata,
                discovered_at=now,
            )
            if relationship.relationship_type == RelationshipType.DUPLICATE_CANDIDATE:
                checksum = files_by_id[relationship.file_id].checksum
                self._file_metadata.set_duplicate_group_key(relationship.file_id, checksum)
                self._file_metadata.set_duplicate_group_key(relationship.related_file_id, checksum)

        self._events.record(
            enrichment_job_id=job.id,
            event_type="relationships_discovered",
            metadata={"count": len(discovered)},
        )

    def _check_cancelled(self, enrichment_job_id: uuid.UUID) -> None:
        if self._jobs.is_cancel_requested(enrichment_job_id):
            raise EnrichmentCancelled()
