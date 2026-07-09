from fastapi.testclient import TestClient

from app.api.deps import get_state_service
from app.api.main import app
from app.models.schemas import TripPlanRequest


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


def test_create_trip_plan_returns_trip_plan_payload():
    app.dependency_overrides[get_state_service] = lambda: FakeStateService()
    response = client.post(
        "/api/trip/plan",
        json={
            "session_id": "11111111-1111-1111-1111-111111111111",
            "city": "北京",
            "start_date": "2026-06-01",
            "end_date": "2026-06-03",
            "days": 3,
            "preferences": "历史文化",
            "budget": "中等",
            "transportation": "公共交通",
            "accommodation": "经济型酒店",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["plan_id"] == "plan-1"
    assert body["status"] == "completed"
    assert body["city"] == "北京"
    assert len(body["days"]) == 0
    assert "budget" in body
    app.dependency_overrides.clear()
