import uuid
from collections import Counter

from sqlalchemy.orm import Session

from vault_shared import get_logger
from vault_shared.db.models import (
    EntityStatus,
    EntityType,
    File,
    FileIntelligence,
    OrganizationEntity,
    OrganizationRecommendationKind,
)
from vault_shared.db.repositories import (
    FileEntityLinkRepository,
    FileIntelligenceRepository,
    FileRepository,
    FolderRepository,
    OrganizationEntityRepository,
    OrganizationRecommendationRepository,
)
from worker.organization.rename_proposal import is_uninformative_name, propose_name

logger = get_logger("worker.organization.organization_recommendation_generator")

# Static entity_type -> destination-folder-bucket mapping. No second GLM
# call to "pick" a folder name for an already-named entity — the spec's own
# cost/reliability principle (don't call AI where a deterministic answer is
# just as good) argues against it, and the entity's name was already
# validated when it was created (Phase 2 §4's reuse of
# `validate_recommendation`'s component-name rules via `CREATE_FOLDER`/
# `MOVE` validation).
_DESTINATION_BUCKET_BY_TYPE = {
    EntityType.PROJECT: "Projects",
    EntityType.CLIENT: "Clients",
    EntityType.CAMPAIGN: "Campaigns",
}
_RECOMMENDATION_KIND_BY_TYPE = {
    EntityType.PROJECT: OrganizationRecommendationKind.GROUP_PROJECT,
    EntityType.CLIENT: OrganizationRecommendationKind.GROUP_CLIENT,
    EntityType.CAMPAIGN: OrganizationRecommendationKind.GROUP_CAMPAIGN,
}
_MAX_EVIDENCE_SAMPLE = 3
_RENAME_MIN_ENTITY_CONFIDENCE = 0.6
_SUMMARY_EVIDENCE_CHARS = 160


class OrganizationRecommendationGenerator:
    """Deterministic generation of `GROUP_PROJECT`/`GROUP_CLIENT`/
    `GROUP_CAMPAIGN` recommendations — no AI call anywhere in this file.
    One entity scattered across more than one folder is itself sufficient
    evidence to propose consolidating it; nothing here asks GLM to
    re-justify what entity inference (`entity_inference_service`) already
    established with its own evidence."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._entities = OrganizationEntityRepository(db)
        self._entity_links = FileEntityLinkRepository(db)
        self._files = FileRepository(db)
        self._folders = FolderRepository(db)
        self._recommendations = OrganizationRecommendationRepository(db)
        self._intelligence = FileIntelligenceRepository(db)

    def generate_for_organization(self, organization_id: uuid.UUID) -> int:
        generated = 0
        entities = self._entities.list_for_organization(organization_id, status=EntityStatus.ACTIVE)
        for entity in entities:
            if self._generate_for_entity(organization_id, entity):
                generated += 1
        generated += self._generate_renames(organization_id, entities)
        logger.info(
            "organization_recommendations_generated",
            extra={"organization_id": str(organization_id), "generated": generated},
        )
        return generated

    def _generate_for_entity(self, organization_id: uuid.UUID, entity: OrganizationEntity) -> bool:
        links = self._entity_links.list_for_entity(entity.id)
        if not links:
            return False

        files = self._files.list_by_ids([link.file_id for link in links])
        if not files:
            return False

        folder_ids = {file.parent_folder_id for file in files if file.parent_folder_id}
        if len(folder_ids) <= 1:
            existing = self._recommendations.get_active_for_entity(entity.id)
            if existing is not None:
                self._recommendations.mark_stale(existing)
            return False

        folders_by_id = {
            folder.id: folder for folder in self._folders.list_by_ids(list(folder_ids))
        }
        counts = Counter(file.parent_folder_id for file in files if file.parent_folder_id)
        current_locations = [
            {
                "folder_id": str(folder_id),
                "path": folders_by_id[folder_id].path if folder_id in folders_by_id else None,
                "file_count": count,
            }
            for folder_id, count in counts.items()
        ]

        bucket = _DESTINATION_BUCKET_BY_TYPE.get(entity.entity_type, "Other")
        suggested_destination = [bucket, entity.name]
        evidence = [
            {
                "type": "scattered_locations",
                "description": f"Found in {len(folder_ids)} different folders.",
            },
            *entity.evidence[:_MAX_EVIDENCE_SAMPLE],
        ]
        affected_file_ids = [str(file.id) for file in files]
        estimated_storage_impact_bytes = sum(file.size_bytes or 0 for file in files)

        self._recommendations.upsert_for_entity(
            organization_id=organization_id,
            entity_id=entity.id,
            kind=_RECOMMENDATION_KIND_BY_TYPE.get(
                entity.entity_type, OrganizationRecommendationKind.GROUP_PROJECT
            ),
            title=f"Consolidate '{entity.name}' into one folder",
            reasoning_summary=(
                f"Files linked to the {entity.entity_type} '{entity.name}' are spread across "
                f"{len(folder_ids)} folders. Moving them into a single folder makes this "
                f"{entity.entity_type} easier to find and manage."
            ),
            evidence=evidence,
            confidence=entity.confidence,
            affected_file_ids=affected_file_ids,
            current_locations=current_locations,
            suggested_destination=suggested_destination,
            estimated_storage_impact_bytes=estimated_storage_impact_bytes,
        )
        return True

    def _generate_renames(
        self, organization_id: uuid.UUID, entities: list[OrganizationEntity]
    ) -> int:
        """`RENAME_FILE` for files whose name says nothing about them, named
        only from evidence already on record (see `rename_proposal`). A file
        with no such evidence keeps its name — unknown is allowed."""
        best_entity: dict[uuid.UUID, tuple[OrganizationEntity, float]] = {}
        for entity in entities:
            for link in self._entity_links.list_for_entity(entity.id):
                if link.confidence < _RENAME_MIN_ENTITY_CONFIDENCE:
                    continue
                current = best_entity.get(link.file_id)
                if current is None or link.confidence > current[1]:
                    best_entity[link.file_id] = (entity, link.confidence)

        intelligence_by_file: dict[uuid.UUID, tuple[File, FileIntelligence | None]] = {
            file.id: (file, intelligence)
            for file, intelligence in self._intelligence.list_successful_for_organization(
                organization_id
            )
        }
        for file in self._files.list_by_ids(
            [file_id for file_id in best_entity if file_id not in intelligence_by_file]
        ):
            intelligence_by_file[file.id] = (file, None)

        for stale in self._recommendations.list_active_of_kind(
            organization_id, OrganizationRecommendationKind.RENAME_FILE
        ):
            file_entry = intelligence_by_file.get(uuid.UUID(stale.affected_file_ids[0]))
            if file_entry is None or not is_uninformative_name(file_entry[0].name):
                self._recommendations.mark_stale(stale)

        generated = 0
        for file_id, (file, intelligence) in intelligence_by_file.items():
            if file.trashed or not is_uninformative_name(file.name):
                continue
            entity_match = best_entity.get(file_id)
            document_type = intelligence.document_type if intelligence else None
            proposed = propose_name(
                current_name=file.name,
                entity_name=entity_match[0].name if entity_match else None,
                document_type=document_type,
                dated=(file.provider_modified_at.date() if file.provider_modified_at else None),
            )
            if proposed is None:
                continue

            evidence = [
                {
                    "type": "uninformative_name",
                    "description": f"'{file.name}' doesn't say what this file is.",
                }
            ]
            confidences: list[float] = []
            if entity_match:
                entity, link_confidence = entity_match
                evidence.append(
                    {
                        "type": "entity",
                        "description": f"Belongs to the {entity.entity_type} '{entity.name}'.",
                    }
                )
                confidences.append(link_confidence)
            if intelligence and document_type:
                summary = (intelligence.summary or "")[:_SUMMARY_EVIDENCE_CHARS]
                evidence.append(
                    {
                        "type": "content",
                        "description": f"AI read it as {document_type}"
                        + (f": {summary}" if summary else "."),
                    }
                )
                confidences.append(intelligence.confidence or 0.5)

            folder = (
                self._folders.get_by_id(file.parent_folder_id) if file.parent_folder_id else None
            )
            self._recommendations.upsert_for_file(
                organization_id=organization_id,
                file_id=file.id,
                kind=OrganizationRecommendationKind.RENAME_FILE,
                title=f"Rename '{file.name}' to '{proposed}'",
                reasoning_summary=(
                    f"'{file.name}' is hard to find later. Based on what AI Vault knows about "
                    f"this file, '{proposed}' describes it."
                ),
                evidence=evidence,
                confidence=round(sum(confidences) / len(confidences), 2),
                current_locations=[
                    {
                        "folder_id": str(file.parent_folder_id) if file.parent_folder_id else None,
                        "path": folder.path if folder else None,
                        "file_count": 1,
                    }
                ],
                suggested_destination=[proposed],
                estimated_storage_impact_bytes=file.size_bytes or 0,
            )
            generated += 1
        return generated
