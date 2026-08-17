from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
import uuid
from contextlib import asynccontextmanager, suppress

from arq.worker import func
from sqlalchemy import select, text

from app.config import get_settings
from app.database import get_engine, get_session_factory
from app.models.db_models import TripPlanTask
from app.models.schemas import TripPlanRequest
from app.orchestration.checkpoint import create_checkpoint_runtime
from app.orchestration.langgraph_workflow import build_trip_planning_graph
from app.services.amap_mcp_service import close_amap_mcp_service
from app.services.planning_workflow_service import PlanningWorkflowService
from app.services.state_service import StateService
from app.services.task_service import (
    TERMINAL_TASK_STATUSES,
    acquire_task_lease,
    claim_recovery_candidates,
    expire_langgraph_tasks,
    fail_task,
    mark_checkpoint_deleted,
    mark_recovery_enqueue_failed,
    mark_task_recovery_pending,
    refresh_task_heartbeat,
    transition_task,
    update_task_progress,
    utc_now,
)
from app.tasks.queue import redis_settings_from_url


SCANNER_LOCK_KEY = 0x4C47524150485343
logger = logging.getLogger(__name__)


class TaskLeaseLostError(RuntimeError):
    pass


class HeartbeatFailedError(RuntimeError):
    pass


def _advisory_key(value: str) -> int:
    return int.from_bytes(hashlib.blake2b(value.encode(), digest_size=8).digest(), "big", signed=True)


@asynccontextmanager
async def _postgres_advisory_lock(lock_key: int):
    async with get_engine().connect() as connection:
        acquired = bool(
            await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key})
        )
        try:
            yield acquired
        finally:
            if acquired:
                await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})


async def _run_legacy_task(task_id: str, request_payload: dict) -> str | None:
    session_factory = get_session_factory()

    async def report_progress(phase: str, progress: int, message: str) -> None:
        await update_task_progress(session_factory, task_id, phase, progress, message)

    async with session_factory() as db:
        try:
            result = await StateService(db).create_trip_plan(
                TripPlanRequest.model_validate(request_payload),
                progress_callback=report_progress,
            )
            task = await db.scalar(
                select(TripPlanTask).where(TripPlanTask.task_id == task_id).with_for_update()
            )
            if task is None:
                raise RuntimeError(f"Task {task_id} disappeared before completion")
            if task.status in TERMINAL_TASK_STATUSES:
                await db.rollback()
                return None
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
        except asyncio.CancelledError:
            await db.rollback()
            with suppress(Exception):
                await fail_task(session_factory, task_id, RuntimeError("Worker stopped before task completion"))
            raise
        except Exception as exc:
            await db.rollback()
            await fail_task(session_factory, task_id, exc)
            raise


async def _heartbeat_loop(task_id: str, lease_owner: str) -> None:
    settings = get_settings()
    session_factory = get_session_factory()
    while True:
        await asyncio.sleep(settings.langgraph_heartbeat_seconds)
        try:
            refreshed = await refresh_task_heartbeat(session_factory, task_id, lease_owner)
        except Exception as exc:
            raise HeartbeatFailedError(f"Task heartbeat failed: {task_id}") from exc
        if not refreshed:
            raise TaskLeaseLostError(f"Task lease lost: {task_id}")


async def _run_with_heartbeat(coro, task_id: str, lease_owner: str):
    workflow_task = asyncio.create_task(coro, name=f"workflow:{task_id}")
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(task_id, lease_owner),
        name=f"heartbeat:{task_id}",
    )
    try:
        done, _ = await asyncio.wait(
            {workflow_task, heartbeat_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if heartbeat_task in done:
            error = heartbeat_task.exception()
            workflow_task.cancel()
            with suppress(asyncio.CancelledError):
                await workflow_task
            if error is not None:
                raise error
            raise RuntimeError(f"Heartbeat stopped unexpectedly: {task_id}")
        return await workflow_task
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task


async def _run_langgraph_task(ctx: dict, task_id: str) -> str | None:
    session_factory = get_session_factory()
    settings = get_settings()
    graph = ctx.get("trip_planning_graph")
    if graph is None:
        raise RuntimeError("LangGraph worker runtime is not initialized")

    async with _postgres_advisory_lock(_advisory_key(task_id)) as acquired:
        if not acquired:
            return None
        lease_owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
        lease = await acquire_task_lease(session_factory, task_id, lease_owner)
        if lease is None:
            return None
        if lease["workflow_version"] != settings.langgraph_workflow_version:
            error = {"code": "WORKFLOW_VERSION_MISMATCH", "message": "任务工作流版本与 Worker 不兼容"}
            async with session_factory() as db:
                await StateService(db).fail_planning_run(task_id, lease["result_plan_id"], error)
            raise RuntimeError(error["message"])
        if lease["state_schema_version"] != settings.langgraph_state_schema_version:
            error = {"code": "STATE_SCHEMA_MISMATCH", "message": "任务状态版本与 Worker 不兼容"}
            async with session_factory() as db:
                await StateService(db).fail_planning_run(task_id, lease["result_plan_id"], error)
            raise RuntimeError(error["message"])

        workflow = PlanningWorkflowService(session_factory, graph)
        try:
            result = await _run_with_heartbeat(
                workflow.run(
                    task_id=task_id,
                    lease_owner=lease_owner,
                    request_payload=lease["request_payload"],
                    workflow_version=lease["workflow_version"],
                    state_schema_version=lease["state_schema_version"],
                ),
                task_id,
                lease_owner,
            )
            return result.plan_id
        except (asyncio.CancelledError, HeartbeatFailedError):
            with suppress(Exception):
                await mark_task_recovery_pending(session_factory, task_id, lease_owner)
            raise
        except TaskLeaseLostError:
            raise
        except Exception as exc:
            async with session_factory() as db:
                await StateService(db).fail_planning_run(
                    task_id,
                    lease["result_plan_id"],
                    exc,
                    lease_owner=lease_owner,
                )
            raise


async def generate_trip_plan_task(ctx: dict, task_id: str) -> str | None:
    session_factory = get_session_factory()
    async with session_factory() as lookup_db:
        task = await lookup_db.scalar(select(TripPlanTask).where(TripPlanTask.task_id == task_id))
        if task is None or task.status in TERMINAL_TASK_STATUSES:
            return None
        request_payload = dict(task.request_payload)
        backend = task.orchestration_backend
    if backend == "langgraph":
        return await _run_langgraph_task(ctx, task_id)
    return await _run_legacy_task(task_id, request_payload)


async def _scan_recoveries(ctx: dict) -> None:
    settings = get_settings()
    session_factory = get_session_factory()
    async with _postgres_advisory_lock(SCANNER_LOCK_KEY) as acquired:
        if not acquired:
            return
        expired_task_ids = await expire_langgraph_tasks(session_factory)
        saver = getattr(ctx.get("checkpoint_runtime"), "saver", None)
        if saver is not None:
            for expired_task_id in expired_task_ids:
                try:
                    await saver.adelete_thread(expired_task_id)
                    await mark_checkpoint_deleted(session_factory, expired_task_id)
                except Exception:
                    logger.exception("Failed to delete checkpoint for expired task %s", expired_task_id)
        candidates = await claim_recovery_candidates(
            session_factory,
            stale_threshold_seconds=settings.langgraph_stale_seconds,
            queue_stale_seconds=settings.langgraph_recovery_queue_stale_seconds,
            max_recoveries=settings.langgraph_max_recoveries,
            workflow_version=settings.langgraph_workflow_version,
            state_schema_version=settings.langgraph_state_schema_version,
        )
        for candidate in candidates:
            if candidate.exhausted:
                error = {
                    "code": "RECOVERY_EXHAUSTED",
                    "message": "任务恢复次数耗尽或 checkpoint 版本不兼容",
                }
                async with session_factory() as db:
                    await StateService(db).fail_planning_run(
                        candidate.task_id,
                        candidate.plan_id,
                        error,
                    )
                continue
            try:
                job = await ctx["redis"].enqueue_job(
                    "generate_trip_plan_task",
                    candidate.task_id,
                    _job_id=f"{candidate.task_id}:recovery:{candidate.retry_count}",
                )
                if job is None:
                    logger.info(
                        "Recovery job already exists for task %s attempt %d",
                        candidate.task_id,
                        candidate.retry_count,
                    )
            except Exception:
                logger.exception("Failed to enqueue recovery for task %s", candidate.task_id)
                await mark_recovery_enqueue_failed(
                    session_factory,
                    candidate.task_id,
                    candidate.retry_count,
                )


async def _recovery_scanner_loop(ctx: dict) -> None:
    interval = get_settings().langgraph_recovery_scan_seconds
    while True:
        try:
            await _scan_recoveries(ctx)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("LangGraph recovery scan failed")
        await asyncio.sleep(interval)


async def startup_worker(ctx: dict) -> None:
    settings = get_settings()
    if settings.orchestration_backend != "langgraph":
        return
    runtime = create_checkpoint_runtime(settings)
    saver = await runtime.start()
    ctx["checkpoint_runtime"] = runtime
    ctx["trip_planning_graph"] = build_trip_planning_graph(saver, settings=settings)
    ctx["recovery_scanner_task"] = asyncio.create_task(
        _recovery_scanner_loop(ctx),
        name="langgraph-recovery-scanner",
    )


async def shutdown_worker(ctx: dict) -> None:
    scanner = ctx.get("recovery_scanner_task")
    if scanner is not None:
        scanner.cancel()
        with suppress(asyncio.CancelledError):
            await scanner
    runtime = ctx.get("checkpoint_runtime")
    if runtime is not None:
        await runtime.close()
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
    on_startup = startup_worker
    on_shutdown = shutdown_worker
