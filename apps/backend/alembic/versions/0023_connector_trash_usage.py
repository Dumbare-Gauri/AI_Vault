"""connector trash usage — how much of the provider-reported storage is files
in the provider's Trash, which still count until the Trash is emptied.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "storage_connectors", sa.Column("storage_trash_bytes", sa.BigInteger(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("storage_connectors", "storage_trash_bytes")
