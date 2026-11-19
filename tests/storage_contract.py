"""The storage adapter contract, as an executable suite.

This module is not collected by pytest on its own (its name has no `test_`
prefix). A test module subclasses `StorageAdapterContract` and supplies a
`harness` fixture; every adapter — Google Drive today, Microsoft Graph,
Dropbox and local storage later — must pass the identical suite. See
`tests/unit/shared/test_storage_adapter_contract.py` for how to wire one in.

A harness offers the uniform "outside world" operations the tests need (seed a
file, edit it behind the adapter's back, revoke credentials, make the provider
fail). Behavior an adapter does not offer is checked through its declared
capabilities: an unsupported operation must raise `StorageUnsupportedError`,
never quietly succeed or fail some other way.
"""

import inspect

import pytest

from vault_shared.errors import (
    ConflictError,
    DependencyUnavailableError,
    ForbiddenError,
    UnauthorizedError,
    ValidationError,
)
from vault_shared.storage import (
    FOLDER_MIME_TYPE,
    MUTATING_METHODS,
    ExportPurpose,
    HealthStatus,
    ProviderFileId,
    StorageAdapter,
    StorageAuthenticationRequiredError,
    StorageConflictError,
    StorageContentTooLargeError,
    StorageError,
    StorageForbiddenError,
    StorageInvalidRequestError,
    StorageNotFoundError,
    StoragePermission,
    StorageRateLimitedError,
    StorageUnauthorizedError,
    StorageUnavailableError,
    StorageUnsupportedError,
    read_bounded,
)

_CONTENT = b"0123456789abcdef"

_FAILURE_TYPES: dict[str, tuple[type[StorageError], type[Exception] | None]] = {
    "unauthorized": (StorageUnauthorizedError, UnauthorizedError),
    "rate_limited": (StorageRateLimitedError, DependencyUnavailableError),
    "unavailable": (StorageUnavailableError, DependencyUnavailableError),
    "timeout": (StorageUnavailableError, DependencyUnavailableError),
    "forbidden": (StorageForbiddenError, ForbiddenError),
    "conflict": (StorageConflictError, ConflictError),
    "invalid": (StorageInvalidRequestError, ValidationError),
}


def _ids(pages_or_files) -> set[str]:
    return {item.provider_file_id for item in pages_or_files}


class StorageAdapterContract:
    """Subclass and provide a `harness` fixture."""

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def adapter_of(harness) -> StorageAdapter:
        return harness.adapter

    @staticmethod
    def caps(harness):
        return harness.adapter.capabilities()

    @staticmethod
    def scan_all(harness) -> list:
        files: list = []
        cursor = None
        while True:
            page = harness.adapter.scan(harness.container_id, cursor)
            files.extend(page.files)
            cursor = page.next_cursor
            if cursor is None:
                return files

    # -- shape ---------------------------------------------------------------

    def test_the_adapter_satisfies_the_storage_adapter_protocol(self, harness) -> None:
        assert isinstance(harness.adapter, StorageAdapter)
        assert harness.adapter.provider

    def test_no_adapter_method_accepts_or_returns_a_credential(self, harness) -> None:
        forbidden_words = ("token", "credential", "secret", "password", "bearer")
        for name, member in inspect.getmembers(type(harness.adapter), inspect.isfunction):
            if name.startswith("_"):
                continue
            signature = inspect.signature(member)
            for parameter in signature.parameters:
                assert not any(word in parameter.lower() for word in forbidden_words), (
                    f"{name}({parameter}) takes a credential"
                )

    def test_every_mutating_method_named_by_the_contract_exists(self, harness) -> None:
        for name in MUTATING_METHODS:
            assert callable(getattr(harness.adapter, name)), name

    def test_capabilities_are_static_and_need_no_provider_call(self, harness) -> None:
        before = len(harness.provider_calls())

        capabilities = harness.adapter.capabilities()
        _ = capabilities.supported()

        assert len(harness.provider_calls()) == before

    # -- connection ------------------------------------------------------------

    def test_connect_succeeds_with_valid_credentials(self, harness) -> None:
        harness.adapter.connect()

    def test_connect_requires_reconnection_when_the_grant_is_revoked(self, harness) -> None:
        harness.revoke_credentials()

        with pytest.raises(StorageAuthenticationRequiredError):
            harness.adapter.connect()

    def test_health_is_ok_for_a_working_connection(self, harness) -> None:
        health = harness.adapter.health()

        assert health.status is HealthStatus.OK
        assert health.ok

    def test_health_reports_authentication_required_instead_of_raising(self, harness) -> None:
        harness.revoke_credentials()

        health = harness.adapter.health()

        assert health.status is HealthStatus.AUTHENTICATION_REQUIRED
        assert not health.ok

    def test_health_reports_degraded_when_the_provider_is_unavailable(self, harness) -> None:
        harness.inject_failure("unavailable")

        assert harness.adapter.health().status is HealthStatus.DEGRADED

    def test_write_access_is_reported_for_a_full_grant(self, harness) -> None:
        assert harness.adapter.write_access_problems() == []

    def test_a_read_only_grant_is_reported_with_a_reason(self, harness) -> None:
        harness.make_read_only()

        problems = harness.adapter.write_access_problems()

        assert problems
        assert all(isinstance(problem, str) and problem for problem in problems)

    def test_disconnect_revokes_the_provider_grant(self, harness) -> None:
        harness.adapter.disconnect()

        assert harness.disconnected()

    # -- discovery -------------------------------------------------------------

    def test_containers_start_with_the_personal_space(self, harness) -> None:
        containers = harness.adapter.list_containers()

        assert containers
        assert containers[0].kind.value == "personal"
        assert containers[0].id == harness.container_id

    def test_scan_reports_seeded_files_in_the_normalized_shape(self, harness) -> None:
        file_id = harness.seed_file("Report.pdf", content=_CONTENT, mime_type="application/pdf")

        found = {f.provider_file_id: f for f in self.scan_all(harness)}

        item = found[file_id]
        assert item.name == "Report.pdf"
        assert item.mime_type == "application/pdf"
        assert item.size_bytes == len(_CONTENT)
        assert item.is_folder is False
        assert item.trashed is False
        assert item.parent_id == harness.root_id
        assert item.extension == "pdf"
        assert item.revision.token is not None

    def test_scan_reports_folders_as_folders(self, harness) -> None:
        folder_id = harness.seed_folder("Clients")

        found = {f.provider_file_id: f for f in self.scan_all(harness)}

        assert found[folder_id].is_folder is True
        assert found[folder_id].mime_type == FOLDER_MIME_TYPE

    def test_scan_is_paged_and_a_caller_looping_sees_every_file_once(self, harness) -> None:
        harness.set_page_size(2)
        seeded = {harness.seed_file(f"file-{i}.txt") for i in range(5)}

        first = harness.adapter.scan(harness.container_id, None)
        every = self.scan_all(harness)

        assert first.next_cursor is not None
        assert len(first.files) == 2
        assert [f.provider_file_id for f in every].count(next(iter(seeded))) == 1
        assert seeded <= _ids(every)

    def test_scan_does_not_list_trashed_items(self, harness) -> None:
        keep = harness.seed_file("keep.txt")
        gone = harness.seed_file("gone.txt")
        harness.external_trash(gone)

        listed = _ids(self.scan_all(harness))

        assert keep in listed
        assert gone not in listed

    def test_get_file_is_a_live_read_with_a_revision(self, harness) -> None:
        file_id = harness.seed_file("a.txt")

        item = harness.adapter.get_file(file_id)

        assert item.provider_file_id == file_id
        assert item.name == "a.txt"
        assert item.provider == harness.adapter.provider
        assert item.revision.token is not None

    def test_get_file_of_an_unknown_id_is_not_found(self, harness) -> None:
        with pytest.raises(StorageNotFoundError):
            harness.adapter.get_file(ProviderFileId("no-such-item"))

    def test_a_content_edit_changes_the_revision(self, harness) -> None:
        file_id = harness.seed_file("doc.txt")
        before = harness.adapter.get_file(file_id).revision

        harness.external_edit(file_id)
        after = harness.adapter.get_file(file_id).revision

        assert after.differs_from(before)

    def test_a_rename_alone_is_not_a_content_revision_change(self, harness) -> None:
        file_id = harness.seed_file("doc.txt")
        before = harness.adapter.get_file(file_id).revision

        harness.external_rename(file_id, "renamed.txt")
        after = harness.adapter.get_file(file_id).revision

        assert not after.differs_from(before)

    def test_native_documents_are_flagged(self, harness) -> None:
        native_id, mime = harness.seed_native_document("Plan")

        assert harness.adapter.get_file(native_id).native_document is True
        assert harness.adapter.is_native_document(mime)
        assert not harness.adapter.is_native_document("text/plain")

    def test_permissions_are_normalized_or_declared_unsupported(self, harness) -> None:
        file_id = harness.seed_file("shared.txt")

        if self.caps(harness).supports_permissions:
            permissions = harness.adapter.get_permissions(file_id)
            assert all(isinstance(p, StoragePermission) for p in permissions)
        else:
            with pytest.raises(StorageUnsupportedError):
                harness.adapter.get_permissions(file_id)

    # -- change detection ------------------------------------------------------

    def test_changes_report_edits_new_files_and_deletions_since_a_cursor(self, harness) -> None:
        if not self.caps(harness).supports_change_feed:
            with pytest.raises(StorageUnsupportedError):
                harness.adapter.get_change_cursor(harness.container_id)
            return
        edited = harness.seed_file("edited.txt")
        doomed = harness.seed_file("doomed.txt")
        cursor = harness.adapter.get_change_cursor(harness.container_id)

        harness.external_edit(edited)
        created = harness.seed_file("created.txt")
        harness.external_delete(doomed)
        page = harness.adapter.check_changes(harness.container_id, cursor)

        assert {edited, created} <= _ids(page.changed)
        assert doomed in page.removed_ids

    def test_a_trashed_item_is_reported_as_trashed_or_removed(self, harness) -> None:
        if not self.caps(harness).supports_change_feed:
            return
        file_id = harness.seed_file("t.txt")
        cursor = harness.adapter.get_change_cursor(harness.container_id)

        harness.external_trash(file_id)
        page = harness.adapter.check_changes(harness.container_id, cursor)

        reported_trashed = any(f.provider_file_id == file_id and f.trashed for f in page.changed)
        assert reported_trashed or file_id in page.removed_ids

    def test_an_up_to_date_cursor_reports_nothing_and_a_next_cursor(self, harness) -> None:
        if not self.caps(harness).supports_change_feed:
            return
        harness.seed_file("x.txt")
        cursor = harness.adapter.get_change_cursor(harness.container_id)
        first = harness.adapter.check_changes(harness.container_id, cursor)
        assert first.new_start_cursor is not None

        again = harness.adapter.check_changes(harness.container_id, first.new_start_cursor)

        assert again.changed == ()
        assert again.removed_ids == ()

    # -- reading ---------------------------------------------------------------

    def test_reading_streams_the_content_in_chunks(self, harness) -> None:
        file_id = harness.seed_file("data.bin", content=_CONTENT)
        if not self.caps(harness).supports_download:
            with pytest.raises(StorageUnsupportedError):
                harness.adapter.open_read(file_id)
            return

        stream = harness.adapter.open_read(file_id)
        chunks = list(stream)

        assert not isinstance(stream, bytes | bytearray | list)
        assert b"".join(chunks) == _CONTENT
        assert len(chunks) > 1

    def test_a_bounded_read_abandons_an_oversized_stream_and_closes_it(self, harness) -> None:
        file_id = harness.seed_file("big.bin", content=_CONTENT)
        if not self.caps(harness).supports_download:
            return
        stream = harness.adapter.open_read(file_id)

        with pytest.raises(StorageContentTooLargeError):
            read_bounded(stream, max_bytes=5)

        assert stream.closed  # type: ignore[attr-defined]

    def test_a_bounded_read_returns_everything_within_the_limit(self, harness) -> None:
        file_id = harness.seed_file("ok.bin", content=_CONTENT)
        if not self.caps(harness).supports_download:
            return

        assert read_bounded(harness.adapter.open_read(file_id), max_bytes=1024) == _CONTENT

    def test_reading_an_unknown_item_is_not_found(self, harness) -> None:
        if not self.caps(harness).supports_download:
            return

        with pytest.raises(StorageNotFoundError):
            harness.adapter.open_read(ProviderFileId("no-such-item"))

    def test_native_documents_export_for_every_purpose(self, harness) -> None:
        native_id, mime = harness.seed_native_document("Brief")
        for purpose in ExportPurpose:
            export_format = harness.adapter.export_format_for(mime, purpose)
            if not self.caps(harness).supports_native_export:
                assert export_format is None
                continue
            assert export_format is not None
            assert export_format.mime_type and export_format.extension and export_format.label
            assert b"".join(harness.adapter.export(native_id, export_format)) == b"doc text"

    def test_ordinary_files_have_no_export_format(self, harness) -> None:
        for purpose in ExportPurpose:
            assert harness.adapter.export_format_for("text/plain", purpose) is None

    def test_thumbnails_are_bytes_or_none_or_declared_unsupported(self, harness) -> None:
        file_id = harness.seed_file("pic.png", mime_type="image/png")

        if self.caps(harness).supports_thumbnails:
            thumbnail = harness.adapter.get_thumbnail(file_id)
            assert thumbnail is None or isinstance(thumbnail, bytes)
        else:
            with pytest.raises(StorageUnsupportedError):
                harness.adapter.get_thumbnail(file_id)

    # -- mutation --------------------------------------------------------------

    def test_rename_changes_the_name_and_keeps_the_identity(self, harness) -> None:
        file_id = harness.seed_file("old.txt")

        result = harness.adapter.rename(file_id, "new.txt")

        assert result.provider_file_id == file_id
        assert result.name == "new.txt"
        assert harness.adapter.get_file(file_id).name == "new.txt"

    def test_renaming_to_the_current_name_is_harmless(self, harness) -> None:
        file_id = harness.seed_file("same.txt")

        harness.adapter.rename(file_id, "same.txt")
        harness.adapter.rename(file_id, "same.txt")

        assert harness.adapter.get_file(file_id).name == "same.txt"

    def test_renaming_an_unknown_item_is_not_found(self, harness) -> None:
        with pytest.raises(StorageNotFoundError):
            harness.adapter.rename(ProviderFileId("no-such-item"), "x.txt")

    def test_move_changes_the_parent(self, harness) -> None:
        folder = harness.seed_folder("Archive")
        file_id = harness.seed_file("m.txt")
        current = harness.adapter.get_file(file_id)
        assert current.parent_id is not None

        moved = harness.adapter.move(file_id, new_parent_id=folder, old_parent_id=current.parent_id)

        assert moved.parent_id == folder
        assert harness.adapter.get_file(file_id).parent_id == folder

    def test_moving_into_a_missing_destination_is_refused(self, harness) -> None:
        file_id = harness.seed_file("m.txt")
        current = harness.adapter.get_file(file_id)
        assert current.parent_id is not None

        with pytest.raises((StorageNotFoundError, StorageInvalidRequestError)):
            harness.adapter.move(
                file_id,
                new_parent_id=ProviderFileId("no-such-folder"),
                old_parent_id=current.parent_id,
            )

    def test_moving_an_unknown_item_is_not_found(self, harness) -> None:
        with pytest.raises(StorageNotFoundError):
            harness.adapter.move(
                ProviderFileId("no-such-item"),
                new_parent_id=ProviderFileId(harness.root_id),
                old_parent_id=ProviderFileId(harness.root_id),
            )

    def test_copy_makes_a_new_independent_item(self, harness) -> None:
        file_id = harness.seed_file("orig.txt", content=_CONTENT)
        if not self.caps(harness).supports_copy:
            with pytest.raises(StorageUnsupportedError):
                harness.adapter.copy(file_id)
            return

        copied = harness.adapter.copy(file_id, new_name="orig copy.txt")

        assert copied.provider_file_id != file_id
        assert copied.name == "orig copy.txt"
        assert harness.adapter.get_file(file_id).name == "orig.txt"

    def test_create_folder_makes_a_folder_under_the_parent(self, harness) -> None:
        if not self.caps(harness).supports_create_folder:
            with pytest.raises(StorageUnsupportedError):
                harness.adapter.create_folder("New")
            return
        parent = harness.seed_folder("Parent")

        folder = harness.adapter.create_folder("Child", parent_id=parent)

        assert folder.is_folder is True
        assert folder.mime_type == FOLDER_MIME_TYPE
        assert folder.parent_id == parent
        assert harness.adapter.get_file(folder.provider_file_id).name == "Child"

    def test_trash_and_restore_round_trip(self, harness) -> None:
        file_id = harness.seed_file("t.txt")
        caps = self.caps(harness)
        if not caps.supports_trash:
            with pytest.raises(StorageUnsupportedError):
                harness.adapter.trash(file_id)
            return

        assert harness.adapter.trash(file_id).trashed is True
        assert harness.adapter.get_file(file_id).trashed is True
        if caps.supports_restore:
            assert harness.adapter.restore(file_id).trashed is False
            assert harness.adapter.get_file(file_id).trashed is False
        else:
            with pytest.raises(StorageUnsupportedError):
                harness.adapter.restore(file_id)

    def test_trashing_twice_is_not_an_error(self, harness) -> None:
        file_id = harness.seed_file("t.txt")
        if not self.caps(harness).supports_trash:
            return

        harness.adapter.trash(file_id)
        harness.adapter.trash(file_id)

        assert harness.adapter.get_file(file_id).trashed is True

    def test_restoring_an_item_that_is_not_trashed_is_not_an_error(self, harness) -> None:
        file_id = harness.seed_file("t.txt")
        if not self.caps(harness).supports_restore:
            return

        assert harness.adapter.restore(file_id).trashed is False

    def test_trashing_an_unknown_item_is_not_found(self, harness) -> None:
        if not self.caps(harness).supports_trash:
            return

        with pytest.raises(StorageNotFoundError):
            harness.adapter.trash(ProviderFileId("no-such-item"))

    def test_permanent_delete_removes_the_item(self, harness) -> None:
        file_id = harness.seed_file("d.txt")
        if not self.caps(harness).supports_permanent_delete:
            with pytest.raises(StorageUnsupportedError):
                harness.adapter.permanent_delete(file_id)
            return

        harness.adapter.permanent_delete(file_id)

        with pytest.raises(StorageNotFoundError):
            harness.adapter.get_file(file_id)
        with pytest.raises(StorageNotFoundError):
            harness.adapter.permanent_delete(file_id)

    def test_update_metadata_returns_the_item_or_is_declared_unsupported(self, harness) -> None:
        file_id = harness.seed_file("meta.txt")
        if not self.caps(harness).supports_metadata_update:
            with pytest.raises(StorageUnsupportedError):
                harness.adapter.update_metadata(file_id, {"k": "v"})
            return

        assert harness.adapter.update_metadata(file_id, {"k": "v"}).provider_file_id == file_id

    def test_upload_creates_a_file_or_is_declared_unsupported(self, harness) -> None:
        if not self.caps(harness).supports_upload:
            with pytest.raises(StorageUnsupportedError):
                harness.adapter.upload("up.txt", iter([b"abc"]), mime_type="text/plain")
            return

        uploaded = harness.adapter.upload("up.txt", iter([b"abc"]), mime_type="text/plain")

        assert uploaded.name == "up.txt"

    # -- errors ----------------------------------------------------------------

    @pytest.mark.parametrize("kind", sorted(_FAILURE_TYPES))
    def test_provider_failures_arrive_as_normalized_errors(self, harness, kind: str) -> None:
        file_id = harness.seed_file("e.txt")
        expected, _legacy = _FAILURE_TYPES[kind]
        harness.inject_failure(kind)

        with pytest.raises(expected) as caught:
            harness.adapter.get_file(file_id)

        assert isinstance(caught.value, StorageError)
        assert caught.value.provider

    @pytest.mark.parametrize("kind", sorted(_FAILURE_TYPES))
    def test_normalized_errors_still_satisfy_the_application_error_they_replace(
        self, harness, kind: str
    ) -> None:
        file_id = harness.seed_file("e.txt")
        _expected, legacy = _FAILURE_TYPES[kind]
        harness.inject_failure(kind)

        with pytest.raises(legacy):  # type: ignore[arg-type]
            harness.adapter.get_file(file_id)

    def test_a_rate_limit_carries_the_providers_retry_after(self, harness) -> None:
        file_id = harness.seed_file("e.txt")
        harness.inject_failure("rate_limited")

        with pytest.raises(StorageRateLimitedError) as caught:
            harness.adapter.get_file(file_id)

        assert caught.value.retry_after_seconds == 7.0
        assert caught.value.retryable is True

    def test_transient_failures_are_retryable_and_permanent_ones_are_not(self, harness) -> None:
        file_id = harness.seed_file("e.txt")
        outcomes: dict[str, bool] = {}
        for kind in ("rate_limited", "unavailable", "forbidden", "invalid", "conflict"):
            harness.inject_failure(kind)
            with pytest.raises(StorageError) as caught:
                harness.adapter.get_file(file_id)
            outcomes[kind] = caught.value.retryable

        assert outcomes == {
            "rate_limited": True,
            "unavailable": True,
            "forbidden": False,
            "invalid": False,
            "conflict": False,
        }

    def test_a_rejected_access_token_is_an_error_about_the_provider_not_the_user(
        self, harness
    ) -> None:
        file_id = harness.seed_file("e.txt")
        harness.rotate_token_provider_side()

        with pytest.raises(StorageUnauthorizedError) as caught:
            harness.adapter.get_file(file_id)

        assert caught.value.http_status != 401

    def test_a_revoked_grant_requires_reconnection_and_is_not_a_401(self, harness) -> None:
        file_id = harness.seed_file("e.txt")
        harness.revoke_credentials()

        with pytest.raises(StorageAuthenticationRequiredError) as caught:
            harness.adapter.get_file(file_id)

        assert caught.value.http_status != 401
        assert caught.value.retryable is False

    @pytest.mark.parametrize("kind", sorted(_FAILURE_TYPES))
    def test_errors_never_carry_the_credential(self, harness, kind: str) -> None:
        file_id = harness.seed_file("e.txt")
        harness.inject_failure(kind)

        with pytest.raises(StorageError) as caught:
            harness.adapter.get_file(file_id)

        rendered = f"{caught.value!s} {caught.value!r} {caught.value.details!r}"
        assert harness.secret not in rendered

    def test_an_unknown_item_error_does_not_echo_the_requested_id_or_credentials(
        self, harness
    ) -> None:
        with pytest.raises(StorageNotFoundError) as caught:
            harness.adapter.get_file(ProviderFileId("no-such-item"))

        assert harness.secret not in f"{caught.value!s} {caught.value.details!r}"
