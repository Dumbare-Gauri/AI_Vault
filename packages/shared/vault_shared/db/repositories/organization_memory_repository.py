import uuid

from sqlalchemy.orm import Session

from vault_shared.db.models import MemoryType, OrganizationMemory


class OrganizationMemoryRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        organization_id: uuid.UUID,
        memory_type: str,
        key: str,
        value: dict,
        evidence: str | None,
        confidence: float,
    ) -> OrganizationMemory:
        memory = OrganizationMemory(
            organization_id=organization_id,
            memory_type=memory_type,
            key=key,
            value=value,
            evidence=evidence,
            confidence=confidence,
        )
        self._session.add(memory)
        self._session.flush()
        return memory

    def list_for_organization(
        self, organization_id: uuid.UUID, *, memory_type: str | None = None
    ) -> list[OrganizationMemory]:
        query = self._session.query(OrganizationMemory).filter_by(organization_id=organization_id)
        if memory_type is not None:
            query = query.filter(OrganizationMemory.memory_type == memory_type)
        return query.order_by(OrganizationMemory.created_at.desc()).all()

    def list_corrections_for_keys(
        self, organization_id: uuid.UUID, keys: set[str]
    ) -> list[OrganizationMemory]:
        if not keys:
            return []
        return (
            self._session.query(OrganizationMemory)
            .filter(
                OrganizationMemory.organization_id == organization_id,
                OrganizationMemory.memory_type == MemoryType.CORRECTION,
                OrganizationMemory.key.in_(keys),
            )
            .order_by(OrganizationMemory.created_at.desc())
            .all()
        )
