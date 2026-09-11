from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from langgraph.checkpoint.memory import InMemorySaver

from app.agents.trip_planner import PlannerAgent
from app.config import Settings
from app.models.schemas import CritiqueResult, CritiqueScores, TripPlanRequest
from app.orchestration.base import AgentDefinition, RetryPolicy
from app.orchestration.checkpoint import postgres_checkpoint_dsn
from app.orchestration.langgraph_workflow import build_trip_planning_graph
from app.orchestration.registry import AgentRegistry
from app.services.mock_data import build_mock_attractions, build_mock_hotels, build_mock_weather
from app.services import planning_workflow_service, task_service
from app.services.planning_workflow_service import PlanningWorkflowService
from app.services.task_service import TripPlanTaskService, progress_from_graph_state, transition_task
from app.services.task_service import active_phase_progress
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
        plan = await PlannerAgent().execute(context)
        if self._invalid:
            plan.days.pop()
        return plan


class _TimeoutOncePlannerAgent:
    def __init__(self, calls: dict[str, int]):
        self._calls = calls

    async def execute(self, context):
        self._calls["planner"] = self._calls.get("planner", 0) + 1
        if self._calls["planner"] == 1:
            await asyncio.sleep(0.08)
        return await PlannerAgent().execute(context)


class _StagedPlannerAgent:
    enable_critique = True
    max_refinement_rounds = 1
    min_pass_score = 7.0
    llm_service = object()

    def __init__(
        self,
        calls: dict[str, int],
        *,
        fail_refine: bool = False,
        refine_delay: float = 0,
    ):
        self._calls = calls
        self._fail_refine = fail_refine
        self._refine_delay = refine_delay

    async def execute(self, context):
        self._calls["planner"] = self._calls.get("planner", 0) + 1
        return await PlannerAgent().execute(context)

    async def refine_once(self, _context, previous_plan, _critique):
        self._calls["refine"] = self._calls.get("refine", 0) + 1
        if self._refine_delay:
            await asyncio.sleep(self._refine_delay)
        if self._fail_refine:
            raise TimeoutError("refine timeout")
        return previous_plan


class _FailingAttractionAgent:
    def __init__(self, calls: dict[str, int]):
        self._calls = calls

    async def execute(self, _context):
        self._calls["attraction"] = self._calls.get("attraction", 0) + 1
        raise ConnectionError("temporary attraction failure")


def _registry(
    calls: dict[str, int],
    *,
    fail_attraction: bool = False,
    invalid_plan: bool = False,
    planner_factory=None,
    planner_attempts: int = 1,
    planner_timeout: float = 5,
):
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
            agent_class=planner_factory or (lambda: _PlannerAgent(calls, invalid=invalid_plan)),
            retry_policy=RetryPolicy(max_attempts=planner_attempts, base_delay=0),
            timeout_seconds=planner_timeout,
        )
    )
    return registry


def _settings() -> Settings:
    return Settings(
        ENABLE_EXTERNAL_SERVICES=False,
        LLM_API_KEY="",
        BAIDU_MAP_API_KEY="",
        LANGGRAPH_MEAL_TIMEOUT_SECONDS=5,
        PLANNER_DRAFT_TIMEOUT_SECONDS=90,
        PLANNER_CRITIQUE_TIMEOUT_SECONDS=35,
        PLANNER_REFINE_TIMEOUT_SECONDS=55,
    )


def test_checkpoint_dsn_reuses_database_url_with_psycopg_driver():
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://user:p%40ss@db:5432/trips",
        LANGGRAPH_CHECKPOINT_DSN="",
    )
    assert postgres_checkpoint_dsn(settings) == "postgresql://user:p%40ss@db:5432/trips"


def test_graph_completed_checkpoint_does_not_run_planner_twice(monkeypatch):
    calls: dict[str, int] = {}
    graph = build_trip_planning_graph(
        InMemorySaver(),
        registry=_registry(calls),
        settings=_settings(),
    )
    config = {"configurable": {"thread_id": "resume-task"}}
    first = asyncio.run(graph.ainvoke(_initial_state(_request(), "resume-task"), config))

    async def ignore_progress(*_args, **_kwargs):
        return None

    async def finish(_task_id, _lease_owner, _plan_id, values):
        return values

    monkeypatch.setattr(planning_workflow_service, "reconcile_task_progress", ignore_progress)
    service = PlanningWorkflowService(None, graph)
    monkeypatch.setattr(service, "_finish", finish)
    second = asyncio.run(
        service.run(
            task_id="resume-task",
            lease_owner="test-worker",
            request_payload=_request().model_dump(mode="json"),
            workflow_version="trip_planning_v1",
            state_schema_version=1,
        )
    )

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


def test_planner_timeout_is_retried_and_attempt_diagnostics_are_preserved():
    calls: dict[str, int] = {}
    graph = build_trip_planning_graph(
        registry=_registry(
            calls,
            planner_factory=lambda: _TimeoutOncePlannerAgent(calls),
            planner_attempts=2,
            planner_timeout=0.05,
        ),
        settings=_settings(),
    )

    result = asyncio.run(graph.ainvoke(_initial_state(_request())))
    planner_result = next(
        item for item in result["agent_results"] if item["agent_name"] == "trip_planner"
    )

    assert calls["planner"] == 2
    assert planner_result["attempt"] == 2
    assert planner_result["fallback_used"] is None
    assert planner_result["error_type"] == "TimeoutError"
    assert planner_result["error_message"] == "TimeoutError"
    assert [item["status"] for item in planner_result["attempt_history"]] == [
        "failed",
        "completed",
    ]


def test_zero_draft_timeout_disables_outer_planner_cutoff():
    calls: dict[str, int] = {}
    settings = _settings()
    settings.planner_draft_timeout_seconds = 0
    graph = build_trip_planning_graph(
        registry=_registry(
            calls,
            planner_factory=lambda: _TimeoutOncePlannerAgent(calls),
            planner_attempts=2,
            planner_timeout=0.01,
        ),
        settings=settings,
    )

    result = asyncio.run(graph.ainvoke(_initial_state(_request())))

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


def test_critique_failure_preserves_checkpointed_draft(monkeypatch):
    from app.orchestration import langgraph_workflow

    async def fail_critique(*_args, **_kwargs):
        raise TimeoutError("critic timeout")

    monkeypatch.setattr(langgraph_workflow.PlanCritic, "evaluate", fail_critique)
    calls: dict[str, int] = {}
    graph = build_trip_planning_graph(
        registry=_registry(calls, planner_factory=lambda: _StagedPlannerAgent(calls)),
        settings=_settings(),
    )
    result = asyncio.run(graph.ainvoke(_initial_state(_request())))

    assert result["validation_passed"] is True
    assert result["planner_draft_ready"] is True
    assert result["plan_critique_status"] == "unavailable_preserved"
    assert any(
        item["agent_name"] == "trip_planner_critique"
        and item["fallback_used"] == "preserve_valid_draft"
        for item in result["agent_results"]
    )


def test_refine_failure_preserves_checkpointed_draft(monkeypatch):
    from app.orchestration import langgraph_workflow

    async def request_revision(*_args, **_kwargs):
        return CritiqueResult(
            scores=CritiqueScores(
                attraction_diversity=6,
                description_quality=6,
                weather_compatibility=6,
                schedule_feasibility=6,
                budget_realism=6,
            ),
            issues=[],
            suggestions=["调整安排"],
            needs_revision=True,
            revision_summary="需要修订",
        )

    monkeypatch.setattr(langgraph_workflow.PlanCritic, "evaluate", request_revision)
    calls: dict[str, int] = {}
    graph = build_trip_planning_graph(
        registry=_registry(
            calls,
            planner_factory=lambda: _StagedPlannerAgent(calls, fail_refine=True),
        ),
        settings=_settings(),
    )
    result = asyncio.run(graph.ainvoke(_initial_state(_request())))

    assert calls["refine"] == 1
    assert result["validation_passed"] is True
    assert result["plan_critique_status"] == "refinement_failed_preserved"
    assert any(
        item["agent_name"] == "trip_planner_refine"
        and item["fallback_used"] == "preserve_valid_draft"
        for item in result["agent_results"]
    )


def test_zero_refine_timeout_waits_for_natural_completion(monkeypatch):
    from app.orchestration import langgraph_workflow

    async def request_revision(*_args, **_kwargs):
        return CritiqueResult(
            scores=CritiqueScores(
                attraction_diversity=6,
                description_quality=6,
                weather_compatibility=6,
                schedule_feasibility=6,
                budget_realism=6,
            ),
            issues=[],
            suggestions=["调整安排"],
            needs_revision=True,
            revision_summary="需要修订",
        )

    monkeypatch.setattr(langgraph_workflow.PlanCritic, "evaluate", request_revision)
    calls: dict[str, int] = {}
    settings = _settings()
    settings.planner_refine_timeout_seconds = 0
    graph = build_trip_planning_graph(
        registry=_registry(
            calls,
            planner_factory=lambda: _StagedPlannerAgent(calls, refine_delay=0.03),
        ),
        settings=settings,
    )

    result = asyncio.run(graph.ainvoke(_initial_state(_request())))

    assert result["validation_passed"] is True
    assert result["plan_critique_status"] == "refined"
    assert calls["refine"] == 1


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
    assert [progress for _phase, progress, _message in projections] == [10, 18, 26, 40, 75, 90, 95]
    assert projections[3][0] == "draft_planning"


def test_progress_projection_exposes_checkpointed_planner_subphases():
    states = [
        {"attraction_search": [], "weather_query": [], "hotel_recommendation": []},
        {"planner_draft_ready": True, "trip_planner": {}},
        {
            "planner_draft_ready": True,
            "trip_planner": {},
            "plan_critique_status": "needs_revision",
        },
        {"planner_draft_ready": True, "trip_planner": {}, "planner_finalized": True},
    ]
    projections = [progress_from_graph_state(state) for state in states]
    assert [(phase, progress) for phase, progress, _message in projections] == [
        ("draft_planning", 40),
        ("critiquing", 60),
        ("refining", 68),
        ("llm_planning", 75),
    ]


def test_active_planner_progress_moves_with_elapsed_time_without_claiming_completion():
    start = active_phase_progress(
        "llm_planning",
        0,
        planner_attempt_timeout_seconds=150,
        planner_max_attempts=2,
    )
    middle = active_phase_progress(
        "llm_planning",
        151,
        planner_attempt_timeout_seconds=150,
        planner_max_attempts=2,
    )
    exhausted = active_phase_progress(
        "llm_planning",
        999,
        planner_attempt_timeout_seconds=150,
        planner_max_attempts=2,
    )

    assert start is not None and start[0] == 40
    assert middle is not None and 40 < middle[0] < 74
    assert exhausted is not None and exhausted[0] == 74
    assert active_phase_progress(
        "meal_enrichment",
        30,
        planner_attempt_timeout_seconds=150,
        planner_max_attempts=2,
    ) is None


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


def test_task_creation_snapshots_workflow_versions(monkeypatch):
    settings = Settings(
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
