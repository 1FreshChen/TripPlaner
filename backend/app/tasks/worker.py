from __future__ import annotations

import asyncio
import uuid

from arq.worker import func
from sqlalchemy import select

from app.config import get_settings
from app.database import get_session_factory
from app.models.db_models import TripPlanTask
from app.models.schemas import TripPlanRequest
from app.services.amap_mcp_service import close_amap_mcp_service
from app.services.state_service import StateService
from app.services.task_service import fail_task, transition_task, update_task_progress, utc_now
from app.tasks.queue import redis_settings_from_url


async def generate_trip_plan_task(ctx: dict, task_id: str) -> str | None:
    session_factory = get_session_factory()
    async with session_factory() as lookup_db:
        task = await lookup_db.scalar(select(TripPlanTask).where(TripPlanTask.task_id == task_id))
        if task is None or task.status in {"succeeded", "cancelled", "expired"}:
            return None
        request_payload = dict(task.request_payload)

    async def report_progress(phase: str, progress: int, message: str) -> None:
        await update_task_progress(session_factory, task_id, phase, progress, message)

    async with session_factory() as db:
        try:
            result = await StateService(db).create_trip_plan(
                TripPlanRequest.model_validate(request_payload),
                progress_callback=report_progress,
            )
            task = await db.scalar(select(TripPlanTask).where(TripPlanTask.task_id == task_id).with_for_update())
            if task is None:
                raise RuntimeError(f"Task {task_id} disappeared before completion")
            now = utc_now()
            transition_task(
                task,
                status="succeeded",
                phase="completed",
                progress=100,
                message="行程规划已完成",
                now=now,
            )
            task.result_plan_id = uuid.UUID(result.plan_id)
            task.result_payload = result.model_dump(mode="json")
            task.finished_at = now
            task.error_code = None
            task.error_message = None
            await db.commit()
            return result.plan_id
        except Exception as exc:
            await db.rollback()
            await fail_task(session_factory, task_id, exc)
            raise


async def shutdown_worker(ctx: dict) -> None:
    await asyncio.to_thread(close_amap_mcp_service)


_settings = get_settings()


class WorkerSettings:
    functions = [
        func(
            generate_trip_plan_task,
            name="generate_trip_plan_task",
            timeout=_settings.task_worker_timeout_seconds,
            keep_result=0,
            max_tries=1,
        )
    ]
    redis_settings = redis_settings_from_url(_settings.redis_url)
    max_jobs = 4
    job_timeout = _settings.task_worker_timeout_seconds
    keep_result = 0
    on_shutdown = shutdown_worker
