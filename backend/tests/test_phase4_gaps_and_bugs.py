import asyncio
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.schemas import ConversationRequest, DayPlan, TripPlan, TripPlanRequest
from app.services.mock_data import build_mock_attractions, build_mock_hotels, build_mock_weather
from app.services import state_service
from app.services.llm_service import TokenUsage as LLMTokenUsage
from app.services.state_service import StateService


def test_plan_response_raises_http_exception_when_plan_json_is_missing():
    service = StateService(db=None)
    plan = SimpleNamespace(id=uuid.uuid4(), status="generating", version=1, plan_json=None)

    with pytest.raises(HTTPException) as exc:
        service._plan_response(plan)

    assert exc.value.status_code == 500
    assert "plan_json" in exc.value.detail


def test_state_service_rejects_completed_plan_when_latest_critique_requires_revision():
    request = TripPlanRequest(
        city="台州",
        start_date="2026-07-17",
        end_date="2026-07-19",
        days=3,
        preferences="自然风光",
        budget="中等",
        transportation="公共交通",
        accommodation="经济型酒店",
    )
    weather = build_mock_weather(request.start_date, request.days)
    attractions = build_mock_attractions(request.city, request.preferences, request.days)
    hotels = build_mock_hotels(request.city, request.accommodation, request.budget)
    trip_plan = TripPlan(
        city=request.city,
        start_date=request.start_date,
        end_date=request.end_date,
        days=[
            DayPlan(
                date=item.date,
                day_index=index,
                description=f"第{index + 1}天台州自然风光行程。",
                transportation=request.transportation,
                accommodation=request.accommodation,
                hotel=hotels[index % len(hotels)],
                attractions=[attractions[index], attractions[(index + 1) % len(attractions)]],
                meals=[],
            )
            for index, item in enumerate(weather)
        ],
        weather_info=weather,
        overall_suggestions="按天气安排节奏。",
        budget=None,
    )
    service = StateService(db=None)

    with pytest.raises(HTTPException) as exc:
        service._ensure_completed_plan_is_publishable(
            request,
            trip_plan,
            critique_events=[{"details": {"needs_revision": True, "revision_summary": "仍需修改"}}],
        )

    assert exc.value.status_code == 422
    assert "不能保存" in exc.value.detail


def test_list_conversation_uses_before_id_cursor():
    session_id = uuid.uuid4()
    before_id = uuid.uuid4()
    before_created_at = datetime(2026, 6, 30, 10, 0, 0)

    class FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [
                SimpleNamespace(
                    id=uuid.uuid4(),
                    role="user",
                    content="older",
                    tool_calls_json=None,
                    created_at=before_created_at - timedelta(minutes=1),
                )
            ]

    class FakeDB:
        def __init__(self):
            self.scalar_calls = []

        async def scalar(self, statement):
            self.scalar_calls.append(statement)
            return SimpleNamespace(id=before_id, created_at=before_created_at)

        async def execute(self, statement):
            self.last_execute_statement = statement
            return FakeResult()

    fake_db = FakeDB()
    service = StateService(fake_db)

    result = asyncio.run(
        service.list_conversation(
            session_id=str(session_id),
            limit=1,
            before_id=str(before_id),
        )
    )

    assert fake_db.scalar_calls, "before_id should load the cursor message"
    assert len(result.messages) == 1
    assert result.messages[0].content == "older"


def test_send_conversation_message_calls_llm_tools_and_records_usage(monkeypatch):
    session_id = uuid.uuid4()

    class FakeDB:
        def __init__(self):
            self.added = []

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            return None

        async def execute(self, statement):
            class EmptyResult:
                def scalars(self):
                    return self

                def all(self):
                    return []

            return EmptyResult()

        async def scalar(self, statement):
            return None

    class FakeLLMService:
        calls = []

        def __init__(self, *args):
            pass

        async def chat_with_tools(self, system_prompt, user_prompt, tools, tool_executor, max_tool_rounds=5):
            self.__class__.calls.append((system_prompt, user_prompt, tools, tool_executor, max_tool_rounds))
            await tool_executor.execute_by_name("fake_weather", city="Beijing")
            return (
                "Move day two to a park walk.",
                [{"tool": "fake_weather", "arguments": {"city": "Beijing"}, "id": "call_1"}],
                LLMTokenUsage(model="gpt-test", provider="test", prompt_tokens=7, completion_tokens=5, total_tokens=12),
            )

    class FakeToolRegistry:
        def get_openai_functions(self):
            return [{"type": "function", "function": {"name": "fake_weather", "parameters": {"type": "object"}}}]

        def get(self, name):
            return SimpleNamespace(name=name)

    class FakeToolExecutor:
        calls = []

        def __init__(self, registry=None):
            self.registry = registry

        async def execute_by_name(self, tool_name, **kwargs):
            self.__class__.calls.append((tool_name, kwargs))
            return {"success": True, "weather": "sunny"}

    FakeLLMService.calls = []
    FakeToolExecutor.calls = []
    monkeypatch.setattr(state_service, "LLMService", FakeLLMService, raising=False)
    monkeypatch.setattr(state_service, "bootstrap_tools", lambda: FakeToolRegistry(), raising=False)
    monkeypatch.setattr(state_service, "ToolExecutor", FakeToolExecutor, raising=False)

    fake_db = FakeDB()
    service = StateService(fake_db)

    result = asyncio.run(
        service.send_conversation_message(
            session_id=str(session_id),
            request=ConversationRequest(message="less museum time, more parks"),
        )
    )

    assert result.content == "Move day two to a park walk."
    assert result.tool_calls == [{"tool": "fake_weather", "arguments": {"city": "Beijing"}, "id": "call_1"}]
    assert FakeToolExecutor.calls == [("fake_weather", {"city": "Beijing"})]
    assert len(FakeLLMService.calls) == 1
    assert any(getattr(item, "total_tokens", 0) == 12 for item in fake_db.added)
