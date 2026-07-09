from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_base_url: str = Field(default="https://api.openai.com/v1", alias="LLM_BASE_URL")
    llm_model: str = Field(default="gpt-4o-mini", alias="LLM_MODEL")
    amap_api_key: str = Field(default="", alias="AMAP_API_KEY")
    baidu_map_api_key: str = Field(default="", alias="BAIDU_MAP_API_KEY")
    unsplash_access_key: str = Field(default="", alias="UNSPLASH_ACCESS_KEY")
    enable_external_services: bool = Field(default=True, alias="ENABLE_EXTERNAL_SERVICES")
    use_enhanced_prompt: bool = Field(default=True, alias="USE_ENHANCED_PROMPT")
    enable_llm_tool_planning: bool = Field(default=True, alias="ENABLE_LLM_TOOL_PLANNING")
    enable_plan_critique: bool = Field(default=True, alias="ENABLE_PLAN_CRITIQUE")
    max_refinement_rounds: int = Field(default=3, ge=0, alias="MAX_REFINEMENT_ROUNDS")
    min_pass_score: float = Field(default=7.0, ge=0, le=10, alias="MIN_PASS_SCORE")
    frontend_origin: str = Field(default="http://localhost:5173", alias="FRONTEND_ORIGIN")
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/trip_planner",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    encryption_key: str = Field(default="", alias="ENCRYPTION_KEY")
    max_conversation_messages: int = Field(default=20, alias="MAX_CONVERSATION_MESSAGES")
    rate_limit_per_minute: int = Field(default=5, alias="RATE_LIMIT_PER_MINUTE")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()

