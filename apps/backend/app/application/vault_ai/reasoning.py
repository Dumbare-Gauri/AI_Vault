"""Where the model actually reasons: grouping files into a sensible
structure, suggesting clear names, and reading a request the fixed rules
didn't recognize.

The model only ever sees short numbered references (F1, F2…) — never real
ids — and every answer is parsed as JSON and checked: unknown references are
dropped, names must be single safe path components, a renamed file keeps its
extension, and anything the model left out is reported as unsure rather than
guessed. A malformed or unavailable answer returns None and the caller falls
back to a deterministic path.
"""

import json
import re
from dataclasses import replace
from typing import Any

from app.application.vault_ai.understanding import KINDS, Request
from vault_shared import AIUnavailableError, get_logger
from vault_shared.ai_gateway import AIGateway, Message
from vault_shared.ai_gateway.boundary import with_boundary_rules
from vault_shared.ai_gateway.recommendation import validate_component_name
from vault_shared.db.models import File
from vault_shared.db.repositories import FileIntelligenceRepository
from vault_shared.formatting import human_bytes

logger = get_logger("app.application.vault_ai.reasoning")

_MAX_FILES_FOR_MODEL = 150
_MAX_OUTPUT_TOKENS = 2048
_SUMMARY_CHARS = 140
_FILE_TYPES = {
    "pdf",
    "video",
    "image",
    "spreadsheet",
    "presentation",
    "audio",
    "archive",
    "code",
    "document",
}

_ORGANIZE_PROMPT = """You organize a user's files into a clear folder structure.
You receive a numbered list of files (F1, F2, ...) with their current path, type and a
short summary when known. Group files that belong together — by project, client,
campaign or purpose, using the names, paths and summaries as evidence. Use at most 8
folders with short, plain names (no slashes). Put a file in "unsure" when the evidence
is weak; never force a file into a folder. Answer with JSON only:
{"root": "<top folder name>",
 "folders": [{"name": "<folder>", "files": ["F1", ...], "reason": "<one sentence>",
              "confidence": <0..1>}],
 "unsure": ["F7", ...]}"""

_RENAME_PROMPT = """You suggest clear, consistent file names. For each file that has an
unclear name (random numbers, "final2", "copy", camera names), suggest a better one based
on its summary, folder and type. Keep the same extension. Skip files whose names are
already clear. Answer with JSON only:
{"renames": [{"file": "F1", "new_name": "<name with extension>"}]}"""

_CLASSIFY_PROMPT = """You map a user's request about their files to one action of a
storage app. Answer with JSON only:
{"kind": one of %s,
 "scope": "local" | "drive" | null,
 "file_type": one of %s or null,
 "query": "<what the files are about, or empty>",
 "name": "<folder name if one is given, else null>"}
Use "ask" when the user asks a question about what is written inside files."""


def _json_object(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _safe_name(value: Any) -> str | None:
    try:
        return validate_component_name(str(value).strip(), what="Name")
    except ValueError:
        return None


def _extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


class ModelReasoning:
    def __init__(self, gateway: AIGateway, *, summaries: FileIntelligenceRepository) -> None:
        self._gateway = gateway
        self._summaries = summaries

    def _ask(self, system: str, data: str) -> dict[str, Any] | None:
        try:
            completion = self._gateway.complete(
                messages=[
                    Message(role="system", content=with_boundary_rules(system)),
                    Message(role="user", content="Here is the data. Answer with JSON only."),
                ],
                context=data,
                max_tokens=_MAX_OUTPUT_TOKENS,
            )
        except AIUnavailableError as error:
            logger.warning("vault_reasoning_unavailable", extra={"reason": error.reason})
            return None
        return _json_object(completion.text)

    def _listing(self, files: list[File]) -> tuple[str, dict[str, File]]:
        refs: dict[str, File] = {}
        lines = []
        for number, file in enumerate(files[:_MAX_FILES_FOR_MODEL], start=1):
            ref = f"F{number}"
            refs[ref] = file
            intelligence = self._summaries.get_by_file_id(file.id)
            summary = (intelligence.summary or "")[:_SUMMARY_CHARS] if intelligence else ""
            lines.append(
                f"{ref} | {file.name} | {file.path or ''} | {file.mime_type or ''} | "
                f"{human_bytes(file.size_bytes or 0)} | {summary}"
            )
        return "\n".join(lines), refs

    def organize(self, files: list[File], *, root: str) -> dict[str, Any] | None:
        listing, refs = self._listing(files)
        answer = self._ask(_ORGANIZE_PROMPT, f"Suggested top folder: {root}\n{listing}")
        if answer is None:
            return None
        used: set[str] = set()
        folders = []
        for folder in answer.get("folders") or []:
            if not isinstance(folder, dict):
                continue
            name = _safe_name(folder.get("name"))
            if not name:
                continue
            ids = []
            for ref in folder.get("files") or []:
                if isinstance(ref, str) and ref in refs and ref not in used:
                    used.add(ref)
                    ids.append(str(refs[ref].id))
            if name and ids:
                confidence = folder.get("confidence")
                folders.append(
                    {
                        "name": name,
                        "file_ids": ids,
                        "reason": str(folder.get("reason") or "")[:300] or None,
                        "confidence": float(confidence)
                        if isinstance(confidence, (int, float)) and 0 <= confidence <= 1
                        else None,
                    }
                )
        if not folders:
            return None
        unsure = [str(file.id) for ref, file in refs.items() if ref not in used]
        # Files beyond what the model could see stay where they are.
        unsure += [str(f.id) for f in files[_MAX_FILES_FOR_MODEL:]]
        return {
            "root": _safe_name(answer.get("root")) or root,
            "basis": "names, folders and content (AI-suggested)",
            "folders": folders,
            "unsure": unsure,
        }

    def rename(self, files: list[File]) -> list[dict[str, str]]:
        listing, refs = self._listing(files)
        answer = self._ask(_RENAME_PROMPT, listing)
        renames = []
        seen: set[str] = set()
        for item in (answer or {}).get("renames") or []:
            if not isinstance(item, dict) or item.get("file") not in refs or item["file"] in seen:
                continue
            file = refs[item["file"]]
            new_name = _safe_name(item.get("new_name"))
            if not new_name or new_name == file.name:
                continue
            if _extension(new_name) != _extension(file.name):
                continue
            seen.add(item["file"])
            renames.append({"file_id": str(file.id), "new_name": new_name})
        return renames

    def classify(self, question: str, fallback: Request) -> Request:
        prompt = _CLASSIFY_PROMPT % (sorted(KINDS), sorted(_FILE_TYPES))
        answer = self._ask(prompt, f"Request: {question[:500]}")
        if answer is None or answer.get("kind") not in KINDS:
            return fallback
        scope = answer.get("scope") if answer.get("scope") in ("local", "drive") else None
        file_type = answer.get("file_type") if answer.get("file_type") in _FILE_TYPES else None
        query = str(answer.get("query") or "")[:100]
        return replace(
            fallback,
            kind=answer["kind"],
            scope=scope or fallback.scope,
            file_type=file_type or fallback.file_type,
            query=query,
            search_text=query or fallback.search_text,
            name=_safe_name(answer.get("name")) if answer.get("name") else fallback.name,
        )
