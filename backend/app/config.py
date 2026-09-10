from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PlannerBackendName = Literal["pydantic_ai", "openai_tools", "deterministic"]


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_base_url: str = Field(default="https://api.openai.com/v1", alias="LLM_BASE_URL")
    llm_model: str = Field(default="gpt-4o-mini", alias="LLM_MODEL")
    amap_api_key: str = Field(default="", alias="AMAP_API_KEY")
    amap_mcp_enabled: bool = Field(default=True, alias="AMAP_MCP_ENABLED")
    amap_mcp_command: str = Field(default="npx", alias="AMAP_MCP_COMMAND")
    amap_mcp_args: list[str] = Field(
        default_factory=lambda: ["-y", "@amap/amap-maps-mcp-server"],
        alias="AMAP_MCP_ARGS",
    )
    amap_mcp_startup_timeout_seconds: float = Field(
        default=90.0,
        ge=1.0,
        alias="AMAP_MCP_STARTUP_TIMEOUT_SECONDS",
    )
    amap_mcp_call_timeout_seconds: float = Field(
        default=20.0,
        ge=1.0,
        alias="AMAP_MCP_CALL_TIMEOUT_SECONDS",
    )
    amap_mcp_http_fallback: bool = Field(default=True, alias="AMAP_MCP_HTTP_FALLBACK")
    amap_qps_budget: float = Field(default=2.0, ge=0.0, alias="AMAP_QPS_BUDGET")
    amap_qps_retry_attempts: int = Field(default=2, ge=0, alias="AMAP_QPS_RETRY_ATTEMPTS")
    amap_qps_retry_delay_seconds: float = Field(
        default=1.0,
        ge=0.0,
        alias="AMAP_QPS_RETRY_DELAY_SECONDS",
    )
    amap_enrich_detail_limit: int = Field(default=3, ge=0, alias="AMAP_ENRICH_DETAIL_LIMIT")
    baidu_map_api_key: str = Field(default="", alias="BAIDU_MAP_API_KEY")
    baidu_qps_budget: float = Field(default=2.0, ge=0.0, alias="BAIDU_QPS_BUDGET")
    baidu_qps_retry_attempts: int = Field(default=2, ge=0, alias="BAIDU_QPS_RETRY_ATTEMPTS")
    baidu_qps_retry_delay_seconds: float = Field(
        default=0.5,
        ge=0.0,
        alias="BAIDU_QPS_RETRY_DELAY_SECONDS",
    )
    baidu_meal_concurrency: int = Field(default=2, ge=1, le=20, alias="BAIDU_MEAL_CONCURRENCY")
    unsplash_access_key: str = Field(default="", alias="UNSPLASH_ACCESS_KEY")
    enable_external_services: bool = Field(default=True, alias="ENABLE_EXTERNAL_SERVICES")
    planner_backend: PlannerBackendName = Field(
        default="pydantic_ai",
        alias="PLANNER_BACKEND",
    )
    pydantic_ai_request_limit: int = Field(default=12, ge=2, alias="PYDANTIC_AI_REQUEST_LIMIT")
    pydantic_ai_tool_call_limit: int = Field(default=8, ge=1, alias="PYDANTIC_AI_TOOL_CALL_LIMIT")
    pydantic_ai_tool_round_limit: int = Field(default=3, ge=1, alias="PYDANTIC_AI_TOOL_ROUND_LIMIT")
    enable_plan_critique: bool = Field(default=True, alias="ENABLE_PLAN_CRITIQUE")
    max_refinement_rounds: int = Field(default=1, ge=0, alias="MAX_REFINEMENT_ROUNDS")
    min_pass_score: float = Field(default=7.0, ge=0, le=10, alias="MIN_PASS_SCORE")
    frontend_origin: str = Field(default="http://localhost:5173", alias="FRONTEND_ORIGIN")
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/trip_planner",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    task_result_ttl_days: int = Field(default=30, ge=1, alias="TASK_RESULT_TTL_DAYS")
    task_worker_timeout_seconds: int = Field(default=900, ge=30, alias="TASK_WORKER_TIMEOUT_SECONDS")
    task_sse_timeout_seconds: int = Field(default=300, ge=30, alias="TASK_SSE_TIMEOUT_SECONDS")
    trip_planner_attempt_timeout_seconds: float = Field(
        default=150.0,
        ge=10.0,
        alias="TRIP_PLANNER_ATTEMPT_TIMEOUT_SECONDS",
    )
    trip_planner_max_attempts: int = Field(
        default=2,
        ge=1,
        le=3,
        alias="TRIP_PLANNER_MAX_ATTEMPTS",
    )
    planner_draft_timeout_seconds: float = Field(
        default=90.0,
        ge=0.0,
        alias="PLANNER_DRAFT_TIMEOUT_SECONDS",
    )
    planner_draft_max_attempts: int = Field(
        default=2,
        ge=1,
        le=3,
        alias="PLANNER_DRAFT_MAX_ATTEMPTS",
    )
    planner_critique_timeout_seconds: float = Field(
        default=35.0,
        ge=0.0,
        alias="PLANNER_CRITIQUE_TIMEOUT_SECONDS",
    )
    planner_refine_timeout_seconds: float = Field(
        default=55.0,
        ge=0.0,
        alias="PLANNER_REFINE_TIMEOUT_SECONDS",
    )
    planner_global_request_limit: int = Field(
        default=4,
        ge=2,
        le=12,
        alias="PLANNER_GLOBAL_REQUEST_LIMIT",
    )
    pydantic_ai_model_request_timeout_seconds: float = Field(
        default=90.0,
        ge=0.0,
        alias="PYDANTIC_AI_MODEL_REQUEST_TIMEOUT_SECONDS",
    )
    task_maintenance_interval_seconds: int = Field(
        default=60,
        ge=10,
        alias="TASK_MAINTENANCE_INTERVAL_SECONDS",
    )
    task_stale_queued_seconds: int = Field(
        default=300,
        ge=60,
        alias="TASK_STALE_QUEUED_SECONDS",
    )
    langgraph_checkpoint_dsn: str | None = Field(default=None, alias="LANGGRAPH_CHECKPOINT_DSN")
    langgraph_workflow_version: str = Field(
        default="trip_planning_v2",
        min_length=1,
        alias="LANGGRAPH_WORKFLOW_VERSION",
    )
    langgraph_state_schema_version: int = Field(
        default=2,
        ge=1,
        alias="LANGGRAPH_STATE_SCHEMA_VERSION",
    )
    langgraph_heartbeat_seconds: int = Field(default=20, ge=5, alias="LANGGRAPH_HEARTBEAT_SECONDS")
    langgraph_stale_seconds: int = Field(default=90, ge=15, alias="LANGGRAPH_STALE_SECONDS")
    langgraph_recovery_scan_seconds: int = Field(
        default=30,
        ge=5,
        alias="LANGGRAPH_RECOVERY_SCAN_SECONDS",
    )
    langgraph_recovery_queue_stale_seconds: int = Field(
        default=180,
        ge=30,
        alias="LANGGRAPH_RECOVERY_QUEUE_STALE_SECONDS",
    )
    langgraph_max_recoveries: int = Field(default=3, ge=0, alias="LANGGRAPH_MAX_RECOVERIES")
    langgraph_meal_timeout_seconds: int = Field(
        default=60,
        ge=1,
        alias="LANGGRAPH_MEAL_TIMEOUT_SECONDS",
    )
    encryption_key: str = Field(default="", alias="ENCRYPTION_KEY")
    max_conversation_messages: int = Field(default=20, alias="MAX_CONVERSATION_MESSAGES")
    rate_limit_per_minute: int = Field(default=5, alias="RATE_LIMIT_PER_MINUTE")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
