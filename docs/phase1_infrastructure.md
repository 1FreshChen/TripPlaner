# Phase 1: 基础设施 — PostgreSQL + SQLAlchemy

> **所属项目**: 智能旅行助手 Harness 架构升级
> **依赖**: 无（最先实施）
> **被依赖**: Phase 2、3、4、6 均需 Phase 1 的数据库和配置

---

## 目标

建立项目的持久化基础设施：PostgreSQL 数据库、SQLAlchemy 异步 ORM、Alembic 迁移系统，为后续所有 Phase 提供数据层支撑。

---

## 新增依赖

```
# requirements.txt 新增
sqlalchemy[asyncio]>=2.0.30
asyncpg>=0.29.0
alembic>=1.13.0
slowapi>=0.1.9
bleach>=6.1.0
cryptography>=42.0.0
tenacity>=8.3.0
redis[hiredis]>=5.0.0
structlog>=24.1.0

# package.json 新增（全局前端依赖，Phase 1 提前声明）
pinia
pinia-plugin-persistedstate
@vueuse/core
```

---

## 数据库核心表概览

**public schema — 业务数据：**
- `users` — 用户标识（session-based，无需登录）
- `trip_plans` — 行程计划（含状态机：draft→generating→completed→editing→archived，版本号）
- `trip_plan_versions` — 版本历史（append-only）
- `conversation_messages` — 多轮对话消息
- `user_preferences` — 长期偏好（类别、预算、旅行风格）
- `saved_items` — 收藏（景点/酒店/餐厅/计划）

**audit schema — 审计数据：**
- `audit.event_log` — 结构化审计事件
- `audit.token_usage` — LLM Token 用量与成本

---

## 1.1 表结构 DDL

```sql
-- ============================================================
-- public schema: 业务数据
-- ============================================================

-- 1. 用户表（session-based，无需登录）
CREATE TABLE public.users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_token VARCHAR(255) NOT NULL UNIQUE,
    display_name  VARCHAR(100),
    avatar_url    TEXT,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_users_session_token ON public.users(session_token);

-- 2. 行程计划表（核心业务表）
CREATE TABLE public.trip_plans (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES public.users(id) ON DELETE SET NULL,
    session_id      UUID NOT NULL,
    status          VARCHAR(32) NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','generating','completed','editing','archived')),
    version         INT NOT NULL DEFAULT 1,
    city            VARCHAR(100) NOT NULL,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    days_count      INT NOT NULL,
    preferences     TEXT NOT NULL DEFAULT '',
    budget_level    VARCHAR(32) NOT NULL DEFAULT '中等',
    transportation  VARCHAR(50) NOT NULL DEFAULT '公共交通',
    accommodation   VARCHAR(50) NOT NULL DEFAULT '经济型酒店',
    request_json    JSONB NOT NULL DEFAULT '{}',
    plan_json       JSONB,
    overall_suggestions TEXT,
    budget_summary  JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_trip_plans_user_id    ON public.trip_plans(user_id);
CREATE INDEX idx_trip_plans_session_id ON public.trip_plans(session_id);
CREATE INDEX idx_trip_plans_status     ON public.trip_plans(status);
CREATE INDEX idx_trip_plans_city       ON public.trip_plans(city);
CREATE INDEX idx_trip_plans_created    ON public.trip_plans(created_at DESC);

-- 3. 版本历史表（append-only，每次编辑保存时生成新版本）
CREATE TABLE public.trip_plan_versions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_plan_id        UUID NOT NULL REFERENCES public.trip_plans(id) ON DELETE CASCADE,
    version             INT NOT NULL,
    plan_json           JSONB NOT NULL,
    change_summary      TEXT,
    change_type         VARCHAR(32) NOT NULL DEFAULT 'manual_edit'
                        CHECK (change_type IN ('initial_create','manual_edit','agent_regenerate','import')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(trip_plan_id, version)
);
CREATE INDEX idx_versions_plan_id ON public.trip_plan_versions(trip_plan_id);

-- 4. 对话消息表（多轮对话）
CREATE TABLE public.conversation_messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID NOT NULL,
    role            VARCHAR(16) NOT NULL CHECK (role IN ('user','assistant','system','tool')),
    content         TEXT NOT NULL,
    tool_calls_json JSONB DEFAULT NULL,
    tool_name       VARCHAR(64),
    metadata_json   JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_conv_session_id  ON public.conversation_messages(session_id);
CREATE INDEX idx_conv_created_at  ON public.conversation_messages(session_id, created_at);

-- 5. 用户偏好表（长期记忆）
CREATE TABLE public.user_preferences (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID NOT NULL UNIQUE REFERENCES public.users(id) ON DELETE CASCADE,
    preferred_categories    TEXT[] DEFAULT '{}',
    budget_profile          JSONB DEFAULT '{}',
    travel_style            VARCHAR(50),
    dietary_restrictions    TEXT[] DEFAULT '{}',
    avg_trip_days           NUMERIC(4,1),
    favorite_cities         TEXT[] DEFAULT '{}',
    crowd_avoidance         BOOLEAN DEFAULT FALSE,
    walking_tolerance       VARCHAR(20) DEFAULT '中等',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 6. 收藏表（景点/酒店/餐厅/计划）
CREATE TABLE public.saved_items (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    item_type   VARCHAR(32) NOT NULL CHECK (item_type IN ('attraction','hotel','restaurant','trip_plan')),
    item_ref_id UUID,
    item_data   JSONB NOT NULL,
    tags        TEXT[] DEFAULT '{}',
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_saved_user_type ON public.saved_items(user_id, item_type);

-- ============================================================
-- audit schema: 审计数据（独立 schema 便于权限隔离）
-- ============================================================
CREATE SCHEMA IF NOT EXISTS audit;

-- 7. 审计事件日志
CREATE TABLE audit.event_log (
    id              BIGSERIAL PRIMARY KEY,
    event_id        UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    event_type      VARCHAR(64) NOT NULL,
    actor_id        UUID,
    session_id      UUID,
    severity        VARCHAR(16) NOT NULL DEFAULT 'info'
                    CHECK (severity IN ('debug','info','warning','error','critical')),
    resource_type   VARCHAR(64),
    resource_id     UUID,
    action          VARCHAR(64) NOT NULL,
    details_json    JSONB DEFAULT '{}',
    ip_address      INET,
    user_agent      TEXT,
    duration_ms     NUMERIC(10,2),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_event_type  ON audit.event_log(event_type);
CREATE INDEX idx_audit_event_time  ON audit.event_log(created_at DESC);
CREATE INDEX idx_audit_session_id  ON audit.event_log(session_id);
CREATE INDEX idx_audit_resource    ON audit.event_log(resource_type, resource_id);

-- 8. Token 用量追踪表
CREATE TABLE audit.token_usage (
    id                BIGSERIAL PRIMARY KEY,
    trip_plan_id      UUID,
    conversation_id   UUID,
    model             VARCHAR(64) NOT NULL,
    provider          VARCHAR(64) NOT NULL,
    prompt_tokens     INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    total_tokens      INT NOT NULL DEFAULT 0,
    estimated_cost_usd NUMERIC(10,6) DEFAULT 0,
    request_duration_ms NUMERIC(10,2),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_token_usage_plan   ON audit.token_usage(trip_plan_id);
CREATE INDEX idx_token_usage_time   ON audit.token_usage(created_at DESC);
CREATE INDEX idx_token_usage_model  ON audit.token_usage(model);
```

---

## 1.2 database.py 设计

```python
# backend/app/database.py

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_session_factory = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            pool_size=10,
            max_overflow=20,
            pool_recycle=3600,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def get_db() -> AsyncSession:
    """FastAPI 依赖注入：提供数据库会话"""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

---

## 1.3 config.py 扩展

```python
# backend/app/config.py（新增字段）

class Settings(BaseSettings):
    # --- 现有字段 ---
    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_base_url: str = Field(default="https://api.openai.com/v1", alias="LLM_BASE_URL")
    llm_model: str = Field(default="gpt-4o-mini", alias="LLM_MODEL")
    amap_api_key: str = Field(default="", alias="AMAP_API_KEY")
    unsplash_access_key: str = Field(default="", alias="UNSPLASH_ACCESS_KEY")
    enable_external_services: bool = Field(default=True, alias="ENABLE_EXTERNAL_SERVICES")
    frontend_origin: str = Field(default="http://localhost:5173", alias="FRONTEND_ORIGIN")

    # --- 新增字段 ---
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/trip_planner",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        alias="REDIS_URL",
    )
    encryption_key: str = Field(
        default="",
        alias="ENCRYPTION_KEY",
        description="Fernet 密钥（base64 编码的 32 字节），用于加密 API Key",
    )
    max_conversation_messages: int = Field(default=20, alias="MAX_CONVERSATION_MESSAGES")
    rate_limit_per_minute: int = Field(default=5, alias="RATE_LIMIT_PER_MINUTE")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
```

---

## 1.4 SQLAlchemy ORM 模型

```python
# backend/app/models/db_models.py

import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Boolean, Date, Text, Float, Numeric, ForeignKey, UniqueConstraint, CheckConstraint, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB, INET, ARRAY
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
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    trip_plans = relationship("TripPlan", back_populates="user")
    preferences = relationship("UserPreference", back_populates="user", uselist=False)
    saved_items = relationship("SavedItem", back_populates="user")


class TripPlan(Base):
    __tablename__ = "trip_plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", server_default="draft")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    start_date: Mapped[datetime] = mapped_column(Date, nullable=False)
    end_date: Mapped[datetime] = mapped_column(Date, nullable=False)
    days_count: Mapped[int] = mapped_column(Integer, nullable=False)
    preferences: Mapped[str] = mapped_column(Text, default="", server_default="")
    budget_level: Mapped[str] = mapped_column(String(32), default="中等", server_default="中等")
    transportation: Mapped[str] = mapped_column(String(50), default="公共交通", server_default="公共交通")
    accommodation: Mapped[str] = mapped_column(String(50), default="经济型酒店", server_default="经济型酒店")
    request_json: Mapped[dict] = mapped_column(JSONB, default={}, server_default="{}")
    plan_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    overall_suggestions: Mapped[str | None] = mapped_column(Text)
    budget_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    user = relationship("User", back_populates="trip_plans")
    versions = relationship("TripPlanVersion", back_populates="trip_plan", order_by="TripPlanVersion.version")


class TripPlanVersion(Base):
    __tablename__ = "trip_plan_versions"
    __table_args__ = (
        UniqueConstraint("trip_plan_id", "version"),
        Index("idx_versions_plan_id", "trip_plan_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    trip_plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("trip_plans.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text)
    change_type: Mapped[str] = mapped_column(String(32), default="manual_edit", server_default="manual_edit")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    trip_plan = relationship("TripPlan", back_populates="versions")


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_calls_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSONB, default={}, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class UserPreference(Base):
    __tablename__ = "user_preferences"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    preferred_categories: Mapped[list] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    budget_profile: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    travel_style: Mapped[str | None] = mapped_column(String(50))
    dietary_restrictions: Mapped[list] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    avg_trip_days: Mapped[float | None] = mapped_column(Numeric(4, 1))
    favorite_cities: Mapped[list] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    crowd_avoidance: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    walking_tolerance: Mapped[str] = mapped_column(String(20), default="中等", server_default="中等")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    user = relationship("User", back_populates="preferences")


class SavedItem(Base):
    __tablename__ = "saved_items"
    __table_args__ = (Index("idx_saved_user_type", "user_id", "item_type"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    item_type: Mapped[str] = mapped_column(String(32), nullable=False)
    item_ref_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    item_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tags: Mapped[list] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    user = relationship("User", back_populates="saved_items")


# audit schema models
class AuditEvent(Base):
    __tablename__ = "event_log"
    __table_args__ = {"schema": "audit"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), server_default=func.gen_random_uuid(), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    severity: Mapped[str] = mapped_column(String(16), default="info", server_default="info")
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    details_json: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[float | None] = mapped_column(Numeric(10, 2))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TokenUsage(Base):
    __tablename__ = "token_usage"
    __table_args__ = {"schema": "audit"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trip_plan_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    estimated_cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), default=0, server_default="0")
    request_duration_ms: Mapped[float | None] = mapped_column(Numeric(10, 2))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
```

---

## 1.5 Alembic 迁移初始版本

```python
# backend/alembic/versions/001_initial.py

revision = '001_initial'
down_revision = None
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB, INET, ARRAY

def upgrade():
    # 1. users
    op.create_table('users',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('session_token', sa.String(255), unique=True, nullable=False),
        sa.Column('display_name', sa.String(100)),
        sa.Column('avatar_url', sa.Text()),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('idx_users_session_token', 'users', ['session_token'])

    # 2. trip_plans
    op.create_table('trip_plans',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('session_id', UUID(as_uuid=True), nullable=False),
        sa.Column('status', sa.String(32), nullable=False, server_default='draft'),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('city', sa.String(100), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=False),
        sa.Column('days_count', sa.Integer(), nullable=False),
        sa.Column('preferences', sa.Text(), server_default=''),
        sa.Column('budget_level', sa.String(32), server_default='中等'),
        sa.Column('transportation', sa.String(50), server_default='公共交通'),
        sa.Column('accommodation', sa.String(50), server_default='经济型酒店'),
        sa.Column('request_json', JSONB(), nullable=False, server_default='{}'),
        sa.Column('plan_json', JSONB()),
        sa.Column('overall_suggestions', sa.Text()),
        sa.Column('budget_summary', JSONB()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    for idx_col in ['user_id', 'session_id', 'status', 'city']:
        op.create_index(f'idx_trip_plans_{idx_col}', 'trip_plans', [idx_col])
    op.create_index('idx_trip_plans_created', 'trip_plans', ['created_at'], postgresql_using='btree')

    # 3. trip_plan_versions
    op.create_table('trip_plan_versions',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('trip_plan_id', UUID(as_uuid=True), sa.ForeignKey('trip_plans.id', ondelete='CASCADE'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('plan_json', JSONB(), nullable=False),
        sa.Column('change_summary', sa.Text()),
        sa.Column('change_type', sa.String(32), nullable=False, server_default='manual_edit'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('trip_plan_id', 'version'),
    )
    op.create_index('idx_versions_plan_id', 'trip_plan_versions', ['trip_plan_id'])

    # 4. conversation_messages
    op.create_table('conversation_messages',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('session_id', UUID(as_uuid=True), nullable=False),
        sa.Column('role', sa.String(16), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('tool_calls_json', JSONB()),
        sa.Column('tool_name', sa.String(64)),
        sa.Column('metadata_json', JSONB(), server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('idx_conv_session_id', 'conversation_messages', ['session_id'])
    op.create_index('idx_conv_created_at', 'conversation_messages', ['session_id', 'created_at'])

    # 5. user_preferences
    op.create_table('user_preferences',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), unique=True, nullable=False),
        sa.Column('preferred_categories', ARRAY(sa.Text()), server_default='{}'),
        sa.Column('budget_profile', JSONB(), server_default='{}'),
        sa.Column('travel_style', sa.String(50)),
        sa.Column('dietary_restrictions', ARRAY(sa.Text()), server_default='{}'),
        sa.Column('avg_trip_days', sa.Numeric(4, 1)),
        sa.Column('favorite_cities', ARRAY(sa.Text()), server_default='{}'),
        sa.Column('crowd_avoidance', sa.Boolean(), server_default='false'),
        sa.Column('walking_tolerance', sa.String(20), server_default='中等'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )

    # 6. saved_items
    op.create_table('saved_items',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('item_type', sa.String(32), nullable=False),
        sa.Column('item_ref_id', UUID(as_uuid=True)),
        sa.Column('item_data', JSONB(), nullable=False),
        sa.Column('tags', ARRAY(sa.Text()), server_default='{}'),
        sa.Column('note', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('idx_saved_user_type', 'saved_items', ['user_id', 'item_type'])

    # 7. audit.event_log
    op.execute('CREATE SCHEMA IF NOT EXISTS audit')
    op.create_table('event_log',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('event_id', UUID(as_uuid=True), nullable=False, server_default=sa.text('gen_random_uuid()'), unique=True),
        sa.Column('event_type', sa.String(64), nullable=False),
        sa.Column('actor_id', UUID(as_uuid=True)),
        sa.Column('session_id', UUID(as_uuid=True)),
        sa.Column('severity', sa.String(16), nullable=False, server_default='info'),
        sa.Column('resource_type', sa.String(64)),
        sa.Column('resource_id', UUID(as_uuid=True)),
        sa.Column('action', sa.String(64), nullable=False),
        sa.Column('details_json', JSONB(), server_default='{}'),
        sa.Column('ip_address', INET()),
        sa.Column('user_agent', sa.Text()),
        sa.Column('duration_ms', sa.Numeric(10, 2)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        schema='audit',
    )
    for idx_col in ['event_type', 'session_id', 'resource_type']:
        op.create_index(f'idx_audit_{idx_col}', 'event_log', [idx_col], schema='audit')
    op.create_index('idx_audit_event_time', 'event_log', ['created_at'], schema='audit', postgresql_using='btree')

    # 8. audit.token_usage
    op.create_table('token_usage',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('trip_plan_id', UUID(as_uuid=True)),
        sa.Column('conversation_id', UUID(as_uuid=True)),
        sa.Column('model', sa.String(64), nullable=False),
        sa.Column('provider', sa.String(64), nullable=False),
        sa.Column('prompt_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('completion_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('estimated_cost_usd', sa.Numeric(10, 6), server_default='0'),
        sa.Column('request_duration_ms', sa.Numeric(10, 2)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        schema='audit',
    )
    op.create_index('idx_token_usage_plan', 'token_usage', ['trip_plan_id'], schema='audit')
    op.create_index('idx_token_usage_time', 'token_usage', ['created_at'], schema='audit', postgresql_using='btree')


def downgrade():
    op.drop_table('token_usage', schema='audit')
    op.drop_table('event_log', schema='audit')
    op.execute('DROP SCHEMA IF EXISTS audit CASCADE')
    op.drop_table('saved_items')
    op.drop_table('user_preferences')
    op.drop_table('conversation_messages')
    op.drop_table('trip_plan_versions')
    op.drop_table('trip_plans')
    op.drop_table('users')
```

---

## 关键文件清单

| 文件 | 操作 |
|---|---|
| `backend/app/database.py` | 新建：AsyncEngine + AsyncSession 工厂 |
| `backend/app/models/db_models.py` | 新建：SQLAlchemy ORM 模型（8 张表） |
| `backend/app/config.py` | 修改：增加 DATABASE_URL, REDIS_URL, ENCRYPTION_KEY |
| `backend/alembic/` | 新建：数据库迁移配置 + 001_initial.py |
| `backend/requirements.txt` | 修改：新增 sqlalchemy, asyncpg, alembic 等依赖 |
| `backend/.env` | 修改：新增 DATABASE_URL, REDIS_URL 等环境变量 |

---

## 验证方式

1. 启动 PostgreSQL：`docker-compose up -d postgres`
2. 执行迁移：`cd backend && alembic upgrade head`
3. 验证表结构：`psql -h localhost -U postgres -d trip_planner -c "\dt public.*" && psql -h localhost -U postgres -d trip_planner -c "\dt audit.*"`
4. 单元测试：`pytest tests/test_database.py -v`（新建，验证 CRUD 操作）
