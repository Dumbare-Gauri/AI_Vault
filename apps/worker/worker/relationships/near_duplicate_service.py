import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from vault_shared import get_logger
from vault_shared.ai_gateway.similarity import rank_by_similarity
from vault_shared.db.models import Embedding, File, RelationshipType
from vault_shared.db.repositories import EmbeddingRepository, FileRelationshipRepository

logger = get_logger("worker.relationships.near_duplicate_service")

# A connector's whole embedded-file pool, capped for the same cost-safety
# reason `RelationshipDiscoveryService._MAX_GROUP_SIZE` caps a folder/owner
# group — but sub-batched rather than skipped entirely above this size,
# since "all embedded files for the connector" is the natural unit here
# (unlike a folder/owner group, skipping it outright would silently produce
# zero near-duplicate detection for the whole connector).
_MAX_POOL_PER_BATCH = 500

# Conservative cut on the mean-centered cosine score (see `rank_by_
# similarity`'s own docstring: related/unrelated pairs separate out to
# roughly -0.5..0.5 on real data) — deliberately tunable, not derived from
# any real calibration yet.
NEAR_DUPLICATE_SIMILARITY_THRESHOLD = 0.35


class NearDuplicateService:
    """Embedding-based near-duplicate detection (spec's `NEAR_DUPLICATE`
    relationship type) — reuses `Embedding` rows and `rank_by_similarity`
    (ADR-018) entirely as-is, no new AI infrastructure. Runs off
    `EmbeddingJob` completion (`worker.tasks.embedding.run_embedding`),
    never folded into `RelationshipDiscoveryService.discover()`: that pass
    runs synchronously inside `EnrichmentJob`, before `EmbeddingJob` is even
    enqueued, so no `Embedding` rows exist yet at that point."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._embeddings = EmbeddingRepository(db)
        self._relationships = FileRelationshipRepository(db)

    def discover_for_connector(self, connector_id: uuid.UUID) -> int:
        pairs = self._embeddings.list_for_connector(connector_id)
        discovered = 0
        for batch_start in range(0, len(pairs), _MAX_POOL_PER_BATCH):
            batch = pairs[batch_start : batch_start + _MAX_POOL_PER_BATCH]
            discovered += self._discover_in_batch(connector_id, batch)
        return discovered

    def _discover_in_batch(
        self, connector_id: uuid.UUID, batch: list[tuple[File, Embedding]]
    ) -> int:
        files = [file for file, _embedding in batch]
        vectors = [embedding.vector for _file, embedding in batch]
        discovered_at = datetime.now(UTC)
        discovered = 0

        for i in range(len(batch) - 1):
            # Centered over the *whole* batch every time, not just the
            # files after `i`: `rank_by_similarity` centers on the pool it is
            # given, and a shrinking pool degenerates (two vectors centered
            # on their own mean always score -1 against each other).
            others = vectors[:i] + vectors[i + 1 :]
            scores = rank_by_similarity(vectors[i], others)
            for j in range(i + 1, len(batch)):
                score = scores[j - 1]
                if score < NEAR_DUPLICATE_SIMILARITY_THRESHOLD:
                    continue
                other = files[j]
                if files[i].checksum and files[i].checksum == other.checksum:
                    # Already an exact DUPLICATE_CANDIDATE edge — a
                    # near-duplicate edge for the same pair would be
                    # redundant and confusing, not additional signal.
                    continue
                self._relationships.upsert(
                    connector_id=connector_id,
                    file_id=files[i].id,
                    related_file_id=other.id,
                    relationship_type=RelationshipType.NEAR_DUPLICATE,
                    confidence=max(0.0, min(1.0, score)),
                    metadata={
                        "match_reason": "embedding_cosine_mean_centered",
                        "similarity": score,
                    },
                    discovered_at=discovered_at,
                )
                discovered += 1

        logger.info(
            "near_duplicate_batch_processed",
            extra={"connector_id": str(connector_id), "discovered": discovered},
        )
        return discovered
