"""citation location — which page and passage of a file an answer came from,
so every file-grounded answer is traceable to the exact text it used.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("citations", sa.Column("page_number", sa.Integer(), nullable=True))
    op.add_column("citations", sa.Column("passage_index", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("citations", "passage_index")
    op.drop_column("citations", "page_number")
