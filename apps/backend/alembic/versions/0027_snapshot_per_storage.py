"""snapshot per storage — Storage Intelligence keeps one snapshot for the
whole organization (connector_id NULL) and one per connected storage, so the
page can be switched between them.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "storage_analysis_snapshots",
        sa.Column(
            "connector_id",
            UUID(as_uuid=True),
            sa.ForeignKey("storage_connectors.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_storage_analysis_snapshots_connector_id",
        "storage_analysis_snapshots",
        ["connector_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_storage_analysis_snapshots_connector_id", "storage_analysis_snapshots")
    op.drop_column("storage_analysis_snapshots", "connector_id")
