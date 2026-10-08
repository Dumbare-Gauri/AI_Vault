import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from vault_shared.db.session import Base


class LocalAgent(Base):
    """An AI Vault Local Agent running on a user's machine, bound to one
    `StorageConnector` (provider `local_agent`). Only a hash of the agent's
    key is stored; the key itself is shown to the user once."""

    __tablename__ = "local_agents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    connector_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_connectors.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    device_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(255), nullable=True)
    roots: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Folders on a drive that isn't plugged in now; their index is kept.
    offline_roots: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    volumes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scan_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentCommandStatus(enum.StrEnum):
    PENDING = "pending"
    SENT = "sent"
    DONE = "done"
    FAILED = "failed"


class AgentCommand(Base):
    """One typed operation queued for a Local Agent. The agent fetches it,
    runs it from its fixed table of operations, and posts back what the
    filesystem showed afterwards."""

    __tablename__ = "agent_commands"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    connector_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_connectors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    op: Mapped[str] = mapped_column(String(50), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AgentCommandStatus.PENDING, index=True
    )
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
