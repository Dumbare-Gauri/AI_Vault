"""organization intelligence tables — organization_entities, file_entity_links,
file_lifecycles, organization_recommendations, organization_memories,
organization_analysis_jobs (Phase 2 — Intelligence Engine + Organization)

`RelationshipType.NEAR_DUPLICATE` needs no migration — `file_relationships.
relationship_type` is a plain `String` column backed by a Python `StrEnum`.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organization_entities",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "organization_id",
            "entity_type",
            "normalized_name",
            name="uq_organization_entity_org_type_name",
        ),
    )
    op.create_index(
        "ix_organization_entities_organization_id", "organization_entities", ["organization_id"]
    )

    op.create_table(
        "file_entity_links",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("is_user_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("inference_version", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["organization_entities.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("file_id", "entity_id", name="uq_file_entity_link"),
    )
    op.create_index("ix_file_entity_links_file_id", "file_entity_links", ["file_id"])
    op.create_index("ix_file_entity_links_entity_id", "file_entity_links", ["entity_id"])

    op.create_table(
        "file_lifecycles",
        sa.Column("file_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("signals", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("scorer_version", sa.String(length=20), nullable=False),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "organization_recommendations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("reasoning_summary", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("affected_file_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("current_locations", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("suggested_destination", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("estimated_storage_impact_bytes", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("execution_plan_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["organization_entities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["execution_plan_id"], ["execution_plans.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_organization_recommendations_organization_id",
        "organization_recommendations",
        ["organization_id"],
    )

    op.create_table(
        "organization_memories",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("memory_type", sa.String(length=30), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_organization_memories_organization_id", "organization_memories", ["organization_id"]
    )

    op.create_table(
        "organization_analysis_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("triggered_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clusters_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("entities_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recommendations_generated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(length=2048), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["triggered_by_user_id"], ["users.id"]),
    )
    op.create_index(
        "ix_organization_analysis_jobs_organization_id",
        "organization_analysis_jobs",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_organization_analysis_jobs_organization_id",
        table_name="organization_analysis_jobs",
    )
    op.drop_table("organization_analysis_jobs")

    op.drop_index("ix_organization_memories_organization_id", table_name="organization_memories")
    op.drop_table("organization_memories")

    op.drop_index(
        "ix_organization_recommendations_organization_id",
        table_name="organization_recommendations",
    )
    op.drop_table("organization_recommendations")

    op.drop_table("file_lifecycles")

    op.drop_index("ix_file_entity_links_entity_id", table_name="file_entity_links")
    op.drop_index("ix_file_entity_links_file_id", table_name="file_entity_links")
    op.drop_table("file_entity_links")

    op.drop_index("ix_organization_entities_organization_id", table_name="organization_entities")
    op.drop_table("organization_entities")
