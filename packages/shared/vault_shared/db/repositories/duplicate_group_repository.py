import uuid

from sqlalchemy.orm import Session

from vault_shared.db.models import DuplicateGroup, DuplicateGroupMember, File, StorageSource


class DuplicateGroupRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_org_and_checksum(
        self, organization_id: uuid.UUID, checksum: str
    ) -> DuplicateGroup | None:
        return (
            self._session.query(DuplicateGroup)
            .filter_by(organization_id=organization_id, checksum=checksum)
            .first()
        )

    def upsert_group(
        self,
        *,
        organization_id: uuid.UUID,
        storage_analysis_job_id: uuid.UUID,
        checksum: str,
        file_count: int,
        total_size_bytes: int,
        recoverable_size_bytes: int,
        recommended_keep_file_id: uuid.UUID | None,
        recommended_keep_reason: str | None,
        recommended_keep_confidence: float | None,
    ) -> DuplicateGroup:
        """Identity key is `(organization_id, checksum)` — updated in place
        on every rerun so the group's `id` (and any deep link to it) stays
        stable across analyses, mirroring `Recommendation`'s
        upsert-by-rule-name convention."""
        group = self.get_by_org_and_checksum(organization_id, checksum)
        if group is None:
            group = DuplicateGroup(organization_id=organization_id, checksum=checksum)
            self._session.add(group)

        group.storage_analysis_job_id = storage_analysis_job_id
        group.file_count = file_count
        group.total_size_bytes = total_size_bytes
        group.recoverable_size_bytes = recoverable_size_bytes
        group.recommended_keep_file_id = recommended_keep_file_id
        group.recommended_keep_reason = recommended_keep_reason
        group.recommended_keep_confidence = recommended_keep_confidence
        self._session.flush()
        return group

    def replace_members(self, group: DuplicateGroup, members: list[tuple[uuid.UUID, bool]]) -> None:
        """Full delete-then-insert — group membership is a pure derived
        fact recomputed from scratch every run (see `DuplicateGroupMember`
        docstring), not an audit trail worth preserving partially."""
        self._session.query(DuplicateGroupMember).filter_by(duplicate_group_id=group.id).delete()
        for file_id, is_recommended_keep in members:
            self._session.add(
                DuplicateGroupMember(
                    duplicate_group_id=group.id,
                    file_id=file_id,
                    is_recommended_keep=is_recommended_keep,
                )
            )
        self._session.flush()

    def delete_groups_for_organization_not_in(
        self, organization_id: uuid.UUID, checksums_still_duplicated: set[str]
    ) -> None:
        """A checksum that no longer has ≥2 files (the last duplicate was
        removed/moved) stops being a duplicate group at all — deleted
        outright (cascades to its members), unlike `Recommendation`'s
        soft-resolve, since there's no "this used to be a duplicate"
        narrative worth keeping for storage accounting."""
        query = self._session.query(DuplicateGroup).filter(
            DuplicateGroup.organization_id == organization_id
        )
        if checksums_still_duplicated:
            query = query.filter(DuplicateGroup.checksum.notin_(checksums_still_duplicated))
        query.delete(synchronize_session=False)
        self._session.flush()

    def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
        connector_id: uuid.UUID | None = None,
    ) -> tuple[list[DuplicateGroup], int]:
        """With `connector_id`, only groups that have a redundant copy in
        that storage — the copies that storage could free."""
        query = self._session.query(DuplicateGroup).filter_by(organization_id=organization_id)
        if connector_id is not None:
            in_storage = (
                self._session.query(DuplicateGroupMember.duplicate_group_id)
                .join(File, DuplicateGroupMember.file_id == File.id)
                .join(StorageSource, File.storage_source_id == StorageSource.id)
                .filter(
                    StorageSource.connector_id == connector_id,
                    DuplicateGroupMember.is_recommended_keep.is_(False),
                )
            )
            query = query.filter(DuplicateGroup.id.in_(in_storage))
        total = query.count()
        items = (
            query.order_by(DuplicateGroup.recoverable_size_bytes.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )
        return items, total

    def get_owned(
        self, group_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> DuplicateGroup | None:
        return (
            self._session.query(DuplicateGroup)
            .filter_by(id=group_id, organization_id=organization_id)
            .first()
        )

    def list_recoverable_members_for_organization(
        self, organization_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, uuid.UUID, int]]:
        """`(group_id, file_id, size)` for every redundant copy — lets a
        per-storage summary count only the copies in that storage."""
        rows = (
            self._session.query(
                DuplicateGroupMember.duplicate_group_id,
                DuplicateGroupMember.file_id,
                File.size_bytes,
            )
            .join(DuplicateGroup, DuplicateGroupMember.duplicate_group_id == DuplicateGroup.id)
            .join(File, DuplicateGroupMember.file_id == File.id)
            .filter(
                DuplicateGroup.organization_id == organization_id,
                DuplicateGroupMember.is_recommended_keep.is_(False),
            )
            .all()
        )
        return [(group_id, file_id, size or 0) for group_id, file_id, size in rows]

    def list_recoverable_file_ids_with_sizes_for_organization(
        self, organization_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, int]]:
        """Every non-keep duplicate member across all of an org's groups —
        the exact recoverable file set, read from already-persisted
        `DuplicateGroup`/`DuplicateGroupMember` rows so the Savings Engine
        can deduplicate it against other candidate categories at the
        individual-file-id level (Phase 1 spec §12: "avoid double
        counting"), not just sum each category's own total independently."""
        rows = (
            self._session.query(DuplicateGroupMember.file_id, File.size_bytes)
            .join(DuplicateGroup, DuplicateGroupMember.duplicate_group_id == DuplicateGroup.id)
            .join(File, DuplicateGroupMember.file_id == File.id)
            .filter(
                DuplicateGroup.organization_id == organization_id,
                DuplicateGroupMember.is_recommended_keep.is_(False),
            )
            .all()
        )
        return [(file_id, size_bytes or 0) for file_id, size_bytes in rows]

    def get_membership_for_file(self, file_id: uuid.UUID) -> DuplicateGroupMember | None:
        """Used by `worker.organization.lifecycle_service` to check whether
        one file is a non-kept duplicate — a single-file lookup, unlike
        every other method on this repository which operates group-first."""
        return self._session.query(DuplicateGroupMember).filter_by(file_id=file_id).first()

    def list_members_with_files_for_groups(
        self, group_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[tuple[DuplicateGroupMember, File]]]:
        """Every listed group's members in one query, kept copy first."""
        members: dict[uuid.UUID, list[tuple[DuplicateGroupMember, File]]] = {
            group_id: [] for group_id in group_ids
        }
        if not group_ids:
            return members
        rows = (
            self._session.query(DuplicateGroupMember, File)
            .join(File, DuplicateGroupMember.file_id == File.id)
            .filter(DuplicateGroupMember.duplicate_group_id.in_(group_ids))
            .order_by(DuplicateGroupMember.is_recommended_keep.desc(), File.name)
            .all()
        )
        for member, file in rows:
            members[member.duplicate_group_id].append((member, file))
        return members

    def list_members_with_files(
        self, group_id: uuid.UUID
    ) -> list[tuple[DuplicateGroupMember, File]]:
        rows = (
            self._session.query(DuplicateGroupMember, File)
            .join(File, DuplicateGroupMember.file_id == File.id)
            .filter(DuplicateGroupMember.duplicate_group_id == group_id)
            .order_by(DuplicateGroupMember.is_recommended_keep.desc(), File.name)
            .all()
        )
        return [(member, file) for member, file in rows]
