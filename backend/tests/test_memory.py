import asyncio
import uuid
from types import SimpleNamespace

from app.agents.trip_planner import build_planner_query
from app.models.schemas import DayPlan, TripPlan, TripPlanRequest
from app.services import state_service
from app.services.mock_data import build_mock_attractions, build_mock_hotels, build_mock_weather
from app.services.state_service import StateService


def run(coro):
    return asyncio.run(coro)


class ResultList:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


def test_short_term_memory_keeps_window_and_persists_messages():
    from app.memory.short_term import ShortTermMemory

    session_id = uuid.uuid4()

    class FakeDB:
        def __init__(self):
            self.added = []

        def add(self, value):
            self.added.append(value)

    memory = ShortTermMemory(session_id=session_id, max_messages=20)
    fake_db = FakeDB()

    async def add_messages():
        for index in range(21):
            await memory.add(
                role="user",
                content=f"message-{index}",
                db=fake_db,
                metadata={"index": index},
            )

    run(add_messages())

    context = memory.get_context()
    assert len(context) == 20
    assert context[0] == {"role": "user", "content": "message-1"}
    assert context[-1] == {"role": "user", "content": "message-20"}
    assert len(fake_db.added) == 21
    assert fake_db.added[-1].metadata_json == {"index": 20}


def test_short_term_memory_restores_recent_messages_from_db():
    from app.memory.short_term import ShortTermMemory

    rows = [
        SimpleNamespace(
            role="user",
            content="hello",
            tool_calls_json=None,
            tool_name=None,
            metadata_json={"source": "db"},
            created_at="2026-06-30T10:00:00",
        ),
        SimpleNamespace(
            role="assistant",
            content="hi",
            tool_calls_json=[],
            tool_name=None,
            metadata_json={},
            created_at="2026-06-30T10:00:01",
        ),
    ]

    class FakeDB:
        async def execute(self, statement):
            self.statement = statement
            return ResultList(rows)

    memory = ShortTermMemory(session_id=uuid.uuid4(), max_messages=20)
    fake_db = FakeDB()

    run(memory.restore_from_db(fake_db))

    assert memory.get_context() == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    assert memory.get_last_n(1)[0]["metadata"] == {}


def test_long_term_memory_incrementally_updates_user_preferences():
    from app.memory.long_term import LongTermMemory
    from app.models.db_models import UserPreference

    user_id = uuid.uuid4()
    prefs = UserPreference(
        id=uuid.uuid4(),
        user_id=user_id,
        preferred_categories=["美食"],
        budget_profile={"中等": 2},
        avg_trip_days=2.0,
        favorite_cities=["上海"],
    )

    class FakeDB:
        def __init__(self):
            self.flush_count = 0

        async def execute(self, statement):
            return ResultList([prefs])

        async def flush(self):
            self.flush_count += 1

    fake_db = FakeDB()
    memory = LongTermMemory()

    run(
        memory.update_from_trip(
            user_id=user_id,
            db=fake_db,
            city="北京",
            preferences=["历史文化", "美食", " "],
            budget_level="中等",
            days=4,
        )
    )

    assert set(prefs.preferred_categories) == {"美食", "历史文化"}
    assert prefs.budget_profile == {"中等": 3}
    assert float(prefs.avg_trip_days) == 3.0
    assert prefs.favorite_cities == ["上海", "北京"]
    assert fake_db.flush_count == 1


def test_memory_recall_returns_preferences_and_same_city_saved_attractions():
    from app.memory.long_term import LongTermMemory
    from app.memory.recall import MemoryRecall
    from app.models.db_models import SavedItem, UserPreference

    user_id = uuid.uuid4()
    prefs = UserPreference(
        id=uuid.uuid4(),
        user_id=user_id,
        preferred_categories=["历史文化"],
        budget_profile={"中等": 3, "舒适": 1},
        travel_style="慢节奏",
        avg_trip_days=3.0,
        favorite_cities=["北京"],
    )
    saved_items = [
        SavedItem(
            id=uuid.uuid4(),
            user_id=user_id,
            item_type="attraction",
            item_data={"name": "故宫", "city": "北京"},
            tags=["文化"],
        ),
        SavedItem(
            id=uuid.uuid4(),
            user_id=user_id,
            item_type="attraction",
            item_data={"name": "外滩", "city": "上海"},
            tags=["城市"],
        ),
    ]

    class FakeDB:
        def __init__(self):
            self.calls = 0

        async def execute(self, statement):
            self.calls += 1
            return ResultList([prefs] if self.calls == 1 else saved_items)

        async def flush(self):
            return None

    recalled = run(MemoryRecall(LongTermMemory()).recall(user_id, FakeDB(), city="北京", preferences="历史文化"))

    assert recalled["preferred_categories"] == ["历史文化"]
    assert recalled["budget_profile"] == {"中等": 3, "舒适": 1}
    assert recalled["travel_style"] == "慢节奏"
    assert recalled["avg_trip_days"] == 3.0
    assert recalled["saved_attractions_in_city"] == [{"name": "故宫", "city": "北京"}]


def test_planner_query_includes_recalled_memory_and_conversation_context():
    request = TripPlanRequest(
        city="北京",
        start_date="2026-07-01",
        end_date="2026-07-03",
        days=3,
        preferences="历史文化",
        budget="中等",
        transportation="公共交通",
        accommodation="经济型酒店",
    )

    query = build_planner_query(
        request=request,
        attraction_response="故宫",
        weather_response="晴",
        hotel_response="王府井酒店",
        memory_context={
            "preferred_categories": ["历史文化"],
            "budget_profile": {"中等": 3},
            "favorite_cities": ["北京"],
            "saved_attractions_in_city": [{"name": "故宫", "city": "北京"}],
            "avg_trip_days": 3.0,
        },
        conversation_context=[
            {"role": "user", "content": "我想少走路"},
            {"role": "assistant", "content": "会安排慢节奏"},
        ],
    )

    assert "历史记忆" in query
    assert "历史文化" in query
    assert "中等: 3" in query
    assert "故宫" in query
    assert "user: 我想少走路" in query


def test_state_service_create_trip_plan_uses_memory_recall_and_updates_preferences(monkeypatch):
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    captured = {}

    request = TripPlanRequest(
        session_id=str(session_id),
        city="北京",
        start_date="2026-07-01",
        end_date="2026-07-03",
        days=3,
        preferences="历史文化,博物馆",
        budget="中等",
        transportation="公共交通",
        accommodation="经济型酒店",
    )

    class FakeLongTermMemory:
        async def update_from_trip(self, **kwargs):
            captured["updated_trip"] = kwargs

    class FakeMemoryRecall:
        def __init__(self, long_term_memory):
            self._ltm = long_term_memory

        async def recall(self, user_id, db, city, preferences):
            captured["recall_args"] = (user_id, city, preferences)
            return {"preferred_categories": ["历史文化"], "budget_profile": {"中等": 2}}

    class FakeShortTermMemory:
        def __init__(self, session_id, max_messages=20):
            captured["short_term_session"] = session_id

        async def restore_from_db(self, db):
            captured["short_term_restored"] = True

        def get_context(self):
            return [{"role": "user", "content": "少走路"}]

    class FakeTripPlannerAgent:
        def __init__(self, **kwargs):
            captured["planner_init_kwargs"] = kwargs
            self.last_critique_events = []

        async def aplan_trip(self, request, memory_context=None, conversation_context=None):
            captured["planner_memory_context"] = memory_context
            captured["planner_conversation_context"] = conversation_context
            self.last_critique_events = [
                {
                    "event_type": "plan_critique",
                    "severity": "info",
                    "details": {
                        "round": 0,
                        "average_score": 8.0,
                        "needs_revision": False,
                    },
                }
            ]
            weather = build_mock_weather(request.start_date, request.days)
            attractions = build_mock_attractions(request.city, request.preferences, request.days)
            hotels = build_mock_hotels(request.city, request.accommodation, request.budget)
            return TripPlan(
                city=request.city,
                start_date=request.start_date,
                end_date=request.end_date,
                days=[
                    DayPlan(
                        date=item.date,
                        day_index=index,
                        description=f"第{index + 1}天围绕{request.city}安排记忆测试行程。",
                        transportation=request.transportation,
                        accommodation=request.accommodation,
                        hotel=hotels[index % len(hotels)],
                        attractions=[attractions[index], attractions[(index + 1) % len(attractions)]],
                        meals=[],
                    )
                    for index, item in enumerate(weather)
                ],
                weather_info=weather,
                overall_suggestions="测试计划",
                budget=None,
            )

    class FakeDB:
        def __init__(self):
            self.added = []

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            return None

        async def scalar(self, statement):
            return SimpleNamespace(id=user_id, session_token=str(session_id))

    monkeypatch.setattr(state_service, "LongTermMemory", FakeLongTermMemory, raising=False)
    monkeypatch.setattr(state_service, "MemoryRecall", FakeMemoryRecall, raising=False)
    monkeypatch.setattr(state_service, "ShortTermMemory", FakeShortTermMemory, raising=False)
    monkeypatch.setattr(state_service, "TripPlannerAgent", FakeTripPlannerAgent, raising=False)

    fake_db = FakeDB()
    response = run(StateService(fake_db).create_trip_plan(request))

    assert response.city == "北京"
    assert "tool_registry" in captured["planner_init_kwargs"]
    assert "tool_executor" in captured["planner_init_kwargs"]
    assert captured["recall_args"] == (user_id, "北京", "历史文化,博物馆")
    assert captured["short_term_session"] == session_id
    assert captured["short_term_restored"] is True
    assert captured["planner_memory_context"] == {"preferred_categories": ["历史文化"], "budget_profile": {"中等": 2}}
    assert captured["planner_conversation_context"] == [{"role": "user", "content": "少走路"}]
    assert captured["updated_trip"]["user_id"] == user_id
    assert captured["updated_trip"]["city"] == "北京"
    assert captured["updated_trip"]["preferences"] == ["历史文化", "博物馆"]
    assert captured["updated_trip"]["budget_level"] == "中等"
    assert captured["updated_trip"]["days"] == 3
    critique_events = [item for item in fake_db.added if getattr(item, "event_type", None) == "plan_critique"]
    assert len(critique_events) == 1
    assert critique_events[0].details_json["average_score"] == 8.0
    assert critique_events[0].resource_type == "trip_plan"
