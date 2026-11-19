import uuid

from sqlalchemy.orm import Session

from vault_shared.db.models import MemoryType
from vault_shared.db.repositories import OrganizationMemoryRepository

_MAX_NAMING_CONVENTIONS = 10
_MAX_CONSTRAINTS = 10


def build_naming_conventions(db: Session, organization_id: uuid.UUID) -> tuple[str, ...]:
    """Feeds `OrganizationMemory` rows back into `AIContext.
    naming_conventions` — an existing field, no schema change needed."""
    repo = OrganizationMemoryRepository(db)
    conventions = repo.list_for_organization(
        organization_id, memory_type=MemoryType.NAMING_CONVENTION
    )
    structures = repo.list_for_organization(
        organization_id, memory_type=MemoryType.PREFERRED_STRUCTURE
    )
    lines = [memory.evidence or str(memory.value) for memory in (*conventions, *structures)]
    return tuple(lines[:_MAX_NAMING_CONVENTIONS])


def build_constraints_for_entities(
    db: Session, organization_id: uuid.UUID, entity_ids: set[uuid.UUID]
) -> tuple[str, ...]:
    """Feeds recorded `CORRECTION` memories back into `AIContext.
    constraints` for a future entity-inference call touching the same
    entities — written once, from `ExecutionPlanService.create_ad_hoc_plan`
    (see that method's own docstring)."""
    keys = {f"entity:{entity_id}" for entity_id in entity_ids}
    corrections = OrganizationMemoryRepository(db).list_corrections_for_keys(organization_id, keys)
    lines = [
        memory.evidence
        or "A user previously moved files for this entity to a different location than suggested."
        for memory in corrections
    ]
    return tuple(lines[:_MAX_CONSTRAINTS])
