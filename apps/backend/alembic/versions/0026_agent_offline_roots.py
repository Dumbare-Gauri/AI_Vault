"""agent offline roots — folders the user gave a Local Agent that are on a
drive not plugged in right now; kept so their index survives until it returns.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "local_agents",
        sa.Column("offline_roots", JSONB, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("local_agents", "offline_roots")
