import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vault_shared.db.models import EntityStatus, FileEntityLink, OrganizationEntity


class OrganizationEntityRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_owned(
        self, entity_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> OrganizationEntity | None:
        return (
            self._session.query(OrganizationEntity)
            .filter_by(id=entity_id, organization_id=organization_id)
            .first()
        )

    def get_by_normalized_name(
        self, *, organization_id: uuid.UUID, entity_type: str, normalized_name: str
    ) -> OrganizationEntity | None:
        return (
            self._session.query(OrganizationEntity)
            .filter_by(
                organization_id=organization_id,
                entity_type=entity_type,
                normalized_name=normalized_name,
            )
            .first()
        )

    def upsert(
        self,
        *,
        organization_id: uuid.UUID,
        entity_type: str,
        name: str,
        normalized_name: str,
        confidence: float,
        evidence: list[dict],
    ) -> OrganizationEntity:
        """Identity is `(organization_id, entity_type, normalized_name)` —
        a repeat inference for the same entity blends in rather than
        creating a near-duplicate row: confidence takes the max seen so
        far, evidence accumulates (capped by the caller)."""
        entity = self.get_by_normalized_name(
            organization_id=organization_id,
            entity_type=entity_type,
            normalized_name=normalized_name,
        )
        if entity is None:
            entity = OrganizationEntity(
                organization_id=organization_id,
                entity_type=entity_type,
                normalized_name=normalized_name,
                confidence=confidence,
                evidence=evidence,
            )
            self._session.add(entity)
        else:
            entity.confidence = max(entity.confidence, confidence)
            entity.evidence = evidence
        entity.name = name
        entity.status = EntityStatus.ACTIVE
        self._session.flush()
        return entity

    def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        entity_type: str | None = None,
        status: str | None = None,
    ) -> list[OrganizationEntity]:
        query = self._session.query(OrganizationEntity).filter_by(organization_id=organization_id)
        if entity_type is not None:
            query = query.filter(OrganizationEntity.entity_type == entity_type)
        if status is not None:
            query = query.filter(OrganizationEntity.status == status)
        return query.order_by(OrganizationEntity.name).all()

    def count_active_by_type(self, organization_id: uuid.UUID) -> dict[str, int]:
        rows = self._session.execute(
            select(OrganizationEntity.entity_type, func.count())
            .where(
                OrganizationEntity.organization_id == organization_id,
                OrganizationEntity.status == EntityStatus.ACTIVE,
            )
            .group_by(OrganizationEntity.entity_type)
        ).all()
        return {entity_type: count for entity_type, count in rows}


class FileEntityLinkRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, *, file_id: uuid.UUID, entity_id: uuid.UUID) -> FileEntityLink | None:
        return (
            self._session.query(FileEntityLink)
            .filter_by(file_id=file_id, entity_id=entity_id)
            .first()
        )

    def upsert(
        self,
        *,
        file_id: uuid.UUID,
        entity_id: uuid.UUID,
        confidence: float,
        evidence: list[dict],
        source: str,
        inference_version: str,
    ) -> FileEntityLink:
        link = self.get(file_id=file_id, entity_id=entity_id)
        if link is None:
            link = FileEntityLink(file_id=file_id, entity_id=entity_id)
            self._session.add(link)

        link.confidence = confidence
        link.evidence = evidence
        link.source = source
        link.inference_version = inference_version
        self._session.flush()
        return link

    def list_for_file(self, file_id: uuid.UUID) -> list[FileEntityLink]:
        return self._session.query(FileEntityLink).filter_by(file_id=file_id).all()

    def list_for_entity(self, entity_id: uuid.UUID) -> list[FileEntityLink]:
        return self._session.query(FileEntityLink).filter_by(entity_id=entity_id).all()

    def has_current_link(self, *, file_id: uuid.UUID, inference_version: str) -> bool:
        """Used by entity-inference clustering to skip a file that's
        already linked at the current scorer version, so re-running the
        whole pass costs nothing for already-processed files."""
        return (
            self._session.query(FileEntityLink.id)
            .filter_by(file_id=file_id, inference_version=inference_version)
            .first()
            is not None
        )
