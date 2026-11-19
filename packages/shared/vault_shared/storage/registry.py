import uuid
from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable

from vault_shared.db.models import StorageConnector
from vault_shared.storage.adapter import StorageAdapter
from vault_shared.storage.errors import StorageUnsupportedError

AdapterFactory = Callable[[StorageConnector], StorageAdapter]


@runtime_checkable
class StorageAdapterProvider(Protocol):
    """What a service needs in order to reach storage: the adapter for one
    connection. Depending on this (not on the concrete registry) lets a test
    inject a fake adapter."""

    def adapter_for(self, connector: StorageConnector) -> StorageAdapter: ...


class StorageAdapterRegistry:
    """Maps a connection's provider to the adapter that serves it.

    connection → provider → factory → adapter

    Deliberately an ordinary object and not a module-level singleton: each
    request or job builds its own (bound to its database session), and tests
    build one with fakes. Adapters are memoized per connection for the life
    of the registry, so a job that touches thousands of files builds one
    adapter — and therefore one credential lookup per token lifetime, not one
    per file.

    Only backend services and worker composition roots hold a registry. It is
    never handed to AI code, which is enforced by the architecture tests.
    """

    def __init__(self, factories: Mapping[str, AdapterFactory] | None = None) -> None:
        self._factories: dict[str, AdapterFactory] = dict(factories or {})
        self._adapters: dict[uuid.UUID, StorageAdapter] = {}

    def register(self, provider: str, factory: AdapterFactory) -> None:
        self._factories[provider] = factory

    def providers(self) -> frozenset[str]:
        return frozenset(self._factories)

    def adapter_for(self, connector: StorageConnector) -> StorageAdapter:
        cached = self._adapters.get(connector.id)
        if cached is not None:
            return cached
        factory = self._factories.get(connector.provider)
        if factory is None:
            raise StorageUnsupportedError(
                "No storage adapter is available for this connection's provider.",
                provider=connector.provider,
            )
        adapter = factory(connector)
        self._adapters[connector.id] = adapter
        return adapter


class StaticAdapterProvider:
    """Serves one already-built adapter for every connection — for tests and
    for callers that have exactly one adapter in hand."""

    def __init__(self, adapter: StorageAdapter) -> None:
        self._adapter = adapter

    def adapter_for(self, connector: StorageConnector) -> StorageAdapter:
        return self._adapter
