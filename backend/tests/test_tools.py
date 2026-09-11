import asyncio
import json

from app.services.llm_service import LLMService
from app.tools.base import BaseTool
from app.tools.bootstrap import bootstrap_tools
from app.tools.cache import ToolCache
from app.tools.executor import ToolExecutor
from app.tools.implementations.baidu_direction import BaiduDirectionTool
from app.tools.implementations.baidu_poi_search import BaiduPOISearchTool
from app.tools.implementations.budget_calculator import BudgetCalculatorTool
from app.tools.registry import ToolRegistry


class FakeAmapService:
    def search_pois(self, keywords, city, offset=10):
        return [
            {
                "name": f"{city}{keywords}",
                "address": "中心区",
                "location": "116.397128,39.916527",
                "biz_ext": {"rating": "4.8"},
                "type": "景点",
            }
        ][:offset]

    def get_weather(self, city):
        return []

    def poi_to_hotel(self, poi, accommodation):
        return None


class FakeUnsplashService:
    def search_photos(self, query, per_page=10):
        return [{"url": "https://example.test/photo.jpg", "description": query, "photographer": "tester"}][
            :per_page
        ]


class FakeBaiduService:
    def search_pois(self, keywords, city, tag=None, sort_by=None, offset=10):
        self.last_search = {
            "keywords": keywords,
            "city": city,
            "tag": tag,
            "sort_by": sort_by,
            "offset": offset,
        }
        return [{"name": "测试川菜"}][:offset]

    def poi_to_restaurant(self, poi):
        return {
            "name": poi["name"],
            "address": "东城区",
            "taste_rating": 4.9,
            "price": 120,
        }

    def get_direction(self, origin, destination, city, mode="transit"):
        self.last_direction = {
            "origin": origin,
            "destination": destination,
            "city": city,
            "mode": mode,
        }
        return [{"distance_m": 15200, "duration_s": 2700, "duration_text": "约45分钟"}]


def test_bootstrap_registers_all_phase3_tools():
    registry = bootstrap_tools(FakeAmapService(), FakeUnsplashService(), FakeBaiduService(), reset=True)

    assert [tool.name for tool in registry.list_all()] == [
        "amap_poi_search",
        "amap_weather",
        "hotel_search",
        "baidu_poi_search",
        "baidu_direction",
        "budget_calculator",
        "unsplash_image",
    ]
    assert len(registry.get_openai_functions()) == 7


def test_bootstrap_returns_registry_bound_to_each_callers_services():
    first_amap = FakeAmapService()
    first_unsplash = FakeUnsplashService()
    first_baidu = FakeBaiduService()
    second_amap = FakeAmapService()
    second_unsplash = FakeUnsplashService()
    second_baidu = FakeBaiduService()

    first = bootstrap_tools(first_amap, first_unsplash, first_baidu, reset=True)
    second = bootstrap_tools(second_amap, second_unsplash, second_baidu)

    assert first is not second
    assert first.get("amap_poi_search")._amap is first_amap
    assert second.get("amap_poi_search")._amap is second_amap
    assert first.get("baidu_poi_search")._baidu is first_baidu
    assert second.get("baidu_poi_search")._baidu is second_baidu


def test_baidu_poi_search_tool_normalizes_limit_and_returns_restaurants():
    service = FakeBaiduService()
    tool = BaiduPOISearchTool(service)

    result = asyncio.run(
        tool.execute(keywords="川菜", city="北京", tag="川菜", sort_by="taste_rating", limit=50)
    )

    assert result == {
        "restaurants": [
            {
                "name": "测试川菜",
                "address": "东城区",
                "taste_rating": 4.9,
                "price": 120,
            }
        ],
        "count": 1,
        "city": "北京",
        "success": True,
    }
    assert service.last_search == {
        "keywords": "川菜",
        "city": "北京",
        "tag": "川菜",
        "sort_by": "taste_rating",
        "offset": 20,
    }


def test_baidu_direction_tool_returns_routes():
    service = FakeBaiduService()
    tool = BaiduDirectionTool(service)

    result = asyncio.run(tool.execute(origin="故宫", destination="颐和园", city="北京", mode="flying"))

    assert result == {
        "routes": [{"distance_m": 15200, "duration_s": 2700, "duration_text": "约45分钟"}],
        "count": 1,
        "city": "北京",
        "success": True,
    }
    assert service.last_direction == {
        "origin": "故宫",
        "destination": "颐和园",
        "city": "北京",
        "mode": "transit",
    }


def test_tool_openai_function_shape_is_function_calling_compatible():
    tool = BudgetCalculatorTool()

    definition = tool.to_openai_function()

    assert definition["type"] == "function"
    assert definition["function"]["name"] == "budget_calculator"
    assert definition["function"]["parameters"]["type"] == "object"
    assert "transportation" in definition["function"]["parameters"]["properties"]
    assert "days" in definition["function"]["parameters"]["required"]


def test_tool_executor_retries_timeout_and_returns_error():
    class SlowTool(BaseTool):
        name = "slow_tool"
        description = "A slow test tool"
        parameters = {"type": "object", "properties": {}, "required": []}
        attempts = 0

        async def execute(self, **kwargs):
            type(self).attempts += 1
            await asyncio.sleep(0.05)
            return {"success": True}

    SlowTool.attempts = 0
    executor = ToolExecutor(default_timeout=0.01, retry_attempts=2, retry_base_delay=0)

    result = asyncio.run(executor.execute(SlowTool()))

    assert result["success"] is False
    assert result["tool"] == "slow_tool"
    assert "超时" in result["error"]
    assert SlowTool.attempts == 2


def test_tool_cache_get_set_flow():
    class FakeRedis:
        def __init__(self):
            self.values = {}

        async def get(self, key):
            return self.values.get(key)

        async def setex(self, key, ttl, value):
            self.values[key] = value

    cache = ToolCache(FakeRedis())

    asyncio.run(cache.set("tool_cache:test", {"answer": 42}, ttl=60))
    cached = asyncio.run(cache.get("tool_cache:test"))

    assert cached == {"answer": 42}


def test_tool_executor_uses_cache_before_running_tool():
    class CachedTool(BaseTool):
        name = "cached_tool"
        description = "A cached test tool"
        parameters = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}

        async def execute(self, **kwargs):
            raise AssertionError("tool should not execute when cache hits")

    class FakeCache:
        async def get(self, key):
            return {"cached": True}

        async def set(self, key, value, ttl=300):
            raise AssertionError("cache hit should not write")

    executor = ToolExecutor(cache=FakeCache())

    result = asyncio.run(executor.execute(CachedTool(), value="x"))

    assert result == {"cached": True}


def test_llm_service_chat_with_tools_executes_tool_calls_and_returns_final_response(monkeypatch):
    class FakeResponse:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    class FakeAsyncClient:
        requests = []

        def __init__(self, *args, **kwargs):
            self._responses = [
                {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "amap_weather",
                                            "arguments": json.dumps({"city": "杭州"}, ensure_ascii=False),
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
                {
                    "choices": [{"message": {"content": "杭州天气晴，适合步行游览。"}}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 6, "total_tokens": 13},
                },
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, **kwargs):
            self.__class__.requests.append(kwargs["json"])
            return FakeResponse(self._responses.pop(0))

    class FakeToolExecutor:
        calls = []

        async def execute_by_name(self, tool_name, **kwargs):
            self.__class__.calls.append((tool_name, kwargs))
            return {"success": True, "weather": "sunny"}

    FakeAsyncClient.requests = []
    FakeToolExecutor.calls = []
    monkeypatch.setattr("app.services.llm_service.httpx.AsyncClient", FakeAsyncClient)
    service = LLMService("test-key", "https://api.openai.com/v1", "gpt-test")

    content, tool_calls, usage = asyncio.run(
        service.chat_with_tools(
            "system",
            "user",
            [{"type": "function", "function": {"name": "amap_weather", "parameters": {"type": "object"}}}],
            FakeToolExecutor(),
        )
    )

    assert content == "杭州天气晴，适合步行游览。"
    assert tool_calls == [{"tool": "amap_weather", "arguments": {"city": "杭州"}, "id": "call_1"}]
    assert usage.total_tokens == 28
    assert usage.provider == "openai"
    assert FakeToolExecutor.calls == [("amap_weather", {"city": "杭州"})]

    second_messages = FakeAsyncClient.requests[1]["messages"]
    assert second_messages[-2]["role"] == "assistant"
    assert second_messages[-2]["tool_calls"][0]["id"] == "call_1"
    assert second_messages[-1]["role"] == "tool"
    assert second_messages[-1]["tool_call_id"] == "call_1"
    assert json.loads(second_messages[-1]["content"]) == {"success": True, "weather": "sunny"}


def test_llm_service_turns_malformed_tool_arguments_into_tool_error(monkeypatch):
    class FakeResponse:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    class FakeAsyncClient:
        requests = []

        def __init__(self, *args, **kwargs):
            self._responses = [
                {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_bad",
                                        "type": "function",
                                        "function": {
                                            "name": "amap_weather",
                                            "arguments": "{bad json",
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {},
                },
                {
                    "choices": [{"message": {"content": "已忽略无效工具调用。"}}],
                    "usage": {},
                },
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, **kwargs):
            self.__class__.requests.append(kwargs["json"])
            return FakeResponse(self._responses.pop(0))

    class RejectingExecutor:
        async def execute_by_name(self, tool_name, **kwargs):
            raise AssertionError("malformed arguments must not execute a tool")

    FakeAsyncClient.requests = []
    monkeypatch.setattr("app.services.llm_service.httpx.AsyncClient", FakeAsyncClient)
    service = LLMService("test-key", "https://api.openai.com/v1", "gpt-test")

    content, tool_calls, _ = asyncio.run(
        service.chat_with_tools(
            "system",
            "user",
            [{"type": "function", "function": {"name": "amap_weather", "parameters": {"type": "object"}}}],
            RejectingExecutor(),
        )
    )

    assert content == "已忽略无效工具调用。"
    assert tool_calls[0]["id"] == "call_bad"
    assert "Malformed tool arguments" in tool_calls[0]["error"]
    tool_message = FakeAsyncClient.requests[1]["messages"][-1]
    assert json.loads(tool_message["content"])["success"] is False


def test_llm_service_chat_with_tools_respects_max_tool_rounds(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "amap_weather", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }

    class FakeAsyncClient:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            self.__class__.calls += 1
            return FakeResponse()

    class FakeToolExecutor:
        calls = []

        async def execute_by_name(self, tool_name, **kwargs):
            self.__class__.calls.append((tool_name, kwargs))
            return {"success": True}

    FakeAsyncClient.calls = 0
    FakeToolExecutor.calls = []
    monkeypatch.setattr("app.services.llm_service.httpx.AsyncClient", FakeAsyncClient)
    service = LLMService("test-key", "https://api.openai.com/v1", "gpt-test")

    content, tool_calls, usage = asyncio.run(
        service.chat_with_tools(
            "system",
            "user",
            [{"type": "function", "function": {"name": "amap_weather", "parameters": {"type": "object"}}}],
            FakeToolExecutor(),
            max_tool_rounds=0,
        )
    )

    assert content == ""
    assert tool_calls == [{"tool": "amap_weather", "arguments": {}, "id": "call_1"}]
    # After max_tool_rounds, _finalize_with_json makes an extra call → 2 calls, 30 tokens
    assert usage.total_tokens == 30
    assert FakeAsyncClient.calls == 2
    assert FakeToolExecutor.calls == []



def test_llm_service_finalize_uses_clean_messages_after_tool_round_limit(monkeypatch):
    class FakeResponse:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    class FakeAsyncClient:
        requests = []

        def __init__(self, *args, **kwargs):
            self._responses = [
                {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_unanswered",
                                        "type": "function",
                                        "function": {"name": "amap_weather", "arguments": "{}"},
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
                {
                    "choices": [{"message": {"content": "{\"city\": \"杭州\"}"}}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 6, "total_tokens": 13},
                },
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, **kwargs):
            self.__class__.requests.append(kwargs["json"])
            return FakeResponse(self._responses.pop(0))

    class FakeToolExecutor:
        calls = []

        async def execute_by_name(self, tool_name, **kwargs):
            self.__class__.calls.append((tool_name, kwargs))
            return {"success": True}

    FakeAsyncClient.requests = []
    FakeToolExecutor.calls = []
    monkeypatch.setattr("app.services.llm_service.httpx.AsyncClient", FakeAsyncClient)
    service = LLMService("test-key", "https://api.deepseek.com", "deepseek-test")

    content, tool_calls, usage = asyncio.run(
        service.chat_with_tools(
            "system",
            "user",
            [{"type": "function", "function": {"name": "amap_weather", "parameters": {"type": "object"}}}],
            FakeToolExecutor(),
            max_tool_rounds=0,
            response_format={"type": "json_object"},
        )
    )

    assert content == '{"city": "杭州"}'
    assert tool_calls == [{"tool": "amap_weather", "arguments": {}, "id": "call_unanswered"}]
    assert usage.total_tokens == 28
    assert FakeToolExecutor.calls == []

    finalize_request = FakeAsyncClient.requests[1]
    assert "tools" not in finalize_request
    assert finalize_request["response_format"] == {"type": "json_object"}
    assert all("tool_calls" not in message for message in finalize_request["messages"])
    assert all(message.get("role") != "tool" for message in finalize_request["messages"])
    assert finalize_request["messages"][-1]["role"] == "user"
    assert "call_unanswered" in finalize_request["messages"][-1]["content"]


def test_tool_registry_keyword_search_and_duplicate_guard():
    registry = ToolRegistry()
    registry.clear()
    tool = BudgetCalculatorTool()
    registry.register(tool)

    assert registry.get("budget_calculator") is tool
    assert registry.find_by_keyword("预算") == [tool]

    try:
        registry.register(tool)
    except ValueError as exc:
        assert "已注册" in str(exc)
    else:
        raise AssertionError("duplicate tool registration should fail")
