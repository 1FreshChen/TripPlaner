from __future__ import annotations

import asyncio
import logging
import operator
import time
from typing import Annotated, Any, TypedDict

from langgraph.errors import NodeError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, RetryPolicy as LangGraphRetryPolicy

from app.agents.critic import PlanCritic
from app.agents.planner.validation import validate_planner_output
from app.config import Settings, get_settings
from app.models.schemas import Attraction, CritiqueResult, Hotel, TripPlan, TripPlanRequest, WeatherInfo
from app.orchestration.bootstrap import bootstrap_orchestration, build_default_fallback_chain
from app.orchestration.registry import AgentRegistry
from app.services.baidu_map_service import BaiduMapService
from app.services.attraction_image_service import enrich_attraction_images
from app.services.amap_mcp_service import get_amap_mcp_service
from app.services.meal_enrichment import enrich_meals_with_baidu
from app.services.plan_quality import PlanQualityError
from app.services.unsplash_service import UnsplashService


logger = logging.getLogger(__name__)


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
    trip_planner_diagnostics: dict[str, Any] | None
    planner_attraction_pois: list[dict[str, Any]]
    plan_critique_events: list[dict[str, Any]]
    planner_draft_ready: bool
    planner_draft_fallback_used: bool
    plan_critique: dict[str, Any] | None
    plan_critique_status: str
    planner_refinement_rounds: int
    planner_model_requests_used: int
    planner_finalized: bool
    agent_results: Annotated[list[dict[str, Any]], operator.add]
    meal_enriched: bool
    validation_passed: bool
    terminal_error: dict[str, Any] | None


def _agent_context(state: PlanningState) -> dict[str, Any]:
    context: dict[str, Any] = {
        "request": TripPlanRequest.model_validate(state["request"]),
        "memory_context": state.get("memory_context") or {},
        "conversation_context": state.get("conversation_context") or [],
        "planner_attraction_pois": state.get("planner_attraction_pois") or [],
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
    attempt_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    error_message = (str(error).strip() or type(error).__name__) if error else None
    error_stage = None
    if attempt_history:
        failed_attempts = [item for item in attempt_history if item.get("status") == "failed"]
        if failed_attempts:
            error_stage = failed_attempts[-1].get("stage")
    return {
        "agent_name": agent_name,
        "status": status,
        "attempt": attempt,
        "error_type": type(error).__name__ if error else None,
        "error_message": error_message,
        "error_stage": error_stage,
        "fallback_used": fallback,
        "attempt_history": attempt_history or [],
        "duration_ms": round((time.monotonic() - started_at) * 1000, 3),
    }


def _agent_node(
    agent_name: str,
    registry: AgentRegistry,
    *,
    disable_critique: bool = False,
    timeout_seconds: float | None = None,
    max_attempts: int | None = None,
    global_request_limit: int | None = None,
):
    async def run(state: PlanningState) -> dict[str, Any]:
        started_at = time.monotonic()
        context = _agent_context(state)
        definition = registry.get(agent_name)
        policy = definition.retry_policy
        attempts_limit = max_attempts or (policy.max_attempts if policy is not None else 1)
        agent = definition.create_agent()
        if disable_critique and hasattr(agent, "enable_critique"):
            agent.enable_critique = False
        last_error: Exception | None = None
        attempt = 0
        output = None
        attempt_history: list[dict[str, Any]] = []
        model_requests_used = int(state.get("planner_model_requests_used") or 0)
        for attempt in range(1, attempts_limit + 1):
            attempt_started_at = time.monotonic()
            if agent_name == "trip_planner":
                context.pop("trip_planner_quality_failed", None)
                if global_request_limit is not None:
                    context["planner_request_limit"] = max(
                        global_request_limit - model_requests_used,
                        1,
                    )
            try:
                if timeout_seconds is not None and timeout_seconds <= 0:
                    output = await agent.execute(context)
                else:
                    effective_timeout = (
                        min(timeout_seconds, definition.timeout_seconds)
                        if timeout_seconds is not None
                        else definition.timeout_seconds
                    )
                    output = await asyncio.wait_for(
                        agent.execute(context),
                        timeout=effective_timeout,
                    )
                if agent_name == "trip_planner" and context.get("trip_planner_quality_failed"):
                    raise PlanQualityError("generated plan did not pass quality review")
                attempt_history.append(
                    {
                        "attempt": attempt,
                        "status": "completed",
                        "stage": (context.get("trip_planner_diagnostics") or {}).get("stage"),
                        "duration_ms": round((time.monotonic() - attempt_started_at) * 1000, 3),
                    }
                )
                break
            except Exception as exc:
                last_error = exc
                output = None
                diagnostics = context.get("trip_planner_diagnostics") or {}
                error_message = str(exc).strip() or type(exc).__name__
                attempt_history.append(
                    {
                        "attempt": attempt,
                        "status": "failed",
                        "stage": diagnostics.get("stage") or "agent_execute",
                        "error_type": type(exc).__name__,
                        "error_message": error_message,
                        "duration_ms": round((time.monotonic() - attempt_started_at) * 1000, 3),
                        "diagnostics": _json_value(diagnostics),
                    }
                )
                should_retry = (
                    attempt < attempts_limit
                    and policy is not None
                    and isinstance(exc, policy.retryable_exceptions)
                )
                if agent_name == "trip_planner" and global_request_limit is not None:
                    attempt_requests = int(diagnostics.get("model_requests") or 1)
                    model_requests_used += attempt_requests
                    should_retry = should_retry and model_requests_used < global_request_limit
                logger.warning(
                    "Agent attempt failed: agent=%s attempt=%d/%d stage=%s "
                    "error_type=%s error=%s retry=%s",
                    agent_name,
                    attempt,
                    attempts_limit,
                    attempt_history[-1]["stage"],
                    type(exc).__name__,
                    error_message,
                    should_retry,
                )
                if not should_retry:
                    output = None
                    break
                delay = policy.delay_for_attempt(attempt - 1)
                if delay > 0:
                    await asyncio.sleep(delay)

        if agent_name == "trip_planner" and output is not None:
            diagnostics = context.get("trip_planner_diagnostics") or {}
            model_requests_used += int(diagnostics.get("model_requests") or 1)

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
                    attempt_history=attempt_history,
                )
            ],
        }
        if agent_name == "trip_planner":
            token_usage = context.get("trip_planner_token_usage")
            tool_calls = context.get("trip_planner_tool_calls")
            critique_events = context.get("plan_critique_events")
            update.update(
                {
                    "trip_planner_token_usage": _json_value(token_usage),
                    "trip_planner_tool_calls": _json_value(tool_calls or []),
                    "plan_critique_events": _json_value(critique_events or []),
                    "trip_planner_diagnostics": _json_value(
                        context.get("trip_planner_diagnostics") or {}
                    ),
                    "planner_attraction_pois": _json_value(
                        context.get("planner_attraction_pois") or []
                    ),
                    "planner_draft_ready": True,
                    "planner_draft_fallback_used": fallback_used is not None,
                    "planner_model_requests_used": model_requests_used,
                }
            )
        return update

    return run


def _critique_event(critique: CritiqueResult, round_index: int = 0) -> dict[str, Any]:
    return {
        "event_type": "plan_critique",
        "severity": "warning" if critique.needs_revision else "info",
        "details": {
            "round": round_index,
            "scores": critique.scores.model_dump(mode="json"),
            "average_score": critique.average_score,
            "needs_revision": critique.needs_revision,
            "issues_count": len(critique.issues),
            "revision_summary": critique.revision_summary,
        },
    }


def _critique_node(registry: AgentRegistry, settings: Settings):
    async def run(state: PlanningState) -> dict[str, Any]:
        started_at = time.monotonic()
        requests_used = int(state.get("planner_model_requests_used") or 0)
        agent = registry.get("trip_planner").create_agent()
        critique_supported = (
            not state.get("planner_draft_fallback_used")
            and settings.enable_plan_critique
            and getattr(agent, "enable_critique", False)
            and hasattr(agent, "llm_service")
        )
        if not critique_supported or requests_used >= settings.planner_global_request_limit:
            reason = (
                "draft_fallback"
                if state.get("planner_draft_fallback_used")
                else "disabled_or_request_budget_exhausted"
            )
            return {
                "plan_critique_status": "skipped",
                "planner_finalized": True,
                "agent_results": [
                    _result(
                        "trip_planner_critique",
                        status="completed",
                        started_at=started_at,
                        fallback=f"preserve_draft:{reason}",
                    )
                ],
            }

        request = TripPlanRequest.model_validate(state["request"])
        plan = TripPlan.model_validate(state["trip_planner"])
        try:
            critique_timeout = (
                None
                if settings.planner_critique_timeout_seconds <= 0
                else settings.planner_critique_timeout_seconds
            )
            critique_call = PlanCritic(agent.llm_service).evaluate(
                plan,
                request,
                strict=True,
                timeout_seconds=critique_timeout,
            )
            if settings.planner_critique_timeout_seconds <= 0:
                critique = await critique_call
            else:
                critique = await asyncio.wait_for(
                    critique_call,
                    timeout=settings.planner_critique_timeout_seconds,
                )
        except Exception as exc:
            logger.warning(
                "Plan critique failed; preserving valid draft: error_type=%s error=%s",
                type(exc).__name__,
                str(exc).strip() or type(exc).__name__,
            )
            return {
                "plan_critique_status": "unavailable_preserved",
                "planner_model_requests_used": requests_used + 1,
                "planner_finalized": True,
                "agent_results": [
                    _result(
                        "trip_planner_critique",
                        status="completed",
                        started_at=started_at,
                        error=exc,
                        fallback="preserve_valid_draft",
                    )
                ],
            }

        event = _critique_event(critique, int(state.get("planner_refinement_rounds") or 0))
        needs_revision = critique.needs_revision or critique.average_score < getattr(
            agent,
            "min_pass_score",
            settings.min_pass_score,
        )
        can_refine = (
            needs_revision
            and getattr(agent, "max_refinement_rounds", 0)
            > int(state.get("planner_refinement_rounds") or 0)
            and requests_used + 1 < settings.planner_global_request_limit
            and hasattr(agent, "refine_once")
        )
        critique_status = (
            "needs_revision"
            if can_refine
            else "revision_skipped_preserved"
            if needs_revision
            else "passed"
        )
        return {
            "plan_critique": critique.model_dump(mode="json"),
            "plan_critique_events": [*(state.get("plan_critique_events") or []), event],
            "plan_critique_status": critique_status,
            "planner_model_requests_used": requests_used + 1,
            "planner_finalized": not can_refine,
            "agent_results": [
                _result("trip_planner_critique", status="completed", started_at=started_at)
            ],
        }

    return run


def _route_after_critique(state: PlanningState) -> str:
    if state.get("plan_critique_status") == "needs_revision":
        return "refine_plan"
    return "finalize_plan"


def _merge_usage(primary: dict[str, Any] | None, extra: Any) -> dict[str, Any] | None:
    if extra is None:
        return primary
    extra_payload = _json_value(extra)
    if not isinstance(extra_payload, dict):
        return primary
    merged = dict(primary or {})
    merged.setdefault("model", extra_payload.get("model"))
    merged.setdefault("provider", extra_payload.get("provider"))
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        merged[key] = int(merged.get(key) or 0) + int(extra_payload.get(key) or 0)
    return merged


def _refine_node(registry: AgentRegistry, settings: Settings):
    async def run(state: PlanningState) -> dict[str, Any]:
        started_at = time.monotonic()
        requests_used = int(state.get("planner_model_requests_used") or 0)
        previous_plan = TripPlan.model_validate(state["trip_planner"])
        critique = CritiqueResult.model_validate(state["plan_critique"])
        agent = registry.get("trip_planner").create_agent()
        context = _agent_context(state)
        context["planner_request_limit"] = max(
            settings.planner_global_request_limit - requests_used,
            1,
        )
        try:
            refine_call = agent.refine_once(context, previous_plan, critique)
            if settings.planner_refine_timeout_seconds <= 0:
                revised = await refine_call
            else:
                revised = await asyncio.wait_for(
                    refine_call,
                    timeout=settings.planner_refine_timeout_seconds,
                )
            validate_planner_output(revised, context["request"])
        except Exception as exc:
            diagnostics = context.get("trip_planner_diagnostics") or {}
            refinement_requests = int(diagnostics.get("model_requests") or 1)
            logger.warning(
                "Plan refinement failed; preserving valid draft: error_type=%s error=%s",
                type(exc).__name__,
                str(exc).strip() or type(exc).__name__,
            )
            return {
                "plan_critique_status": "refinement_failed_preserved",
                "planner_model_requests_used": requests_used + refinement_requests,
                "trip_planner_diagnostics": _json_value(diagnostics),
                "planner_finalized": True,
                "agent_results": [
                    _result(
                        "trip_planner_refine",
                        status="completed",
                        started_at=started_at,
                        error=exc,
                        fallback="preserve_valid_draft",
                    )
                ],
            }

        diagnostics = context.get("trip_planner_diagnostics") or {}
        refinement_requests = int(diagnostics.get("model_requests") or 1)
        return {
            "trip_planner": revised.model_dump(mode="json"),
            "trip_planner_token_usage": _merge_usage(
                state.get("trip_planner_token_usage"),
                context.get("trip_planner_token_usage"),
            ),
            "trip_planner_tool_calls": [
                *(state.get("trip_planner_tool_calls") or []),
                *(_json_value(context.get("trip_planner_tool_calls") or [])),
            ],
            "trip_planner_diagnostics": _json_value(diagnostics),
            "plan_critique_status": "refined",
            "planner_refinement_rounds": int(state.get("planner_refinement_rounds") or 0) + 1,
            "planner_model_requests_used": requests_used + refinement_requests,
            "planner_finalized": True,
            "agent_results": [
                _result("trip_planner_refine", status="completed", started_at=started_at)
            ],
        }

    return run


def _finalize_node(settings: Settings):
    async def run(state: PlanningState) -> dict[str, Any]:
        # POI verification is strict whenever real external services are enabled.
        # Image lookup remains best-effort and cannot fail an otherwise verified plan.
        plan = TripPlan.model_validate(state["trip_planner"])
        source_attractions = [
            Attraction.model_validate(item) for item in state.get("attraction_search", [])
        ]
        amap_service = get_amap_mcp_service()
        request = TripPlanRequest.model_validate(state["request"])
        source_attractions.extend(
            attraction
            for attraction in (
                amap_service.poi_to_attraction(poi, request.preferences)
                for poi in state.get("planner_attraction_pois", [])
            )
            if attraction is not None
        )
        enriched = await enrich_attraction_images(
            plan,
            source_attractions,
            UnsplashService(
                settings.unsplash_access_key if settings.enable_external_services else ""
            ),
            amap_service=amap_service,
            preferences=request.preferences,
            require_verified=settings.enable_external_services,
        )
        return {
            "trip_planner": enriched.model_dump(mode="json"),
            "planner_finalized": True,
        }

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
    critique_events = (
        state.get("plan_critique_events") or []
        if state.get("plan_critique_status") == "passed"
        else []
    )
    validate_planner_output(
        TripPlan.model_validate(state["trip_planner"]),
        TripPlanRequest.model_validate(state["request"]),
        critique_events=critique_events,
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
    """Compile a checkpointed draft/critique/refine planning workflow."""
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
        "draft_plan",
        _agent_node(
            "trip_planner",
            registry,
            disable_critique=True,
            timeout_seconds=settings.planner_draft_timeout_seconds,
            max_attempts=settings.planner_draft_max_attempts,
            global_request_limit=settings.planner_global_request_limit,
        ),
    )
    builder.add_node("critique_plan", _critique_node(registry, settings))
    builder.add_node("refine_plan", _refine_node(registry, settings))
    builder.add_node("finalize_plan", _finalize_node(settings))
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
    builder.add_edge(list(COLLECTOR_NODES), "draft_plan")
    builder.add_edge("draft_plan", "critique_plan")
    builder.add_conditional_edges(
        "critique_plan",
        _route_after_critique,
        {"refine_plan": "refine_plan", "finalize_plan": "finalize_plan"},
    )
    builder.add_edge("refine_plan", "finalize_plan")
    builder.add_edge("finalize_plan", "meal_enrichment")
    builder.add_edge("meal_enrichment", "validate")
    builder.add_edge("validate", END)
    return builder.compile(checkpointer=checkpointer, name=settings.langgraph_workflow_version)
