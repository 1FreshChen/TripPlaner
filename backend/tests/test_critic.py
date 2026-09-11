import asyncio

from app.agents.critic import PlanCritic
from app.models.schemas import CritiqueResult, TripPlan, TripPlanRequest
from app.services.mock_data import build_mock_attractions, build_mock_hotels, build_mock_weather


def _request() -> TripPlanRequest:
    return TripPlanRequest(
        city="北京",
        start_date="2026-06-01",
        end_date="2026-06-02",
        days=2,
        preferences="历史文化",
        budget="中等",
        transportation="公共交通",
        accommodation="精品酒店",
    )


def _plan_payload(request: TripPlanRequest) -> dict:
    attraction = build_mock_attractions(request.city, request.preferences, request.days)[0].model_dump()
    hotel = build_mock_hotels(request.city, request.accommodation, request.budget)[0].model_dump()
    weather = build_mock_weather(request.start_date, request.days)
    return {
        "city": request.city,
        "start_date": request.start_date,
        "end_date": request.end_date,
        "days": [
            {
                "date": item.date,
                "day_index": index,
                "description": f"第{index + 1}天围绕历史文化主题安排，路线紧凑度适中。",
                "transportation": request.transportation,
                "accommodation": request.accommodation,
                "hotel": hotel,
                "attractions": [attraction, attraction],
                "meals": [
                    {"type": "breakfast", "name": "早餐店", "estimated_cost": 25},
                    {"type": "lunch", "name": "午餐店", "estimated_cost": 60},
                    {"type": "dinner", "name": "晚餐店", "estimated_cost": 90},
                ],
            }
            for index, item in enumerate(weather)
        ],
        "weather_info": [item.model_dump() for item in weather],
        "overall_suggestions": "1. 穿衣：结合天气准备外套。\n2. 必吃：北京烤鸭。",
        "budget": {
            "total_attractions": 0,
            "total_hotels": 800,
            "total_meals": 350,
            "total_transportation": 60,
            "total": 1210,
        },
    }


class FakeLLMService:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def generate_json(self, system_prompt, user_prompt):
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt})
        return self.response


def _critique_response(score: int = 8) -> dict:
    return {
        "scores": {
            "attraction_diversity": score,
            "description_quality": score,
            "weather_compatibility": score,
            "schedule_feasibility": score,
            "budget_realism": score,
        },
        "issues": [],
        "suggestions": ["保持当前路线"],
        "needs_revision": False,
        "revision_summary": "质量达标",
    }


def test_critique_result_computes_average_score():
    result = CritiqueResult.model_validate(_critique_response(score=6))

    assert result.average_score == 6.0


def test_plan_critic_sends_request_and_plan_json_to_llm():
    request = _request()
    plan = TripPlan.model_validate(_plan_payload(request))
    llm = FakeLLMService(_critique_response(score=9))
    critic = PlanCritic(llm)

    result = asyncio.run(critic.evaluate(plan, request))

    assert result.average_score == 9.0
    assert "用户请求" in llm.calls[0]["user_prompt"]
    assert "待审视行程" in llm.calls[0]["user_prompt"]


def test_plan_critic_prompt_contains_serialized_trip_plan():
    request = _request()
    plan = TripPlan.model_validate(_plan_payload(request))
    llm = FakeLLMService(_critique_response())
    critic = PlanCritic(llm)

    prompt = critic._build_critique_prompt(plan, request)

    assert '"city": "北京"' in prompt
    assert '"preferences": "历史文化"' in prompt


def test_plan_critic_returns_neutral_pass_when_llm_returns_no_result():
    request = _request()
    plan = TripPlan.model_validate(_plan_payload(request))
    llm = FakeLLMService(None)
    critic = PlanCritic(llm)

    result = asyncio.run(critic.evaluate(plan, request))

    assert result.average_score == 7.0
    assert result.needs_revision is False
    assert "未返回结果" in result.revision_summary


def test_plan_critic_passes_none_to_disable_inner_timeout():
    class AsyncLLMService(FakeLLMService):
        async def generate_json_async(self, system_prompt, user_prompt, *, timeout_seconds):
            self.calls.append({"timeout_seconds": timeout_seconds})
            return _critique_response()

    request = _request()
    plan = TripPlan.model_validate(_plan_payload(request))
    llm = AsyncLLMService(None)

    result = asyncio.run(PlanCritic(llm).evaluate(plan, request, timeout_seconds=None))

    assert result.average_score == 8.0
    assert llm.calls[0]["timeout_seconds"] is None
