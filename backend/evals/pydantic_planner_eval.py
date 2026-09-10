from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import EqualsExpected

from app.agents.planner.pydantic_support import PlannerToolState
from app.models.schemas import TripPlanRequest


class ToolPolicyEvalInput(BaseModel):
    days: int = 2
    preferences: str = "历史文化"
    budget: str = "中等"
    attractions: int = 4
    weather_days: int = 2
    hotels: int = 1
    force_finalize: bool = False
    tool_rounds: int = 0
    total_tool_calls: int = 0


class ToolPolicyEvalOutput(BaseModel):
    baseline_sufficient: bool
    visible_tools: list[str]


def evaluate_tool_policy(inputs: ToolPolicyEvalInput) -> ToolPolicyEvalOutput:
    start = date(2026, 6, 1)
    request = TripPlanRequest(
        city="北京",
        start_date=start.isoformat(),
        end_date=(start + timedelta(days=inputs.days - 1)).isoformat(),
        days=inputs.days,
        preferences=inputs.preferences,
        budget=inputs.budget,
        transportation="公共交通",
        accommodation="精品酒店",
    )
    state = PlannerToolState(
        request=request,
        baseline_attractions=inputs.attractions,
        baseline_weather=inputs.weather_days,
        baseline_hotels=inputs.hotels,
        force_finalize=inputs.force_finalize,
        tool_rounds=inputs.tool_rounds,
        total_tool_calls=inputs.total_tool_calls,
    )
    return ToolPolicyEvalOutput(
        baseline_sufficient=state.baseline_is_sufficient,
        visible_tools=sorted(state.visible_tool_names()),
    )


TOOL_CONVERGENCE_DATASET = Dataset(
    name="pydantic-planner-tool-convergence",
    cases=[
        Case(
            name="sufficient-baseline-stops",
            inputs=ToolPolicyEvalInput(),
            expected_output=ToolPolicyEvalOutput(baseline_sufficient=True, visible_tools=[]),
        ),
        Case(
            name="three-missing-dimensions-only-show-acquisition-tools",
            inputs=ToolPolicyEvalInput(attractions=0, weather_days=0, hotels=0),
            expected_output=ToolPolicyEvalOutput(
                baseline_sufficient=False,
                visible_tools=["amap_poi_search", "amap_weather", "hotel_search"],
            ),
        ),
        Case(
            name="food-preference-defers-to-http-postprocessing",
            inputs=ToolPolicyEvalInput(preferences="历史文化和美食"),
            expected_output=ToolPolicyEvalOutput(
                baseline_sufficient=True,
                visible_tools=[],
            ),
        ),
        Case(
            name="explicit-route-and-photo-needs-remain-bounded",
            inputs=ToolPolicyEvalInput(preferences="自驾路线和摄影出片"),
            expected_output=ToolPolicyEvalOutput(
                baseline_sufficient=True,
                visible_tools=["baidu_direction", "unsplash_image"],
            ),
        ),
        Case(
            name="hard-stop-hides-tools-even-with-missing-data",
            inputs=ToolPolicyEvalInput(
                attractions=0,
                weather_days=0,
                hotels=0,
                force_finalize=True,
            ),
            expected_output=ToolPolicyEvalOutput(baseline_sufficient=False, visible_tools=[]),
        ),
    ],
    evaluators=[EqualsExpected()],
)


def run_eval(*, progress: bool = False):
    return TOOL_CONVERGENCE_DATASET.evaluate_sync(evaluate_tool_policy, progress=progress)


if __name__ == "__main__":
    run_eval(progress=True).print()
