import re
import uuid

from sqlalchemy.orm import Session

from vault_shared import AIUnavailableError, get_logger
from vault_shared.ai_gateway import AIGateway
from vault_shared.ai_gateway.boundary import AIContext, AIContextFile
from vault_shared.ai_gateway.org_completion_provider import resolve_org_completion_provider
from vault_shared.ai_gateway.recommendation import RecommendationKind, ValidatedAction
from vault_shared.db.models import (
    EntityLinkSource,
    EntityStatus,
    File,
    OrganizationEntity,
    RelationshipType,
)
from vault_shared.db.repositories import (
    FileEntityLinkRepository,
    FileExtractionRepository,
    FileIntelligenceRepository,
    FileRelationshipRepository,
    OrganizationEntityRepository,
)
from worker.intelligence.entity_prompt import ENTITY_INFERENCE_SYSTEM_PROMPT
from worker.organization.entity_clustering import EntityCluster
from worker.organization.memory_context import (
    build_constraints_for_entities,
    build_naming_conventions,
)

logger = get_logger("worker.intelligence.entity_inference_service")

# Bumped whenever this prompt/pipeline's meaning changes meaningfully
# enough that already-linked files should be reconsidered.
ENTITY_INFERENCE_VERSION = "v1"

# Below this confidence, a proposed label is dropped rather than linked —
# the file stays unlinked for that entity type ("Unknown is always
# allowed"), never linked-with-low-confidence.
ENTITY_LINK_MIN_CONFIDENCE = 0.5

# Never the full `FileIntelligence.summary`/`FileExtraction.extracted_text`
# — this keeps one cluster's prompt cost independent of any single file's
# size, per the spec's "don't send everything to GLM" principle.
_MAX_CONTEXT_TEXT_CHARS = 400
_MAX_RELATIONSHIP_LINES = 10
_LABEL_KEYS = ("client", "project", "campaign")
_STUB_PROVIDER_NAME = "extractive_fallback"

_NORMALIZE_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")

_RELATIONSHIP_PHRASES = {
    RelationshipType.SEQUENTIAL_VERSION: "is an earlier/later version of",
    RelationshipType.DUPLICATE_CANDIDATE: "is an exact duplicate of",
    RelationshipType.NEAR_DUPLICATE: "is very similar in content to",
    RelationshipType.SHARED_OWNERSHIP: "shares an owner with",
}


def normalize_entity_name(name: str) -> str:
    """Lowercased, punctuation-stripped, whitespace-collapsed — the dedup
    key `OrganizationEntity` is uniquely constrained on, so "Acme Corp."
    and "acme corp" resolve to the same row."""
    stripped = _NORMALIZE_RE.sub(" ", name.lower())
    return _WHITESPACE_RE.sub(" ", stripped).strip()


class EntityInferenceService:
    """The first production caller of `AIGateway.recommend()` — reuses
    `RecommendationKind.CLASSIFY` and `ALLOWED_LABEL_KEYS` (already include
    client/project/campaign) with zero validator changes. Structurally
    mirrors `IntelligenceService`'s AI-outage degradation, at cluster
    granularity instead of file granularity: an `INVALID_RESPONSE` for one
    cluster is that cluster's own problem (skip, continue); every other
    `AIUnavailableError`/`DependencyUnavailableError` is a whole-run
    condition (propagated to `OrganizationAnalysisService.run`, mirroring
    `IntelligenceService.run`'s own handling)."""

    def __init__(self, db: Session, *, ai_gateway: AIGateway) -> None:
        self._db = db
        self._ai_gateway = ai_gateway
        self._extractions = FileExtractionRepository(db)
        self._intelligence = FileIntelligenceRepository(db)
        self._relationships = FileRelationshipRepository(db)
        self._entities = OrganizationEntityRepository(db)
        self._entity_links = FileEntityLinkRepository(db)

    def bind_org_completion_provider(self, organization_id: uuid.UUID) -> None:
        provider = resolve_org_completion_provider(self._db, organization_id)
        if provider is not None:
            self._ai_gateway = self._ai_gateway.with_completion_provider(provider)

    @property
    def is_stub_provider(self) -> bool:
        return self._ai_gateway.completion_provider_name == _STUB_PROVIDER_NAME

    def infer_for_clusters(
        self, organization_id: uuid.UUID, clusters: list[EntityCluster]
    ) -> tuple[int, int]:
        """Returns (clusters_processed, distinct_entities_touched). Raises
        `AIUnavailableError`/`DependencyUnavailableError` for any
        whole-run-affecting failure — the caller decides whether that
        means retrying the job or failing it, exactly like
        `IntelligenceService.run`."""
        clusters_processed = 0
        entity_ids: set[uuid.UUID] = set()
        for cluster in clusters:
            try:
                touched = self._infer_for_cluster(organization_id, cluster)
            except AIUnavailableError as exc:
                if exc.reason != AIUnavailableError.INVALID_RESPONSE:
                    raise
                logger.warning(
                    "entity_inference_cluster_invalid_response",
                    extra={
                        "organization_id": str(organization_id),
                        "cluster_size": len(cluster.files),
                    },
                )
                continue
            clusters_processed += 1
            entity_ids |= touched
        return clusters_processed, len(entity_ids)

    def _infer_for_cluster(
        self, organization_id: uuid.UUID, cluster: EntityCluster
    ) -> set[uuid.UUID]:
        unlinked_files = [
            file
            for file in cluster.files
            if not self._entity_links.has_current_link(
                file_id=file.id, inference_version=ENTITY_INFERENCE_VERSION
            )
        ]
        if not unlinked_files:
            return set()

        active_entities = self._entities.list_for_organization(
            organization_id, status=EntityStatus.ACTIVE
        )
        known_entities: dict[str, tuple[str, ...]] = {}
        for entity in active_entities:
            known_entities.setdefault(entity.entity_type, ())
            known_entities[entity.entity_type] = (*known_entities[entity.entity_type], entity.name)

        context, ref_to_file = self._build_context(
            organization_id, unlinked_files, known_entities, active_entities
        )
        recommendation = self._ai_gateway.recommend(
            context, system_instructions=ENTITY_INFERENCE_SYSTEM_PROMPT, max_tokens=1000
        )
        return self._apply_actions(organization_id, recommendation.actions, ref_to_file)

    def _build_context(
        self,
        organization_id: uuid.UUID,
        files: list[File],
        known_entities: dict[str, tuple[str, ...]],
        active_entities: list[OrganizationEntity],
    ) -> tuple[AIContext, dict[str, File]]:
        ref_to_file = {f"f{index}": file for index, file in enumerate(files)}
        candidates = tuple(
            AIContextFile(
                ref=ref,
                name=file.name,
                path=file.path,
                mime_type=file.mime_type,
                size_bytes=file.size_bytes,
                modified_at=(
                    file.provider_modified_at.isoformat() if file.provider_modified_at else None
                ),
                extracted_text=self._context_text_for(file),
            )
            for ref, file in ref_to_file.items()
        )
        relationships = self._relationship_lines(ref_to_file)
        constraints = build_constraints_for_entities(
            self._db, organization_id, {entity.id for entity in active_entities}
        )
        naming_conventions = build_naming_conventions(self._db, organization_id)

        context = AIContext(
            user_intent=(
                "Identify any shared client, project, or campaign across this cluster of files "
                "and propose a 'classify' recommendation per file that genuinely belongs to one."
            ),
            candidates=candidates,
            known_entities=known_entities,
            naming_conventions=naming_conventions,
            relationships=relationships,
            constraints=constraints,
        )
        return context, ref_to_file

    def _context_text_for(self, file: File) -> str | None:
        intelligence = self._intelligence.get_by_file_id(file.id)
        if intelligence is not None and intelligence.summary:
            return intelligence.summary[:_MAX_CONTEXT_TEXT_CHARS]
        extraction = self._extractions.get_by_file_id(file.id)
        if extraction is not None and extraction.extracted_text:
            return extraction.extracted_text[:_MAX_CONTEXT_TEXT_CHARS]
        return None

    def _relationship_lines(self, ref_to_file: dict[str, File]) -> tuple[str, ...]:
        file_to_ref = {file.id: ref for ref, file in ref_to_file.items()}
        lines: list[str] = []
        for ref, file in ref_to_file.items():
            for relationship in self._relationships.list_for_file(file.id):
                other_ref = (
                    file_to_ref.get(relationship.related_file_id)
                    if relationship.file_id == file.id
                    else file_to_ref.get(relationship.file_id)
                    if relationship.related_file_id == file.id
                    else None
                )
                if other_ref is None or other_ref == ref:
                    continue
                phrase = _RELATIONSHIP_PHRASES.get(relationship.relationship_type)
                if phrase is None:
                    continue
                lines.append(f"{ref} {phrase} {other_ref}")
                if len(lines) >= _MAX_RELATIONSHIP_LINES:
                    return tuple(lines)
        return tuple(lines)

    def _apply_actions(
        self,
        organization_id: uuid.UUID,
        actions: tuple[ValidatedAction, ...],
        ref_to_file: dict[str, File],
    ) -> set[uuid.UUID]:
        entity_ids: set[uuid.UUID] = set()
        for action in actions:
            if action.kind != RecommendationKind.CLASSIFY or action.target_ref is None:
                continue
            file = ref_to_file.get(action.target_ref)
            if file is None or action.confidence < ENTITY_LINK_MIN_CONFIDENCE:
                continue

            evidence = [{"type": "ai_inference", "description": item} for item in action.evidence]
            if not evidence and action.reason:
                evidence = [{"type": "ai_inference", "description": action.reason}]

            for key in _LABEL_KEYS:
                value = action.labels.get(key)
                if not value:
                    continue
                entity = self._entities.upsert(
                    organization_id=organization_id,
                    entity_type=key,
                    name=value,
                    normalized_name=normalize_entity_name(value),
                    confidence=action.confidence,
                    evidence=evidence,
                )
                self._entity_links.upsert(
                    file_id=file.id,
                    entity_id=entity.id,
                    confidence=action.confidence,
                    evidence=evidence,
                    source=EntityLinkSource.AI_INFERRED,
                    inference_version=ENTITY_INFERENCE_VERSION,
                )
                entity_ids.add(entity.id)
        return entity_ids
