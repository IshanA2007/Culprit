"""add incident resolution fields + postmortems table (M4 Task 1)

Additive only — no change to the signals schema (plan decision 1). Three
resolution columns on ``incidents`` (resolved_at, fixing_sha, resolution_source)
and a new ``postmortems`` sibling table (one row per incident — idempotent PR).

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-04 18:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "incidents",
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "incidents",
        sa.Column("fixing_sha", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "incidents",
        sa.Column("resolution_source", sa.String(length=16), nullable=True),
    )
    op.create_table(
        "postmortems",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("path", sa.String(length=255), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("pr_url", sa.String(length=255), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("incident_id"),
    )
    op.create_index(
        op.f("ix_postmortems_incident_id"),
        "postmortems",
        ["incident_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_postmortems_incident_id"), table_name="postmortems")
    op.drop_table("postmortems")
    op.drop_column("incidents", "resolution_source")
    op.drop_column("incidents", "fixing_sha")
    op.drop_column("incidents", "resolved_at")
