"""Add durable LangGraph task metadata and failed plan status.

Revision ID: 003_langgraph_state_machine
Revises: 002_trip_plan_tasks
Create Date: 2026-08-17
"""

from alembic import op
import sqlalchemy as sa


revision = "003_langgraph_state_machine"
down_revision = "002_trip_plan_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trip_plan_tasks", sa.Column("heartbeat_at", sa.DateTime(timezone=True)))
    op.add_column("trip_plan_tasks", sa.Column("lease_owner", sa.String(128)))
    op.add_column(
        "trip_plan_tasks",
        sa.Column("orchestration_backend", sa.String(16), nullable=False, server_default="legacy"),
    )
    op.add_column(
        "trip_plan_tasks",
        sa.Column("workflow_version", sa.String(64), nullable=False, server_default="legacy_v1"),
    )
    op.add_column(
        "trip_plan_tasks",
        sa.Column("state_schema_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "trip_plan_tasks",
        sa.Column("recovery_state", sa.String(16), nullable=False, server_default="none"),
    )
    op.add_column("trip_plan_tasks", sa.Column("recovery_enqueued_at", sa.DateTime(timezone=True)))
    op.add_column("trip_plan_tasks", sa.Column("checkpoint_deleted_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_trip_plan_tasks_backend",
        "trip_plan_tasks",
        "orchestration_backend IN ('legacy','langgraph')",
    )
    op.create_check_constraint(
        "ck_trip_plan_tasks_recovery_state",
        "trip_plan_tasks",
        "recovery_state IN ('none','pending','queued')",
    )
    op.create_index(
        "idx_trip_plan_tasks_recovery",
        "trip_plan_tasks",
        ["status", "orchestration_backend", "heartbeat_at"],
    )

    op.drop_constraint("ck_trip_plans_status", "trip_plans", type_="check")
    op.create_check_constraint(
        "ck_trip_plans_status",
        "trip_plans",
        "status IN ('draft','generating','completed','editing','archived','failed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_trip_plans_status", "trip_plans", type_="check")
    op.create_check_constraint(
        "ck_trip_plans_status",
        "trip_plans",
        "status IN ('draft','generating','completed','editing','archived')",
    )

    op.drop_index("idx_trip_plan_tasks_recovery", table_name="trip_plan_tasks")
    op.drop_constraint("ck_trip_plan_tasks_recovery_state", "trip_plan_tasks", type_="check")
    op.drop_constraint("ck_trip_plan_tasks_backend", "trip_plan_tasks", type_="check")
    op.drop_column("trip_plan_tasks", "checkpoint_deleted_at")
    op.drop_column("trip_plan_tasks", "recovery_enqueued_at")
    op.drop_column("trip_plan_tasks", "recovery_state")
    op.drop_column("trip_plan_tasks", "state_schema_version")
    op.drop_column("trip_plan_tasks", "workflow_version")
    op.drop_column("trip_plan_tasks", "orchestration_backend")
    op.drop_column("trip_plan_tasks", "lease_owner")
    op.drop_column("trip_plan_tasks", "heartbeat_at")
