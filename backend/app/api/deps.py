from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.orchestration.bootstrap import bootstrap_orchestration
from app.services.amap_service import AmapService
from app.services.state_service import StateService
from app.services.unsplash_service import UnsplashService
from app.config import get_settings
from app.tools.bootstrap import bootstrap_tools

_agent_registry = None
_tool_registry = None


def get_tool_registry():
    global _tool_registry
    if _tool_registry is None:
        settings = get_settings()
        _tool_registry = bootstrap_tools(
            AmapService(settings.amap_api_key),
            UnsplashService(settings.unsplash_access_key),
        )
    return _tool_registry


def get_orchestrator():
    global _agent_registry
    if _agent_registry is None:
        _agent_registry = bootstrap_orchestration()
    return _agent_registry


def get_state_service(db: AsyncSession = Depends(get_db)) -> StateService:
    return StateService(db)
