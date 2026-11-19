"""The name a user gives an archive: checked once here, so the plan, the
Execution Engine and every storage agree on what is acceptable."""

import re

from vault_shared.errors import ValidationError

MAX_ARCHIVE_NAME_LENGTH = 200
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)


def clean_archive_name(raw: str) -> str:
    """`raw` trimmed and ending in `.zip`, or a `ValidationError` saying what
    to change. Refuses anything a storage could read as a path or that
    Windows can't store."""
    name = raw.strip()
    if name.lower().endswith(".zip"):
        name = name[:-4].rstrip()
    if not name or name.strip(".") == "":
        raise ValidationError("Give the archive a name.")
    if _FORBIDDEN.search(name):
        raise ValidationError('An archive name can\'t contain < > : " / \\ | ? * characters.')
    if name.endswith((".", " ")):
        raise ValidationError("An archive name can't end with a dot or a space.")
    if name.split(".")[0].lower() in _RESERVED:
        raise ValidationError(f"'{name}' is a name Windows reserves. Choose another.")
    full = f"{name}.zip"
    if len(full) > MAX_ARCHIVE_NAME_LENGTH:
        raise ValidationError(
            f"Keep the archive name under {MAX_ARCHIVE_NAME_LENGTH} characters."
        )
    return full


def numbered_archive_name(name: str, taken: set[str]) -> str:
    """`name`, or `name (2).zip`, `name (3).zip`… — the first not in
    `taken` (compared case-insensitively). An existing archive is never
    overwritten."""
    lowered = {item.lower() for item in taken}
    if name.lower() not in lowered:
        return name
    stem = name[:-4]
    number = 2
    while f"{stem} ({number}).zip".lower() in lowered:
        number += 1
    return f"{stem} ({number}).zip"
