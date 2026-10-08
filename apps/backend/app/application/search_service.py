import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.application.search_interpreter import SearchInterpreter
from vault_shared.ai_gateway import AIGateway, rank_by_similarity
from vault_shared.db.models import File, StorageConnector
from vault_shared.db.repositories import (
    EmbeddingRepository,
    FileRepository,
    SearchSessionRepository,
)
from vault_shared.grounding import question_terms
from vault_shared.search import FileQuery, merge_queries, parse_file_query

# Empirically validated against this project's own real scanned files
# (see ADR-018) — mean-centered cosine similarity for genuinely related
# documents lands well above this, unrelated documents well below it.
_SEMANTIC_SCORE_THRESHOLD = 0.05
_MAX_SEMANTIC_CANDIDATES = 15
_MAX_RESULTS = 20
# A request this long that the parser couldn't structure at all is worth
# asking the AI to translate; a one- or two-word query is just a name.
_MIN_WORDS_FOR_AI = 4


@dataclass(frozen=True)
class SearchResult:
    file: File
    score: float
    retrieval_method: str  # "metadata" | "semantic" | "both"
    connector: StorageConnector | None = None


@dataclass(frozen=True)
class SearchOutcome:
    results: list[SearchResult]
    total: int
    understood: list[str]
    interpreted_by_ai: bool


class SearchService:
    """Structured search first: the request is parsed deterministically into
    filters (type, size, date, folder, ownership, words), which run as one
    parameterized query against the real index. Only a sentence the parser
    could not place at all is sent to the AI — and the AI only proposes a
    filter, never results. Semantic (embedding) similarity is added for
    plain-text queries, to catch conceptually related files a word match
    misses."""

    def __init__(self, db: Session, *, ai_gateway: AIGateway) -> None:
        self._db = db
        self._ai_gateway = ai_gateway
        self._files = FileRepository(db)
        self._embeddings = EmbeddingRepository(db)
        self._search_sessions = SearchSessionRepository(db)
        self._interpreter = SearchInterpreter(db, ai_gateway=ai_gateway)

    def find(
        self,
        query_text: str,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        filters: FileQuery | None = None,
        allow_ai: bool = True,
    ) -> SearchOutcome:
        query = parse_file_query(query_text)
        if filters is not None:
            query = merge_queries(query, filters)

        interpreted_by_ai = False
        if allow_ai and not query.has_filters and len(query.text.split()) >= _MIN_WORDS_FOR_AI:
            proposed = self._interpreter.interpret(query_text, organization_id=organization_id)
            if proposed is not None:
                query = merge_queries(proposed, filters) if filters else proposed
                interpreted_by_ai = True

        rows, total = self._files.query_files(organization_id, query)
        if total == 0 and query.categories and not (filters and filters.categories):
            # "Board Deck" is a file name, not "presentations about boards":
            # a type word that matches nothing is retried as part of the name.
            literal = parse_file_query(query_text, detect_categories=False)
            query = merge_queries(literal, filters) if filters else literal
            rows, total = self._files.query_files(organization_id, query)
        results_by_file_id: dict[uuid.UUID, SearchResult] = {
            file.id: SearchResult(
                file=file, score=1.0, retrieval_method="metadata", connector=connector
            )
            for file, connector in rows
        }

        understood = list(query.understood)
        if total == 0 and query.text and not query.has_filters and query.offset == 0:
            # Nothing is named like this — look inside the files' text, so
            # "the contract mentioning the payment amount" still finds it.
            for file, connector, score in self._files.rank_by_terms(
                organization_id, question_terms(query.text), limit=query.limit
            ):
                results_by_file_id[file.id] = SearchResult(
                    file=file,
                    score=min(1.0, score / 10),
                    retrieval_method="metadata",
                    connector=connector,
                )
            total = len(results_by_file_id)
            if total:
                understood.append("matched words inside file names and content")
        if query.text and not query.has_filters and query.offset == 0:
            # Exact matches stand on their own in the search page; similar-
            # content files are offered only when nothing matched exactly.
            # Conversation retrieval (allow_ai=False) always blends both.
            add_similar = total == 0 or not allow_ai
            for file, score in self._semantic_matches(query.text, organization_id=organization_id):
                existing = results_by_file_id.get(file.id)
                if existing is None:
                    if not add_similar:
                        continue
                    results_by_file_id[file.id] = SearchResult(
                        file=file, score=score, retrieval_method="semantic"
                    )
                    total += 1
                else:
                    results_by_file_id[file.id] = SearchResult(
                        file=file,
                        score=max(existing.score, score),
                        retrieval_method="both",
                        connector=existing.connector,
                    )

            if total > 0 and add_similar and allow_ai:
                understood.append("no exact matches — showing files with similar content")
        results = list(results_by_file_id.values())
        if query.sort == "relevance" and query.text:
            results.sort(key=lambda r: r.score, reverse=True)

        self._search_sessions.record(
            organization_id=organization_id,
            user_id=user_id,
            query_text=query_text,
            result_count=total,
        )
        self._db.commit()
        return SearchOutcome(
            results=results[: query.limit],
            total=total,
            understood=understood,
            interpreted_by_ai=interpreted_by_ai,
        )

    def search(
        self,
        query_text: str,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        limit: int = _MAX_RESULTS,
    ) -> list[SearchResult]:
        """For answering a question from file content: files ranked by how many
        of the question's meaningful words they match — in the name, inside the
        file's text, in a linked project/client name or the path — followed by
        files with semantically similar content. No AI translation step (the
        caller is already an AI turn)."""
        ranked = self._files.rank_by_terms(organization_id, question_terms(query_text), limit=limit)
        best = max((score for _, _, score in ranked), default=1.0)
        results = [
            SearchResult(
                file=file, score=score / best, retrieval_method="metadata", connector=connector
            )
            for file, connector, score in ranked
        ]
        by_id = {result.file.id: index for index, result in enumerate(results)}
        for file, similarity in self._semantic_matches(query_text, organization_id=organization_id):
            index = by_id.get(file.id)
            if index is not None:
                found = results[index]
                results[index] = SearchResult(
                    file=file,
                    score=max(found.score, similarity),
                    retrieval_method="both",
                    connector=found.connector,
                )
            elif len(results) < limit:
                results.append(
                    SearchResult(file=file, score=similarity, retrieval_method="semantic")
                )
        return results

    def _semantic_matches(
        self, query_text: str, *, organization_id: uuid.UUID
    ) -> list[tuple[File, float]]:
        rows = [
            (file, embedding)
            for file, embedding in self._embeddings.list_for_organization(organization_id)
            if not file.trashed and file.permanently_deleted_at is None
        ]
        if not rows:
            return []

        [query_embedding] = self._ai_gateway.embed([query_text])
        candidate_vectors = [embedding.vector for _, embedding in rows]
        scores = rank_by_similarity(query_embedding.vector, candidate_vectors)

        candidate_files = [file for file, _ in rows]
        ranked = sorted(
            zip(scores, candidate_files, strict=True), reverse=True, key=lambda pair: pair[0]
        )
        return [
            (file, score)
            for score, file in ranked[:_MAX_SEMANTIC_CANDIDATES]
            if score >= _SEMANTIC_SCORE_THRESHOLD
        ]
