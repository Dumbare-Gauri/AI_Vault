import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.application.search_service import SearchResult
from vault_shared.db.models import File, provider_display_name
from vault_shared.db.repositories import (
    FileExtractionRepository,
    FileMetadataRepository,
    FileRelationshipRepository,
    FileRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
)
from vault_shared.grounding import Passage, best_passages, split_passages

# A rough token-budget stand-in (~4 chars/token, no real tokenizer in this
# phase — see `ExtractiveCompletionProvider`'s identical approximation).
# Keeps the assembled context well inside any real LLM's context window
# once one is configured, without pretending to a precision this stubbed
# phase can't back up.
_DEFAULT_MAX_CONTEXT_CHARS = 8000
_MAX_RELATED_NAMES = 3
_PASSAGES_PER_FILE = 2


@dataclass(frozen=True)
class CitationCandidate:
    file: File
    snippet: str | None
    confidence: float
    retrieval_method: str
    page_number: int | None = None
    passage_index: int | None = None
    # Whether a passage matched the question, rather than the file's opening
    # being included for lack of a match.
    matched: bool = False


@dataclass(frozen=True)
class ContextBundle:
    context_text: str
    citations: list[CitationCandidate]


class ContextBuilderService:
    """Phase 6's Context Builder — merges structured metadata, extracted
    content, and relationship data into one ranked, deduplicated,
    token-budget-aware block of text. For each file it includes the passages
    that match the question (see `vault_shared.grounding`), each labelled with
    its page and passage number, so an answer can be traced to the exact text
    it came from. Deliberately generic over its input (`SearchResult`, already
    ranked by `SearchService`) and output (a plain string plus a parallel
    citation list) so other features can reuse it."""

    def __init__(self, db: Session) -> None:
        self._files = FileRepository(db)
        self._extractions = FileExtractionRepository(db)
        self._file_metadata = FileMetadataRepository(db)
        self._relationships = FileRelationshipRepository(db)
        self._sources = StorageSourceRepository(db)
        self._connectors = StorageConnectorRepository(db)

    def build(
        self,
        results: list[SearchResult],
        *,
        question: str = "",
        max_chars: int = _DEFAULT_MAX_CONTEXT_CHARS,
    ) -> ContextBundle:
        blocks: list[str] = []
        citations: list[CitationCandidate] = []
        used_chars = 0
        seen_file_ids: set[uuid.UUID] = set()

        for result in results:
            if result.file.id in seen_file_ids:
                continue
            seen_file_ids.add(result.file.id)

            block, passages, matched = self._render_block(result.file, question)
            if used_chars + len(block) > max_chars:
                if not blocks:
                    blocks.append(block[:max_chars])
                break

            blocks.append(block)
            used_chars += len(block)
            first = passages[0] if passages else None
            citations.append(
                CitationCandidate(
                    file=result.file,
                    snippet=first.text if first else None,
                    confidence=result.score,
                    retrieval_method=result.retrieval_method,
                    page_number=first.page if first else None,
                    passage_index=first.index if first else None,
                    matched=matched,
                )
            )

        return ContextBundle(context_text="\n\n".join(blocks), citations=citations)

    def _render_block(self, file: File, question: str) -> tuple[str, list[Passage], bool]:
        lines = [f"### {file.name} — {self._provider_name(file)} — {file.path}"]

        metadata = self._file_metadata.get_by_file_id(file.id)
        if metadata is not None and (metadata.owner_summary or metadata.sharing_summary):
            lines.append(
                f"Owner: {metadata.owner_summary or 'unknown'}; "
                f"Sharing: {metadata.sharing_summary or 'unknown'}"
            )

        related_names = self._related_file_names(file.id)
        if related_names:
            lines.append(f"Related files: {', '.join(related_names)}")

        extraction = self._extractions.get_by_file_id(file.id)
        text = extraction.extracted_text if extraction is not None else None
        if not text:
            lines.append("[no extracted content]")
            return "\n".join(lines), [], False

        passages = best_passages(question, text, file_name=file.name, limit=_PASSAGES_PER_FILE)
        matched = bool(passages)
        if not passages:
            passages = split_passages(text)[:1]
            lines.append("[opening of the file — no passage matched the question]")
        for passage in passages:
            where = f"page {passage.page}, " if passage.page else ""
            lines.append(f"[{where}passage {passage.index + 1}]\n{passage.text}")
        return "\n".join(lines), passages, matched

    def _provider_name(self, file: File) -> str:
        source = self._sources.get_by_id(file.storage_source_id)
        connector = self._connectors.get_by_id(source.connector_id) if source else None
        return provider_display_name(connector.provider) if connector else "connected storage"

    def _related_file_names(self, file_id: uuid.UUID) -> list[str]:
        relationships = self._relationships.list_for_file(file_id)[:_MAX_RELATED_NAMES]
        other_ids = [
            (
                relationship.related_file_id
                if relationship.file_id == file_id
                else relationship.file_id
            )
            for relationship in relationships
        ]
        files_by_id = {file.id: file for file in self._files.list_by_ids(other_ids)}
        return [files_by_id[other_id].name for other_id in other_ids if other_id in files_by_id]
