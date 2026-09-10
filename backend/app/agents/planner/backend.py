from __future__ import annotations

from typing import Any, Protocol

from app.config import PlannerBackendName
from app.models.schemas import CritiqueResult, TripPlan


class PlannerBackend(Protocol):
    """Common contract for Planner implementations hosted by LangGraph."""

    name: str

    async def execute(self, context: dict[str, Any]) -> TripPlan:
        ...


class RefinablePlannerBackend(PlannerBackend, Protocol):
    async def refine_once(
        self,
        context: dict[str, Any],
        previous_plan: TripPlan,
        critique: CritiqueResult,
    ) -> TripPlan:
        ...
