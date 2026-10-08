import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from vault_shared.db.models import File
from vault_shared.db.repositories import DuplicateGroupRepository, FileRepository

_MAX_GROUP_SIZE = 200
# A path containing one of these segments is a weaker "canonical" candidate
# — not evidence the file is unwanted (Phase 1 spec explicitly forbids that
# claim), only evidence it's less likely to be the file's intended home.
_WEAK_LOCATION_SEGMENTS = ("download", "desktop", "temp", "tmp")


@dataclass(frozen=True)
class DuplicateAnalysisSummary:
    group_count: int
    file_count: int
    recoverable_bytes: int


class DuplicateDetector:
    """Exact-duplicate detection via content-checksum equality (Phase 1
    spec §6.1) — reads `File.checksum` directly (Drive's own `md5Checksum`,
    populated at scan time), never Phase 5's enrichment-owned
    `duplicate_group_key`, so this has no dependency on the enrichment
    pipeline having run. Candidate generation is a single DB-side
    `GROUP BY` (see `FileRepository.list_duplicate_checksum_groups_for_
    organization`) — O(n) over the organization, never a pairwise O(n²)
    comparison."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._files = FileRepository(session)
        self._groups = DuplicateGroupRepository(session)

    def analyze(
        self, organization_id: uuid.UUID, *, storage_analysis_job_id: uuid.UUID
    ) -> DuplicateAnalysisSummary:
        checksum_groups = self._files.list_duplicate_checksum_groups_for_organization(
            organization_id
        )

        total_file_count = 0
        total_recoverable = 0
        seen_checksums: set[str] = set()

        for checksum, _reported_count, _reported_total in checksum_groups:
            members = self._files.list_for_checksum_in_organization(organization_id, checksum)
            # Google-native files (Docs/Sheets/Slides) never carry an
            # md5Checksum (Phase 1 spec §6.7) — `checksum` is only ever
            # non-null for real binary content, so every member here has a
            # trustworthy, provider-issued content identity already.
            if len(members) < 2:
                continue
            if len(members) > _MAX_GROUP_SIZE:
                # A checksum shared by an implausibly large number of files
                # (e.g. a template/boilerplate zero-byte-adjacent file) is
                # not a meaningful "duplicate cluster" to surface — same
                # protective cap spirit as Phase 5's relationship discovery.
                continue

            seen_checksums.add(checksum)
            keep_file, reason, confidence = _recommend_keep(members)
            total_size = sum(f.size_bytes or 0 for f in members)
            recoverable = total_size - (keep_file.size_bytes or 0)

            group = self._groups.upsert_group(
                organization_id=organization_id,
                storage_analysis_job_id=storage_analysis_job_id,
                checksum=checksum,
                file_count=len(members),
                total_size_bytes=total_size,
                recoverable_size_bytes=recoverable,
                recommended_keep_file_id=keep_file.id,
                recommended_keep_reason=reason,
                recommended_keep_confidence=confidence,
            )
            self._groups.replace_members(group, [(f.id, f.id == keep_file.id) for f in members])

            total_file_count += len(members)
            total_recoverable += recoverable

        self._groups.delete_groups_for_organization_not_in(organization_id, seen_checksums)

        return DuplicateAnalysisSummary(
            group_count=len(seen_checksums),
            file_count=total_file_count,
            recoverable_bytes=total_recoverable,
        )


def _recommend_keep(members: list[File]) -> tuple[File, str, float]:
    """Advisory only (Phase 1 spec §6.5) — never a destructive decision.
    Combines two independent, explainable signals rather than trusting
    recency alone ("do not blindly assume newest = correct"):

    1. Path structure — a file NOT sitting in a Downloads/Desktop/Temp-like
       location scores higher (more likely its intentional, organized
       home).
    2. Recency — the most recently modified copy scores higher, as a
       tiebreaker among files with the same location signal.

    Confidence reflects how much the signals agree: unanimous agreement
    (one file wins both signals outright) reports higher confidence than a
    close/ambiguous call."""
    # The database returns duplicates in no defined order, so two copies that
    # tie on both signals must not be separated by row order. Oldest-created
    # first (the original), then name, makes the choice reproducible; the
    # scoring sort below is stable, so it only breaks exact ties.
    ordered = sorted(members, key=lambda f: (_created_sort_key(f), f.name))
    scored = sorted(
        ordered,
        key=lambda f: (_is_structured_location(f.path), _modified_sort_key(f)),
        reverse=True,
    )
    best = scored[0]
    structured = _is_structured_location(best.path)
    is_most_recent = best is max(ordered, key=_modified_sort_key)

    if structured and is_most_recent:
        reason = f"Most recently modified copy in a structured location ({best.path})."
        confidence = 0.86
    elif structured:
        reason = f"In a structured location, not the most recently modified copy ({best.path})."
        confidence = 0.65
    elif is_most_recent:
        reason = (
            f"Most recently modified copy, though its location may not be canonical ({best.path})."
        )
        confidence = 0.55
    else:
        reason = f"Best available signal among otherwise-similar copies ({best.path})."
        confidence = 0.4

    return best, reason, confidence


def _is_structured_location(path: str) -> bool:
    lowered = path.lower()
    return not any(segment in lowered for segment in _WEAK_LOCATION_SEGMENTS)


def _modified_sort_key(file: File) -> datetime:
    return file.provider_modified_at or datetime.min.replace(tzinfo=UTC)


def _created_sort_key(file: File) -> datetime:
    return file.provider_created_at or datetime.max.replace(tzinfo=UTC)
