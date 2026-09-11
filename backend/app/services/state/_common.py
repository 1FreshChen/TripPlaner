from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Awaitable, Callable, Optional

from fastapi import HTTPException
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.trip_planner import TripPlannerAgent
from app.agents.planner.validation import validate_planner_output
from app.config import get_settings
from app.memory.long_term import LongTermMemory
from app.memory.recall import MemoryRecall
from app.memory.short_term import ShortTermMemory
from app.models.db_models import AuditEvent, ConversationMessage, TokenUsage as TokenUsageModel, TripPlan as TripPlanModel
from app.models.db_models import SavedItem, TripPlanVersion, User, UserPreference
from app.models.db_models import TripPlanTask
from app.models.schemas import (
    ConversationListResponse,
    ConversationMessageResponse,
    ConversationRequest,
    ConversationResponse,
    PlanVersionsResponse,
    SavedItemCreateRequest,
    SavedItemResponse,
    SessionCreateResponse,
    SessionDetailResponse,
    SessionTripPlanSummary,
    TripPlan,
    TripPlanRequest,
    TripPlanResponse,
    TripPlanUpdateRequest,
    UserPreferenceResponse,
    UserPreferenceUpdateRequest,
)
from app.services.content_filter import filter_llm_output
from app.services.llm_service import LLMService, TokenUsage as LLMTokenUsage, estimate_cost
from app.services.plan_quality import PlanQualityError
from app.tools.bootstrap import bootstrap_tools
from app.tools.executor import ToolExecutor
from app.utils.json_utils import strip_json_fence


logger = logging.getLogger(__name__)
ProgressCallback = Callable[[str, int, str], Awaitable[None]]
PUBLIC_PLAN_STATUSES = frozenset({"completed"})


class PlanModificationError(RuntimeError):
    """Raised when an intended conversation plan update cannot be persisted."""


@dataclass(frozen=True)
class ModificationIntentDecision:
    """Result of deciding whether a conversation turn should update a trip plan."""

    should_modify: bool
    instruction: str
    source: str
    reason: str = ""


def _plan_quality_error_detail(exc: PlanQualityError, *, retryable: bool = True) -> dict:
    message = str(exc)
    issues = [part.strip() for part in re.split(r"[;；]", message) if part.strip()]
    return {
        "code": "PLAN_QUALITY_FAILED",
        "retryable": retryable,
        "issues": issues or [message],
    }


def _iso(value) -> str:
    return value.isoformat() if value else ""


def _parse_uuid(value: str, label: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"{label} 不存在") from exc


def _split_preferences(value: str) -> list[str]:
    return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]

def _compat_symbol(name: str, default):
    """Resolve facade-level overrides without coupling mixins to the facade module."""
    import importlib

    facade = importlib.import_module("app.services.state_service")
    return getattr(facade, name, default)


__all__ = [name for name in globals() if not name.startswith("__")]
__all__ += [
    "_compat_symbol",
    "_iso",
    "_parse_uuid",
    "_split_preferences",
    "_plan_quality_error_detail",
]
