import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.deps import get_trip_task_reader
from app.api.main import app
from app.models.db_models import TripPlanTask
from app.models.schemas import TripPlanTaskStatusResponse
from app.services.task_service import (
    TripPlanTaskService,
    cleanup_expired_tasks,
    recover_stale_queued_tasks,
    transition_task,
)
from app.tasks import worker


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


class FakeMaintenanceDB:
    def __init__(self, tasks):
        self.tasks = tasks
        self.commit_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def scalars(self, statement):
        return SimpleNamespace(all=lambda: self.tasks)

    async def commit(self):
        self.commit_calls += 1


class FakeMaintenanceFactory:
    def __init__(self, db):
        self.db = db

    def __call__(self):
        return self.db


def test_task_maintenance_expires_and_recovers_stale_tasks():
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    expired = _task(now - timedelta(days=2))
    expired.expires_at = now - timedelta(seconds=1)
    expired_db = FakeMaintenanceDB([expired])

    stale = _task(now - timedelta(minutes=10))
    stale.expires_at = now + timedelta(days=1)
    stale_db = FakeMaintenanceDB([stale])

    assert asyncio.run(cleanup_expired_tasks(FakeMaintenanceFactory(expired_db), now=now)) == 1
    assert expired.status == "expired"
    assert expired.error_code == "TASK_EXPIRED"
    assert expired.finished_at == now

    assert asyncio.run(
        recover_stale_queued_tasks(
            FakeMaintenanceFactory(stale_db),
            stale_threshold_seconds=120,
            now=now,
        )
    ) == 1
    assert stale.status == "failed"
    assert stale.error_code == "ENQUEUE_LOST"
    assert stale.finished_at == now

def test_worker_always_delegates_to_langgraph_runtime(monkeypatch):
    calls = []

    async def fake_run(ctx, task_id):
        calls.append((ctx, task_id))
        return "plan-id"

    monkeypatch.setattr(worker, "_run_langgraph_task", fake_run)

    result = asyncio.run(worker.generate_trip_plan_task({"runtime": True}, "task-id"))

    assert result == "plan-id"
    assert calls == [({"runtime": True}, "task-id")]


class CompletedTaskService:
    async def get_status(self, task_id: str, session_id: str | None = None):
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
    async def get_status(self, task_id: str, session_id: str | None = None):
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
        response = client.get(
            "/api/trip/tasks/tp_20260713_test/events",
            headers={"X-Session-ID": "11111111-1111-1111-1111-111111111111"},
        )

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
            response = client.get(
                "/api/trip/tasks/tp_20260713_stalled/events",
                headers={"X-Session-ID": "11111111-1111-1111-1111-111111111111"},
            )

    assert response.status_code == 200
    assert "event: timeout" in response.text
    assert "已切换为状态轮询" in response.text
    app.dependency_overrides.clear()
