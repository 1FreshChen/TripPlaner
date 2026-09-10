from __future__ import annotations

from app.agents.llm_planner import LLMPlannerAgent
from app.agents.planner.collectors import AttractionSearchAgent, HotelAgent, WeatherQueryAgent
from app.agents.planner.deterministic import PlannerAgent
from app.agents.pydantic_planner import PydanticAIPlannerAgent
from app.config import PlannerBackendName, get_settings
from app.orchestration.base import AgentDefinition, FallbackLevel, RetryPolicy
from app.orchestration.fallback import FallbackChain
from app.orchestration.registry import AgentRegistry
from app.services.amap_service import AmapService
from app.services.amap_mcp_service import get_amap_mcp_service
from app.services.baidu_map_service import BaiduMapService
from app.services.llm_service import LLMService
from app.services.mock_data import build_mock_attractions, build_mock_hotels, build_mock_weather
from app.tools.bootstrap import bootstrap_tools
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry


def bootstrap_orchestration(
    amap_service: AmapService | None = None,
    llm_service: LLMService | None = None,
    enable_external_services: bool | None = None,
    tool_registry: ToolRegistry | None = None,
    tool_executor: ToolExecutor | None = None,
    planner_backend: PlannerBackendName | None = None,
    enable_plan_critique: bool | None = None,
    max_refinement_rounds: int | None = None,
    min_pass_score: float | None = None,
    baidu_service: BaiduMapService | None = None,
) -> AgentRegistry:
    settings = get_settings()
    use_external = settings.enable_external_services if enable_external_services is None else enable_external_services
    selected_planner = planner_backend or settings.planner_backend
    use_plan_critique = settings.enable_plan_critique if enable_plan_critique is None else enable_plan_critique
    refinement_rounds = (
        settings.max_refinement_rounds if max_refinement_rounds is None else max_refinement_rounds
    )
    pass_score = settings.min_pass_score if min_pass_score is None else min_pass_score
    amap = amap_service or get_amap_mcp_service()
    llm = llm_service or LLMService(settings.llm_api_key, settings.llm_base_url, settings.llm_model)
    tools = tool_registry or bootstrap_tools(amap_service=amap, baidu_service=baidu_service)
    executor = tool_executor or ToolExecutor(registry=tools)

    registry = AgentRegistry()
    registry.register(
        AgentDefinition(
            name="attraction_search",
            agent_class=lambda: AttractionSearchAgent(amap, use_external),
            description="搜索目的地城市的景点信息",
            retry_policy=RetryPolicy(max_attempts=3, base_delay=1.0),
            timeout_seconds=15.0,
        )
    )
    registry.register(
        AgentDefinition(
            name="weather_query",
            agent_class=lambda: WeatherQueryAgent(amap, use_external),
            description="查询目的地城市的天气预报",
            retry_policy=RetryPolicy(max_attempts=2, base_delay=1.0),
            timeout_seconds=10.0,
        )
    )
    registry.register(
        AgentDefinition(
            name="hotel_recommendation",
            agent_class=lambda: HotelAgent(amap, use_external),
            description="推荐目的地城市的酒店",
            retry_policy=RetryPolicy(max_attempts=3, base_delay=1.0),
            timeout_seconds=15.0,
        )
    )
    if use_external and llm.enabled:
        if selected_planner == "pydantic_ai":
            planner_factory = lambda: PydanticAIPlannerAgent(
                llm,
                tools,
                executor,
                enable_critique=use_plan_critique,
                max_refinement_rounds=refinement_rounds,
                min_pass_score=pass_score,
                request_limit=settings.pydantic_ai_request_limit,
                tool_call_limit=settings.pydantic_ai_tool_call_limit,
                tool_round_limit=settings.pydantic_ai_tool_round_limit,
                model_request_timeout_seconds=settings.pydantic_ai_model_request_timeout_seconds,
            )
        elif selected_planner == "openai_tools":
            planner_factory = lambda: LLMPlannerAgent(
                llm,
                tools,
                executor,
                enable_critique=use_plan_critique,
                max_refinement_rounds=refinement_rounds,
                min_pass_score=pass_score,
            )
        else:
            planner_factory = PlannerAgent
    else:
        planner_factory = PlannerAgent
    registry.register(
        AgentDefinition(
            name="trip_planner",
            agent_class=planner_factory,
            description="综合所有信息生成完整旅行计划",
            retry_policy=RetryPolicy(
                max_attempts=settings.trip_planner_max_attempts,
                base_delay=2.0,
            ),
            timeout_seconds=settings.trip_planner_attempt_timeout_seconds,
        )
    )
    return registry


def build_default_fallback_chain() -> FallbackChain:
    chain = FallbackChain()
    chain.register_handler(
        "attraction_search",
        FallbackLevel.MOCK,
        lambda context: build_mock_attractions(
            context["request"].city, context["request"].preferences, context["request"].days
        ),
    )
    chain.register_handler(
        "weather_query",
        FallbackLevel.MOCK,
        lambda context: build_mock_weather(context["request"].start_date, context["request"].days),
    )
    chain.register_handler(
        "hotel_recommendation",
        FallbackLevel.MOCK,
        lambda context: build_mock_hotels(
            context["request"].city, context["request"].accommodation, context["request"].budget
        ),
    )
    chain.register_handler(
        "trip_planner",
        FallbackLevel.DETERMINISTIC,
        lambda context: PlannerAgent().execute(context),
    )
    return chain
