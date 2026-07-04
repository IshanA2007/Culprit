"""add incidents.diagnosis jsonb (M3 Task 4 — ranked hypotheses / M4 input)

Additive column only; no change to the signals schema (plan decision 1).

Revision ID: a1b2c3d4e5f6
Revises: e989c92f40f8
Create Date: 2026-07-04 16:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "e989c92f40f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "incidents",
        sa.Column(
            "diagnosis",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("incidents", "diagnosis")
