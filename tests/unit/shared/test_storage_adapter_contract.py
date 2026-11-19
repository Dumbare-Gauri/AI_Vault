"""Runs the shared storage adapter contract against every adapter we have.

To add a provider: implement its adapter, give it a harness in
`tests/storage_testing.py` (seed/edit/revoke/fail operations against a fake of
that provider — never a real one), and add a subclass here. The suite in
`tests/storage_contract.py` then applies unchanged.
"""

import pytest
from storage_contract import StorageAdapterContract
from storage_testing import GoogleDriveHarness, InMemoryHarness

from vault_shared.storage import StorageCapabilities


class TestGoogleDriveAdapterContract(StorageAdapterContract):
    @pytest.fixture
    def harness(self) -> GoogleDriveHarness:
        return GoogleDriveHarness()


class TestInMemoryAdapterContract(StorageAdapterContract):
    """A second implementation that shares no code with the Google one —
    passing the same suite shows the contract is not Google-shaped."""

    @pytest.fixture
    def harness(self) -> InMemoryHarness:
        return InMemoryHarness()


class TestMinimalProviderContract(StorageAdapterContract):
    """A provider that supports almost nothing optional: no trash, restore,
    permanent delete, copy, folders, upload, thumbnails, permissions, export
    or metadata updates. Every one of those must say so explicitly."""

    @pytest.fixture
    def harness(self) -> InMemoryHarness:
        return InMemoryHarness(
            StorageCapabilities(
                supports_download=True,
                supports_streaming=True,
                supports_change_feed=True,
            )
        )
