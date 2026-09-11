from app.agents.llm_planner import LLMPlannerAgent
from app.agents.trip_planner import PlannerAgent, build_planner_query
from app.models.schemas import TripPlanRequest
from app.orchestration.bootstrap import bootstrap_orchestration


class FakeLLMService:
    enabled = True


class FakeToolRegistry:
    def get_openai_functions(self):
        return []


class FakeToolExecutor:
    pass


def _request() -> TripPlanRequest:
    return TripPlanRequest(
        city="上海",
        start_date="2026-07-01",
        end_date="2026-07-02",
        days=2,
        preferences="美食,城市漫步",
        budget="舒适",
        transportation="地铁",
        accommodation="精品酒店",
    )


def test_bootstrap_registers_openai_tools_planner_when_selected():
    registry = bootstrap_orchestration(
        llm_service=FakeLLMService(),
        enable_external_services=True,
        planner_backend="openai_tools",
        tool_registry=FakeToolRegistry(),
        tool_executor=FakeToolExecutor(),
    )

    agent = registry.get("trip_planner").create_agent()

    assert isinstance(agent, LLMPlannerAgent)


def test_bootstrap_passes_critique_settings_to_llm_planner():
    registry = bootstrap_orchestration(
        llm_service=FakeLLMService(),
        enable_external_services=True,
        planner_backend="openai_tools",
        enable_plan_critique=False,
        max_refinement_rounds=1,
        min_pass_score=8.5,
        tool_registry=FakeToolRegistry(),
        tool_executor=FakeToolExecutor(),
    )

    agent = registry.get("trip_planner").create_agent()

    assert isinstance(agent, LLMPlannerAgent)
    assert agent.enable_critique is False
    assert agent.max_refinement_rounds == 1
    assert agent.min_pass_score == 8.5


def test_bootstrap_registers_deterministic_planner_when_selected():
    registry = bootstrap_orchestration(
        llm_service=FakeLLMService(),
        enable_external_services=True,
        planner_backend="deterministic",
        tool_registry=FakeToolRegistry(),
        tool_executor=FakeToolExecutor(),
    )

    agent = registry.get("trip_planner").create_agent()

    assert isinstance(agent, PlannerAgent)


def test_build_planner_query_includes_diversity_and_dedup_instructions():
    request = _request()

    query = build_planner_query(
        request=request,
        attraction_response="外滩; 豫园",
        weather_response="晴 28度",
        hotel_response="人民广场附近精品酒店",
        memory_context={"saved_attractions_in_city": [{"name": "外滩"}]},
        conversation_context=[{"role": "user", "content": "外滩去过了，想换新路线"}],
    )

    assert "baseline 数据" in query
    assert "40%-60%" in query
    assert "去重" in query
    assert "外滩" in query
    assert "可用工具" in query
    assert "baidu_poi_search" not in query
    assert "baidu_direction" in query
    assert "百度地图 HTTP API" in query
    assert "days[].meals 必须返回空数组" in query


def test_bootstrap_passes_baidu_service_to_tool_bootstrap(monkeypatch):
    captured = {}

    def fake_bootstrap_tools(**kwargs):
        captured.update(kwargs)
        return FakeToolRegistry()

    baidu_service = object()
    monkeypatch.setattr("app.orchestration.bootstrap.bootstrap_tools", fake_bootstrap_tools)

    bootstrap_orchestration(
        llm_service=FakeLLMService(),
        enable_external_services=True,
        tool_registry=None,
        baidu_service=baidu_service,
    )

    assert captured["baidu_service"] is baidu_service
