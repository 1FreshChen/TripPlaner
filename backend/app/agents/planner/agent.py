from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.agents.planner.backend import PlannerBackendName
from app.agents.planner.collectors import AttractionSearchAgent, HotelAgent, WeatherQueryAgent
from app.agents.planner.prompting import (
    build_planner_query,
    summarize_attractions,
    summarize_hotels,
    summarize_weather,
)
from app.config import get_settings
from app.models.schemas import Attraction, Hotel, TripPlan, TripPlanRequest, WeatherInfo
from app.services.amap_service import AmapService
from app.services.amap_mcp_service import get_amap_mcp_service
from app.services.llm_service import LLMService
from app.services.plan_quality import PlanQualityError
from app.services.unsplash_service import UnsplashService
from app.tools.bootstrap import bootstrap_tools
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry


@dataclass(frozen=True)
class PlanningRunTrace:
    """Compatibility projection backed by LangGraph node results."""

    agent_results: list[dict[str, Any]]


class TripPlannerAgent:
    """Compatibility facade that executes the single LangGraph workflow."""

    def __init__(
        self,
        enable_external_services: Optional[bool] = None,
        amap_service: Optional[AmapService] = None,
        llm_service: Optional[LLMService] = None,
        unsplash_service: Optional[UnsplashService] = None,
        tool_registry: Optional[ToolRegistry] = None,
        tool_executor: Optional[ToolExecutor] = None,
        planner_backend: PlannerBackendName | None = None,
    ) -> None:
        settings = get_settings()
        use_external = (
            settings.enable_external_services
            if enable_external_services is None
            else enable_external_services
        )
        self.settings = settings.model_copy(update={"enable_external_services": use_external})
        self.enable_external_services = use_external
        self.amap_service = amap_service or get_amap_mcp_service()
        self.mcp_tool = getattr(self.amap_service, "mcp_tool", None)
        self.llm_service = llm_service or LLMService(
            settings.llm_api_key,
            settings.llm_base_url,
            settings.llm_model,
        )
        self.unsplash_service = unsplash_service or UnsplashService(settings.unsplash_access_key)
        self.tool_registry = tool_registry or bootstrap_tools(
            self.amap_service,
            self.unsplash_service,
        )
        self.tool_executor = tool_executor or ToolExecutor(registry=self.tool_registry)
        self.attraction_agent = AttractionSearchAgent(self.amap_service, use_external)
        self.weather_agent = WeatherQueryAgent(self.amap_service, use_external)
        self.hotel_agent = HotelAgent(self.amap_service, use_external)

        from app.orchestration.bootstrap import bootstrap_orchestration

        self.registry = bootstrap_orchestration(
            amap_service=self.amap_service,
            llm_service=self.llm_service,
            enable_external_services=use_external,
            tool_registry=self.tool_registry,
            tool_executor=self.tool_executor,
            planner_backend=planner_backend,
        )
        self.last_trace: PlanningRunTrace | None = None
        self.last_tool_calls: List[Dict[str, Any]] = []
        self.last_token_usage: Any = None
        self.last_critique_events: List[Dict[str, Any]] = []

    async def aplan_trip(
        self,
        request: TripPlanRequest,
        memory_context: Optional[Dict[str, Any]] = None,
        conversation_context: Optional[List[Dict[str, str]]] = None,
    ) -> TripPlan:
        from app.orchestration.langgraph_workflow import build_trip_planning_graph

        graph = build_trip_planning_graph(registry=self.registry, settings=self.settings)
        result = await graph.ainvoke(
            {
                "task_id": None,
                "plan_id": str(uuid.uuid4()),
                "request": request.model_dump(mode="json"),
                "memory_context": memory_context or {},
                "conversation_context": conversation_context or [],
                "workflow_version": self.settings.langgraph_workflow_version,
                "state_schema_version": self.settings.langgraph_state_schema_version,
                "agent_results": [],
            }
        )
        terminal_error = result.get("terminal_error")
        if terminal_error or not result.get("validation_passed"):
            message = (
                terminal_error.get("message", "行程工作流未通过最终校验")
                if isinstance(terminal_error, dict)
                else "行程工作流未通过最终校验"
            )
            raise PlanQualityError(message)

        self.last_trace = PlanningRunTrace(list(result.get("agent_results") or []))
        self.last_tool_calls = list(result.get("trip_planner_tool_calls") or [])
        self.last_token_usage = result.get("trip_planner_token_usage")
        self.last_critique_events = list(result.get("plan_critique_events") or [])
        return TripPlan.model_validate(result["trip_planner"])

    def plan_trip(self, request: TripPlanRequest) -> TripPlan:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.aplan_trip(request))
        raise RuntimeError("Use aplan_trip() when calling TripPlannerAgent from async code")

    def _build_planner_query(
        self,
        request: TripPlanRequest,
        attraction_response: str,
        weather_response: str,
        hotel_response: str,
    ) -> str:
        return build_planner_query(request, attraction_response, weather_response, hotel_response)

    @staticmethod
    def _summarize_attractions(attractions: List[Attraction]) -> str:
        return summarize_attractions(attractions)

    @staticmethod
    def _summarize_weather(weather_info: List[WeatherInfo]) -> str:
        return summarize_weather(weather_info)

    @staticmethod
    def _summarize_hotels(hotels: List[Hotel]) -> str:
        return summarize_hotels(hotels)
