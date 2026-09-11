import json

import pytest

from app.agents.planner.deterministic import PlannerAgent
from app.agents.planner.validation import parse_planner_output
from app.models.schemas import TripPlanRequest
from app.services.mock_data import build_mock_attractions, build_mock_hotels, build_mock_weather
from app.services.plan_quality import PlanQualityError


def test_shared_planner_parser_applies_schema_and_request_quality_rules():
    request = TripPlanRequest(
        city="北京",
        start_date="2026-06-01",
        end_date="2026-06-02",
        days=2,
        preferences="历史文化",
        budget="中等",
        transportation="公共交通",
        accommodation="经济型酒店",
    )
    plan = PlannerAgent().run(
        request,
        build_mock_attractions(request.city, request.preferences, request.days),
        build_mock_weather(request.start_date, request.days),
        build_mock_hotels(request.city, request.accommodation, request.budget),
    )
    payload = plan.model_dump(mode="json")

    parsed = parse_planner_output(
        f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```",
        request,
    )
    assert parsed == plan

    payload["city"] = "上海"
    with pytest.raises(PlanQualityError, match="城市不一致"):
        parse_planner_output(json.dumps(payload, ensure_ascii=False), request)
