from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from langgraph.checkpoint.memory import InMemorySaver

from app.agents.trip_planner import PlannerAgent
from app.config import Settings
from app.models.schemas import TripPlanRequest
from app.orchestration.base import AgentDefinition, RetryPolicy
from app.orchestration.checkpoint import postgres_checkpoint_dsn
from app.orchestration.langgraph_workflow import build_trip_planning_graph
from app.orchestration.registry import AgentRegistry
from app.services.mock_data import build_mock_attractions, build_mock_hotels, build_mock_weather
from app.services import task_service
from app.services.task_service import TripPlanTaskService, progress_from_graph_state, transition_task
from app.models.db_models import TripPlanTask
from app.services import state_service
from app.services.state_service import StateService
from app.tasks import worker


def _request() -> TripPlanRequest:
    return TripPlanRequest(
        session_id=str(uuid.uuid4()),
        city="北京",
        start_date="2026-09-01",
        end_date="2026-09-02",
        days=2,
        preferences="历史文化",
        budget="中等",
        transportation="公共交通",
        accommodation="经济型酒店",
    )


def _initial_state(request: TripPlanRequest, task_id: str = "task-1") -> dict:
    return {
        "task_id": task_id,
        "plan_id": str(uuid.uuid4()),
        "request": request.model_dump(mode="json"),
        "memory_context": {},
        "conversation_context": [],
        "workflow_version": "trip_planning_v1",
        "state_schema_version": 1,
        "agent_results": [],
    }


class _AttractionAgent:
    async def execute(self, context):
        request = context["request"]
        return build_mock_attractions(request.city, request.preferences, request.days)


class _WeatherAgent:
    async def execute(self, context):
        request = context["request"]
        return build_mock_weather(request.start_date, request.days)


class _HotelAgent:
    async def execute(self, context):
        request = context["request"]
        return build_mock_hotels(request.city, request.accommodation, request.budget)


class _PlannerAgent:
    def __init__(self, calls: dict[str, int], *, invalid: bool = False):
        self._calls = calls
        self._invalid = invalid

    async def execute(self, context):
        self._calls["planner"] = self._calls.get("planner", 0) + 1
        plan = await PlannerAgent(use_llm=False).execute(context)
        if self._invalid:
            plan.days.pop()
        return plan


class _FailingAttractionAgent:
    def __init__(self, calls: dict[str, int]):
        self._calls = calls

    async def execute(self, _context):
        self._calls["attraction"] = self._calls.get("attraction", 0) + 1
        raise ConnectionError("temporary attraction failure")


def _registry(calls: dict[str, int], *, fail_attraction: bool = False, invalid_plan: bool = False):
    registry = AgentRegistry()
    registry.register(
        AgentDefinition(
            name="attraction_search",
            agent_class=(lambda: _FailingAttractionAgent(calls)) if fail_attraction else _AttractionAgent,
            retry_policy=RetryPolicy(max_attempts=2, base_delay=0),
            timeout_seconds=5,
        )
    )
    registry.register(
        AgentDefinition(
            name="weather_query",
            agent_class=_WeatherAgent,
            retry_policy=RetryPolicy(max_attempts=1, base_delay=0),
            timeout_seconds=5,
        )
    )
    registry.register(
        AgentDefinition(
            name="hotel_recommendation",
            agent_class=_HotelAgent,
            retry_policy=RetryPolicy(max_attempts=1, base_delay=0),
            timeout_seconds=5,
        )
    )
    registry.register(
        AgentDefinition(
            name="trip_planner",
            agent_class=lambda: _PlannerAgent(calls, invalid=invalid_plan),
            depends_on=["attraction_search", "weather_query", "hotel_recommendation"],
            retry_policy=RetryPolicy(max_attempts=1, base_delay=0),
            timeout_seconds=5,
        )
    )
    return registry


def _settings() -> Settings:
    return Settings(
        ENABLE_EXTERNAL_SERVICES=False,
        LLM_API_KEY="",
        BAIDU_MAP_API_KEY="",
        LANGGRAPH_MEAL_TIMEOUT_SECONDS=5,
    )


def test_checkpoint_dsn_reuses_database_url_with_psycopg_driver():
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://user:p%40ss@db:5432/trips",
        LANGGRAPH_CHECKPOINT_DSN="",
    )
    assert postgres_checkpoint_dsn(settings) == "postgresql://user:p%40ss@db:5432/trips"


def test_graph_completed_checkpoint_does_not_run_planner_twice():
    calls: dict[str, int] = {}
    graph = build_trip_planning_graph(
        InMemorySaver(),
        registry=_registry(calls),
        settings=_settings(),
    )
    config = {"configurable": {"thread_id": "resume-task", "checkpoint_ns": "trip_planning_v1"}}
    first = asyncio.run(graph.ainvoke(_initial_state(_request(), "resume-task"), config))
    second = asyncio.run(graph.ainvoke(None, config))

    assert first["validation_passed"] is True
    assert second["validation_passed"] is True
    assert calls["planner"] == 1


def test_collector_retries_then_uses_existing_fallback_and_reaches_planner():
    calls: dict[str, int] = {}
    graph = build_trip_planning_graph(registry=_registry(calls, fail_attraction=True), settings=_settings())
    result = asyncio.run(graph.ainvoke(_initial_state(_request())))

    attraction_result = next(
        item for item in result["agent_results"] if item["agent_name"] == "attraction_search"
    )
    assert calls["attraction"] == 2
    assert attraction_result["fallback_used"] == "mock"
    assert result["validation_passed"] is True
    assert calls["planner"] == 1


def test_meal_failure_preserves_original_plan_and_continues_validation(monkeypatch):
    from app.orchestration import langgraph_workflow

    async def fail_enrichment(_plan, _service):
        raise TimeoutError("baidu timeout")

    monkeypatch.setattr(langgraph_workflow, "enrich_meals_with_baidu", fail_enrichment)
    calls: dict[str, int] = {}
    graph = build_trip_planning_graph(registry=_registry(calls), settings=_settings())
    result = asyncio.run(graph.ainvoke(_initial_state(_request())))

    assert result["meal_enriched"] is False
    assert result["validation_passed"] is True
    assert any(
        item["agent_name"] == "meal_enrichment"
        and item["fallback_used"] == "preserve_original_plan"
        for item in result["agent_results"]
    )


def test_validate_failure_becomes_serializable_terminal_error():
    calls: dict[str, int] = {}
    graph = build_trip_planning_graph(
        registry=_registry(calls, invalid_plan=True),
        settings=_settings(),
    )
    result = asyncio.run(graph.ainvoke(_initial_state(_request())))

    assert result["validation_passed"] is False
    assert result["terminal_error"]["code"] == "PLAN_QUALITY_FAILED"
    assert result["terminal_error"]["node"] == "validate"


def test_progress_projection_is_order_independent_and_monotonic():
    states = [
        {},
        {"weather_query": []},
        {"weather_query": [], "attraction_search": []},
        {"weather_query": [], "attraction_search": [], "hotel_recommendation": []},
        {
            "weather_query": [],
            "attraction_search": [],
            "hotel_recommendation": [],
            "trip_planner": {},
        },
        {"trip_planner": {}, "meal_enriched": False},
        {"trip_planner": {}, "meal_enriched": True, "validation_passed": True},
    ]
    projections = [progress_from_graph_state(state) for state in states]
    assert [progress for _phase, progress, _message in projections] == [10, 18, 26, 35, 75, 90, 95]


def test_task_progress_does_not_regress_when_late_events_arrive():
    now = datetime.now(timezone.utc)
    task = TripPlanTask(
        task_id="progress-task",
        request_payload={},
        status="running",
        phase="meal_enrichment",
        progress=90,
        message="餐饮信息处理完成",
        phase_timings={},
        queued_at=now,
        phase_started_at=now,
        updated_at=now,
    )
    transition_task(
        task,
        status="running",
        phase="collecting_context",
        progress=18,
        message="延迟到达的采集事件",
        now=now,
    )
    assert task.phase == "meal_enrichment"
    assert task.progress == 90
    assert task.message == "餐饮信息处理完成"


def test_task_creation_snapshots_langgraph_backend_and_versions(monkeypatch):
    settings = Settings(
        ORCHESTRATION_BACKEND="langgraph",
        LANGGRAPH_WORKFLOW_VERSION="trip_planning_v1",
        LANGGRAPH_STATE_SCHEMA_VERSION=3,
    )
    monkeypatch.setattr(task_service, "get_settings", lambda: settings)
    user_id = uuid.uuid4()

    class FakeDB:
        def __init__(self):
            self.added = []

        async def scalar(self, _statement):
            return user_id

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            return None

    db = FakeDB()
    response = asyncio.run(TripPlanTaskService(db).create_task(_request()))
    created = db.added[0]

    assert response.status == "queued"
    assert created.orchestration_backend == "langgraph"
    assert created.workflow_version == "trip_planning_v1"
    assert created.state_schema_version == 3
    assert created.result_plan_id is None


def test_prepare_reuses_the_task_plan_id_and_failure_terminates_both_rows(monkeypatch):
    request = _request()
    user = SimpleNamespace(id=uuid.uuid4(), session_token=request.session_id)
    now = datetime.now(timezone.utc)
    task = TripPlanTask(
        id=uuid.uuid4(),
        task_id="durable-task",
        user_id=user.id,
        request_payload=request.model_dump(mode="json"),
        status="running",
        phase="preparing",
        progress=5,
        message="正在准备",
        phase_timings={},
        result_plan_id=None,
        retry_count=0,
        orchestration_backend="langgraph",
        workflow_version="trip_planning_v1",
        state_schema_version=1,
        recovery_state="none",
        queued_at=now,
        updated_at=now,
    )

    class FakeShortTermMemory:
        def __init__(self, *_args, **_kwargs):
            pass

        async def restore_from_db(self, _db):
            return None

        def get_context(self):
            return []

    class FakeRecall:
        def __init__(self, _memory):
            pass

        async def recall(self, *_args):
            return {"preferred_categories": ["历史文化"]}

    class FakeDB:
        def __init__(self):
            self.plan = None
            self.commit_calls = 0

        async def scalar(self, statement):
            statement_text = str(statement)
            if "FROM trip_plan_tasks" in statement_text:
                return task
            if "FROM trip_plans" in statement_text:
                return self.plan
            if "FROM users" in statement_text:
                return user
            return None

        def add(self, value):
            if isinstance(value, state_service.TripPlanModel):
                self.plan = value

        async def flush(self):
            return None

        async def commit(self):
            self.commit_calls += 1

    monkeypatch.setattr(state_service, "LongTermMemory", lambda: object())
    monkeypatch.setattr(state_service, "MemoryRecall", FakeRecall)
    monkeypatch.setattr(state_service, "ShortTermMemory", FakeShortTermMemory)
    db = FakeDB()
    service = StateService(db)

    first = asyncio.run(service.prepare_planning_context(request, task_id=task.task_id))
    second = asyncio.run(service.prepare_planning_context(request, task_id=task.task_id))

    assert first["plan_id"] == second["plan_id"] == str(task.result_plan_id)
    assert db.commit_calls == 2

    asyncio.run(
        service.fail_planning_run(
            task.task_id,
            first["plan_id"],
            {"code": "TEST_FAILURE", "message": "failed after prepare"},
        )
    )
    assert task.status == "failed"
    assert task.error_code == "TEST_FAILURE"
    assert db.plan.status == "failed"
    assert db.commit_calls == 3


def test_heartbeat_is_an_independent_task_and_is_cancelled_after_workflow(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_heartbeat(_task_id, _lease_owner):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async def workflow():
        await started.wait()
        return "done"

    monkeypatch.setattr(worker, "_heartbeat_loop", fake_heartbeat)

    async def run():
        result = await worker._run_with_heartbeat(workflow(), "task", "lease")
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        return result

    assert asyncio.run(run()) == "done"
