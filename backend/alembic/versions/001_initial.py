"""Initial phase 1 infrastructure.

Revision ID: 001_initial
Revises:
Create Date: 2026-06-27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE SCHEMA IF NOT EXISTS audit")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("session_token", sa.String(255), nullable=False, unique=True),
        sa.Column("display_name", sa.String(100)),
        sa.Column("avatar_url", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("idx_users_session_token", "users", ["session_token"])

    op.create_table(
        "trip_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("city", sa.String(100), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("days_count", sa.Integer(), nullable=False),
        sa.Column("preferences", sa.Text(), nullable=False, server_default=""),
        sa.Column("budget_level", sa.String(32), nullable=False, server_default="中等"),
        sa.Column("transportation", sa.String(50), nullable=False, server_default="公共交通"),
        sa.Column("accommodation", sa.String(50), nullable=False, server_default="经济型酒店"),
        sa.Column("request_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("plan_json", postgresql.JSONB()),
        sa.Column("overall_suggestions", sa.Text()),
        sa.Column("budget_summary", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('draft','generating','completed','editing','archived')",
            name="ck_trip_plans_status",
        ),
    )
    op.create_index("idx_trip_plans_user_id", "trip_plans", ["user_id"])
    op.create_index("idx_trip_plans_session_id", "trip_plans", ["session_id"])
    op.create_index("idx_trip_plans_status", "trip_plans", ["status"])
    op.create_index("idx_trip_plans_city", "trip_plans", ["city"])
    op.create_index("idx_trip_plans_created", "trip_plans", ["created_at"])

    op.create_table(
        "trip_plan_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "trip_plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trip_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("plan_json", postgresql.JSONB(), nullable=False),
        sa.Column("change_summary", sa.Text()),
        sa.Column("change_type", sa.String(32), nullable=False, server_default="manual_edit"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "change_type IN ('initial_create','manual_edit','agent_regenerate','import')",
            name="ck_trip_plan_versions_change_type",
        ),
        sa.UniqueConstraint("trip_plan_id", "version", name="uq_trip_plan_versions_plan_version"),
    )
    op.create_index("idx_versions_plan_id", "trip_plan_versions", ["trip_plan_id"])

    op.create_table(
        "conversation_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_calls_json", postgresql.JSONB()),
        sa.Column("tool_name", sa.String(64)),
        sa.Column("metadata_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("role IN ('user','assistant','system','tool')", name="ck_conversation_messages_role"),
    )
    op.create_index("idx_conv_session_id", "conversation_messages", ["session_id"])
    op.create_index("idx_conv_created_at", "conversation_messages", ["session_id", "created_at"])

    op.create_table(
        "user_preferences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("preferred_categories", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]")),
        sa.Column("budget_profile", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("travel_style", sa.String(50)),
        sa.Column("dietary_restrictions", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]")),
        sa.Column("avg_trip_days", sa.Numeric(4, 1)),
        sa.Column("favorite_cities", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]")),
        sa.Column("crowd_avoidance", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("walking_tolerance", sa.String(20), server_default="中等"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "saved_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_type", sa.String(32), nullable=False),
        sa.Column("item_ref_id", postgresql.UUID(as_uuid=True)),
        sa.Column("item_data", postgresql.JSONB(), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]")),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("item_type IN ('attraction','hotel','restaurant','trip_plan')", name="ck_saved_items_type"),
    )
    op.create_index("idx_saved_user_type", "saved_items", ["user_id", "item_type"])

    op.create_table(
        "event_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True)),
        sa.Column("session_id", postgresql.UUID(as_uuid=True)),
        sa.Column("severity", sa.String(16), nullable=False, server_default="info"),
        sa.Column("resource_type", sa.String(64)),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True)),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("details_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("ip_address", postgresql.INET()),
        sa.Column("user_agent", sa.Text()),
        sa.Column("duration_ms", sa.Numeric(10, 2)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "severity IN ('debug','info','warning','error','critical')",
            name="ck_audit_event_log_severity",
        ),
        schema="audit",
    )
    op.create_index("idx_audit_event_type", "event_log", ["event_type"], schema="audit")
    op.create_index("idx_audit_event_time", "event_log", ["created_at"], schema="audit")
    op.create_index("idx_audit_session_id", "event_log", ["session_id"], schema="audit")
    op.create_index("idx_audit_resource", "event_log", ["resource_type", "resource_id"], schema="audit")

    op.create_table(
        "token_usage",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trip_plan_id", postgresql.UUID(as_uuid=True)),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_cost_usd", sa.Numeric(10, 6), server_default="0"),
        sa.Column("request_duration_ms", sa.Numeric(10, 2)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        schema="audit",
    )
    op.create_index("idx_token_usage_plan", "token_usage", ["trip_plan_id"], schema="audit")
    op.create_index("idx_token_usage_time", "token_usage", ["created_at"], schema="audit")
    op.create_index("idx_token_usage_model", "token_usage", ["model"], schema="audit")


def downgrade() -> None:
    op.drop_table("token_usage", schema="audit")
    op.drop_table("event_log", schema="audit")
    op.execute("DROP SCHEMA IF EXISTS audit CASCADE")
    op.drop_table("saved_items")
    op.drop_table("user_preferences")
    op.drop_table("conversation_messages")
    op.drop_table("trip_plan_versions")
    op.drop_table("trip_plans")
    op.drop_table("users")
