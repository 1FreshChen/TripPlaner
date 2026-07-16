from fastapi.testclient import TestClient

from app.api.deps import get_state_service, get_trip_task_service
from app.api.main import app
from app.models.schemas import TripPlanRequest, TripPlanTaskCreatedResponse
from app.tasks.queue import get_task_queue


client = TestClient(app)


class FakeStateService:
    async def create_trip_plan(self, request: TripPlanRequest):
        return {
            "plan_id": "plan-1",
            "status": "completed",
            "version": 1,
            "city": request.city,
            "start_date": request.start_date,
            "end_date": request.end_date,
            "days": [],
            "weather_info": [],
            "overall_suggestions": "测试行程",
            "budget": None,
        }


class FakeTaskService:
    def __init__(self):
        self.commits = 0

    async def create_task(self, request: TripPlanRequest):
        return TripPlanTaskCreatedResponse(
            task_id="tp_20260713_test",
            status="queued",
            status_url="/api/trip/tasks/tp_20260713_test",
            events_url="/api/trip/tasks/tp_20260713_test/events",
            result_url="/api/trip/tasks/tp_20260713_test/result",
        )

    async def commit(self):
        self.commits += 1

    async def mark_enqueue_failed(self, task_id: str, error: Exception):
        raise AssertionError("queue should not fail")


class FakeQueue:
    def __init__(self):
        self.calls = []

    async def enqueue_job(self, function: str, *args, **kwargs):
        self.calls.append((function, args, kwargs))
        return object()


def _payload():
    return {
        "session_id": "11111111-1111-1111-1111-111111111111",
        "city": "北京",
        "start_date": "2026-06-01",
        "end_date": "2026-06-03",
        "days": 3,
        "preferences": "历史文化",
        "budget": "中等",
        "transportation": "公共交通",
        "accommodation": "经济型酒店",
    }


def test_create_trip_plan_returns_accepted_task():
    tasks = FakeTaskService()
    queue = FakeQueue()
    app.dependency_overrides[get_trip_task_service] = lambda: tasks
    app.dependency_overrides[get_task_queue] = lambda: queue
    response = client.post(
        "/api/trip/plan",
        json=_payload(),
    )

    assert response.status_code == 202
    body = response.json()
    assert body["task_id"] == "tp_20260713_test"
    assert body["status"] == "queued"
    assert body["events_url"].endswith("/events")
    assert tasks.commits == 1
    assert queue.calls == [
        (
            "generate_trip_plan_task",
            ("tp_20260713_test",),
            {"_job_id": "tp_20260713_test"},
        )
    ]
    app.dependency_overrides.clear()


def test_original_synchronous_plan_flow_remains_available():
    app.dependency_overrides[get_state_service] = lambda: FakeStateService()
    response = client.post("/api/trip/plan/sync", json=_payload())

    assert response.status_code == 201
    assert response.json()["plan_id"] == "plan-1"
    app.dependency_overrides.clear()
