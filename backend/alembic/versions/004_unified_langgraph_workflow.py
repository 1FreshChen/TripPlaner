"""Make LangGraph the only task orchestration workflow.

Revision ID: 004_unified_langgraph_workflow
Revises: 003_langgraph_state_machine
Create Date: 2026-08-24
"""

from alembic import op
import sqlalchemy as sa


revision = "004_unified_langgraph_workflow"
down_revision = "003_langgraph_state_machine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A legacy run has no compatible checkpoint and cannot be resumed safely.
    op.execute(
        sa.text(
            """
            UPDATE trip_plan_tasks
            SET status = 'failed',
                phase = 'failed',
                message = '编排引擎已升级，请重新提交任务',
                error_code = 'WORKFLOW_UPGRADED',
                error_message = '运行中的 legacy 任务没有 LangGraph checkpoint，无法安全恢复',
                finished_at = now(),
                updated_at = now(),
                lease_owner = NULL,
                recovery_state = 'none'
            WHERE orchestration_backend = 'legacy' AND status = 'running'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE trip_plan_tasks
            SET workflow_version = 'trip_planning_v2',
                state_schema_version = 2
            WHERE status = 'queued'
            """
        )
    )
    op.drop_index("idx_trip_plan_tasks_recovery", table_name="trip_plan_tasks")
    op.drop_constraint("ck_trip_plan_tasks_backend", "trip_plan_tasks", type_="check")
    op.drop_column("trip_plan_tasks", "orchestration_backend")
    op.create_index(
        "idx_trip_plan_tasks_recovery",
        "trip_plan_tasks",
        ["status", "heartbeat_at"],
    )
    op.alter_column(
        "trip_plan_tasks",
        "workflow_version",
        server_default="trip_planning_v2",
    )
    op.alter_column(
        "trip_plan_tasks",
        "state_schema_version",
        server_default="2",
    )


def downgrade() -> None:
    op.alter_column("trip_plan_tasks", "state_schema_version", server_default="1")
    op.alter_column("trip_plan_tasks", "workflow_version", server_default="legacy_v1")
    op.drop_index("idx_trip_plan_tasks_recovery", table_name="trip_plan_tasks")
    op.add_column(
        "trip_plan_tasks",
        sa.Column(
            "orchestration_backend",
            sa.String(16),
            nullable=False,
            server_default="langgraph",
        ),
    )
    op.create_check_constraint(
        "ck_trip_plan_tasks_backend",
        "trip_plan_tasks",
        "orchestration_backend IN ('legacy','langgraph')",
    )
    op.create_index(
        "idx_trip_plan_tasks_recovery",
        "trip_plan_tasks",
        ["status", "orchestration_backend", "heartbeat_at"],
    )
