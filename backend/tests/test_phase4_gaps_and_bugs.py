import asyncio
import json
import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.db_models import AuditEvent, TripPlanVersion
from app.models.schemas import ConversationRequest, DayPlan, TripPlan, TripPlanRequest
from app.services.mock_data import build_mock_attractions, build_mock_hotels, build_mock_weather
from app.services import state_service
from app.services.llm_service import TokenUsage as LLMTokenUsage
from app.services.state_service import PlanModificationError, StateService


def _conversation_trip_plan(overall_suggestions: str = "测试行程") -> TripPlan:
    return TripPlan(
        city="北京",
        start_date="2026-07-01",
        end_date="2026-07-03",
        days=[],
        weather_info=[],
        overall_suggestions=overall_suggestions,
        budget=None,
    )


def _fake_plan_model(trip_plan: TripPlan):
    return SimpleNamespace(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        status="completed",
        version=1,
        city=trip_plan.city,
        start_date=date.fromisoformat(trip_plan.start_date),
        end_date=date.fromisoformat(trip_plan.end_date),
        days_count=len(trip_plan.days),
        plan_json=trip_plan.model_dump(),
        overall_suggestions=trip_plan.overall_suggestions,
        budget_summary=None,
    )


class FakeConversationDB:
    def __init__(self):
        self.added = []
        self.flush_count = 0

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1


class FakeModificationLLM:
    enabled = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat_with_tools(self, system_prompt, user_prompt, tools, tool_executor, max_tool_rounds=5):
        self.calls.append((system_prompt, user_prompt, tools, tool_executor, max_tool_rounds))
        return (
            self.responses.pop(0),
            [],
            LLMTokenUsage(model="gpt-test", provider="test", prompt_tokens=7, completion_tokens=5, total_tokens=12),
        )


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
    assert exc.value.detail["code"] == "PLAN_QUALITY_FAILED"
    assert exc.value.detail["retryable"] is False
    assert any("completed" in issue for issue in exc.value.detail["issues"])


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
                    metadata_json={"plan_updated": True},
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
    assert result.messages[0].plan_updated is True


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


def test_detect_modification_intent_matches_adjustment_requests():
    assert StateService._detect_modification_intent("把第二天的故宫换成天坛")
    assert StateService._detect_modification_intent("第二天少去一个博物馆，多安排公园")
    assert StateService._detect_modification_intent("第三天太赶了能不能轻松点")
    assert StateService._detect_modification_intent("帮我把行程优化一下")
    assert StateService._detect_modification_intent("第一天我想去鱼嘴湿地公园，不想去玄武湖")
    assert StateService._detect_modification_intent("我更想去颐和园")
    assert StateService._detect_modification_intent("replace day 2 attraction with Temple of Heaven")
    assert StateService._detect_modification_intent("Can you make day 2 less busy")
    assert not StateService._detect_modification_intent("天坛好玩吗")
    assert not StateService._detect_modification_intent("我想去鱼嘴湿地公园，那里怎么样？")
    assert not StateService._detect_modification_intent("介绍一下北京的历史")
    assert not StateService._detect_modification_intent("不需要修改行程，只是想确认一下")
    assert StateService._classify_modification_intent_by_rules("就按你刚才说的办") is None


def test_resolve_modification_intent_uses_llm_for_contextual_confirmation():
    service = StateService(FakeConversationDB())
    llm = FakeModificationLLM(
        [
            json.dumps(
                {
                    "should_modify": True,
                    "instruction": "把第一天的玄武湖替换为鱼嘴湿地公园",
                    "reason": "用户确认采用上一轮替换方案",
                },
                ensure_ascii=False,
            )
        ]
    )

    decision = asyncio.run(
        service._resolve_modification_intent(
            llm=llm,
            tool_executor=SimpleNamespace(),
            user_message="可以，就按你刚才说的办",
            recent_messages=[
                SimpleNamespace(role="assistant", content="建议把第一天的玄武湖替换为鱼嘴湿地公园。")
            ],
            allow_llm_fallback=True,
            conversation_id=uuid.uuid4(),
            trip_plan_id=uuid.uuid4(),
        )
    )

    assert decision.should_modify is True
    assert decision.source == "llm"
    assert decision.instruction == "把第一天的玄武湖替换为鱼嘴湿地公园"
    assert len(llm.calls) == 1
    assert "建议把第一天的玄武湖替换为鱼嘴湿地公园" in llm.calls[0][1]


def test_resolve_modification_intent_skips_llm_for_clear_question():
    service = StateService(FakeConversationDB())
    llm = FakeModificationLLM([])

    decision = asyncio.run(
        service._resolve_modification_intent(
            llm=llm,
            tool_executor=SimpleNamespace(),
            user_message="玄武湖好玩吗？",
            recent_messages=[],
            allow_llm_fallback=True,
            conversation_id=uuid.uuid4(),
            trip_plan_id=uuid.uuid4(),
        )
    )

    assert decision.should_modify is False
    assert decision.source == "rule"
    assert llm.calls == []


def test_natural_preference_request_updates_referenced_plan(monkeypatch):
    session_id = uuid.uuid4()
    original = _conversation_trip_plan()
    modified = _conversation_trip_plan("已按偏好调整为颐和园。")
    referenced_plan = _fake_plan_model(original)
    referenced_plan.session_id = session_id

    class FakeResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class FakeDB:
        def __init__(self):
            self.added = []

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            return None

        async def execute(self, statement):
            return FakeResult()

        async def scalar(self, statement):
            return referenced_plan

    class FakeLLMService:
        enabled = True

        def __init__(self, *args):
            pass

        async def chat_with_tools(self, system_prompt, user_prompt, tools, tool_executor, max_tool_rounds=5):
            return "我会把行程调整为更符合你的偏好。", [], LLMTokenUsage(model="gpt-test", provider="test")

    class FakeToolRegistry:
        def get_openai_functions(self):
            return []

    class FakeToolExecutor:
        def __init__(self, registry=None):
            self.registry = registry

    monkeypatch.setattr(state_service, "LLMService", FakeLLMService, raising=False)
    monkeypatch.setattr(state_service, "bootstrap_tools", lambda: FakeToolRegistry(), raising=False)
    monkeypatch.setattr(state_service, "ToolExecutor", FakeToolExecutor, raising=False)

    fake_db = FakeDB()
    service = StateService(fake_db)
    modification_calls = []

    async def fake_modify_plan(**kwargs):
        modification_calls.append(kwargs["user_message"])
        referenced_plan.version += 1
        referenced_plan.status = "completed"
        referenced_plan.plan_json = modified.model_dump()
        return service._plan_response(referenced_plan, modified)

    monkeypatch.setattr(service, "_modify_plan_via_conversation", fake_modify_plan)
    message = "我更想去颐和园"
    assert StateService._detect_modification_intent(message)

    result = asyncio.run(
        service.send_conversation_message(
            session_id=str(session_id),
            request=ConversationRequest(
                message=message,
                referenced_plan_id=str(referenced_plan.id),
                apply_to_plan=True,
            ),
        )
    )

    assert modification_calls == [message]
    assert result.updated_plan is not None
    assert result.updated_plan.version == 2
    assert result.updated_plan.status == "completed"
    assert any(
        getattr(item, "metadata_json", {}).get("plan_updated") is True
        for item in fake_db.added
        if getattr(item, "role", "") == "assistant"
    )


def test_apply_to_plan_requires_a_referenced_plan():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            StateService(db=None).send_conversation_message(
                session_id=str(uuid.uuid4()),
                request=ConversationRequest(message="调整一下行程", apply_to_plan=True),
            )
        )

    assert exc.value.status_code == 422


def test_modify_plan_via_conversation_writes_agent_version():
    original = _conversation_trip_plan()
    modified = _conversation_trip_plan("第二天已改为天坛。")
    referenced_plan = _fake_plan_model(original)
    fake_db = FakeConversationDB()
    service = StateService(fake_db)
    llm = FakeModificationLLM([modified.model_dump_json()])

    result = asyncio.run(
        service._modify_plan_via_conversation(
            llm=llm,
            tool_executor=SimpleNamespace(),
            referenced_plan=referenced_plan,
            user_message="把第二天的故宫换成天坛",
            session_uuid=referenced_plan.session_id,
            conversation_id=uuid.uuid4(),
        )
    )

    versions = [item for item in fake_db.added if isinstance(item, TripPlanVersion)]
    assert result is not None
    assert result.version == 2
    assert result.status == "completed"
    assert result.overall_suggestions == "第二天已改为天坛。"
    assert referenced_plan.version == 2
    assert referenced_plan.status == "completed"
    assert referenced_plan.plan_json["overall_suggestions"] == "第二天已改为天坛。"
    assert versions[0].change_type == "agent_regenerate"
    assert versions[0].change_summary == "把第二天的故宫换成天坛"
    assert any(isinstance(item, AuditEvent) and item.event_type == "trip_plan_conversation_updated" for item in fake_db.added)
    assert llm.calls[0][2] == []
    assert llm.calls[0][4] == 1


def test_modify_plan_via_conversation_reports_disabled_llm_as_failure():
    referenced_plan = _fake_plan_model(_conversation_trip_plan())
    llm = FakeModificationLLM([])
    llm.enabled = False

    with pytest.raises(PlanModificationError, match="LLM is not configured"):
        asyncio.run(
            StateService(FakeConversationDB())._modify_plan_via_conversation(
                llm=llm,
                tool_executor=SimpleNamespace(),
                referenced_plan=referenced_plan,
                user_message="把第二天改轻松一些",
                session_uuid=referenced_plan.session_id,
                conversation_id=uuid.uuid4(),
            )
        )


def test_modify_plan_via_conversation_retries_invalid_json_once():
    original = _conversation_trip_plan()
    modified = _conversation_trip_plan("已按要求重排。")
    referenced_plan = _fake_plan_model(original)
    fake_db = FakeConversationDB()
    service = StateService(fake_db)
    llm = FakeModificationLLM(["not json", modified.model_dump_json()])

    result = asyncio.run(
        service._modify_plan_via_conversation(
            llm=llm,
            tool_executor=SimpleNamespace(),
            referenced_plan=referenced_plan,
            user_message="调整一下第一天行程",
            session_uuid=referenced_plan.session_id,
            conversation_id=uuid.uuid4(),
        )
    )

    assert result is not None
    assert result.version == 2
    assert len(llm.calls) == 2
    assert "上一次输出无法解析或校验失败" in llm.calls[1][1]


def test_modify_plan_via_conversation_retries_no_change_for_confirmed_intent():
    original = _conversation_trip_plan()
    modified = _conversation_trip_plan("已把玄武湖替换为鱼嘴湿地公园。")
    referenced_plan = _fake_plan_model(original)
    service = StateService(FakeConversationDB())
    llm = FakeModificationLLM(['{"no_change": true}', modified.model_dump_json()])

    result = asyncio.run(
        service._modify_plan_via_conversation(
            llm=llm,
            tool_executor=SimpleNamespace(),
            referenced_plan=referenced_plan,
            user_message="第一天我想去鱼嘴湿地公园，不想去玄武湖",
            session_uuid=referenced_plan.session_id,
            conversation_id=uuid.uuid4(),
        )
    )

    assert result is not None
    assert result.version == 2
    assert len(llm.calls) == 2
    assert "不能返回 no_change" in llm.calls[1][1]


def test_modify_plan_via_conversation_retries_unchanged_plan():
    original = _conversation_trip_plan()
    modified = _conversation_trip_plan("已按要求改变行程。")
    referenced_plan = _fake_plan_model(original)
    service = StateService(FakeConversationDB())
    llm = FakeModificationLLM([original.model_dump_json(), modified.model_dump_json()])

    result = asyncio.run(
        service._modify_plan_via_conversation(
            llm=llm,
            tool_executor=SimpleNamespace(),
            referenced_plan=referenced_plan,
            user_message="调整第一天的景点",
            session_uuid=referenced_plan.session_id,
            conversation_id=uuid.uuid4(),
        )
    )

    assert result is not None
    assert result.version == 2
    assert len(llm.calls) == 2
    assert "与原计划完全相同" in llm.calls[1][1]


def test_modify_plan_via_conversation_raises_when_retry_fails():
    original = _conversation_trip_plan()
    referenced_plan = _fake_plan_model(original)
    fake_db = FakeConversationDB()
    service = StateService(fake_db)
    llm = FakeModificationLLM(["not json", "still not json"])

    with pytest.raises(PlanModificationError):
        asyncio.run(
            service._modify_plan_via_conversation(
                llm=llm,
                tool_executor=SimpleNamespace(),
                referenced_plan=referenced_plan,
                user_message="删除第二天行程",
                session_uuid=referenced_plan.session_id,
                conversation_id=uuid.uuid4(),
            )
        )

    assert referenced_plan.version == 1
    assert not [item for item in fake_db.added if isinstance(item, TripPlanVersion)]
    assert any(isinstance(item, AuditEvent) and item.event_type == "trip_plan_conversation_update_failed" for item in fake_db.added)


def test_send_conversation_message_notifies_when_plan_update_fails(monkeypatch):
    session_id = uuid.uuid4()
    trip_plan = _conversation_trip_plan()
    referenced_plan = _fake_plan_model(trip_plan)
    referenced_plan.session_id = session_id

    class FakeResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class FakeDB:
        def __init__(self):
            self.added = []
            self.scalar_calls = 0

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            return None

        async def execute(self, statement):
            return FakeResult()

        async def scalar(self, statement):
            self.scalar_calls += 1
            return referenced_plan

    class FakeLLMService:
        calls = []
        responses = [
            ("好的，我来帮你调整。", [], LLMTokenUsage(model="gpt-test", provider="test")),
            ("not json", [], LLMTokenUsage(model="gpt-test", provider="test")),
            ("still not json", [], LLMTokenUsage(model="gpt-test", provider="test")),
        ]

        def __init__(self, *args):
            pass

        @property
        def enabled(self):
            return True

        async def chat_with_tools(self, system_prompt, user_prompt, tools, tool_executor, max_tool_rounds=5):
            self.__class__.calls.append((system_prompt, user_prompt, tools, tool_executor, max_tool_rounds))
            return self.__class__.responses.pop(0)

    class FakeToolRegistry:
        def get_openai_functions(self):
            return []

    class FakeToolExecutor:
        def __init__(self, registry=None):
            self.registry = registry

    FakeLLMService.calls = []
    FakeLLMService.responses = [
        ("好的，我来帮你调整。", [], LLMTokenUsage(model="gpt-test", provider="test")),
        ("not json", [], LLMTokenUsage(model="gpt-test", provider="test")),
        ("still not json", [], LLMTokenUsage(model="gpt-test", provider="test")),
    ]
    monkeypatch.setattr(state_service, "LLMService", FakeLLMService, raising=False)
    monkeypatch.setattr(state_service, "bootstrap_tools", lambda: FakeToolRegistry(), raising=False)
    monkeypatch.setattr(state_service, "ToolExecutor", FakeToolExecutor, raising=False)

    fake_db = FakeDB()
    service = StateService(fake_db)

    result = asyncio.run(
        service.send_conversation_message(
            session_id=str(session_id),
            request=ConversationRequest(message="删除第二天行程", referenced_plan_id=str(referenced_plan.id)),
        )
    )

    assistants = [item for item in fake_db.added if getattr(item, "role", "") == "assistant"]
    assert result.updated_plan is None
    assert result.plan_update_failed is True
    assert "没有成功保存修改" in result.content
    assert assistants[-1].metadata_json["plan_update_failed"] is True
    assert len(FakeLLMService.calls) == 3
