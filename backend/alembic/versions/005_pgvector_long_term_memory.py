"""Add pgvector-backed long-term semantic memory.

Revision ID: 005_pgvector_long_term_memory
Revises: 004_unified_langgraph_workflow
Create Date: 2026-09-10
"""

from alembic import op
from pgvector.sqlalchemy import VECTOR
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "005_pgvector_long_term_memory"
down_revision = "004_unified_langgraph_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "memory_entries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_key", sa.String(160), nullable=False),
        sa.Column("memory_type", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # Keep this migration immutable; application code uses the same fixed 1536 dimensions.
        sa.Column("embedding", VECTOR(1536), nullable=False),
        sa.Column("embedding_model", sa.String(100), nullable=False),
        sa.Column("access_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "source_key", name="uq_memory_entries_user_source"),
    )
    op.create_index(
        "idx_memory_entries_user_type",
        "memory_entries",
        ["user_id", "memory_type"],
    )
    op.create_index(
        "idx_memory_entries_user_created",
        "memory_entries",
        ["user_id", "created_at"],
    )
    op.create_index(
        "idx_memory_entries_embedding_hnsw",
        "memory_entries",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_table("memory_entries")
