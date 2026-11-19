"""storage workspace scope — every record that describes files (a
recommendation, an organization suggestion, a duplicate group, an insight, a
dashboard snapshot) now names the one connected storage it was computed
from, and every conversation names the one storage it works in. Google Drive
and local storage are separate workspaces: nothing mixes them.

Rows from before this have no storage and may mix both, so they are retired
(resolved / stale / removed) rather than shown; the next analysis run
recomputes them per storage. Conversations keep their history but, with no
workspace, can no longer act.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = {
    "recommendations": "CASCADE",
    "organization_recommendations": "CASCADE",
    "duplicate_groups": "CASCADE",
    "insight_records": "CASCADE",
    "dashboard_snapshots": "CASCADE",
    "conversations": "SET NULL",
}


def upgrade() -> None:
    for table, ondelete in _TABLES.items():
        op.add_column(table, sa.Column("connector_id", UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(
            f"{table}_connector_id_fkey",
            table,
            "storage_connectors",
            ["connector_id"],
            ["id"],
            ondelete=ondelete,
        )
        op.create_index(f"ix_{table}_connector_id", table, ["connector_id"])

    op.drop_constraint("uq_recommendation_org_rule", "recommendations", type_="unique")
    op.create_unique_constraint(
        "uq_recommendation_org_rule",
        "recommendations",
        ["organization_id", "connector_id", "rule_name"],
    )
    op.drop_constraint("uq_duplicate_group_org_checksum", "duplicate_groups", type_="unique")
    op.create_unique_constraint(
        "uq_duplicate_group_org_checksum",
        "duplicate_groups",
        ["organization_id", "connector_id", "checksum"],
    )

    op.execute(
        "UPDATE recommendations SET status = 'resolved', resolved_at = now() "
        "WHERE connector_id IS NULL AND status = 'active'"
    )
    op.execute(
        "UPDATE organization_recommendations SET status = 'stale' "
        "WHERE connector_id IS NULL AND status = 'active'"
    )
    op.execute("DELETE FROM duplicate_groups WHERE connector_id IS NULL")


def downgrade() -> None:
    op.drop_constraint("uq_duplicate_group_org_checksum", "duplicate_groups", type_="unique")
    op.execute("DELETE FROM duplicate_groups WHERE connector_id IS NOT NULL")
    op.create_unique_constraint(
        "uq_duplicate_group_org_checksum", "duplicate_groups", ["organization_id", "checksum"]
    )
    op.drop_constraint("uq_recommendation_org_rule", "recommendations", type_="unique")
    op.execute("DELETE FROM recommendations WHERE connector_id IS NOT NULL")
    op.create_unique_constraint(
        "uq_recommendation_org_rule", "recommendations", ["organization_id", "rule_name"]
    )
    for table in _TABLES:
        op.drop_index(f"ix_{table}_connector_id", table_name=table)
        op.drop_constraint(f"{table}_connector_id_fkey", table, type_="foreignkey")
        op.drop_column(table, "connector_id")
