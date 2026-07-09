# 智能旅行助手 — Harness 架构升级方案

## Context

当前项目是基于 Datawhale HelloAgents 教程的"智能旅行助手"基础版（FastAPI + Vue 3），已实现基本的行程生成流程，但缺少正式的状态管理、编排引擎、工具调用系统、记忆系统和安全治理。本次优化的目标是借鉴 Claude Code harness 框架的设计理念，将这些能力系统性地引入项目，使其架构更完整、更接近生产级水平。

---

## 总体架构变更概览

```
                          现有                          →    升级后
┌─────────────────────────────────────────────────────────────────────┐
│ 状态管理    sessionStorage + ref              →  PostgreSQL + Pinia   │
│ 编排引擎    顺序调用 Agent                      →  AgentOrchestrator   │
│             无并行/重试/回退                      (pipeline/parallel/   │
│                                                 retry/fallback)      │
│ 工具调用    Service 直接调用                    →  ToolRegistry +      │
│                                                  JSON Schema +       │
│                                                  LLM 动态工具选择     │
│ 记忆系统    无                                  →  短期(会话上下文)     │
│                                                  + 长期(用户偏好)      │
│ 安全治理    仅 Pydantic 校验                    →  内容过滤 + 频率限制  │
│                                                  + 审计日志 + 加密    │
│                                                  + Token 成本追踪     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Phase 1: 基础设施 — PostgreSQL + SQLAlchemy

### 新增依赖

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

# package.json 新增
pinia, pinia-plugin-persistedstate, @vueuse/core
```

### 数据库核心表（PostgreSQL）

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

### 关键文件

| 文件 | 操作 |
|---|---|
| `backend/app/database.py` | 新建：AsyncEngine + AsyncSession 工厂 |
| `backend/app/models/db_models.py` | 新建：SQLAlchemy ORM 模型 |
| `backend/app/config.py` | 修改：增加 DATABASE_URL, REDIS_URL, ENCRYPTION_KEY |
| `backend/alembic/` | 新建：数据库迁移 |
| `backend/requirements.txt` | 修改：新增依赖 |

---

## Phase 2: 编排引擎

### 核心设计

```
                    AgentOrchestrator
                           │
              ┌────────────┼────────────┐
              │            │            │
         Registry    FallbackChain   Tracer
    (Agent注册+依赖)  (LLM→确定→Mock)  (执行追踪)


执行计划（拓扑排序）:
  Stage 1 (parallel): AttractionSearch │ WeatherQuery │ HotelAgent
                           │            │            │
                           └────────────┼────────────┘
                                        │
                    Stage 2 (pipeline): PlannerAgent
```

### 核心类

- `BaseAgent` — Agent 抽象基类（name, execute 方法）
- `AgentDefinition` — Agent 元数据（依赖、重试策略、超时、执行模式）
- `AgentRegistry` — 注册中心，支持依赖图拓扑排序
- `AgentOrchestrator` — 编排器，按 stage 执行（stage 内并行，stage 间串行）
- `FallbackChain` — 回退链：LLM → 确定性算法 → Mock 数据
- `RetryPolicy` — 指数退避重试
- `ExecutionTrace` — 每次运行的追踪记录（耗时、状态、回退使用情况）

### 关键文件

| 文件 | 操作 |
|---|---|
| `backend/app/orchestration/base.py` | 新建：核心抽象 |
| `backend/app/orchestration/orchestrator.py` | 新建：编排器 |
| `backend/app/orchestration/registry.py` | 新建：Agent 注册中心 |
| `backend/app/orchestration/execution.py` | 新建：执行引擎 |
| `backend/app/orchestration/retry.py` | 新建：重试策略 |
| `backend/app/orchestration/fallback.py` | 新建：回退链 |
| `backend/app/orchestration/trace.py` | 新建：执行追踪 |
| `backend/app/agents/trip_planner.py` | 重构：Agent 继承 BaseAgent，注册到 Registry |
| `backend/tests/test_orchestrator.py` | 新建：编排器测试 |

---

## Phase 3: 工具调用系统

### 核心设计

```
ToolRegistry (单例)                    ToolExecutor
     │                               (超时/重试/缓存)
     │
     ├── AmapPOISearchTool           parameters_schema (JSON Schema)
     ├── AmapWeatherTool             to_openai_tool_definition()
     ├── HotelSearchTool
     ├── BudgetCalculatorTool
     └── UnsplashImageTool

LLM Agent (PlannerAgent)
     │
     ├── 获取所有工具定义 → 发送给 LLM
     ├── LLM 决定调用哪些工具
     ├── 执行工具并将结果回传
     └── LLM 综合所有结果生成最终行程
```

### 核心类

- `BaseTool` — 工具抽象基类（name, description, parameters_schema, execute）
- `ToolRegistry` — 工具注册中心单例，支持按能力搜索
- `ToolExecutor` — 工具执行中间件（超时、重试、Redis 缓存）
- `ToolCache` — Redis 结果缓存

### 关键文件

| 文件 | 操作 |
|---|---|
| `backend/app/tools/base.py` | 新建：BaseTool 抽象 |
| `backend/app/tools/registry.py` | 新建：工具注册中心 |
| `backend/app/tools/cache.py` | 新建：Redis 缓存 |
| `backend/app/tools/implementations/amap_search.py` | 新建 |
| `backend/app/tools/implementations/amap_weather.py` | 新建 |
| `backend/app/tools/implementations/hotel_search.py` | 新建 |
| `backend/app/tools/implementations/budget_calculator.py` | 新建 |
| `backend/app/tools/implementations/image_search.py` | 新建 |
| `backend/app/services/llm_service.py` | 扩展：支持 tool-calling 模式 |
| `backend/tests/test_tools.py` | 新建 |

---

## Phase 4: 状态管理 + API 扩展

### 后端 API 变更

| 端点 | 说明 |
|---|---|
| `POST /api/sessions` | 创建/恢复会话 |
| `GET /api/sessions/{id}` | 获取会话详情（含计划列表） |
| `POST /api/trip/plan` | 创建行程计划（关联 session，状态写入 DB） |
| `GET /api/trip/plan/{id}` | 获取计划详情 |
| `PUT /api/trip/plan/{id}` | 更新计划（触发版本快照） |
| `POST /api/conversation/{session_id}` | 发送对话消息 |
| `GET /api/conversation/{session_id}` | 获取对话历史 |
| `POST /api/preferences` | 更新用户偏好 |
| `GET /api/preferences` | 获取用户偏好 |

### 前端 Pinia Stores

```
stores/
├── tripPlanStore.ts       # 计划 CRUD、状态机、版本历史、编辑模式
├── sessionStore.ts        # 会话 ID、用户 ID、对话历史
├── uiStore.ts             # 加载状态、编辑模式、当前 section、导出状态
└── preferencesStore.ts    # 偏好缓存、收藏管理
```

### Composables（从 Result.vue 抽取）

```
composables/
├── useTripPlanner.ts      # 提交→轮询/回调→结果 流程
├── useExport.ts           # 导出图片/PDF 逻辑
└── useMap.ts              # 高德地图初始化逻辑
```

### 新增页面

- `History.vue` — 历史计划列表，支持查看/继续编辑/归档
- `Conversation.vue` — 多轮对话式行程调整

### 关键文件

| 文件 | 操作 |
|---|---|
| `backend/app/api/deps.py` | 新建：依赖注入（get_db, get_session, get_orchestrator） |
| `backend/app/api/routes/trip.py` | 重构：完整 CRUD + 状态机 |
| `backend/app/api/routes/conversation.py` | 新建：多轮对话端点 |
| `frontend/src/stores/*.ts` | 新建：4 个 Pinia store |
| `frontend/src/composables/*.ts` | 新建：3 个 composable |
| `frontend/src/views/Home.vue` | 重构：使用 store |
| `frontend/src/views/Result.vue` | 重构：使用 store + composables |
| `frontend/src/views/History.vue` | 新建 |
| `frontend/src/views/Conversation.vue` | 新建 |
| `frontend/src/router/index.ts` | 修改：新增路由 |

---

## Phase 5: 记忆系统

### 分层设计

```
┌──────────────────────────────────────────────┐
│              记忆系统                          │
│                                               │
│  短期记忆 (ShortTermMemory)                    │
│  ├── 存储: 内存 + conversation_messages 表     │
│  ├── 范围: 当前会话                           │
│  ├── 策略: 滑动窗口 (最近 20 条消息)            │
│  └── 用途: 多轮对话上下文                      │
│                                               │
│  长期记忆 (LongTermMemory)                     │
│  ├── 存储: user_preferences + saved_items 表   │
│  ├── 范围: 跨会话                             │
│  ├── 策略: 每次完成计划后增量更新               │
│  └── 用途: 偏好学习、收藏管理                   │
│                                               │
│  记忆召回 (MemoryRecall)                       │
│  ├── 新计划生成时召回相关历史偏好和收藏           │
│  └── 注入到编排器 context                      │
└──────────────────────────────────────────────┘
```

### 关键文件

| 文件 | 操作 |
|---|---|
| `backend/app/memory/short_term.py` | 新建 |
| `backend/app/memory/long_term.py` | 新建 |
| `backend/app/memory/recall.py` | 新建 |
| `backend/app/memory/models.py` | 新建 |
| `backend/tests/test_memory.py` | 新建 |

---

## Phase 6: 安全治理

### 安全层架构

```
Request → [RateLimit] → [AuditMiddleware] → [Pydantic+ContentFilter] → Handler
                                                                           │
Response ← [LLMOutputReview] ←────────────────────────────────────────────┘
                                       │
                                audit.event_log
                                audit.token_usage
```

### 各模块职责

| 模块 | 文件 | 职责 |
|---|---|---|
| 内容过滤 | `services/content_filter.py` | sanitize_text (bleach), detect_injection (正则) |
| 频率限制 | `middlewares/rate_limit.py` | slowapi，5次/分钟/IP，50次/小时/IP |
| 审计日志 | `middlewares/audit.py` | ASGI 中间件，写入 audit.event_log |
| 请求 ID | `middlewares/request_id.py` | 注入 X-Request-ID，全链路追踪 |
| 加密 | `services/encryption.py` | Fernet (AES-128-CBC) 加密 API Key |
| Token 追踪 | `services/llm_service.py` | 记录每次 LLM 调用 Token 数 + 成本估算 |
| 输出审查 | `services/content_filter.py` | LLM 输出敏感内容过滤 |

### 审计事件类型

- `trip_plan_created/updated/archived`
- `agent_execution_started/completed/failed`
- `tool_call_invoked`
- `llm_request`
- `content_filter_triggered`
- `rate_limit_exceeded`

### 关键文件

| 文件 | 操作 |
|---|---|
| `backend/app/api/middlewares/__init__.py` | 新建 |
| `backend/app/api/middlewares/rate_limit.py` | 新建 |
| `backend/app/api/middlewares/audit.py` | 新建 |
| `backend/app/api/middlewares/request_id.py` | 新建 |
| `backend/app/services/content_filter.py` | 新建 |
| `backend/app/services/encryption.py` | 新建 |
| `backend/app/api/main.py` | 修改：注册中间件 |
| `backend/app/models/schemas.py` | 修改：增加 content_filter validators |

---

## 实现优先级总览

```
Phase 1 (基础设施)    ████████░░░░░░░░  必须最先完成（其他一切的基础）
Phase 2 (编排引擎)    ████████████░░░░  依赖 Phase 1
Phase 3 (工具系统)    ████████████████  依赖 Phase 1，可与 Phase 2 并行
Phase 4 (状态管理)    ████████████████  依赖 Phase 1，可与 Phase 2/3 并行
Phase 5 (记忆系统)    ██████████████████ 依赖 Phase 1+4
Phase 6 (安全治理)    ██████████████████ 依赖 Phase 1，横向切入所有层
```

建议按 Phase 顺序实施，其中 Phase 2/3/4 可以并行开发。

---

## Verification

每个 Phase 完成后进行以下验证：

1. **Phase 1**: `docker-compose up postgres` → `alembic upgrade head` → 单元测试验证 CRUD
2. **Phase 2**: `pytest tests/test_orchestrator.py -v` → 验证并行执行（耗时应低于串行）、回退链触发
3. **Phase 3**: `pytest tests/test_tools.py -v` → 验证每个工具的 JSON Schema、LLM tool-calling 往返
4. **Phase 4**: 前端 `npm run dev` → 完整流程：创建计划 → 编辑 → 保存 → 查看历史 → 对话式调整
5. **Phase 5**: 连续创建 3 个计划 → 验证偏好更新 → 第 4 个计划是否包含个性化推荐
6. **Phase 6**: 发送注入 payload → 验证拦截 → 查看 `audit.event_log` 表 → 频率限制触发验证

---

---

# 详细设计补充

> 以下为每个 Phase 的详细设计方案，包括数据库 DDL、核心类接口定义、API 契约、前端 Store 类型等。

---

## Phase 1 详细设计：数据库完整 DDL

### 1.1 表结构 DDL

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
    session_id      UUID NOT NULL,          -- 关联到 conversation session
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
    request_json    JSONB NOT NULL DEFAULT '{}',    -- 原始请求快照
    plan_json       JSONB,                          -- 完整 TripPlan 响应
    overall_suggestions TEXT,
    budget_summary  JSONB,                          -- Budget 对象快照
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
    change_summary      TEXT,          -- 变更摘要（人工可读）
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
    tool_calls_json JSONB DEFAULT NULL,       -- assistant 调用工具时的 tool_calls 数据
    tool_name       VARCHAR(64),              -- 工具名称（当 role='tool' 时）
    metadata_json   JSONB DEFAULT '{}',       -- 额外元数据（如 token 用量等）
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_conv_session_id  ON public.conversation_messages(session_id);
CREATE INDEX idx_conv_created_at  ON public.conversation_messages(session_id, created_at);

-- 5. 用户偏好表（长期记忆）
CREATE TABLE public.user_preferences (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID NOT NULL UNIQUE REFERENCES public.users(id) ON DELETE CASCADE,
    preferred_categories    TEXT[] DEFAULT '{}',         -- {'历史文化','美食','自然风光'}
    budget_profile          JSONB DEFAULT '{}',           -- {"经济": 3, "中等": 7, "舒适": 2}
    travel_style            VARCHAR(50),                  -- '慢节奏' / '打卡式' / '深度游'
    dietary_restrictions    TEXT[] DEFAULT '{}',          -- {'素食','不吃辣'}
    avg_trip_days           NUMERIC(4,1),                 -- 平均旅行天数
    favorite_cities         TEXT[] DEFAULT '{}',          -- 最常去的城市
    crowd_avoidance         BOOLEAN DEFAULT FALSE,        -- 是否避开人流
    walking_tolerance       VARCHAR(20) DEFAULT '中等',   -- 步行容忍度
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 6. 收藏表（景点/酒店/餐厅/计划）
CREATE TABLE public.saved_items (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    item_type   VARCHAR(32) NOT NULL CHECK (item_type IN ('attraction','hotel','restaurant','trip_plan')),
    item_ref_id UUID,                         -- 引用 trip_plans.id（收藏计划时）
    item_data   JSONB NOT NULL,               -- 完整数据快照
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
    actor_id        UUID,                        -- users.id
    session_id      UUID,
    severity        VARCHAR(16) NOT NULL DEFAULT 'info'
                    CHECK (severity IN ('debug','info','warning','error','critical')),
    resource_type   VARCHAR(64),                 -- 'trip_plan','user','session'
    resource_id     UUID,
    action          VARCHAR(64) NOT NULL,         -- 'created','updated','deleted','viewed'
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
    trip_plan_id      UUID,                       -- 关联到 trip_plans.id
    conversation_id   UUID,                       -- 关联到 conversation_messages.id
    model             VARCHAR(64) NOT NULL,        -- 'gpt-4o-mini','deepseek-chat'
    provider          VARCHAR(64) NOT NULL,        -- 'openai','deepseek','custom'
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

### 1.2 database.py 设计

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

### 1.3 config.py 扩展

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

## Phase 2 详细设计：编排引擎完整接口

### 2.1 核心数据类

```python
# backend/app/orchestration/base.py

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
import time


class AgentStatus(Enum):
    PENDING    = "pending"
    RUNNING    = "running"
    COMPLETED  = "completed"
    FAILED     = "failed"
    SKIPPED    = "skipped"


class ExecutionMode(Enum):
    PIPELINE = "pipeline"   # 串行（逐 stage 执行）
    PARALLEL = "parallel"   # 并行（同一 stage 内并发）


class FallbackLevel(Enum):
    LLM           = "llm"
    DETERMINISTIC = "deterministic"
    MOCK          = "mock"


@dataclass
class RetryPolicy:
    """重试策略"""
    max_attempts: int = 3           # 最大重试次数
    base_delay: float = 1.0         # 基础延迟（秒）
    max_delay: float = 30.0         # 最大延迟上限
    backoff_multiplier: float = 2.0 # 指数退避乘数
    retryable_exceptions: tuple = (TimeoutError, ConnectionError)

    def delay_for_attempt(self, attempt: int) -> float:
        """计算第 N 次重试的退避延迟"""
        return min(self.base_delay * (self.backoff_multiplier ** attempt), self.max_delay)


@dataclass
class AgentDefinition:
    """Agent 注册元数据"""
    name: str
    agent_class: type
    description: str = ""
    depends_on: List[str] = field(default_factory=list)      # 依赖的 Agent 名称
    execution_mode: ExecutionMode = ExecutionMode.PIPELINE    # 单个 Agent 的执行模式
    retry_policy: Optional[RetryPolicy] = None
    timeout_seconds: float = 30.0
    fallback_levels: List[FallbackLevel] = field(            # 回退路径
        default_factory=lambda: [FallbackLevel.LLM, FallbackLevel.DETERMINISTIC, FallbackLevel.MOCK]
    )


@dataclass
class AgentResult:
    """单个 Agent 执行结果"""
    agent_name: str
    status: AgentStatus
    output: Any = None
    error_message: Optional[str] = None
    duration_ms: float = 0.0
    retries_used: int = 0
    fallback_used: Optional[FallbackLevel] = None
    started_at: float = 0.0
    finished_at: float = 0.0


@dataclass
class ExecutionTrace:
    """一次编排运行的完整追踪"""
    run_id: str
    plan_id: str
    agent_results: List[AgentResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    overall_status: AgentStatus = AgentStatus.PENDING

    @property
    def total_duration_ms(self) -> float:
        return (self.finished_at - self.started_at) * 1000

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.agent_results if r.status == AgentStatus.COMPLETED)

    @property
    def failure_count(self) -> int:
        return sum(1 for r in self.agent_results if r.status == AgentStatus.FAILED)

    @property
    def any_fallback_used(self) -> bool:
        return any(r.fallback_used is not None for r in self.agent_results)


class BaseAgent(ABC):
    """Agent 抽象基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """唯一标识"""
        ...

    @abstractmethod
    async def execute(self, context: Dict[str, Any]) -> Any:
        """
        执行 Agent 逻辑。
        context 为共享上下文字典，包含：
          - 'request': TripPlanRequest
          - '<agent_name>': 前置 Agent 的输出
          - 'user_preferences': 用户偏好
          - 'tool_registry': ToolRegistry 实例（用于工具调用）
        """
        ...
```

### 2.2 AgentRegistry — 注册中心

```python
# backend/app/orchestration/registry.py

from collections import defaultdict, deque
from typing import Dict, List

class AgentRegistry:
    """Agent 注册中心，管理 Agent 的注册和依赖解析"""

    def __init__(self):
        self._agents: Dict[str, AgentDefinition] = {}

    def register(self, definition: AgentDefinition) -> None:
        if definition.name in self._agents:
            raise ValueError(f"Agent '{definition.name}' 已注册")
        self._agents[definition.name] = definition

    def get(self, name: str) -> AgentDefinition:
        if name not in self._agents:
            raise KeyError(f"Agent '{name}' 未注册")
        return self._agents[name]

    def list_all(self) -> List[AgentDefinition]:
        return list(self._agents.values())

    def resolve_execution_plan(self) -> List[List[str]]:
        """
        拓扑排序，将 Agent 依赖图分组成 stage。
        返回: [[stage1_agents], [stage2_agents], ...]

        示例：
          注册: A(dep=[]), B(dep=[]), C(dep=[]), D(dep=[A,B,C])
          输出: [['A','B','C'], ['D']]
        """
        in_degree = {name: len(def_.depends_on) for name, def_ in self._agents.items()}
        dependents = defaultdict(list)
        for name, def_ in self._agents.items():
            for dep in def_.depends_on:
                dependents[dep].append(name)

        queue = deque([name for name, deg in in_degree.items() if deg == 0])
        stages = []

        while queue:
            stage = list(queue)
            stages.append(stage)
            queue.clear()
            for name in stage:
                for dependent in dependents[name]:
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        queue.append(dependent)

        if len(stages) == 0 or sum(len(s) for s in stages) != len(self._agents):
            # 检测到循环依赖
            remaining = [n for n, d in in_degree.items() if d > 0]
            raise ValueError(f"检测到循环依赖: {remaining}")

        return stages
```

### 2.3 AgentOrchestrator — 编排器

```python
# backend/app/orchestration/orchestrator.py

import asyncio
import uuid
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AgentOrchestrator:
    """多 Agent 编排器，支持 pipeline/parallel 执行"""

    def __init__(
        self,
        registry: AgentRegistry,
        fallback_chain: 'FallbackChain',
        tracer: 'ExecutionTracer',
    ):
        self._registry = registry
        self._fallback_chain = fallback_chain
        self._tracer = tracer

    async def run(
        self,
        plan_id: str,
        context: Dict[str, Any],
    ) -> ExecutionTrace:
        """
        执行完整的 Agent 图。
        1. 解析依赖 → 生成 stage 列表
        2. 按 stage 执行（stage 内 asyncio.gather 并行）
        3. 单 Agent 失败 → 走 FallbackChain
        """
        run_id = str(uuid.uuid4())
        trace = self._tracer.start_run(run_id, plan_id)

        try:
            stages = self._registry.resolve_execution_plan()
        except ValueError as e:
            logger.error("无法解析执行计划: %s", e)
            trace.overall_status = AgentStatus.FAILED
            return trace

        for stage_index, stage in enumerate(stages):
            logger.info("Stage %d: 并行执行 %s", stage_index, stage)

            # Stage 内所有 Agent 并发执行
            tasks = [
                self._execute_with_retry(agent_name, context, trace)
                for agent_name in stage
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for agent_name, result in zip(stage, results):
                if isinstance(result, Exception):
                    logger.error("Agent '%s' 彻底失败: %s", agent_name, result)
                else:
                    # 将成功的输出写入共享上下文
                    context[agent_name] = result.output

        trace.finished_at = time.time()
        trace.overall_status = AgentStatus.COMPLETED if trace.failure_count == 0 else AgentStatus.FAILED
        return trace

    async def _execute_with_retry(
        self,
        agent_name: str,
        context: Dict[str, Any],
        trace: ExecutionTrace,
    ) -> AgentResult:
        """带重试和回退的单 Agent 执行"""
        agent_def = self._registry.get(agent_name)
        agent = agent_def.agent_class()
        retry_policy = agent_def.retry_policy or RetryPolicy()

        result = AgentResult(
            agent_name=agent_name,
            status=AgentStatus.RUNNING,
            started_at=time.time(),
        )

        last_error = None
        for attempt in range(retry_policy.max_attempts):
            try:
                output = await asyncio.wait_for(
                    agent.execute(context),
                    timeout=agent_def.timeout_seconds,
                )
                result.status = AgentStatus.COMPLETED
                result.output = output
                result.retries_used = attempt
                result.finished_at = time.time()
                result.duration_ms = (result.finished_at - result.started_at) * 1000
                trace.agent_results.append(result)
                return result

            except Exception as e:
                last_error = e
                logger.warning(
                    "Agent '%s' 第 %d/%d 次尝试失败: %s",
                    agent_name, attempt + 1, retry_policy.max_attempts, e,
                )
                if attempt + 1 < retry_policy.max_attempts:
                    delay = retry_policy.delay_for_attempt(attempt)
                    await asyncio.sleep(delay)

        # 所有重试耗尽 → 走 FallbackChain
        logger.warning("Agent '%s' 所有重试耗尽，进入回退链", agent_name)
        fallback_output = await self._fallback_chain.execute(
            agent_name=agent_name,
            context=context,
            last_error=last_error,
        )
        if fallback_output is not None:
            result.status = AgentStatus.COMPLETED
            result.output = fallback_output
            result.fallback_used = self._fallback_chain.last_level_used
        else:
            result.status = AgentStatus.FAILED
            result.error_message = str(last_error)
            result.fallback_used = None

        result.finished_at = time.time()
        result.duration_ms = (result.finished_at - result.started_at) * 1000
        trace.agent_results.append(result)
        return result
```

### 2.4 FallbackChain — 回退链

```python
# backend/app/orchestration/fallback.py

from typing import Any, Callable, Dict, List, Optional


class FallbackChain:
    """
    回退链：按优先级依次尝试不同等级的处理方式。
    默认路径：LLM → 确定性算法 → Mock 数据
    """

    def __init__(self):
        self._handlers: Dict[str, List[Callable]] = {}  # agent_name → [handler, ...]
        self.last_level_used: Optional[FallbackLevel] = None

    def register_handler(
        self, agent_name: str, level: FallbackLevel, handler: Callable
    ) -> None:
        """为指定 Agent 注册某等级的回退处理器"""
        if agent_name not in self._handlers:
            self._handlers[agent_name] = []
        self._handlers[agent_name].append((level, handler))

    async def execute(
        self,
        agent_name: str,
        context: Dict[str, Any],
        last_error: Optional[Exception] = None,
    ) -> Any:
        """依次尝试回退处理器，返回第一个成功的结果"""
        handlers = self._handlers.get(agent_name, [])
        # 按 FallbackLevel 排序：LLM → DETERMINISTIC → MOCK
        handlers.sort(key=lambda x: (
            [FallbackLevel.LLM, FallbackLevel.DETERMINISTIC, FallbackLevel.MOCK].index(x[0])
        ))

        for level, handler in handlers:
            try:
                result = await handler(context)
                if result is not None:
                    self.last_level_used = level
                    return result
            except Exception:
                continue

        return None
```

### 2.5 Agent 注册示例

```python
# 在应用启动时注册（backend/app/orchestration/__init__.py 或独立 bootstrap.py）

from app.orchestration import AgentRegistry, AgentDefinition, RetryPolicy, FallbackChain, FallbackLevel
from app.agents.trip_planner import AttractionSearchAgent, WeatherQueryAgent, HotelAgent, PlannerAgent

registry = AgentRegistry()

registry.register(AgentDefinition(
    name="attraction_search",
    agent_class=AttractionSearchAgent,
    description="搜索目的地城市的景点信息",
    depends_on=[],
    retry_policy=RetryPolicy(max_attempts=3, base_delay=1.0),
    timeout_seconds=15.0,
))

registry.register(AgentDefinition(
    name="weather_query",
    agent_class=WeatherQueryAgent,
    description="查询目的地城市的天气预报",
    depends_on=[],
    retry_policy=RetryPolicy(max_attempts=2, base_delay=1.0),
    timeout_seconds=10.0,
))

registry.register(AgentDefinition(
    name="hotel_recommendation",
    agent_class=HotelAgent,
    description="推荐目的地城市的酒店",
    depends_on=[],
    retry_policy=RetryPolicy(max_attempts=3, base_delay=1.0),
    timeout_seconds=15.0,
))

registry.register(AgentDefinition(
    name="trip_planner",
    agent_class=PlannerAgent,
    description="综合所有信息生成完整旅行计划",
    depends_on=["attraction_search", "weather_query", "hotel_recommendation"],
    retry_policy=RetryPolicy(max_attempts=2, base_delay=2.0),
    timeout_seconds=60.0,
))
```

---

## Phase 3 详细设计：工具调用系统

### 3.1 BaseTool 接口

```python
# backend/app/tools/base.py

from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseTool(ABC):
    """
    统一工具接口。
    每个工具必须定义：
      - name:          全局唯一标识
      - description:   自然语言描述（给 LLM 看的）
      - parameters:    参数 JSON Schema（OpenAI function-calling 兼容）
      - execute(**kwargs): 异步执行方法
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def parameters(self) -> Dict[str, Any]:
        """
        JSON Schema 格式的参数定义。
        示例：
        {
            "type": "object",
            "properties": {
                "keywords": {"type": "string", "description": "搜索关键词"},
                "city":     {"type": "string", "description": "城市名称"},
            },
            "required": ["keywords", "city"],
        }
        """
        ...

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Dict[str, Any]:
        """执行工具，返回结构化结果字典"""
        ...

    def to_openai_function(self) -> Dict[str, Any]:
        """导出为 OpenAI function-calling 格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_dict(self) -> Dict[str, Any]:
        """通用序列化（供前端展示可用工具列表）"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
```

### 3.2 ToolRegistry

```python
# backend/app/tools/registry.py

from typing import Dict, List, Optional


class ToolRegistry:
    """工具注册中心（单例模式）"""

    _instance: Optional['ToolRegistry'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tools: Dict[str, BaseTool] = {}
        return cls._instance

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具 '{tool.name}' 已注册")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def list_all(self) -> List[BaseTool]:
        return list(self._tools.values())

    def get_openai_functions(self) -> List[Dict[str, Any]]:
        """获取所有工具定义，供 LLM tool-calling 使用"""
        return [t.to_openai_function() for t in self._tools.values()]

    def find_by_keyword(self, keyword: str) -> List[BaseTool]:
        """关键词搜索工具（可用于前端展示或 LLM 路由）"""
        kw = keyword.lower()
        return [
            t for t in self._tools.values()
            if kw in t.name.lower() or kw in t.description.lower()
        ]
```

### 3.3 各工具的参数 Schema

```python
# --- AmapPOISearchTool ---
name: "amap_poi_search"
description: "搜索指定城市的 POI（景点/餐厅/商场等），返回名称、地址、坐标、评分等信息。"
parameters: {
    "type": "object",
    "properties": {
        "keywords": {
            "type": "string",
            "description": "搜索关键词，如 '博物馆'、'公园'、'美食街'。多个关键词用 | 分隔",
        },
        "city": {
            "type": "string",
            "description": "城市名称，如 '北京'、'上海'",
        },
        "offset": {
            "type": "integer",
            "description": "返回结果数量上限，默认 10，最大 25",
            "default": 10,
            "minimum": 1,
            "maximum": 25,
        },
    },
    "required": ["keywords", "city"],
}

# --- AmapWeatherTool ---
name: "amap_weather"
description: "查询指定城市未来数天的天气预报，包含白天/夜间天气、温度、风力风向。"
parameters: {
    "type": "object",
    "properties": {
        "city": {
            "type": "string",
            "description": "城市名称，如 '杭州'",
        },
    },
    "required": ["city"],
}

# --- HotelSearchTool ---
name: "hotel_search"
description: "搜索指定城市的酒店住宿信息，可按类型过滤。"
parameters: {
    "type": "object",
    "properties": {
        "city": {
            "type": "string",
            "description": "城市名称",
        },
        "hotel_type": {
            "type": "string",
            "description": "酒店类型，如 '经济型酒店'、'精品酒店'、'豪华酒店'",
            "default": "经济型酒店",
        },
        "limit": {
            "type": "integer",
            "description": "返回酒店数量",
            "default": 5,
            "minimum": 1,
            "maximum": 10,
        },
    },
    "required": ["city"],
}

# --- BudgetCalculatorTool ---
name: "budget_calculator"
description: "根据每日行程、交通方式和住宿天数计算旅行预算。"
parameters: {
    "type": "object",
    "properties": {
        "days": {
            "type": "integer",
            "description": "旅行天数",
            "minimum": 1,
        },
        "transportation": {
            "type": "string",
            "description": "交通方式：步行|公共交通|地铁|打车|自驾",
            "enum": ["步行", "公共交通", "地铁", "打车", "自驾"],
        },
        "attraction_count": {
            "type": "integer",
            "description": "景点总数",
        },
        "avg_ticket_price": {
            "type": "integer",
            "description": "平均门票价格（元）",
        },
        "hotel_price_per_night": {
            "type": "integer",
            "description": "每晚酒店费用（元）",
        },
        "meal_level": {
            "type": "string",
            "description": "餐饮档次：经济|中等|舒适|高",
            "enum": ["经济", "中等", "舒适", "高"],
        },
    },
    "required": ["days", "transportation", "attraction_count", "hotel_price_per_night", "meal_level"],
}

# --- UnsplashImageTool ---
name: "unsplash_image"
description: "搜索旅游景点相关的图片，用于行程可视化展示。"
parameters: {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "图片搜索关键词（景点名称 + 城市名效果最佳）",
        },
        "count": {
            "type": "integer",
            "description": "返回图片数量",
            "default": 1,
            "minimum": 1,
            "maximum": 5,
        },
    },
    "required": ["query"],
}
```

### 3.4 ToolExecutor — 执行中间件

```python
# backend/app/tools/executor.py

import asyncio
import hashlib
import json
import time
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class ToolExecutor:
    """
    工具执行中间件：每个工具调用经过此层进行：
      1. 缓存检查（Redis）
      2. 超时控制
      3. 重试
      4. 审计日志记录
    """

    def __init__(self, cache: Optional['ToolCache'] = None, default_timeout: float = 30.0):
        self._cache = cache
        self._default_timeout = default_timeout

    async def execute(self, tool: BaseTool, **kwargs) -> Dict[str, Any]:
        cache_key = self._build_cache_key(tool.name, kwargs)

        # 1. 查缓存
        if self._cache:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return cached

        # 2. 执行（带超时 + 重试）
        last_error = None
        for attempt in range(3):
            try:
                start = time.time()
                result = await asyncio.wait_for(
                    tool.execute(**kwargs),
                    timeout=self._default_timeout,
                )
                duration_ms = (time.time() - start) * 1000
                logger.info("工具 '%s' 执行成功, 耗时 %.0fms", tool.name, duration_ms)

                # 3. 写入缓存
                if self._cache and result:
                    await self._cache.set(cache_key, result, ttl=300)

                return result
            except asyncio.TimeoutError:
                last_error = TimeoutError(f"工具 '{tool.name}' 超时 ({self._default_timeout}s)")
            except Exception as e:
                last_error = e

            if attempt < 2:
                delay = 1.0 * (2 ** attempt)
                logger.warning("工具 '%s' 第 %d 次失败, %0.1fs后重试: %s", tool.name, attempt + 1, delay, last_error)
                await asyncio.sleep(delay)

        logger.error("工具 '%s' 所有重试耗尽: %s", tool.name, last_error)
        return {"error": str(last_error), "tool": tool.name, "success": False}

    def _build_cache_key(self, tool_name: str, kwargs: Dict[str, Any]) -> str:
        raw = json.dumps({"tool": tool_name, "args": kwargs}, sort_keys=True, ensure_ascii=False)
        return f"tool_cache:{hashlib.md5(raw.encode()).hexdigest()}"
```

### 3.5 LLM Tool-Calling 集成

```python
# backend/app/services/llm_service.py 扩展

class LLMService:
    # ... 现有代码 ...

    async def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: List[Dict[str, Any]],
        max_tool_rounds: int = 5,
    ) -> Tuple[str, List[Dict], TokenUsage]:
        """
        支持 tool-calling 的聊天接口。
        返回: (最终回复文本, 所有工具调用记录, Token 用量)
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        tool_calls_log = []
        total_usage = TokenUsage(model=self.model, provider=self._infer_provider())

        for _ in range(max_tool_rounds):
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "temperature": 0.4,
                },
                timeout=120,
            )
            response.raise_for_status()
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]

            # 累加 token 用量
            usage = body.get("usage", {})
            total_usage.prompt_tokens += usage.get("prompt_tokens", 0)
            total_usage.completion_tokens += usage.get("completion_tokens", 0)
            total_usage.total_tokens += usage.get("total_tokens", 0)

            if message.get("tool_calls"):
                # LLM 决定调用工具
                messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": message["tool_calls"]})
                for tc in message["tool_calls"]:
                    tool_name = tc["function"]["name"]
                    tool_args = json.loads(tc["function"]["arguments"])
                    tool_calls_log.append({"tool": tool_name, "arguments": tool_args, "id": tc["id"]})
                    # 此处不实际执行工具，由编排器层的 ToolExecutor 执行
                    # 工具结果需要由调用方注入 messages 后再次调用
                    # 此方法仅返回 tool_calls_log，由上层协调
                # 如果所有 tool_calls 都在 log 中，返回让上层处理
                return message.get("content") or "", tool_calls_log, total_usage
            else:
                # LLM 直接返回最终回答
                return message["content"], tool_calls_log, total_usage

        return messages[-1].get("content", ""), tool_calls_log, total_usage

    def _infer_provider(self) -> str:
        if "openai" in self.base_url:
            return "openai"
        if "deepseek" in self.base_url:
            return "deepseek"
        return "custom"
```

---

## Phase 4 详细设计：API 契约 + Pinia Stores

### 4.1 后端 API 请求/响应契约

#### 会话管理

```
POST /api/sessions
  Request:  {}
  Response: {
    "session_id": "uuid",
    "user_id": "uuid",
    "created_at": "2026-06-26T10:00:00Z",
    "is_new": true
  }

GET /api/sessions/{session_id}
  Response: {
    "session_id": "uuid",
    "user_id": "uuid",
    "trip_plans": [
      {
        "id": "uuid",
        "city": "北京",
        "start_date": "2026-07-01",
        "end_date": "2026-07-03",
        "status": "completed",
        "version": 1,
        "created_at": "...",
        "updated_at": "..."
      }
    ],
    "conversation_count": 5,
    "created_at": "...",
    "updated_at": "..."
  }
```

#### 行程计划 CRUD

```
POST /api/trip/plan
  Request: {
    "session_id": "uuid",
    "city": "北京",
    "start_date": "2026-07-01",
    "end_date": "2026-07-03",
    "days": 3,
    "preferences": "历史文化,美食",
    "budget": "中等",
    "transportation": "公共交通",
    "accommodation": "经济型酒店"
  }
  Response (201): {
    "plan_id": "uuid",
    "status": "completed",
    "version": 1,
    ...TripPlan 完整数据
  }

GET /api/trip/plan/{plan_id}
  Response: { ...TripPlan + plan_id + status + version }

PUT /api/trip/plan/{plan_id}
  Request: {
    "plan_json": { ...修改后的 TripPlan },
    "change_summary": "删除了第2天的故宫景点，调整了酒店"
  }
  Response: {
    "plan_id": "uuid",
    "version": 2,
    "status": "editing",
    ...更新后的 TripPlan
  }

GET /api/trip/plan/{plan_id}/versions
  Response: {
    "versions": [
      {"version": 3, "change_summary": "调整预算", "created_at": "..."},
      {"version": 2, "change_summary": "删除故宫", "created_at": "..."},
      {"version": 1, "change_summary": "初始创建", "created_at": "..."}
    ]
  }

POST /api/trip/plan/{plan_id}/revert/{version}
  Response: { ...恢复到指定版本的 TripPlan }

DELETE /api/trip/plan/{plan_id}
  Response (204): (实际执行 archive，软删除)
```

#### 对话

```
POST /api/conversation/{session_id}
  Request: {
    "message": "我想把第二天的行程调整一下，少去一个博物馆，多安排一个公园",
    "referenced_plan_id": "uuid"   // 可选，关联到特定计划
  }
  Response: {
    "message_id": "uuid",
    "role": "assistant",
    "content": "好的，我已经将第二天的大钟寺博物馆替换为玉渊潭公园...",
    "tool_calls": [                     // 如果 LLM 调用了工具
      {"tool": "itinerary_planner", "arguments": {...}}
    ],
    "updated_plan": { ... }            // 如果行程有更新
  }

GET /api/conversation/{session_id}
  Query: ?limit=50&before_id=uuid    // 分页游标
  Response: {
    "messages": [
      {"id": "uuid", "role": "user", "content": "...", "created_at": "..."},
      {"id": "uuid", "role": "assistant", "content": "...", "tool_calls": null, "created_at": "..."}
    ],
    "has_more": false
  }
```

#### 用户偏好

```
GET /api/preferences?user_id=uuid
  Response: {
    "preferred_categories": ["历史文化", "美食"],
    "budget_profile": {"经济": 3, "中等": 7, "舒适": 2},
    "travel_style": "慢节奏",
    "favorite_cities": ["北京", "杭州"],
    ...
  }

PUT /api/preferences?user_id=uuid
  Request: { ...偏好字段 }
  Response: { ...更新后的偏好 }
```

### 4.2 前端 Pinia Store 完整类型定义

```typescript
// frontend/src/stores/tripPlanStore.ts

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { TripPlan, TripPlanRequest, Budget } from '../types'
import { generateTripPlan, updateTripPlan, getTripPlan, getPlanVersions, revertPlanVersion } from '../services/api'

export type PlanStatus = 'idle' | 'generating' | 'completed' | 'editing' | 'archived'

export interface PlanVersion {
  version: number
  changeSummary: string
  createdAt: string
}

export const useTripPlanStore = defineStore('tripPlan', () => {
  // --- State ---
  const currentPlan = ref<TripPlan | null>(null)
  const planId = ref<string | null>(null)
  const planStatus = ref<PlanStatus>('idle')
  const originalPlan = ref<TripPlan | null>(null)   // 编辑前的快照（用于取消）
  const versions = ref<PlanVersion[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const progress = ref(0)
  const progressStatus = ref('')

  // --- Getters ---
  const isEditable = computed(() =>
    planStatus.value === 'completed' || planStatus.value === 'editing'
  )
  const hasUnsavedChanges = computed(() =>
    planStatus.value === 'editing' &&
    JSON.stringify(currentPlan.value) !== JSON.stringify(originalPlan.value)
  )
  const versionCount = computed(() => versions.value.length)

  // --- Actions ---
  async function createPlan(request: TripPlanRequest) {
    loading.value = true
    planStatus.value = 'generating'
    error.value = null
    progress.value = 0

    try {
      // 模拟进度
      const interval = setInterval(() => {
        if (progress.value < 90) progress.value += 10
      }, 300)

      const result = await generateTripPlan(request)
      clearInterval(interval)

      currentPlan.value = result.plan
      planId.value = result.plan_id
      planStatus.value = 'completed'
      progress.value = 100
      versions.value = [{ version: 1, changeSummary: '初始创建', createdAt: new Date().toISOString() }]
    } catch (e) {
      error.value = e instanceof Error ? e.message : '生成计划失败'
      planStatus.value = 'idle'
    } finally {
      loading.value = false
    }
  }

  function startEdit() {
    if (!currentPlan.value) return
    originalPlan.value = JSON.parse(JSON.stringify(currentPlan.value))
    planStatus.value = 'editing'
  }

  async function saveEdit(changeSummary?: string) {
    if (!currentPlan.value || !planId.value) return
    try {
      const result = await updateTripPlan(planId.value, {
        plan_json: currentPlan.value,
        change_summary: changeSummary || '手动编辑',
      })
      currentPlan.value = result.plan
      planStatus.value = 'completed'
      originalPlan.value = null
      // 刷新版本列表
      await loadVersions()
    } catch (e) {
      error.value = e instanceof Error ? e.message : '保存失败'
    }
  }

  function cancelEdit() {
    if (originalPlan.value) {
      currentPlan.value = JSON.parse(JSON.stringify(originalPlan.value))
    }
    originalPlan.value = null
    planStatus.value = 'completed'
  }

  async function loadVersions() {
    if (!planId.value) return
    try {
      const result = await getPlanVersions(planId.value)
      versions.value = result.versions
    } catch { /* silent */ }
  }

  async function revertToVersion(version: number) {
    if (!planId.value) return
    try {
      const result = await revertPlanVersion(planId.value, version)
      currentPlan.value = result.plan
      planStatus.value = 'completed'
      await loadVersions()
    } catch (e) {
      error.value = e instanceof Error ? e.message : '恢复版本失败'
    }
  }

  function moveAttraction(dayIndex: number, fromIndex: number, direction: 'up' | 'down') {
    if (!currentPlan.value) return
    const list = currentPlan.value.days[dayIndex].attractions
    const toIndex = direction === 'up' ? fromIndex - 1 : fromIndex + 1
    if (toIndex < 0 || toIndex >= list.length) return
    ;[list[fromIndex], list[toIndex]] = [list[toIndex], list[fromIndex]]
  }

  function deleteAttraction(dayIndex: number, attractionIndex: number) {
    if (!currentPlan.value) return
    currentPlan.value.days[dayIndex].attractions.splice(attractionIndex, 1)
  }

  function recalculateBudget() {
    if (!currentPlan.value) return
    const plan = currentPlan.value
    const totalAttractions = plan.days.reduce((s, d) => s + d.attractions.reduce((a, b) => a + (b.ticket_price || 0), 0), 0)
    const totalMeals = plan.days.reduce((s, d) => s + d.meals.reduce((a, b) => a + b.estimated_cost, 0), 0)
    const hotelCost = (plan.days.find(d => d.hotel)?.hotel?.estimated_cost || 0) * Math.max(plan.days.length - 1, 0)
    const budget: Budget = {
      total_attractions: totalAttractions,
      total_hotels: hotelCost,
      total_meals: totalMeals,
      total_transportation: plan.budget?.total_transportation || 0,
      total: totalAttractions + hotelCost + totalMeals + (plan.budget?.total_transportation || 0),
    }
    plan.budget = budget
  }

  return {
    currentPlan, planId, planStatus, originalPlan, versions, loading, error, progress, progressStatus,
    isEditable, hasUnsavedChanges, versionCount,
    createPlan, startEdit, saveEdit, cancelEdit, loadVersions, revertToVersion,
    moveAttraction, deleteAttraction, recalculateBudget,
  }
})
```

```typescript
// frontend/src/stores/sessionStore.ts

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { createSession, getSession, sendMessage, getConversation } from '../services/conversationApi'
import type { ConversationMessage } from '../types'

export const useSessionStore = defineStore('session', () => {
  const sessionId = ref<string>(localStorage.getItem('session_id') || '')
  const userId = ref<string | null>(null)
  const messages = ref<ConversationMessage[]>([])
  const isActive = ref(false)

  const messageCount = computed(() => messages.value.length)

  async function initSession() {
    if (sessionId.value) {
      try {
        const session = await getSession(sessionId.value)
        userId.value = session.user_id
        return
      } catch { /* session 可能过期，创建新的 */ }
    }
    const session = await createSession()
    sessionId.value = session.session_id
    userId.value = session.user_id
    localStorage.setItem('session_id', session.session_id)
  }

  async function loadHistory() {
    if (!sessionId.value) return
    const result = await getConversation(sessionId.value)
    messages.value = result.messages
  }

  async function send(content: string, planId?: string) {
    if (!sessionId.value) await initSession()
    messages.value.push({ id: '', role: 'user', content, created_at: new Date().toISOString() })
    const reply = await sendMessage(sessionId.value, { message: content, referenced_plan_id: planId })
    messages.value.push({
      id: reply.message_id,
      role: reply.role,
      content: reply.content,
      tool_calls: reply.tool_calls,
      created_at: new Date().toISOString(),
    })
    return reply
  }

  return { sessionId, userId, messages, isActive, messageCount, initSession, loadHistory, send }
})
```

```typescript
// frontend/src/stores/uiStore.ts

import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useUiStore = defineStore('ui', () => {
  const editMode = ref(false)
  const activeSection = ref('overview')
  const exportingState = ref<'idle' | 'exporting-image' | 'exporting-pdf'>('idle')
  const loadingProgress = ref(0)
  const loadingStatus = ref('')

  function setSection(section: string) { activeSection.value = section }
  function startExport(type: 'image' | 'pdf') { exportingState.value = type === 'image' ? 'exporting-image' : 'exporting-pdf' }
  function finishExport() { exportingState.value = 'idle' }
  function resetLoading() { loadingProgress.value = 0; loadingStatus.value = '' }

  return { editMode, activeSection, exportingState, loadingProgress, loadingStatus, setSection, startExport, finishExport, resetLoading }
})
```

```typescript
// frontend/src/stores/preferencesStore.ts

import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getPreferences, updatePreferences, saveItem, unsaveItem, getSavedItems } from '../services/api'
import type { SavedItem } from '../types'

export const usePreferencesStore = defineStore('preferences', () => {
  const preferredCategories = ref<string[]>([])
  const budgetProfile = ref<Record<string, number>>({})
  const travelStyle = ref('')
  const favoriteCities = ref<string[]>([])
  const savedItems = ref<SavedItem[]>([])
  const loaded = ref(false)

  async function load(userId: string) {
    if (loaded.value) return
    try {
      const prefs = await getPreferences(userId)
      preferredCategories.value = prefs.preferred_categories
      budgetProfile.value = prefs.budget_profile
      travelStyle.value = prefs.travel_style
      favoriteCities.value = prefs.favorite_cities
      const items = await getSavedItems(userId)
      savedItems.value = items
      loaded.value = true
    } catch { /* 静默失败，使用默认值 */ }
  }

  async function save(userId: string) {
    await updatePreferences(userId, {
      preferred_categories: preferredCategories.value,
      budget_profile: budgetProfile.value,
      travel_style: travelStyle.value,
      favorite_cities: favoriteCities.value,
    })
  }

  async function addSavedItem(userId: string, item: Omit<SavedItem, 'id' | 'created_at'>) {
    const result = await saveItem(userId, item)
    savedItems.value.unshift(result)
  }

  async function removeSavedItem(userId: string, itemId: string) {
    await unsaveItem(userId, itemId)
    savedItems.value = savedItems.value.filter(i => i.id !== itemId)
  }

  return { preferredCategories, budgetProfile, travelStyle, favoriteCities, savedItems, loaded, load, save, addSavedItem, removeSavedItem }
})
```

---

## Phase 5 详细设计：记忆系统

### 5.1 ShortTermMemory

```python
# backend/app/memory/short_term.py

from typing import Dict, List, Optional
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import ConversationMessage


class ShortTermMemory:
    """
    短期记忆：当前会话的对话上下文。
    - 内存中维护滑动窗口（最近 N 条消息）
    - 同时持久化到 conversation_messages 表
    """

    def __init__(self, session_id: str, max_messages: int = 20):
        self.session_id = session_id
        self.max_messages = max_messages
        self._buffer: List[Dict] = []  # [{role, content, metadata, ...}, ...]

    async def add(
        self,
        role: str,
        content: str,
        db: AsyncSession,
        tool_calls: Optional[List[Dict]] = None,
        tool_name: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> None:
        """添加一条消息到缓冲区并持久化"""
        entry = {
            "role": role,
            "content": content,
            "tool_calls": tool_calls,
            "tool_name": tool_name,
            "metadata": metadata or {},
            "timestamp": datetime.utcnow(),
        }
        self._buffer.append(entry)

        # 滑动窗口裁剪
        if len(self._buffer) > self.max_messages:
            self._buffer = self._buffer[-self.max_messages:]

        # 持久化
        db_msg = ConversationMessage(
            session_id=self.session_id,
            role=role,
            content=content,
            tool_calls_json=tool_calls,
            tool_name=tool_name,
            metadata_json=metadata or {},
        )
        db.add(db_msg)

    def get_context(self) -> List[Dict[str, str]]:
        """获取 LLM 格式的上下文消息列表"""
        return [
            {"role": m["role"], "content": m["content"]}
            for m in self._buffer
        ]

    def get_last_n(self, n: int) -> List[Dict]:
        """获取最近 n 条消息"""
        return self._buffer[-n:] if n < len(self._buffer) else self._buffer.copy()

    async def restore_from_db(self, db: AsyncSession) -> None:
        """从数据库恢复消息历史（服务重启后调用）"""
        result = await db.execute(
            select(ConversationMessage)
            .where(ConversationMessage.session_id == self.session_id)
            .order_by(ConversationMessage.created_at.asc())
            .limit(self.max_messages)
        )
        rows = result.scalars().all()
        self._buffer = [
            {
                "role": r.role,
                "content": r.content,
                "tool_calls": r.tool_calls_json,
                "tool_name": r.tool_name,
                "metadata": r.metadata_json or {},
                "timestamp": r.created_at,
            }
            for r in rows
        ]

    def clear(self) -> None:
        """清空缓冲区（不删除 DB 记录）"""
        self._buffer.clear()
```

### 5.2 LongTermMemory

```python
# backend/app/memory/long_term.py

from typing import Dict, List, Optional
from uuid import UUID
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import UserPreference, SavedItem


class LongTermMemory:
    """
    长期记忆：跨会话的用户偏好和收藏。
    每次完成一个旅行计划后，增量更新用户偏好画像。
    """

    def __init__(self, db_session_factory):
        self._db_factory = db_session_factory

    async def get_preferences(self, user_id: UUID, db: AsyncSession) -> UserPreference:
        """获取用户偏好，不存在则返回默认偏好"""
        result = await db.execute(
            select(UserPreference).where(UserPreference.user_id == user_id)
        )
        prefs = result.scalar_one_or_none()
        if prefs is None:
            prefs = UserPreference(user_id=user_id)
            db.add(prefs)
            await db.flush()
        return prefs

    async def update_from_trip(
        self,
        user_id: UUID,
        db: AsyncSession,
        city: str,
        preferences: List[str],
        budget_level: str,
        days: int,
        liked_attractions: Optional[List[str]] = None,
    ) -> None:
        """
        根据一个已完成行程更新用户偏好。
        调用时机：用户确认/保存行程计划后。
        """
        prefs = await self.get_preferences(user_id, db)

        # 更新偏好类别计数
        for cat in preferences:
            if cat and cat.strip():
                prefs.preferred_categories = list(set(prefs.preferred_categories or []) | {cat.strip()})

        # 更新预算画像
        profile = dict(prefs.budget_profile or {})
        profile[budget_level] = profile.get(budget_level, 0) + 1
        prefs.budget_profile = profile

        # 更新平均旅行天数
        if prefs.avg_trip_days:
            prefs.avg_trip_days = round((prefs.avg_trip_days + days) / 2, 1)
        else:
            prefs.avg_trip_days = float(days)

        # 更新常去城市
        cities = list(prefs.favorite_cities or [])
        if city not in cities:
            cities.append(city)
        prefs.favorite_cities = cities[:10]  # 最多保留10个城市

        await db.flush()

    async def get_saved_items(
        self, user_id: UUID, db: AsyncSession, item_type: Optional[str] = None
    ) -> List[SavedItem]:
        """获取用户收藏"""
        query = select(SavedItem).where(SavedItem.user_id == user_id)
        if item_type:
            query = query.where(SavedItem.item_type == item_type)
        query = query.order_by(SavedItem.created_at.desc()).limit(50)
        result = await db.execute(query)
        return list(result.scalars().all())

    async def add_saved_item(
        self, user_id: UUID, db: AsyncSession,
        item_type: str, item_data: Dict, tags: Optional[List[str]] = None, note: Optional[str] = None,
    ) -> SavedItem:
        """添加收藏"""
        item = SavedItem(
            user_id=user_id,
            item_type=item_type,
            item_data=item_data,
            tags=tags or [],
            note=note,
        )
        db.add(item)
        await db.flush()
        return item
```

### 5.3 MemoryRecall

```python
# backend/app/memory/recall.py

from typing import Dict, List
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from app.memory.long_term import LongTermMemory


class MemoryRecall:
    """
    记忆召回：在生成新计划时，从长期记忆中召回相关信息注入上下文。
    """

    def __init__(self, long_term_memory: LongTermMemory):
        self._ltm = long_term_memory

    async def recall(
        self,
        user_id: UUID,
        db: AsyncSession,
        city: str,
        preferences: str,
    ) -> Dict:
        """
        召回与当前请求相关的历史信息。
        返回:
          {
            "user_preferences": {...},
            "past_plans_in_city": [...],    # 同城历史计划
            "saved_attractions": [...],      # 收藏的景点
            "suggested_additions": [...]     # 基于偏好的推荐
          }
        """
        prefs = await self._ltm.get_preferences(user_id, db)

        # 召回同城历史计划中的景点
        saved_items = await self._ltm.get_saved_items(user_id, db, item_type="attraction")
        saved_attractions = [
            item.item_data for item in saved_items
            if item.item_data.get("city", "") == city
        ]

        recall_context = {
            "preferred_categories": prefs.preferred_categories or [],
            "travel_style": prefs.travel_style or "",
            "budget_profile": prefs.budget_profile or {},
            "favorite_cities": prefs.favorite_cities or [],
            "saved_attractions_in_city": saved_attractions,
            "avg_trip_days": prefs.avg_trip_days,
        }

        return recall_context
```

---

## Phase 6 详细设计：安全治理

### 6.1 ContentFilter

```python
# backend/app/services/content_filter.py

import re
import bleach

# 允许的 HTML 标签（纯文本场景下为空）
ALLOWED_TAGS: list = []
ALLOWED_ATTRIBUTES: dict = {}

# Prompt 注入检测模式
INJECTION_PATTERNS = [
    r"(?i)(ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|messages?))",
    r"(?i)(you\s+are\s+now\s+(DAN|free|unrestricted))",
    r"(?i)(system\s*[:：]\s*)",
    r"(?i)(<\|im_start\|>)",
    r"(?i)(\[INST\])",
    r"(?i)(\[SYS\])",
    r"(?i)(prompt\s*injection)",
    r"(?i)(jailbreak)",
]

# 敏感内容关键词（用于 LLM 输出审查）
SENSITIVE_KEYWORDS = [
    "暴力", "色情", "赌博", "毒品",
    "political_sensitive",   # 占位，实际使用时需要更完善的词表
]


def sanitize_text(value: str) -> str:
    """
    清洗输入文本：
    1. 去除所有 HTML 标签
    2. 标准化空白字符
    """
    if not isinstance(value, str):
        return value
    cleaned = bleach.clean(
        value,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        strip=True,
    )
    return " ".join(cleaned.split())


def detect_injection(value: str) -> bool:
    """检测输入是否包含 prompt 注入模式"""
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, value):
            return True
    return False


def filter_llm_output(text: str) -> tuple[str, list[str]]:
    """
    审查 LLM 输出。
    返回: (过滤后文本, 触发的问题列表)
    """
    issues = []
    lower = text.lower()
    for keyword in SENSITIVE_KEYWORDS:
        if keyword.lower() in lower:
            text = text.replace(keyword, "[已过滤]")
            issues.append(f"检测到敏感内容: {keyword}")
    return text, issues
```

### 6.2 AuditMiddleware

```python
# backend/app/api/middlewares/audit.py

import json
import time
import uuid
import logging
from typing import Callable
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("audit")


class AuditMiddleware(BaseHTTPMiddleware):
    """
    ASGI 审计中间件：记录每个 HTTP 请求的关键信息。
    写入 audit.event_log 表（异步、非阻塞）。
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id

        start = time.time()
        response = await call_next(request)
        duration_ms = (time.time() - start) * 1000

        # 异步写入审计日志（不阻塞响应）
        await self._log_event(
            event_type="http_request",
            action=f"{request.method} {request.url.path}",
            details={
                "method": request.method,
                "path": request.url.path,
                "query_string": str(request.query_params),
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 2),
                "request_id": request_id,
            },
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("User-Agent", ""),
            duration_ms=duration_ms,
        )

        response.headers["X-Request-ID"] = request_id
        return response

    async def _log_event(self, **kwargs) -> None:
        try:
            # 使用独立数据库连接写入，避免事务污染
            from app.database import get_session_factory
            from app.models.db_models import AuditEvent

            factory = get_session_factory()
            async with factory() as db:
                event = AuditEvent(**kwargs)
                db.add(event)
                await db.commit()
        except Exception as e:
            # 审计失败不应影响主流程
            logger.warning("审计日志写入失败: %s", e)
```

### 6.3 RateLimitMiddleware

```python
# backend/app/api/middlewares/rate_limit.py

from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import Request, HTTPException

limiter = Limiter(key_func=get_remote_address, default_limits=["100/hour"])


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    raise HTTPException(
        status_code=429,
        detail="请求过于频繁，请稍后再试",
        headers={"Retry-After": "60", "X-Rate-Limit-Exceeded": "true"},
    )
```

### 6.4 Encryption

```python
# backend/app/services/encryption.py

from cryptography.fernet import Fernet, InvalidToken
import base64
import logging

logger = logging.getLogger(__name__)


class KeyEncryptor:
    """
    API Key 加密/解密。
    使用 Fernet（AES-128-CBC + HMAC）对称加密。
    """

    def __init__(self, master_key: str):
        if not master_key:
            self._fernet = None
            logger.warning("ENCRYPTION_KEY 未设置，API Key 加密不可用")
            return
        try:
            key_bytes = base64.urlsafe_b64encode(master_key.encode().ljust(32)[:32])
            self._fernet = Fernet(key_bytes)
        except Exception as e:
            self._fernet = None
            logger.error("初始化 KeyEncryptor 失败: %s", e)

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def encrypt(self, plaintext: str) -> bytes:
        if not self.available:
            raise RuntimeError("加密服务不可用")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        if not self.available:
            raise RuntimeError("加密服务不可用")
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken:
            raise ValueError("无法解密：密钥不匹配或数据已损坏")
```

### 6.5 Token Cost Tracking

```python
# 追加到 backend/app/services/llm_service.py

MODEL_PRICING = {
    # (prompt_price_per_1M, completion_price_per_1M)
    "gpt-4o-mini":        (0.15, 0.60),
    "gpt-4o":             (2.50, 10.00),
    "gpt-4o-2024-08-06":  (2.50, 10.00),
    "deepseek-chat":      (0.14, 0.28),
    "deepseek-v3":        (0.14, 0.28),
}


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """估算 LLM 调用成本（USD）"""
    pricing = MODEL_PRICING.get(model)
    if not pricing:
        # 模糊匹配
        for key, value in MODEL_PRICING.items():
            if key in model:
                pricing = value
                break
    if not pricing:
        return 0.0

    prompt_price, completion_price = pricing
    cost = (
        (prompt_tokens / 1_000_000) * prompt_price
        + (completion_tokens / 1_000_000) * completion_price
    )
    return round(cost, 8)
```

---

## 补充：Alembic 迁移初始版本

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
