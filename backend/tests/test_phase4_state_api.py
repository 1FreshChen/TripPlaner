from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.api.deps import get_state_service
from app.api.main import app


class FakeStateService:
    def __init__(self):
        self.plan = {
            "plan_id": "plan-1",
            "status": "completed",
            "version": 1,
            "city": "北京",
            "start_date": "2026-07-01",
            "end_date": "2026-07-03",
            "days": [],
            "weather_info": [],
            "overall_suggestions": "测试行程",
            "budget": None,
        }
        self.versions = [{"version": 1, "change_summary": "初始创建", "created_at": "2026-06-29T10:00:00Z"}]
        self.saved_items = []

    async def create_session(self):
        return {
            "session_id": "11111111-1111-1111-1111-111111111111",
            "user_id": "22222222-2222-2222-2222-222222222222",
            "created_at": "2026-06-29T10:00:00Z",
            "is_new": True,
        }

    async def get_session(self, session_id):
        return {
            "session_id": session_id,
            "user_id": "22222222-2222-2222-2222-222222222222",
            "trip_plans": [
                {
                    "id": "plan-1",
                    "city": "北京",
                    "start_date": "2026-07-01",
                    "end_date": "2026-07-03",
                    "status": "completed",
                    "version": 1,
                    "created_at": "2026-06-29T10:00:00Z",
                    "updated_at": "2026-06-29T10:00:00Z",
                }
            ],
            "conversation_count": 2,
            "created_at": "2026-06-29T10:00:00Z",
            "updated_at": "2026-06-29T10:00:00Z",
        }

    async def create_trip_plan(self, request):
        self.plan["city"] = request.city
        return self.plan

    async def get_trip_plan(self, plan_id, session_id=None):
        return {**self.plan, "plan_id": plan_id}

    async def update_trip_plan(self, plan_id, update, session_id=None):
        self.plan = {**update.plan_json.model_dump(), "plan_id": plan_id, "status": "completed", "version": 2}
        self.versions.insert(0, {"version": 2, "change_summary": update.change_summary, "created_at": "2026-06-29T10:01:00Z"})
        return self.plan

    async def list_plan_versions(self, plan_id, session_id=None):
        return {"versions": self.versions}

    async def revert_plan(self, plan_id, version, session_id=None):
        self.plan = {**self.plan, "plan_id": plan_id, "status": "completed", "version": 3}
        return self.plan

    async def archive_plan(self, plan_id, session_id=None):
        self.plan["status"] = "archived"

    async def send_conversation_message(self, session_id, request):
        return {
            "message_id": "33333333-3333-3333-3333-333333333333",
            "role": "assistant",
            "content": f"已收到：{request.message}",
            "tool_calls": [],
            "updated_plan": None,
        }

    async def list_conversation(self, session_id, limit=50, before_id=None):
        return {
            "messages": [
                {"id": "m1", "role": "user", "content": "你好", "tool_calls": None, "created_at": "2026-06-29T10:00:00Z"},
                {
                    "id": "m2",
                    "role": "assistant",
                    "content": "你好，我可以帮你调整行程。",
                    "tool_calls": [],
                    "created_at": "2026-06-29T10:00:01Z",
                },
            ],
            "has_more": False,
        }

    async def get_preferences(self, user_id):
        return {
            "preferred_categories": ["历史文化"],
            "budget_profile": {"中等": 1},
            "travel_style": "慢节奏",
            "favorite_cities": ["北京"],
        }

    async def update_preferences(self, user_id, request):
        return request.model_dump()

    async def list_saved_items(self, user_id):
        return self.saved_items

    async def create_saved_item(self, user_id, request):
        item = {
            **request.model_dump(),
            "id": "44444444-4444-4444-4444-444444444444",
            "created_at": "2026-06-29T10:02:00Z",
        }
        self.saved_items.insert(0, item)
        return item

    async def delete_saved_item(self, user_id, item_id):
        self.saved_items = [item for item in self.saved_items if item["id"] != item_id]

    async def _get_user_by_session(self, session_id):
        from types import SimpleNamespace
        return SimpleNamespace(id="22222222-2222-2222-2222-222222222222")


@contextmanager
def client_with_fake_state():
    fake = FakeStateService()
    app.dependency_overrides[get_state_service] = lambda: fake
    try:
        with TestClient(app) as client:
            yield client, fake
    finally:
        app.dependency_overrides.clear()


def test_phase4_session_endpoints():
    with client_with_fake_state() as (client, _):
        response = client.post("/api/sessions")

        assert response.status_code == 201
        body = response.json()
        assert body["session_id"] == "11111111-1111-1111-1111-111111111111"
        assert body["is_new"] is True

        detail = client.get(f"/api/sessions/{body['session_id']}")
        assert detail.status_code == 200
        assert detail.json()["conversation_count"] == 2


def test_phase4_trip_plan_crud_and_versions():
    with client_with_fake_state() as (client, _):
        session_id = "11111111-1111-1111-1111-111111111111"
        headers = {"X-Session-ID": session_id}

        created = client.post(
            "/api/trip/plan/sync",
            headers=headers,
            json={
                "session_id": session_id,
                "city": "北京",
                "start_date": "2026-07-01",
                "end_date": "2026-07-03",
                "days": 3,
                "preferences": "历史文化",
                "budget": "中等",
                "transportation": "公共交通",
                "accommodation": "经济型酒店",
            },
        )
        assert created.status_code == 201
        assert created.json()["plan_id"] == "plan-1"
        assert created.json()["status"] == "completed"

        plan = created.json()
        plan["overall_suggestions"] = "已调整"
        updated = client.put(
            "/api/trip/plan/plan-1",
            headers=headers,
            json={"plan_json": plan, "expected_version": 1, "change_summary": "调整总体建议"},
        )
        assert updated.status_code == 200
        assert updated.json()["version"] == 2
        assert updated.json()["status"] == "completed"

        versions = client.get("/api/trip/plan/plan-1/versions", headers=headers)
        assert versions.status_code == 200
        assert versions.json()["versions"][0]["version"] == 2

        reverted = client.post("/api/trip/plan/plan-1/revert/1", headers=headers)
        assert reverted.status_code == 200
        assert reverted.json()["version"] == 3

        archived = client.delete("/api/trip/plan/plan-1", headers=headers)
        assert archived.status_code == 204


def test_phase4_conversation_and_preferences_endpoints():
    with client_with_fake_state() as (client, _):
        session_id = "11111111-1111-1111-1111-111111111111"

        reply = client.post(f"/api/conversation/{session_id}", json={"message": "帮我调整第二天"})
        assert reply.status_code == 200
        assert reply.json()["role"] == "assistant"
        assert reply.json()["tool_calls"] == []

        history = client.get(f"/api/conversation/{session_id}")
        assert history.status_code == 200
        assert len(history.json()["messages"]) == 2

        preferences = client.get(f"/api/preferences?session_id={session_id}")
        assert preferences.status_code == 200
        assert preferences.json()["favorite_cities"] == ["北京"]

        updated = client.put(
            f"/api/preferences?session_id={session_id}",
            json={
                "preferred_categories": ["自然风光"],
                "budget_profile": {"舒适": 1},
                "travel_style": "轻松",
                "favorite_cities": ["杭州"],
            },
        )
        assert updated.status_code == 200
        assert updated.json()["favorite_cities"] == ["杭州"]


def test_saved_item_endpoints_persist_and_delete_items():
    with client_with_fake_state() as (client, _):
        session_id = "11111111-1111-1111-1111-111111111111"
        created = client.post(
            f"/api/saved-items?session_id={session_id}",
            json={
                "item_type": "attraction",
                "item_data": {"name": "故宫", "city": "北京"},
                "tags": ["历史文化"],
                "note": "上午参观",
            },
        )

        assert created.status_code == 201
        assert created.json()["item_data"]["name"] == "故宫"

        listed = client.get(f"/api/saved-items?session_id={session_id}")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [created.json()["id"]]

        deleted = client.delete(
            f"/api/saved-items/{created.json()['id']}?session_id={session_id}"
        )
        assert deleted.status_code == 204
        assert client.get(f"/api/saved-items?session_id={session_id}").json() == []
