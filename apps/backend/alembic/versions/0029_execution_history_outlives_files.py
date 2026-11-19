"""execution history outlives files — an execution step keeps its row (and
with it its result, rollback record and audit trail) when the file it acted
on leaves the index, e.g. when a local folder is removed from the agent.
Before this, removing a folder cascaded through files into every step that
had trashed one of its files, erasing what was done and the way to undo it.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "execution_steps_target_file_id_fkey"


def upgrade() -> None:
    op.alter_column("execution_steps", "target_file_id", nullable=True)
    op.drop_constraint(_CONSTRAINT, "execution_steps", type_="foreignkey")
    op.create_foreign_key(
        _CONSTRAINT, "execution_steps", "files", ["target_file_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "execution_steps", type_="foreignkey")
    op.execute("DELETE FROM execution_steps WHERE target_file_id IS NULL")
    op.create_foreign_key(
        _CONSTRAINT, "execution_steps", "files", ["target_file_id"], ["id"], ondelete="CASCADE"
    )
    op.alter_column("execution_steps", "target_file_id", nullable=False)
