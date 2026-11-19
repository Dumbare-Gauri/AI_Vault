import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from vault_shared.db.models import File
from worker.enrichment.extraction import ExtractionOutcome
from worker.enrichment.naming import detect_naming_pattern, extract_plausible_extension


@dataclass(frozen=True)
class EnrichmentContext:
    """Everything a single-file processor needs — deliberately just data, no
    DB session or network access, so every processor is a pure function
    over its input and independently unit-testable (Phase 5 spec)."""

    file: File
    extraction: ExtractionOutcome | None


@dataclass
class ProcessorResult:
    """A processor's contribution — any field left at its default means
    "no opinion," letting the pipeline merge many processors' partial
    results without them needing to know about each other."""

    metadata: dict = field(default_factory=dict)
    classification: tuple[str, float, str] | None = None  # (document_type, confidence, method)
    knowledge_attributes: list[dict] = field(default_factory=list)


class FileProcessor(Protocol):
    """A single-file metadata processor (Phase 5 spec's "each processor
    should operate independently... contribute structured metadata rather
    than free-form text"). Deterministic by construction — none of these
    call the AI Gateway (Handbook's Design Principles: "be deterministic
    where possible")."""

    name: str

    def run(self, context: EnrichmentContext) -> ProcessorResult: ...


class ExtensionNormalizerProcessor:
    name = "extension_normalizer"

    def run(self, context: EnrichmentContext) -> ProcessorResult:
        extension = extract_plausible_extension(context.file.name)
        return ProcessorResult(metadata={"normalized_extension": extension})


_EXTENSION_MIME_HINTS: dict[str, set[str]] = {
    "pdf": {"application/pdf"},
    "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    "pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    "txt": {"text/plain"},
    "md": {"text/plain", "text/markdown"},
    "png": {"image/png"},
    "jpg": {"image/jpeg"},
    "jpeg": {"image/jpeg"},
    "gif": {"image/gif"},
}


class MimeValidatorProcessor:
    """Only flags a mismatch when the extension is one we have a confident
    expectation for — an unknown extension is never treated as invalid,
    just unverifiable (Phase 5 Design Principles: "produce explainable
    outputs," not false positives from an incomplete lookup table)."""

    name = "mime_validator"

    def run(self, context: EnrichmentContext) -> ProcessorResult:
        extension = extract_plausible_extension(context.file.name)
        expected = _EXTENSION_MIME_HINTS.get(extension or "")
        mime_type = context.file.mime_type

        if expected is None or mime_type is None or mime_type in expected:
            return ProcessorResult(
                metadata={"mime_type_validated": True, "mime_mismatch_reason": None}
            )
        reason = f"extension .{extension} does not match mime type {mime_type}"
        return ProcessorResult(
            metadata={"mime_type_validated": False, "mime_mismatch_reason": reason}
        )


_SOURCE_CODE_EXTENSIONS = {
    "py", "js", "ts", "tsx", "jsx", "java", "go", "rs", "c", "cpp", "rb", "php", "sql", "sh",
}
_DESIGN_ASSET_EXTENSIONS = {"psd", "ai", "sketch", "fig", "xd"}
_ARCHIVE_EXTENSIONS = {"zip", "tar", "gz", "rar", "7z"}
_SPREADSHEET_MIMES = {
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
}
_PRESENTATION_MIMES = {
    "application/vnd.google-apps.presentation",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint",
}
_DOCUMENT_MIMES = {
    "application/vnd.google-apps.document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/pdf",
    "text/plain",
    "text/markdown",
}
_INVOICE_KEYWORDS = ("invoice", "receipt")
_CONTRACT_KEYWORDS = ("contract", "agreement", "nda")
_DOCUMENTATION_KEYWORDS = ("readme", "guide", "manual", "documentation")


def _ordered_words(text: str) -> list[str]:
    return [word for word in re.split(r"[^a-z0-9]+", text.lower()) if word]


def _words(text: str) -> set[str]:
    """Whole words only — "nda" is a word in "Acme NDA.pdf" but not in
    "agenda.docx", and "dev" is not in "/devices"."""
    return set(_ordered_words(text))


class FileTypeClassifierProcessor:
    """The Classification Pipeline (Phase 5 spec) — an ordered set of
    deterministic rules, first match wins. Broad type (mime/extension) is
    checked before document-like items get a keyword-based refinement, so
    "invoice" only overrides the generic "Documentation" default for
    document-shaped files, never an image or archive that happens to be
    named "invoice.zip". `method` records exactly which rule fired, which
    is what makes the output explainable rather than a black box."""

    name = "file_type_classifier"

    def run(self, context: EnrichmentContext) -> ProcessorResult:
        file = context.file
        mime_type = file.mime_type or ""
        name_lower = file.name.lower()
        extension = extract_plausible_extension(file.name) or ""

        if mime_type.startswith("image/"):
            return self._result("Image", 0.9, "mime_image")
        if mime_type in _PRESENTATION_MIMES or extension == "ppt":
            return self._result("Presentation", 0.9, "mime_presentation")
        if mime_type in _SPREADSHEET_MIMES or extension in {"xls", "csv"}:
            return self._result("Spreadsheet", 0.9, "mime_spreadsheet")
        if extension in _ARCHIVE_EXTENSIONS:
            return self._result("Archive", 0.9, "extension_archive")
        if extension in _SOURCE_CODE_EXTENSIONS:
            return self._result("Source Code", 0.9, "extension_source_code")
        if extension in _DESIGN_ASSET_EXTENSIONS:
            return self._result("Design Asset", 0.9, "extension_design_asset")

        if mime_type in _DOCUMENT_MIMES or extension in {"doc", "docx", "pdf", "txt", "md"}:
            words = _words(name_lower)
            if words & set(_INVOICE_KEYWORDS):
                return self._result("Invoice", 0.75, "keyword_invoice")
            if words & set(_CONTRACT_KEYWORDS):
                return self._result("Contract", 0.75, "keyword_contract")
            if words & set(_DOCUMENTATION_KEYWORDS):
                return self._result("Documentation", 0.7, "keyword_documentation")
            return self._result("Documentation", 0.5, "mime_document_default")

        return self._result("Uncategorized", 0.2, "fallback_uncategorized")

    @staticmethod
    def _result(document_type: str, confidence: float, method: str) -> ProcessorResult:
        return ProcessorResult(classification=(document_type, confidence, method))


_DEPARTMENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Marketing": ("marketing", "campaign", "brand"),
    "Finance": ("finance", "accounting", "budget"),
    "Legal": ("legal", "compliance"),
    "HR": ("hr", "human resources", "recruiting", "people"),
    "Engineering": ("engineering", "eng", "dev"),
    "Sales": ("sales", "deals", "crm"),
}


class FolderContextAnalyzerProcessor:
    """Infers a `department` knowledge attribute from folder-path keywords
    (Handbook §16 — department is a lightweight, keyword-driven signal here,
    not a per-department AI agent). Low-to-medium confidence by design —
    this is a deterministic *candidate*, not a claim; Phase 6's AI-assisted
    classification is the place a low-confidence guess like this gets
    corroborated or overridden."""

    name = "folder_context_analyzer"

    def run(self, context: EnrichmentContext) -> ProcessorResult:
        folders = context.file.path.lower().split("/")[:-1]
        words = set().union(*(_words(folder) for folder in folders)) if folders else set()
        phrases = {" ".join(_ordered_words(folder)) for folder in folders}
        for department, keywords in _DEPARTMENT_KEYWORDS.items():
            if any(keyword in words or keyword in phrases for keyword in keywords):
                return ProcessorResult(
                    knowledge_attributes=[
                        {
                            "attribute_type": "department",
                            "value": department,
                            "confidence": 0.6,
                            "source": self.name,
                        }
                    ]
                )
        return ProcessorResult()


class NamingPatternAnalyzerProcessor:
    """Detects version-like naming tokens (`naming_pattern`/`version_label`,
    via the shared `naming.detect_naming_pattern` — the same helper
    `RelationshipDiscoveryService` uses to group version *sequences*) and
    derives a `time_period` knowledge attribute from the file's own provider
    timestamp — the latter is fully deterministic (a calendar computation,
    not an inference), included here because it's still a naming/temporal-
    context signal the Knowledge Builder consumes the same way as the
    inferred ones."""

    name = "naming_pattern_analyzer"

    def run(self, context: EnrichmentContext) -> ProcessorResult:
        naming_pattern, version_label = detect_naming_pattern(context.file.name)
        metadata: dict = {"naming_pattern": naming_pattern, "version_label": version_label}

        attributes: list[dict] = []
        modified = context.file.provider_modified_at or context.file.provider_created_at
        if modified is not None:
            attributes.append(
                {
                    "attribute_type": "time_period",
                    "value": _quarter_label(modified),
                    "confidence": 1.0,
                    "source": self.name,
                }
            )

        return ProcessorResult(metadata=metadata, knowledge_attributes=attributes)


def _quarter_label(when: datetime) -> str:
    quarter = (when.month - 1) // 3 + 1
    return f"{when.year}-Q{quarter}"


class OwnershipAnalyzerProcessor:
    name = "ownership_analyzer"

    def run(self, context: EnrichmentContext) -> ProcessorResult:
        return ProcessorResult(metadata={"owner_summary": context.file.owner_email or "unknown"})


class SharingAnalyzerProcessor:
    name = "sharing_analyzer"

    def run(self, context: EnrichmentContext) -> ProcessorResult:
        summary = "shared" if context.file.is_shared else "private"
        return ProcessorResult(metadata={"sharing_summary": summary})


_WORD_RE = re.compile(r"[a-zA-ZÀ-ÿ]+")
# A minimal stopword-frequency heuristic, not a language-ID library — "where
# practical" per the phase spec, scoped to a handful of common languages
# rather than pulling in a heavy/ML-based dependency for this phase.
_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset({"the", "and", "is", "of", "to", "in", "a", "that", "for", "with"}),
    "es": frozenset({"el", "la", "de", "que", "y", "en", "los", "un", "para", "con"}),
    "fr": frozenset({"le", "la", "de", "et", "un", "les", "des", "pour", "que", "avec"}),
    "de": frozenset({"der", "die", "und", "das", "ist", "den", "von", "für", "ein", "mit"}),
}
_MIN_WORDS_FOR_DETECTION = 20


class LanguageDetectionProcessor:
    """Runs only when extracted text is available (Phase 5 spec's "where
    practical") — a file with no supported extractor, or too little text,
    is left as "unknown" rather than guessed at."""

    name = "language_detection"

    def run(self, context: EnrichmentContext) -> ProcessorResult:
        if context.extraction is None or not context.extraction.text:
            return ProcessorResult()

        words = _WORD_RE.findall(context.extraction.text.lower())
        if len(words) < _MIN_WORDS_FOR_DETECTION:
            return ProcessorResult(metadata={"language": "unknown"})

        scores = {
            lang: sum(1 for word in words if word in stops) for lang, stops in _STOPWORDS.items()
        }
        best_language = max(scores, key=lambda lang: scores[lang])
        if scores[best_language] == 0:
            return ProcessorResult(metadata={"language": "unknown"})
        return ProcessorResult(metadata={"language": best_language})


DEFAULT_PIPELINE: list[FileProcessor] = [
    ExtensionNormalizerProcessor(),
    MimeValidatorProcessor(),
    FileTypeClassifierProcessor(),
    FolderContextAnalyzerProcessor(),
    NamingPatternAnalyzerProcessor(),
    OwnershipAnalyzerProcessor(),
    SharingAnalyzerProcessor(),
    LanguageDetectionProcessor(),
]
