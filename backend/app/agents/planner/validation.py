from __future__ import annotations

from typing import Any, Iterable

from pydantic import TypeAdapter

from app.models.schemas import TripPlan, TripPlanRequest
from app.services.plan_quality import validate_trip_plan_for_request
from app.utils.json_utils import strip_json_fence


_TRIP_PLAN_ADAPTER = TypeAdapter(TripPlan)


def parse_planner_output(
    text: str,
    request: TripPlanRequest,
    *,
    critique_events: Iterable[dict[str, Any]] | None = None,
) -> TripPlan:
    """Parse model JSON and apply the same publishability rules as every backend."""
    plan = _TRIP_PLAN_ADAPTER.validate_json(strip_json_fence(text))
    return validate_planner_output(plan, request, critique_events=critique_events)


def validate_planner_output(
    plan: TripPlan,
    request: TripPlanRequest,
    *,
    critique_events: Iterable[dict[str, Any]] | None = None,
) -> TripPlan:
    validate_trip_plan_for_request(
        plan,
        request,
        critique_events=list(critique_events or []),
    )
    return plan
