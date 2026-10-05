"""product reset — archives stored and verified in connected storage, storage
quota per connection, and a provider choice for the organization's AI model.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "archive_jobs", sa.Column("destination_provider_file_id", sa.String(255), nullable=True)
    )
    op.add_column("archive_jobs", sa.Column("destination_path", sa.String(4096), nullable=True))
    op.add_column(
        "archive_jobs", sa.Column("destination_web_view_link", sa.String(2048), nullable=True)
    )
    op.add_column("archive_jobs", sa.Column("archive_md5", sa.String(64), nullable=True))
    op.add_column("archive_jobs", sa.Column("archive_sha256", sa.String(64), nullable=True))
    op.add_column(
        "archive_jobs", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "archive_jobs",
        sa.Column("remove_originals", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "archive_jobs",
        sa.Column("originals_removed_count", sa.Integer(), nullable=False, server_default="0"),
    )

    op.add_column("storage_connectors", sa.Column("display_name", sa.String(255), nullable=True))
    op.add_column(
        "storage_connectors", sa.Column("storage_used_bytes", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "storage_connectors", sa.Column("storage_total_bytes", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "storage_connectors",
        sa.Column("quota_checked_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "ai_provider_configs",
        sa.Column("provider", sa.String(30), nullable=False, server_default="openrouter"),
    )


def downgrade() -> None:
    op.drop_column("ai_provider_configs", "provider")
    for column in ("quota_checked_at", "storage_total_bytes", "storage_used_bytes", "display_name"):
        op.drop_column("storage_connectors", column)
    for column in (
        "originals_removed_count",
        "remove_originals",
        "verified_at",
        "archive_sha256",
        "archive_md5",
        "destination_web_view_link",
        "destination_path",
        "destination_provider_file_id",
    ):
        op.drop_column("archive_jobs", column)
