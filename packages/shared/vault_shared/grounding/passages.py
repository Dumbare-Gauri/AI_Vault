"""Deterministic passage selection for file-grounded answers.

Extracted text is split into passages that remember their page, and the
passages that share the most distinctive words with the question are the ones
handed to the AI — so an answer about page 7 is grounded in page 7, and every
fact can be cited back to a file, page and passage."""

import math
import re
from dataclasses import dataclass

# Written between pages by text extraction (form feed), so page numbers
# survive into stored text.
PAGE_BREAK = "\f"
_DEFAULT_MAX_CHARS = 700
_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    [
        "a",
        "about",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "contain",
        "containing",
        "contains",
        "could",
        "did",
        "do",
        "doc",
        "docs",
        "document",
        "documents",
        "does",
        "explain",
        "file",
        "files",
        "find",
        "for",
        "from",
        "give",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "its",
        "list",
        "me",
        "mention",
        "mentioned",
        "mentioning",
        "mentions",
        "my",
        "of",
        "on",
        "or",
        "our",
        "please",
        "said",
        "say",
        "says",
        "show",
        "summarise",
        "summarize",
        "summary",
        "tell",
        "than",
        "that",
        "the",
        "their",
        "them",
        "there",
        "these",
        "this",
        "those",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)


@dataclass(frozen=True)
class Passage:
    index: int
    page: int | None
    text: str


def question_terms(text: str) -> list[str]:
    """The distinctive words of a question, in order, without duplicates."""
    seen: dict[str, None] = {}
    for word in _WORD.findall(text.lower()):
        if word not in _STOPWORDS and len(word) > 1:
            seen.setdefault(word.rstrip("s") if len(word) > 3 else word, None)
    return list(seen)


def _terms(text: str) -> set[str]:
    return {
        word.rstrip("s") if len(word) > 3 else word
        for word in _WORD.findall(text.lower())
        if word not in _STOPWORDS and len(word) > 1
    }


def split_passages(text: str, *, max_chars: int = _DEFAULT_MAX_CHARS) -> list[Passage]:
    pages = text.split(PAGE_BREAK)
    paged = len(pages) > 1
    passages: list[Passage] = []
    for page_number, page in enumerate(pages, start=1):
        current = ""
        for paragraph in (p.strip() for p in re.split(r"\n\s*\n", page)):
            if not paragraph:
                continue
            while len(paragraph) > max_chars:
                if current:
                    passages.append(Passage(len(passages), page_number if paged else None, current))
                    current = ""
                cut = paragraph.rfind(" ", 0, max_chars)
                cut = cut if cut > 0 else max_chars
                passages.append(
                    Passage(len(passages), page_number if paged else None, paragraph[:cut].strip())
                )
                paragraph = paragraph[cut:].strip()
            if current and len(current) + len(paragraph) + 2 > max_chars:
                passages.append(Passage(len(passages), page_number if paged else None, current))
                current = ""
            current = f"{current}\n\n{paragraph}" if current else paragraph
        if current:
            passages.append(Passage(len(passages), page_number if paged else None, current))
    return passages


def best_passages(
    question: str, text: str, *, file_name: str = "", limit: int = 2
) -> list[Passage]:
    """Passages ranked by the distinctive question words they contain. Words
    that are already in the file's name identify the file, not a passage, so
    they don't count. Returns nothing when no passage shares a meaningful word
    with the question — the caller then has no evidence to answer from."""
    passages = split_passages(text)
    wanted = _terms(question) - _terms(file_name)
    if not passages or not wanted:
        return []
    passage_terms = [_terms(passage.text) for passage in passages]
    document_frequency = {
        term: sum(1 for terms in passage_terms if term in terms) for term in wanted
    }
    scored = []
    for passage, terms in zip(passages, passage_terms, strict=True):
        score = sum(
            math.log(1 + len(passages) / document_frequency[term]) for term in wanted & terms
        )
        if score > 0:
            scored.append((score, passage))
    scored.sort(key=lambda pair: (-pair[0], pair[1].index))
    return [passage for _, passage in scored[:limit]]
