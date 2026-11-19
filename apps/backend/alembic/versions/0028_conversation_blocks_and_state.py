"""conversation blocks and state — an assistant answer is stored as
structured blocks (file lists, duplicate groups, action proposals…) next to
its text, and each conversation keeps AI Vault's own state (last results,
storage scope, pending actions) so context survives switching AI models.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversation_messages",
        sa.Column("blocks", JSONB, nullable=False, server_default="[]"),
    )
    op.add_column(
        "conversations",
        sa.Column("state", JSONB, nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("conversations", "state")
    op.drop_column("conversation_messages", "blocks")
