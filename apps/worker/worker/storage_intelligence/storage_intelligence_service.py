import uuid

from sqlalchemy.orm import Session

from vault_shared import get_logger
from vault_shared.db.models import File, StorageAnalysisJob
from vault_shared.db.repositories import (
    DuplicateGroupRepository,
    FileRepository,
    StorageAnalysisEventRepository,
    StorageAnalysisJobRepository,
    StorageAnalysisSnapshotRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
)
from worker.storage_intelligence.candidate_analyzer import CandidateAnalyzer
from worker.storage_intelligence.duplicate_detector import DuplicateDetector
from worker.storage_intelligence.savings_analyzer import SavingsAnalyzer
from worker.storage_intelligence.storage_analyzer import StorageAnalyzer

logger = get_logger("worker.storage_intelligence.storage_intelligence_service")


class StorageIntelligenceService:
    """The Storage Intelligence Layer's orchestrator (Phase 1) — mirrors
    `RecommendationService` exactly: organization-scoped, a single
    deterministic pass over already-stored data (no external I/O, no
    per-file network calls), one job per run, one snapshot per run.
    Delegates each concern to its own analyzer (`DuplicateDetector`,
    `StorageAnalyzer`, `CandidateAnalyzer`, `SavingsAnalyzer`) rather than
    one large service class (Phase 1 spec §14: "keep individual
    responsibilities separated")."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._files = FileRepository(db)
        self._jobs = StorageAnalysisJobRepository(db)
        self._events = StorageAnalysisEventRepository(db)
        self._snapshots = StorageAnalysisSnapshotRepository(db)
        self._duplicates = DuplicateDetector(db)
        self._storage_analyzer = StorageAnalyzer(db)
        self._candidate_analyzer = CandidateAnalyzer()
        self._savings_analyzer = SavingsAnalyzer(db)
        self._duplicate_groups = DuplicateGroupRepository(db)
        self._connectors = StorageConnectorRepository(db)
        self._sources = StorageSourceRepository(db)

    def run(self, storage_analysis_job_id: uuid.UUID) -> None:
        job = self._jobs.get_by_id(storage_analysis_job_id)
        if job is None:
            logger.warning(
                "storage_analysis_job_not_found",
                extra={"storage_analysis_job_id": str(storage_analysis_job_id)},
            )
            return

        self._jobs.mark_running(job)
        self._events.record(storage_analysis_job_id=job.id, event_type="storage_analysis_started")
        self._db.commit()

        try:
            self._generate(job)
        except Exception as exc:  # noqa: BLE001 - job execution boundary must never crash the worker
            logger.exception(
                "storage_analysis_job_failed", extra={"storage_analysis_job_id": str(job.id)}
            )
            self._db.rollback()
            self._jobs.mark_failed(job, error=str(exc))
            self._events.record(
                storage_analysis_job_id=job.id,
                event_type="storage_analysis_failed",
                message=str(exc),
            )
            self._db.commit()
            return

        self._jobs.mark_completed(job)
        self._events.record(storage_analysis_job_id=job.id, event_type="storage_analysis_completed")
        self._db.commit()

    def _generate(self, job: StorageAnalysisJob) -> None:
        organization_id = job.organization_id

        duplicate_summary = self._duplicates.analyze(
            organization_id, storage_analysis_job_id=job.id
        )
        self._db.commit()

        files = self._files.list_all_for_organization(organization_id)
        breakdown = self._storage_analyzer.analyze(organization_id, files)
        candidates = self._candidate_analyzer.analyze(files)
        savings = self._savings_analyzer.analyze(
            organization_id, temporary_candidate_sizes=candidates.temporary_candidate_sizes
        )

        self._snapshots.create(
            organization_id=organization_id,
            storage_analysis_job_id=job.id,
            total_size_bytes=breakdown.total_size_bytes,
            total_files=breakdown.total_files,
            total_folders=breakdown.total_folders,
            breakdown_by_type_bytes=breakdown.breakdown_by_type_bytes,
            breakdown_by_size_bucket_bytes=breakdown.breakdown_by_size_bucket_bytes,
            breakdown_by_source_bytes=breakdown.breakdown_by_source_bytes,
            duplicate_group_count=duplicate_summary.group_count,
            duplicate_file_count=duplicate_summary.file_count,
            duplicate_recoverable_bytes=duplicate_summary.recoverable_bytes,
            large_file_count=candidates.large_file_count,
            large_file_bytes=candidates.large_file_bytes,
            old_file_count=candidates.old_file_count,
            old_file_bytes=candidates.old_file_bytes,
            inactive_file_count=candidates.inactive_file_count,
            inactive_file_bytes=candidates.inactive_file_bytes,
            temporary_candidate_count=candidates.temporary_candidate_count,
            temporary_candidate_bytes=candidates.temporary_candidate_bytes,
            total_potential_savings_bytes=savings.total_potential_savings_bytes,
        )
        self._db.commit()
        self._snapshot_each_storage(job, files)

    def _snapshot_each_storage(self, job: StorageAnalysisJob, files: list[File]) -> None:
        """One snapshot per connected storage, so each can be looked at on
        its own. A duplicate counts in the storage that holds the redundant
        copy, so the per-storage figures add up to the organization's."""
        organization_id = job.organization_id
        redundant = self._duplicate_groups.list_recoverable_members_for_organization(
            organization_id
        )
        for connector in self._connectors.list_for_organization(organization_id):
            source_ids = {source.id for source in self._sources.list_for_connector(connector.id)}
            own = [file for file in files if file.storage_source_id in source_ids]
            own_ids = {file.id for file in own}
            breakdown = self._storage_analyzer.analyze(
                organization_id, own, connector_id=connector.id
            )
            candidates = self._candidate_analyzer.analyze(own)
            copies = [(group, size) for group, file_id, size in redundant if file_id in own_ids]
            duplicate_bytes = sum(size for _, size in copies)
            redundant_ids = {file_id for _, file_id, _ in redundant if file_id in own_ids}
            temporary_bytes = sum(
                size
                for file_id, size in candidates.temporary_candidate_sizes.items()
                if file_id not in redundant_ids
            )
            self._snapshots.create(
                organization_id=organization_id,
                connector_id=connector.id,
                storage_analysis_job_id=job.id,
                total_size_bytes=breakdown.total_size_bytes,
                total_files=breakdown.total_files,
                total_folders=breakdown.total_folders,
                breakdown_by_type_bytes=breakdown.breakdown_by_type_bytes,
                breakdown_by_size_bucket_bytes=breakdown.breakdown_by_size_bucket_bytes,
                breakdown_by_source_bytes=breakdown.breakdown_by_source_bytes,
                duplicate_group_count=len({group for group, _ in copies}),
                duplicate_file_count=len(copies),
                duplicate_recoverable_bytes=duplicate_bytes,
                large_file_count=candidates.large_file_count,
                large_file_bytes=candidates.large_file_bytes,
                old_file_count=candidates.old_file_count,
                old_file_bytes=candidates.old_file_bytes,
                inactive_file_count=candidates.inactive_file_count,
                inactive_file_bytes=candidates.inactive_file_bytes,
                temporary_candidate_count=candidates.temporary_candidate_count,
                temporary_candidate_bytes=candidates.temporary_candidate_bytes,
                total_potential_savings_bytes=duplicate_bytes + temporary_bytes,
            )
        self._db.commit()
