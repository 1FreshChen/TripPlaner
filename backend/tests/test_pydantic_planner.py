import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ThinkingPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RequestUsage

from app.agents.llm_planner import LLMPlannerAgent
from app.agents.pydantic_planner import PydanticAIPlannerAgent
from app.agents.planner.pydantic_support import (
    PYDANTIC_PLANNER_TOOLS,
    PlannerDeps,
    PlannerToolState,
    _execute_tool,
    canonical_tool_signature,
)
from app.models.schemas import TripPlanRequest
from app.orchestration.bootstrap import bootstrap_orchestration
from app.services.llm_service import LLMService
from app.services.mock_data import build_mock_attractions, build_mock_hotels, build_mock_weather
from app.services.pydantic_ai_model import OpenAICompatiblePydanticModel


def _request(preferences: str = "历史文化") -> TripPlanRequest:
    return TripPlanRequest(
        city="北京",
        start_date="2026-06-01",
        end_date="2026-06-02",
        days=2,
        preferences=preferences,
        budget="中等",
        transportation="公共交通",
        accommodation="精品酒店",
    )


def _valid_plan_json(request: TripPlanRequest) -> str:
    attractions = build_mock_attractions(request.city, request.preferences, request.days)
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
                "description": f"第{index + 1}天按相邻片区安排，景点间约20分钟，节奏适中。",
                "transportation": request.transportation,
                "accommodation": request.accommodation,
                "hotel": hotel,
                "attractions": [
                    attractions[(index * 2) % len(attractions)].model_dump(),
                    attractions[(index * 2 + 1) % len(attractions)].model_dump(),
                ],
                "meals": [
                    {"type": "breakfast", "name": "早餐店", "estimated_cost": 25},
                    {"type": "lunch", "name": "午餐店", "estimated_cost": 60},
                    {"type": "dinner", "name": "晚餐店", "estimated_cost": 90},
                ],
            }
            for index, item in enumerate(weather)
        ],
        "weather_info": [item.model_dump() for item in weather],
        "overall_suggestions": (
            "1. 穿衣：结合温差准备外套。\n2. 必吃：北京烤鸭。\n3. 交通：优先地铁。\n"
            "4. 避坑：只走官方预约。\n5. 隐藏玩法：傍晚胡同漫步。"
        ),
        "budget": {
            "total_attractions": 0,
            "total_hotels": 400,
            "total_meals": 350,
            "total_transportation": 60,
            "total": 810,
        },
    }
    return json.dumps(payload, ensure_ascii=False)


class FakeLLMService:
    enabled = True
    api_key = "test-key"
    base_url = "https://api.deepseek.com"
    model = "deepseek-test"

    @staticmethod
    def _infer_provider():
        return "deepseek"


class FakeRegistry:
    def get_openai_functions(self):
        return []


class FakeExecutor:
    def __init__(self):
        self.calls = []

    async def execute_by_name(self, tool_name, **kwargs):
        self.calls.append((tool_name, kwargs))
        return {"success": True, "count": 3, "city": kwargs.get("city")}


def test_bootstrap_selects_each_model_planner_explicitly():
    old_registry = bootstrap_orchestration(
        llm_service=FakeLLMService(),
        enable_external_services=True,
        planner_backend="openai_tools",
        tool_registry=FakeRegistry(),
        tool_executor=FakeExecutor(),
    )
    new_registry = bootstrap_orchestration(
        llm_service=FakeLLMService(),
        enable_external_services=True,
        planner_backend="pydantic_ai",
        tool_registry=FakeRegistry(),
        tool_executor=FakeExecutor(),
    )

    assert type(old_registry.get("trip_planner").create_agent()) is LLMPlannerAgent
    assert isinstance(new_registry.get("trip_planner").create_agent(), PydanticAIPlannerAgent)


def test_all_planner_tools_have_typed_generated_schemas():
    schemas = {tool.name: tool.tool_def.parameters_json_schema for tool in PYDANTIC_PLANNER_TOOLS}

    assert set(schemas) == {
        "amap_poi_search",
        "amap_weather",
        "hotel_search",
        "baidu_direction",
        "budget_calculator",
        "unsplash_image",
    }
    assert schemas["amap_poi_search"]["required"] == ["keywords", "city"]
    assert schemas["amap_poi_search"]["properties"]["offset"]["maximum"] == 25
    assert schemas["baidu_direction"]["properties"]["mode"]["enum"] == [
        "driving",
        "walking",
        "transit",
        "riding",
    ]
    assert schemas["unsplash_image"]["properties"]["count"]["maximum"] == 5


def test_tool_visibility_is_staged_and_sufficient_baseline_hides_everything():
    request = _request()
    sufficient = PlannerToolState(request, baseline_attractions=4, baseline_weather=2, baseline_hotels=1)
    missing = PlannerToolState(request, baseline_attractions=0, baseline_weather=0, baseline_hotels=0)
    food_only = PlannerToolState(_request("历史文化和美食"), 4, 2, 1)

    assert sufficient.visible_tool_names() == set()
    assert missing.visible_tool_names() == {"amap_poi_search", "amap_weather", "hotel_search"}
    assert food_only.visible_tool_names() == set()


def test_equivalent_tool_arguments_are_deduplicated_before_external_execution():
    request = _request()
    state = PlannerToolState(request, 0, 2, 1)
    executor = FakeExecutor()
    deps = PlannerDeps(request=request, tool_executor=executor, tool_state=state)
    ctx = SimpleNamespace(deps=deps, tool_call_id="call-1")
    first_args = {"keywords": "博物馆|公园", "city": "北京", "offset": 10}
    duplicate_args = {"keywords": " 公园 | 博物馆 ", "city": " 北京 ", "offset": 10}

    first = asyncio.run(_execute_tool(ctx, "amap_poi_search", first_args))
    ctx.tool_call_id = "call-2"
    second = asyncio.run(_execute_tool(ctx, "amap_poi_search", duplicate_args))

    assert canonical_tool_signature("amap_poi_search", first_args) == canonical_tool_signature(
        "amap_poi_search", duplicate_args
    )
    assert first["success"] is True
    assert second["duplicate"] is True
    assert len(executor.calls) == 1
    assert state.tool_calls[-1]["duplicate"] is True


def test_amap_tool_results_are_preserved_for_final_poi_verification():
    state = PlannerToolState(_request(), 0, 2, 1)
    pois = [
        {
            "id": "poi-1",
            "name": "明孝陵景区",
            "address": "南京市玄武区",
            "location": "118.835,32.059",
            "_source": "amap_mcp",
        }
    ]

    state.record_result(
        "amap_poi_search",
        {"keywords": "明孝陵", "city": "南京"},
        {"success": True, "count": 1, "pois": pois},
        tool_call_id="call-poi",
    )

    assert state.amap_pois == pois
    assert state.diagnostics()["verified_amap_pois"] == 1


class FakeHTTPResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class RecordingHTTPClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def post(self, url, headers, json):
        self.requests.append(json)
        return FakeHTTPResponse(self.responses.pop(0))


class SlowHTTPClient:
    async def post(self, url, headers, json):
        await asyncio.sleep(0.05)
        return FakeHTTPResponse(
            {
                "id": "slow-response",
                "model": "deepseek-test",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "{}"},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        )


def test_model_request_timeout_identifies_the_exact_planner_stage():
    request = _request()
    state = PlannerToolState(request, 4, 2, 1)
    state.current_stage = "initial_generation"
    service = LLMService("test-key", "https://api.deepseek.com", "deepseek-test")
    model = OpenAICompatiblePydanticModel(
        service,
        state,
        timeout_seconds=0.01,
        http_client=SlowHTTPClient(),
    )
    params = ModelRequestParameters(function_tools=[], output_mode="prompted")
    request_message = ModelRequest(parts=[UserPromptPart("规划北京")], instructions="system")

    with pytest.raises(
        TimeoutError,
        match=r"initial_generation\.model_request_1",
    ):
        asyncio.run(model.request([request_message], None, params))

    assert state.model_requests == 1
    assert state.current_stage == "initial_generation.model_request_1"


def test_zero_model_request_timeout_waits_for_natural_completion():
    request = _request()
    state = PlannerToolState(request, 4, 2, 1)
    service = LLMService("test-key", "https://api.deepseek.com", "deepseek-test")
    model = OpenAICompatiblePydanticModel(
        service,
        state,
        timeout_seconds=0,
        http_client=SlowHTTPClient(),
    )
    params = ModelRequestParameters(function_tools=[], output_mode="prompted")
    request_message = ModelRequest(parts=[UserPromptPart("规划北京")], instructions="system")

    response = asyncio.run(model.request([request_message], None, params))

    assert response.model_name == "deepseek-test"
    assert state.model_requests == 1


def test_deepseek_reasoning_is_replayed_and_tool_round_limit_forces_json_without_tools():
    request = _request()
    state = PlannerToolState(request, 0, 2, 1, max_tool_rounds=1)
    client = RecordingHTTPClient(
        [
            {
                "id": "r1",
                "model": "deepseek-test",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "",
                            "reasoning_content": "需要补齐景点",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "amap_poi_search",
                                        "arguments": '{"keywords":"博物馆","city":"北京"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
            {
                "id": "r2",
                "model": "deepseek-test",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"city":"北京"}'},
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
        ]
    )
    service = LLMService("test-key", "https://api.deepseek.com", "deepseek-test")
    model = OpenAICompatiblePydanticModel(service, state, http_client=client)
    params = ModelRequestParameters(
        function_tools=[
            ToolDefinition(
                name="amap_poi_search",
                description="search",
                parameters_json_schema={"type": "object", "properties": {}},
            )
        ],
        output_mode="prompted",
    )
    first_request = ModelRequest(parts=[UserPromptPart("规划北京")], instructions="system")

    first_response = asyncio.run(model.request([first_request], None, params))
    second_request = ModelRequest(
        parts=[ToolReturnPart("amap_poi_search", {"success": True}, "call-1")],
        instructions="system",
    )
    asyncio.run(model.request([first_request, first_response, second_request], None, params))

    assert any(isinstance(part, ThinkingPart) for part in first_response.parts)
    assert first_response.finish_reason == "tool_call"
    assert client.requests[0]["tool_choice"] == "auto"
    assert len(client.requests[0]["tools"]) == 1
    assert "tools" not in client.requests[1]
    assert "tool_choice" not in client.requests[1]
    assert client.requests[1]["response_format"] == {"type": "json_object"}
    assert client.requests[1]["messages"][0]["role"] == "system"
    assistant = next(message for message in client.requests[1]["messages"] if message["role"] == "assistant")
    assert assistant["reasoning_content"] == "需要补齐景点"
    assistant_index = client.requests[1]["messages"].index(assistant)
    assert client.requests[1]["messages"][assistant_index + 1]["role"] == "tool"


def test_typed_planner_runs_offline_and_preserves_context_audit_contract():
    request = _request()
    seen_tools = []

    def model_function(messages, agent_info):
        seen_tools.append([tool.name for tool in agent_info.function_tools])
        return ModelResponse(
            parts=[TextPart(_valid_plan_json(request))],
            usage=RequestUsage(input_tokens=20, output_tokens=10),
        )

    llm = FakeLLMService()
    executor = FakeExecutor()
    agent = PydanticAIPlannerAgent(
        llm,
        FakeRegistry(),
        executor,
        enable_critique=False,
        model_factory=lambda service, state: FunctionModel(model_function),
    )
    context = {
        "request": request,
        "attraction_search": build_mock_attractions(request.city, request.preferences, request.days),
        "weather_query": build_mock_weather(request.start_date, request.days),
        "hotel_recommendation": build_mock_hotels(request.city, request.accommodation, request.budget),
    }

    plan = asyncio.run(agent.execute(context))

    assert plan.city == request.city
    assert seen_tools == [[]]
    assert context["trip_planner_tool_calls"] == []
    assert context["trip_planner_token_usage"].total_tokens == 30
    assert context["event_log"][-1]["event_type"] == "planner_convergence"
