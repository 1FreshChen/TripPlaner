import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("idx_users_session_token", "session_token"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    session_token: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    trip_plans: Mapped[list["TripPlan"]] = relationship(back_populates="user")
    preferences: Mapped["UserPreference | None"] = relationship(back_populates="user", uselist=False)
    saved_items: Mapped[list["SavedItem"]] = relationship(back_populates="user")


class TripPlanTask(Base):
    __tablename__ = "trip_plan_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled','expired')",
            name="ck_trip_plan_tasks_status",
        ),
        CheckConstraint("progress >= 0 AND progress <= 100", name="ck_trip_plan_tasks_progress"),
        CheckConstraint(
            "recovery_state IN ('none','pending','queued')",
            name="ck_trip_plan_tasks_recovery_state",
        ),
        Index("idx_trip_plan_tasks_user_id", "user_id"),
        Index("idx_trip_plan_tasks_status", "status"),
        Index("idx_trip_plan_tasks_updated", "updated_at"),
        Index(
            "idx_trip_plan_tasks_recovery",
            "status",
            "heartbeat_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", server_default=text("'queued'"))
    phase: Mapped[str] = mapped_column(String(64), nullable=False, default="queued", server_default=text("'queued'"))
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    message: Mapped[str] = mapped_column(Text, nullable=False, default="任务已进入队列", server_default=text("'任务已进入队列'"))
    phase_timings: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    result_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trip_plans.id", ondelete="SET NULL"),
    )
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    workflow_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="trip_planning_v2",
        server_default=text("'trip_planning_v2'"),
    )
    state_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default=text("2"))
    recovery_state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="none",
        server_default=text("'none'"),
    )
    recovery_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phase_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TripPlan(Base):
    __tablename__ = "trip_plans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','generating','completed','editing','archived','failed')",
            name="ck_trip_plans_status",
        ),
        Index("idx_trip_plans_user_id", "user_id"),
        Index("idx_trip_plans_session_id", "session_id"),
        Index("idx_trip_plans_status", "status"),
        Index("idx_trip_plans_city", "city"),
        Index("idx_trip_plans_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", server_default=text("'draft'"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    days_count: Mapped[int] = mapped_column(Integer, nullable=False)
    preferences: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    budget_level: Mapped[str] = mapped_column(String(32), nullable=False, default="中等", server_default=text("'中等'"))
    transportation: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="公共交通",
        server_default=text("'公共交通'"),
    )
    accommodation: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="经济型酒店",
        server_default=text("'经济型酒店'"),
    )
    request_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    plan_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    overall_suggestions: Mapped[str | None] = mapped_column(Text)
    budget_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    user: Mapped[User | None] = relationship(back_populates="trip_plans")
    versions: Mapped[list["TripPlanVersion"]] = relationship(
        back_populates="trip_plan",
        cascade="all, delete-orphan",
        order_by="TripPlanVersion.version",
    )


class TripPlanVersion(Base):
    __tablename__ = "trip_plan_versions"
    __table_args__ = (
        CheckConstraint(
            "change_type IN ('initial_create','manual_edit','agent_regenerate','import')",
            name="ck_trip_plan_versions_change_type",
        ),
        UniqueConstraint("trip_plan_id", "version", name="uq_trip_plan_versions_plan_version"),
        Index("idx_versions_plan_id", "trip_plan_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    trip_plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trip_plans.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text)
    change_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="manual_edit",
        server_default=text("'manual_edit'"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    trip_plan: Mapped[TripPlan] = relationship(back_populates="versions")


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user','assistant','system','tool')", name="ck_conversation_messages_role"),
        Index("idx_conv_session_id", "session_id"),
        Index("idx_conv_created_at", "session_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_calls_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tool_name: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class UserPreference(Base):
    __tablename__ = "user_preferences"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    preferred_categories: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default=text("'{}'::text[]"))
    budget_profile: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    travel_style: Mapped[str | None] = mapped_column(String(50))
    dietary_restrictions: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default=text("'{}'::text[]"))
    avg_trip_days: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    favorite_cities: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default=text("'{}'::text[]"))
    crowd_avoidance: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    walking_tolerance: Mapped[str] = mapped_column(String(20), default="中等", server_default=text("'中等'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    user: Mapped[User] = relationship(back_populates="preferences")


class SavedItem(Base):
    __tablename__ = "saved_items"
    __table_args__ = (
        CheckConstraint("item_type IN ('attraction','hotel','restaurant','trip_plan')", name="ck_saved_items_type"),
        Index("idx_saved_user_type", "user_id", "item_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    item_type: Mapped[str] = mapped_column(String(32), nullable=False)
    item_ref_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    item_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default=text("'{}'::text[]"))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    user: Mapped[User] = relationship(back_populates="saved_items")


class AuditEvent(Base):
    __tablename__ = "event_log"
    __table_args__ = (
        CheckConstraint("severity IN ('debug','info','warning','error','critical')", name="ck_audit_event_log_severity"),
        Index("idx_audit_event_type", "event_type"),
        Index("idx_audit_event_time", "created_at"),
        Index("idx_audit_session_id", "session_id"),
        Index("idx_audit_resource", "resource_type", "resource_id"),
        {"schema": "audit"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        unique=True,
        server_default=func.gen_random_uuid(),
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info", server_default=text("'info'"))
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TokenUsage(Base):
    __tablename__ = "token_usage"
    __table_args__ = (
        Index("idx_token_usage_plan", "trip_plan_id"),
        Index("idx_token_usage_time", "created_at"),
        Index("idx_token_usage_model", "model"),
        {"schema": "audit"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trip_plan_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=0, server_default=text("0"))
    request_duration_ms: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
