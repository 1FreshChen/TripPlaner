import logging

from app.agents.trip_planner import PlannerAgent
from app.models.schemas import TripPlanRequest
from app.services.mock_data import build_mock_attractions, build_mock_hotels, build_mock_weather


class InvalidPlanLLMService:
    enabled = True

    @staticmethod
    def generate_json(system_prompt, user_prompt):
        return {"city": "北京"}


def test_planner_logs_schema_failure_before_deterministic_fallback(caplog):
    request = TripPlanRequest(
        city="北京",
        start_date="2026-07-20",
        end_date="2026-07-21",
        days=2,
        preferences="历史文化",
        budget="中等",
        transportation="公共交通",
        accommodation="经济型酒店",
    )
    planner = PlannerAgent(InvalidPlanLLMService(), use_llm=True)

    with caplog.at_level(logging.WARNING, logger="app.agents.trip_planner"):
        plan = planner.run(
            request,
            build_mock_attractions(request.city, request.preferences, request.days),
            build_mock_weather(request.start_date, request.days),
            build_mock_hotels(request.city, request.accommodation, request.budget),
            "test planner query",
        )

    assert plan.city == request.city
    assert "using deterministic fallback" in caplog.text
