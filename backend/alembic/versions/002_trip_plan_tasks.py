"""Add persistent asynchronous trip plan tasks.

Revision ID: 002_trip_plan_tasks
Revises: 001_initial
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "002_trip_plan_tasks"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trip_plan_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_id", sa.String(64), nullable=False, unique=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("request_payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("phase", sa.String(64), nullable=False, server_default="queued"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("message", sa.Text(), nullable=False, server_default="任务已进入队列"),
        sa.Column("phase_timings", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("result_plan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("trip_plans.id", ondelete="SET NULL")),
        sa.Column("result_payload", postgresql.JSONB()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text()),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("phase_started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled','expired')",
            name="ck_trip_plan_tasks_status",
        ),
        sa.CheckConstraint("progress >= 0 AND progress <= 100", name="ck_trip_plan_tasks_progress"),
    )
    op.create_index("idx_trip_plan_tasks_user_id", "trip_plan_tasks", ["user_id"])
    op.create_index("idx_trip_plan_tasks_status", "trip_plan_tasks", ["status"])
    op.create_index("idx_trip_plan_tasks_updated", "trip_plan_tasks", ["updated_at"])


def downgrade() -> None:
    op.drop_table("trip_plan_tasks")
