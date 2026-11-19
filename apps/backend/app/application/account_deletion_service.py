import uuid
from dataclasses import dataclass

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from vault_shared import ConflictError, ValidationError, get_logger
from vault_shared.connectors.google_workspace import GoogleWorkspaceOAuthClient
from vault_shared.db.models import (
    ArchiveJob,
    AuditLog,
    ConnectorCredentials,
    ExecutionJob,
    ExecutionJobStatus,
    File,
    Organization,
    StorageConnector,
    StorageSource,
    User,
)
from vault_shared.db.repositories import AuditLogRepository
from vault_shared.object_storage import ObjectStorageClient
from vault_shared.security.encryption import TokenEncryptionError, decrypt_token

logger = get_logger("app.application.account_deletion_service")

_ACTIVE_JOB_STATUSES = (
    ExecutionJobStatus.PENDING,
    ExecutionJobStatus.RUNNING,
    ExecutionJobStatus.PAUSED,
)


@dataclass(frozen=True)
class AccountDeletionSummary:
    connections_removed: int
    tokens_revoked: int
    files_unindexed: int
    cached_archives_deleted: int


class AccountDeletionService:
    """Deletes a person's AI Vault account and everything AI Vault holds for
    them: their workspace (organization), its storage connections and stored
    tokens, Local Agent registrations, file and folder indexes, extracted
    text and embeddings, conversations, recommendations, archives' records
    and cached copies, workflows and their schedules, and their audit trail.

    It never touches the files in the person's own storage — their Google
    Drive and their computer keep every original and every archive zip.
    Removing those is a separate act the person does themselves."""

    def __init__(
        self,
        db: Session,
        *,
        oauth_client: GoogleWorkspaceOAuthClient,
        object_storage: ObjectStorageClient | None,
    ) -> None:
        self._db = db
        self._oauth_client = oauth_client
        self._object_storage = object_storage
        self._audit_logs = AuditLogRepository(db)

    def delete_account(
        self, user: User, *, confirm_email: str, ip_address: str | None
    ) -> AccountDeletionSummary:
        if confirm_email.strip().lower() != user.email.lower():
            raise ValidationError("Type your account's email address exactly to confirm.")
        organization_id = user.organization_id
        others = (
            self._db.query(func.count(User.id))
            .filter(User.organization_id == organization_id, User.id != user.id)
            .scalar()
        )
        if others:
            # Each sign-in has its own workspace today; a shared one is never
            # deleted out from under its other members.
            raise ConflictError("This workspace has other members — it can't be deleted with you.")
        if self._has_running_storage_changes(organization_id):
            raise ConflictError(
                "Changes are still being made in your storage. Wait for them to finish, then "
                "delete your account."
            )

        connectors = (
            self._db.query(StorageConnector)
            .filter(StorageConnector.organization_id == organization_id)
            .all()
        )
        tokens_revoked = sum(self._revoke(connector.id) for connector in connectors)
        files_unindexed = (
            self._db.query(func.count(File.id))
            .join(StorageSource, File.storage_source_id == StorageSource.id)
            .join(StorageConnector, StorageSource.connector_id == StorageConnector.id)
            .filter(StorageConnector.organization_id == organization_id)
            .scalar()
            or 0
        )
        cached_archives = [
            key
            for (key,) in self._db.query(ArchiveJob.object_storage_key).filter(
                ArchiveJob.organization_id == organization_id,
                ArchiveJob.object_storage_key.is_not(None),
            )
        ]

        # The audit trail names the person, so it goes with the account; it
        # is the only table that doesn't follow the workspace on its own.
        self._db.query(AuditLog).filter(
            or_(AuditLog.organization_id == organization_id, AuditLog.user_id == user.id)
        ).delete(synchronize_session=False)
        # Everything else the workspace owns is removed by the database's
        # own ON DELETE CASCADE rules, in one statement.
        self._db.query(Organization).filter(Organization.id == organization_id).delete(
            synchronize_session=False
        )
        # Kept without anything that identifies the person.
        self._audit_logs.record(
            event_type="account_deleted",
            metadata={
                "connections_removed": len(connectors),
                "files_unindexed": files_unindexed,
            },
            ip_address=ip_address,
        )
        self._db.commit()
        self._db.expunge_all()

        cached_deleted = self._delete_cached_archives(cached_archives)
        return AccountDeletionSummary(
            connections_removed=len(connectors),
            tokens_revoked=tokens_revoked,
            files_unindexed=files_unindexed,
            cached_archives_deleted=cached_deleted,
        )

    def _has_running_storage_changes(self, organization_id: uuid.UUID) -> bool:
        return (
            self._db.query(ExecutionJob.id)
            .filter(
                ExecutionJob.organization_id == organization_id,
                ExecutionJob.status.in_(_ACTIVE_JOB_STATUSES),
            )
            .first()
            is not None
        )

    def _revoke(self, connector_id: uuid.UUID) -> int:
        """Asks Google to forget AI Vault's access — best effort: the stored
        tokens are deleted with the account either way."""
        credentials = (
            self._db.query(ConnectorCredentials).filter_by(connector_id=connector_id).first()
        )
        if credentials is None:
            return 0
        try:
            token = decrypt_token(credentials.refresh_token_encrypted)
        except TokenEncryptionError:
            logger.warning("account_deletion_could_not_decrypt_token")
            return 0
        return 1 if self._oauth_client.revoke(token=token) else 0

    def _delete_cached_archives(self, keys: list[str]) -> int:
        if self._object_storage is None:
            return 0
        deleted = 0
        for key in keys:
            try:
                self._object_storage.delete_object(key=key)
                deleted += 1
            except Exception:  # noqa: BLE001 - a cache copy left behind must not fail the deletion
                logger.exception("account_deletion_cache_delete_failed")
        return deleted
