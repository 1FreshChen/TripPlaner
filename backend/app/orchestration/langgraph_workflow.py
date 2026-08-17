from __future__ import annotations

import asyncio
import operator
import time
from typing import Annotated, Any, TypedDict

from langgraph.errors import NodeError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, RetryPolicy as LangGraphRetryPolicy

from app.config import Settings, get_settings
from app.models.schemas import Attraction, Hotel, TripPlan, TripPlanRequest, WeatherInfo
from app.orchestration.bootstrap import bootstrap_orchestration, build_default_fallback_chain
from app.orchestration.registry import AgentRegistry
from app.services.baidu_map_service import BaiduMapService
from app.services.meal_enrichment import enrich_meals_with_baidu
from app.services.plan_quality import PlanQualityError, validate_trip_plan_for_request


COLLECTOR_NODES = ("attraction_search", "weather_query", "hotel_recommendation")


class PlanningState(TypedDict, total=False):
    task_id: str | None
    plan_id: str
    request: dict[str, Any]
    memory_context: dict[str, Any]
    conversation_context: list[dict[str, str]]
    workflow_version: str
    state_schema_version: int
    attraction_search: list[dict[str, Any]]
    weather_query: list[dict[str, Any]]
    hotel_recommendation: list[dict[str, Any]]
    trip_planner: dict[str, Any]
    trip_planner_token_usage: dict[str, Any] | None
    trip_planner_tool_calls: list[dict[str, Any]]
    plan_critique_events: list[dict[str, Any]]
    agent_results: Annotated[list[dict[str, Any]], operator.add]
    meal_enriched: bool
    validation_passed: bool
    terminal_error: dict[str, Any] | None


def _agent_context(state: PlanningState) -> dict[str, Any]:
    context: dict[str, Any] = {
        "request": TripPlanRequest.model_validate(state["request"]),
        "memory_context": state.get("memory_context") or {},
        "conversation_context": state.get("conversation_context") or [],
    }
    model_by_node = {
        "attraction_search": Attraction,
        "weather_query": WeatherInfo,
        "hotel_recommendation": Hotel,
    }
    for node_name, model in model_by_node.items():
        if node_name in state:
            context[node_name] = [model.model_validate(item) for item in state[node_name]]
    return context


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dataclass_fields__"):
        return {
            key: _json_value(getattr(value, key))
            for key in value.__dataclass_fields__
        }
    if hasattr(value, "value"):
        return _json_value(value.value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _result(
    agent_name: str,
    *,
    status: str,
    started_at: float,
    error: BaseException | None = None,
    fallback: str | None = None,
    attempt: int = 1,
) -> dict[str, Any]:
    return {
        "agent_name": agent_name,
        "status": status,
        "attempt": attempt,
        "error_message": str(error) if error else None,
        "fallback_used": fallback,
        "duration_ms": round((time.monotonic() - started_at) * 1000, 3),
    }


def _agent_node(agent_name: str, registry: AgentRegistry):
    async def run(state: PlanningState) -> dict[str, Any]:
        started_at = time.monotonic()
        context = _agent_context(state)
        definition = registry.get(agent_name)
        policy = definition.retry_policy
        max_attempts = policy.max_attempts if policy is not None else 1
        agent = definition.create_agent()
        last_error: Exception | None = None
        attempt = 0
        output = None
        for attempt in range(1, max_attempts + 1):
            try:
                output = await asyncio.wait_for(
                    agent.execute(context),
                    timeout=definition.timeout_seconds,
                )
                if agent_name == "trip_planner" and context.get("trip_planner_quality_failed"):
                    raise PlanQualityError("generated plan did not pass quality review")
                break
            except Exception as exc:
                last_error = exc
                should_retry = (
                    attempt < max_attempts
                    and policy is not None
                    and isinstance(exc, policy.retryable_exceptions)
                )
                if not should_retry:
                    output = None
                    break
                delay = policy.delay_for_attempt(attempt - 1)
                if delay > 0:
                    await asyncio.sleep(delay)

        fallback_used: str | None = None
        if output is None:
            chain = build_default_fallback_chain()
            output = await chain.execute(agent_name, context, last_error=last_error)
            if output is None:
                raise last_error or RuntimeError(f"{agent_name} did not produce output")
            fallback_used = chain.last_level_used.value if chain.last_level_used else None
        update: dict[str, Any] = {
            agent_name: _json_value(output),
            "agent_results": [
                _result(
                    agent_name,
                    status="completed",
                    started_at=started_at,
                    error=last_error,
                    fallback=fallback_used,
                    attempt=attempt,
                )
            ],
        }
        if agent_name == "trip_planner":
            token_usage = context.get("trip_planner_token_usage") if fallback_used is None else None
            tool_calls = context.get("trip_planner_tool_calls") if fallback_used is None else []
            critique_events = context.get("plan_critique_events") if fallback_used is None else []
            update.update(
                {
                    "trip_planner_token_usage": _json_value(token_usage),
                    "trip_planner_tool_calls": _json_value(tool_calls or []),
                    "plan_critique_events": _json_value(critique_events or []),
                }
            )
        return update

    return run


def _meal_node(settings: Settings):
    async def run(state: PlanningState) -> dict[str, Any]:
        plan = TripPlan.model_validate(state["trip_planner"])
        enriched = await enrich_meals_with_baidu(plan, BaiduMapService(settings.baidu_map_api_key))
        return {"trip_planner": enriched.model_dump(mode="json"), "meal_enriched": True}

    return run


async def _meal_fallback(state: PlanningState, error: NodeError) -> Command:
    return Command(
        update={
            "meal_enriched": False,
            "agent_results": [
                {
                    "agent_name": "meal_enrichment",
                    "status": "completed",
                    "attempt": 1,
                    "error_message": str(error.error),
                    "fallback_used": "preserve_original_plan",
                    "duration_ms": 0.0,
                }
            ],
        },
        goto="validate",
    )


async def _validate_node(state: PlanningState) -> dict[str, Any]:
    validate_trip_plan_for_request(
        TripPlan.model_validate(state["trip_planner"]),
        TripPlanRequest.model_validate(state["request"]),
        critique_events=state.get("plan_critique_events") or [],
    )
    return {"validation_passed": True, "terminal_error": None}


async def _validate_error(_state: PlanningState, error: NodeError) -> Command:
    return Command(
        update={
            "validation_passed": False,
            "terminal_error": {
                "code": "PLAN_QUALITY_FAILED",
                "message": str(error.error),
                "node": "validate",
            },
        },
        goto=END,
    )


def build_trip_planning_graph(
    checkpointer=None,
    *,
    registry: AgentRegistry | None = None,
    settings: Settings | None = None,
):
    """Compile the six business nodes; tiny barrier nodes preserve fan-in after fallback."""
    settings = settings or get_settings()
    registry = registry or bootstrap_orchestration()
    builder = StateGraph(PlanningState)

    for agent_name in COLLECTOR_NODES:
        builder.add_node(
            agent_name,
            _agent_node(agent_name, registry),
        )
        builder.add_edge(START, agent_name)

    builder.add_node(
        "trip_planner",
        _agent_node("trip_planner", registry),
    )
    builder.add_node(
        "meal_enrichment",
        _meal_node(settings),
        retry_policy=LangGraphRetryPolicy(max_attempts=1),
        error_handler=_meal_fallback,
        timeout=settings.langgraph_meal_timeout_seconds,
    )
    builder.add_node(
        "validate",
        _validate_node,
        retry_policy=LangGraphRetryPolicy(max_attempts=1),
        error_handler=_validate_error,
    )
    builder.add_edge(list(COLLECTOR_NODES), "trip_planner")
    builder.add_edge("trip_planner", "meal_enrichment")
    builder.add_edge("meal_enrichment", "validate")
    builder.add_edge("validate", END)
    return builder.compile(checkpointer=checkpointer, name=settings.langgraph_workflow_version)
