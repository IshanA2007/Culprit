"""pgvector extension + incidents.embedding (M3 Task 5 — similar-incident search)

Additive: creates the `vector` extension and an incidents.embedding column.
No change to the signals schema (plan decision 1). Requires the
pgvector/pgvector:pg17 image (docker-compose + CI, plan decision 13).

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-04 16:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 1024


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column(
        "incidents",
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("incidents", "embedding")
    # Leave the `vector` extension in place — other objects may depend on it.
