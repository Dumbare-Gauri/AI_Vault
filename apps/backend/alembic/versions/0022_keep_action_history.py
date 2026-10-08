"""keep action history — an execution plan is the record of a real change in
connected storage, so refreshing the recommendation or duplicate group it came
from clears the link instead of deleting the plan (and its steps, results and
rollback records with it).

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LINKS = (
    ("execution_plans_recommendation_id_fkey", "recommendation_id", "recommendations"),
    ("fk_execution_plans_duplicate_group_id", "duplicate_group_id", "duplicate_groups"),
)


def _relink(ondelete: str) -> None:
    for name, column, target in _LINKS:
        op.drop_constraint(name, "execution_plans", type_="foreignkey")
        op.create_foreign_key(name, "execution_plans", target, [column], ["id"], ondelete=ondelete)


def upgrade() -> None:
    _relink("SET NULL")


def downgrade() -> None:
    _relink("CASCADE")
