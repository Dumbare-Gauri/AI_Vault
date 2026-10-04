from vault_shared.connectors.google_workspace import DRIVE_WRITE_SCOPE
from vault_shared.db.models import ConnectorCredentials, ConnectorStatus, StorageConnector

__all__ = ["DRIVE_WRITE_SCOPE", "validate_execution_permissions"]


def validate_execution_permissions(
    *, connector: StorageConnector | None, credentials: ConnectorCredentials | None
) -> list[str]:
    """The Phase 8 spec's "Permission Validation" — the DB-only checks
    (connector availability, provider scope) that don't need a live Drive
    call, so this is shared and callable from both `apps/backend`
    (`ApprovalService`, before enqueueing an execution job) and
    `apps/worker` (`ExecutionService`, as a defense-in-depth re-check
    right before actually executing, since time passes between approval
    and execution). Returns a list of human-readable failure reasons —
    empty means valid. The *other* half of the spec's validation list
    ("Resource existence," "Current file state") needs a live Drive read
    and happens only in the worker, per-step, immediately before that
    step's mutating call — see `ExecutionService`."""
    failures: list[str] = []

    if connector is None:
        failures.append("Connector no longer exists.")
        return failures
    if connector.status != ConnectorStatus.CONNECTED:
        failures.append(f"Connector is not connected (status: {connector.status}).")

    if credentials is None:
        failures.append("Connector has no stored credentials.")
    else:
        granted = (credentials.granted_scopes or "").split()
        if DRIVE_WRITE_SCOPE not in granted:
            failures.append(
                "Connector was authorized without Drive write access — reconnect Google "
                "Workspace to grant it before this plan can execute."
            )

    return failures
