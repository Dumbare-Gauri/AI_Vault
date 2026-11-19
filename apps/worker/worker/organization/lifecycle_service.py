import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from vault_shared import get_logger
from vault_shared.db.models import EntityStatus, ExtractionStatus, File, RelationshipType
from vault_shared.db.repositories import (
    DuplicateGroupRepository,
    FileClassificationRepository,
    FileEntityLinkRepository,
    FileExtractionRepository,
    FileLifecycleRepository,
    FileRelationshipRepository,
    FileRepository,
    OrganizationEntityRepository,
)
from worker.organization.lifecycle_scorer import (
    LIFECYCLE_SCORER_VERSION,
    FileLifecycleSignals,
    score_lifecycle,
)

logger = get_logger("worker.organization.lifecycle_service")


class LifecycleService:
    """The Lifecycle Intelligence Layer — purely deterministic (no AI call
    anywhere in this file or in `lifecycle_scorer`), run as the last step
    of `OrganizationAnalysisService.run` so it can see this run's own
    freshly-written `FileEntityLink` rows, not just already-stored
    `FileRelationship`/`DuplicateGroupMember` data."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._files = FileRepository(db)
        self._duplicate_groups = DuplicateGroupRepository(db)
        self._relationships = FileRelationshipRepository(db)
        self._entity_links = FileEntityLinkRepository(db)
        self._entities = OrganizationEntityRepository(db)
        self._classifications = FileClassificationRepository(db)
        self._extractions = FileExtractionRepository(db)
        self._lifecycles = FileLifecycleRepository(db)

    def run_for_organization(self, organization_id: uuid.UUID) -> int:
        entity_status_by_id = {
            entity.id: entity.status
            for entity in self._entities.list_for_organization(organization_id)
        }
        files = self._files.list_all_for_organization(organization_id)
        now = datetime.now(UTC)
        scored = 0
        for file in files:
            self._score_one_file(file, entity_status_by_id=entity_status_by_id, now=now)
            scored += 1
        logger.info(
            "lifecycle_scoring_completed",
            extra={"organization_id": str(organization_id), "files_scored": scored},
        )
        return scored

    def _score_one_file(
        self, file: File, *, entity_status_by_id: dict[uuid.UUID, str], now: datetime
    ) -> None:
        membership = self._duplicate_groups.get_membership_for_file(file.id)
        is_duplicate_not_kept = membership is not None and not membership.is_recommended_keep

        relationships = self._relationships.list_for_file(file.id)
        has_newer_version = any(
            relationship.relationship_type == RelationshipType.SEQUENTIAL_VERSION
            and relationship.file_id == file.id
            for relationship in relationships
        )

        entity_links = self._entity_links.list_for_file(file.id)
        linked_statuses = [
            entity_status_by_id[link.entity_id]
            for link in entity_links
            if link.entity_id in entity_status_by_id
        ]

        classification = self._classifications.get_by_file_id(file.id)
        extraction = self._extractions.get_by_file_id(file.id)

        signals = FileLifecycleSignals(
            is_duplicate_not_kept=is_duplicate_not_kept,
            has_newer_version=has_newer_version,
            is_linked_to_active_entity=EntityStatus.ACTIVE in linked_statuses,
            is_linked_to_any_entity=bool(entity_links),
            has_any_relationship=bool(relationships),
            has_classification=classification is not None,
            has_extraction=extraction is not None and extraction.status == ExtractionStatus.SUCCESS,
            modified_days_ago=_days_ago(file.provider_modified_at, now),
            viewed_days_ago=_days_ago(file.provider_viewed_at, now),
        )
        result = score_lifecycle(signals)
        self._lifecycles.upsert(
            file_id=file.id,
            state=result.state,
            confidence=result.confidence,
            evidence=result.evidence,
            signals=result.signals,
            scorer_version=LIFECYCLE_SCORER_VERSION,
            analyzed_at=now,
        )


def _days_ago(value: datetime | None, now: datetime) -> float | None:
    if value is None:
        return None
    return (now - value).total_seconds() / 86400
