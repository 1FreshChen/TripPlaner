import asyncio
import json

import pytest

from app.agents.llm_planner import LLMPlannerAgent
from app.models.schemas import TripPlanRequest
from app.services.llm_service import TokenUsage
from app.services.mock_data import build_mock_attractions, build_mock_hotels, build_mock_weather
from app.services.plan_quality import PlanQualityError


def _request() -> TripPlanRequest:
    return TripPlanRequest(
        city="北京",
        start_date="2026-06-01",
        end_date="2026-06-03",
        days=3,
        preferences="历史文化",
        budget="中等",
        transportation="公共交通",
        accommodation="精品酒店",
    )


def _valid_plan_json(request: TripPlanRequest) -> str:
    attraction = build_mock_attractions(request.city, request.preferences, request.days)[0].model_dump()
    hotel = build_mock_hotels(request.city, request.accommodation, request.budget)[0].model_dump()
    weather = build_mock_weather(request.start_date, request.days)
    payload = {
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
        "overall_suggestions": "1. 穿衣：结合天气准备外套。\n2. 必吃：北京烤鸭。\n3. 交通：优先地铁。\n4. 避坑：官方预约。\n5. 隐藏玩法：傍晚胡同漫步。",
        "budget": {
            "total_attractions": 0,
            "total_hotels": 800,
            "total_meals": 525,
            "total_transportation": 90,
            "total": 1415,
        },
    }
    return json.dumps(payload, ensure_ascii=False)


class FakeRegistry:
    def get_openai_functions(self):
        return [{"type": "function", "function": {"name": "amap_poi_search", "parameters": {"type": "object"}}}]


class FakeExecutor:
    pass


class FakeLLMService:
    enabled = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat_with_tools(self, system_prompt, user_prompt, tools, tool_executor, max_tool_rounds=5, response_format=None):
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "tools": tools,
                "tool_executor": tool_executor,
                "max_tool_rounds": max_tool_rounds,
            }
        )
        return self.responses.pop(0)


class FakeCritiquingLLMService(FakeLLMService):
    def __init__(self, responses, critiques):
        super().__init__(responses)
        self.critiques = list(critiques)
        self.critique_calls = []

    def generate_json(self, system_prompt, user_prompt):
        self.critique_calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
            }
        )
        return self.critiques.pop(0)


def _context(request: TripPlanRequest):
    return {
        "request": request,
        "attraction_search": build_mock_attractions(request.city, request.preferences, request.days),
        "weather_query": build_mock_weather(request.start_date, request.days),
        "hotel_recommendation": build_mock_hotels(request.city, request.accommodation, request.budget),
        "memory_context": {"saved_attractions_in_city": [{"name": "故宫博物院"}]},
        "conversation_context": [{"role": "user", "content": "上次去过故宫，这次想换一些新点。"}],
    }


def _critique_payload(score: int, needs_revision: bool, problem: str = "故宫重复出现"):
    return {
        "scores": {
            "attraction_diversity": score,
            "description_quality": score,
            "weather_compatibility": score,
            "schedule_feasibility": score,
            "budget_realism": score,
        },
        "issues": [
            {
                "severity": "high",
                "day": 1,
                "problem": problem,
                "suggestion": "替换为北海公园",
            }
        ]
        if needs_revision
        else [],
        "suggestions": ["调整重复景点"],
        "needs_revision": needs_revision,
        "revision_summary": "需要修正重复景点" if needs_revision else "质量达标",
    }


def _plan_json_with_summary(request: TripPlanRequest, summary: str) -> str:
    payload = json.loads(_valid_plan_json(request))
    payload["overall_suggestions"] = summary
    return json.dumps(payload, ensure_ascii=False)


def test_llm_planner_calls_chat_with_tools_using_baseline_context():
    request = _request()
    usage = TokenUsage(model="gpt-test", provider="openai", prompt_tokens=10, completion_tokens=5, total_tokens=15)
    tool_calls = [{"tool": "amap_poi_search", "arguments": {"keywords": "胡同", "city": "北京"}, "id": "call_1"}]
    llm = FakeLLMService([( _valid_plan_json(request), tool_calls, usage)])
    executor = FakeExecutor()
    agent = LLMPlannerAgent(llm, FakeRegistry(), executor)
    context = _context(request)

    plan = asyncio.run(agent.execute(context))

    assert plan.city == "北京"
    assert llm.calls[0]["tools"] == FakeRegistry().get_openai_functions()
    assert llm.calls[0]["tool_executor"] is executor
    assert llm.calls[0]["max_tool_rounds"] == 5
    assert "baseline 数据" in llm.calls[0]["user_prompt"]
    assert "40%-60%" in llm.calls[0]["user_prompt"]
    assert "故宫博物院" in llm.calls[0]["user_prompt"]
    assert context["trip_planner_tool_calls"] == tool_calls
    assert context["trip_planner_token_usage"] is usage


def test_llm_planner_requests_json_correction_after_invalid_output():
    request = _request()
    first_usage = TokenUsage(model="gpt-test", provider="openai", prompt_tokens=10, completion_tokens=5, total_tokens=15)
    correction_usage = TokenUsage(
        model="gpt-test",
        provider="openai",
        prompt_tokens=8,
        completion_tokens=7,
        total_tokens=15,
    )
    llm = FakeLLMService(
        [
            ("not json", [], first_usage),
            (_valid_plan_json(request), [], correction_usage),
        ]
    )
    agent = LLMPlannerAgent(llm, FakeRegistry(), FakeExecutor())
    context = _context(request)

    plan = asyncio.run(agent.execute(context))

    assert plan.city == "北京"
    assert len(llm.calls) == 2
    assert llm.calls[1]["tools"] == []
    assert llm.calls[1]["max_tool_rounds"] == 1
    assert "格式错误" in llm.calls[1]["user_prompt"]
    assert context["trip_planner_token_usage"].total_tokens == 30


def test_llm_planner_refines_plan_when_critique_requires_revision():
    request = _request()
    first_usage = TokenUsage(model="gpt-test", provider="openai", prompt_tokens=10, completion_tokens=5, total_tokens=15)
    revision_usage = TokenUsage(model="gpt-test", provider="openai", prompt_tokens=12, completion_tokens=8, total_tokens=20)
    llm = FakeCritiquingLLMService(
        [
            (_plan_json_with_summary(request, "初版建议"), [{"tool": "initial"}], first_usage),
            (_plan_json_with_summary(request, "修正版建议"), [{"tool": "revision"}], revision_usage),
        ],
        [
            _critique_payload(4, True),
            _critique_payload(9, False),
        ],
    )
    agent = LLMPlannerAgent(llm, FakeRegistry(), FakeExecutor(), max_refinement_rounds=2, min_pass_score=7.0)
    context = _context(request)

    plan = asyncio.run(agent.execute(context))

    assert plan.overall_suggestions == "修正版建议"
    assert len(llm.calls) == 2
    assert "上一版行程存在以下问题" in llm.calls[1]["user_prompt"]
    assert "故宫重复出现" in llm.calls[1]["user_prompt"]
    assert len(llm.critique_calls) == 2
    assert context["trip_planner_tool_calls"] == [{"tool": "initial"}, {"tool": "revision"}]
    assert context["trip_planner_token_usage"].total_tokens == 35
    assert [event["details"]["average_score"] for event in context["event_log"]] == [4.0, 9.0]


def test_llm_planner_refines_plan_when_critique_requires_revision_even_with_passing_score():
    request = _request()
    first_usage = TokenUsage(model="gpt-test", provider="openai", prompt_tokens=10, completion_tokens=5, total_tokens=15)
    revision_usage = TokenUsage(model="gpt-test", provider="openai", prompt_tokens=12, completion_tokens=8, total_tokens=20)
    llm = FakeCritiquingLLMService(
        [
            (_plan_json_with_summary(request, "初版建议"), [{"tool": "initial"}], first_usage),
            (_plan_json_with_summary(request, "高分修正版建议"), [{"tool": "revision"}], revision_usage),
        ],
        [
            _critique_payload(8, True, problem="第 3 天晚餐地点与住宿地冲突"),
            _critique_payload(9, False),
        ],
    )
    agent = LLMPlannerAgent(llm, FakeRegistry(), FakeExecutor(), max_refinement_rounds=2, min_pass_score=7.0)
    context = _context(request)

    plan = asyncio.run(agent.execute(context))

    assert plan.overall_suggestions == "高分修正版建议"
    assert len(llm.calls) == 2
    assert len(llm.critique_calls) == 2
    assert [event["details"]["needs_revision"] for event in context["event_log"]] == [True, False]


def test_llm_planner_rejects_plan_when_refinement_limit_is_reached_with_unresolved_critique():
    request = _request()
    first_usage = TokenUsage(model="gpt-test", provider="openai", prompt_tokens=10, completion_tokens=5, total_tokens=15)
    revision_usage = TokenUsage(model="gpt-test", provider="openai", prompt_tokens=12, completion_tokens=8, total_tokens=20)
    llm = FakeCritiquingLLMService(
        [
            (_plan_json_with_summary(request, "初版建议"), [], first_usage),
            (_plan_json_with_summary(request, "更低分修正版"), [], revision_usage),
        ],
        [
            _critique_payload(6, True, problem="描述质量不足"),
            _critique_payload(5, True, problem="预算仍不合理"),
        ],
    )
    agent = LLMPlannerAgent(llm, FakeRegistry(), FakeExecutor(), max_refinement_rounds=1, min_pass_score=7.0)
    context = _context(request)

    with pytest.raises(PlanQualityError):
        asyncio.run(agent.execute(context))

    assert len(llm.calls) == 2
    assert len(llm.critique_calls) == 2
    assert [event["details"]["round"] for event in context["event_log"]] == [0, 1]
    assert context["trip_planner_quality_failed"] is True


def test_llm_planner_requests_correction_after_plan_quality_validation_fails():
    request = _request()
    bad_payload = json.loads(_valid_plan_json(request))
    bad_payload["city"] = "上海"
    first_usage = TokenUsage(model="gpt-test", provider="openai", prompt_tokens=10, completion_tokens=5, total_tokens=15)
    correction_usage = TokenUsage(
        model="gpt-test",
        provider="openai",
        prompt_tokens=8,
        completion_tokens=7,
        total_tokens=15,
    )
    llm = FakeLLMService(
        [
            (json.dumps(bad_payload, ensure_ascii=False), [], first_usage),
            (_valid_plan_json(request), [], correction_usage),
        ]
    )
    agent = LLMPlannerAgent(llm, FakeRegistry(), FakeExecutor(), enable_critique=False)
    context = _context(request)

    plan = asyncio.run(agent.execute(context))

    assert plan.city == request.city
    assert len(llm.calls) == 2
    assert "质量校验失败" in llm.calls[1]["user_prompt"]


def test_llm_planner_marks_quality_failed_when_quality_correction_still_fails():
    request = _request()
    bad_payload = json.loads(_valid_plan_json(request))
    bad_payload["city"] = "上海"
    usage = TokenUsage(model="gpt-test", provider="openai", prompt_tokens=10, completion_tokens=5, total_tokens=15)
    llm = FakeLLMService(
        [
            (json.dumps(bad_payload, ensure_ascii=False), [], usage),
            (json.dumps(bad_payload, ensure_ascii=False), [], usage),
        ]
    )
    agent = LLMPlannerAgent(llm, FakeRegistry(), FakeExecutor(), enable_critique=False)
    context = _context(request)

    with pytest.raises(PlanQualityError):
        asyncio.run(agent.execute(context))

    assert context["trip_planner_quality_failed"] is True


def test_llm_planner_skips_critique_when_disabled():
    request = _request()
    usage = TokenUsage(model="gpt-test", provider="openai", prompt_tokens=10, completion_tokens=5, total_tokens=15)
    llm = FakeCritiquingLLMService(
        [(_valid_plan_json(request), [], usage)],
        [_critique_payload(1, True)],
    )
    agent = LLMPlannerAgent(llm, FakeRegistry(), FakeExecutor(), enable_critique=False)
    context = _context(request)

    plan = asyncio.run(agent.execute(context))

    assert plan.city == "北京"
    assert llm.critique_calls == []
    assert "event_log" not in context
