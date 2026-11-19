import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from vault_shared import NotFoundError
from vault_shared.db.models import File, OrganizationEntity
from vault_shared.db.repositories import (
    FileEntityLinkRepository,
    FileRepository,
    OrganizationEntityRepository,
)


@dataclass(frozen=True)
class OrganizationEntityDetail:
    entity: OrganizationEntity
    files: list[File]


class OrganizationEntityService:
    """Read surface for the Intelligence Engine's discovered Projects/
    Clients/Campaigns (spec's "Project Discovery" UI) — all rows are
    written by `apps/worker`'s `EntityInferenceService`; this service only
    reads what it wrote, mirroring `RecommendationService`'s read-only
    role for the worker-computed `Recommendation` table."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._entities = OrganizationEntityRepository(db)
        self._entity_links = FileEntityLinkRepository(db)
        self._files = FileRepository(db)

    def list_for_organization(
        self, organization_id: uuid.UUID, *, entity_type: str | None = None
    ) -> list[OrganizationEntity]:
        return self._entities.list_for_organization(organization_id, entity_type=entity_type)

    def get_detail(
        self, entity_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> OrganizationEntityDetail:
        entity = self._entities.get_owned(entity_id, organization_id=organization_id)
        if entity is None:
            raise NotFoundError("Entity not found.")
        links = self._entity_links.list_for_entity(entity.id)
        files = self._files.list_by_ids([link.file_id for link in links])
        return OrganizationEntityDetail(entity=entity, files=files)
