from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.deps import get_trip_task_reader
from app.api.main import app
from app.models.db_models import TripPlanTask
from app.models.schemas import TripPlanTaskStatusResponse
from app.services.task_service import TripPlanTaskService, transition_task


def _task(now: datetime) -> TripPlanTask:
    return TripPlanTask(
        task_id="tp_20260713_test",
        request_payload={},
        status="queued",
        phase="queued",
        progress=0,
        message="任务已进入队列",
        phase_timings={},
        retry_count=0,
        queued_at=now,
        phase_started_at=now,
        updated_at=now,
    )


def test_transition_task_records_real_phase_elapsed_time():
    started = datetime(2026, 7, 13, tzinfo=timezone.utc)
    task = _task(started)

    transition_task(
        task,
        status="running",
        phase="preparing",
        progress=5,
        message="正在准备",
        now=started,
    )
    transition_task(
        task,
        status="running",
        phase="llm_planning",
        progress=35,
        message="正在生成",
        now=started + timedelta(seconds=2),
    )

    assert task.phase_timings == {"queued": 0, "preparing": 2000}
    assert task.progress == 35
    assert task.started_at == started


def test_task_status_response_reports_elapsed_time_and_result_url():
    started = datetime(2026, 7, 13, tzinfo=timezone.utc)
    task = _task(started)
    task.status = "succeeded"
    task.phase = "completed"
    task.progress = 100
    task.started_at = started + timedelta(seconds=1)
    task.phase_started_at = started + timedelta(seconds=4)
    task.finished_at = started + timedelta(seconds=5)

    response = TripPlanTaskService.to_status_response(task, now=started + timedelta(seconds=10))

    assert response.elapsed_ms == 5000
    assert response.phase_elapsed_ms == 1000
    assert response.result_url == "/api/trip/tasks/tp_20260713_test/result"


class CompletedTaskService:
    async def get_status(self, task_id: str):
        return TripPlanTaskStatusResponse(
            task_id=task_id,
            status="succeeded",
            phase="completed",
            progress=100,
            message="行程规划已完成",
            queued_at="2026-07-13T00:00:00+00:00",
            started_at="2026-07-13T00:00:01+00:00",
            finished_at="2026-07-13T00:00:02+00:00",
            updated_at="2026-07-13T00:00:02+00:00",
            elapsed_ms=1000,
            phase_elapsed_ms=0,
            result_url=f"/api/trip/tasks/{task_id}/result",
        )


class RunningTaskService:
    async def get_status(self, task_id: str):
        return TripPlanTaskStatusResponse(
            task_id=task_id,
            status="running",
            phase="llm_planning",
            progress=35,
            message="正在生成每日行程",
            queued_at="2026-07-13T00:00:00+00:00",
            started_at="2026-07-13T00:00:01+00:00",
            updated_at="2026-07-13T00:00:02+00:00",
            elapsed_ms=1000,
            phase_elapsed_ms=1000,
        )


def test_sse_emits_completed_event_from_persisted_status():
    app.dependency_overrides[get_trip_task_reader] = lambda: CompletedTaskService()
    with TestClient(app) as client:
        response = client.get("/api/trip/tasks/tp_20260713_test/events")

    assert response.status_code == 200
    assert "event: completed" in response.text
    assert '"progress":100' in response.text
    app.dependency_overrides.clear()


def test_sse_closes_stalled_connection_after_configured_timeout():
    app.dependency_overrides[get_trip_task_reader] = lambda: RunningTaskService()
    with patch(
        "app.api.routes.trip.get_settings",
        return_value=SimpleNamespace(task_sse_timeout_seconds=0),
    ):
        with TestClient(app) as client:
            response = client.get("/api/trip/tasks/tp_20260713_stalled/events")

    assert response.status_code == 200
    assert "event: timeout" in response.text
    assert "已切换为状态轮询" in response.text
    app.dependency_overrides.clear()
