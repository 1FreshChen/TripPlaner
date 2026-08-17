from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.models.db_models import TripPlanTask, User
from app.models.schemas import (
    TripPlanRequest,
    TripPlanResponse,
    TripPlanTaskCreatedResponse,
    TripPlanTaskPendingResult,
    TripPlanTaskStatusResponse,
)


TERMINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled", "expired"}
PHASE_RANK = {
    "queued": 0,
    "preparing": 1,
    "collecting_context": 2,
    "llm_planning": 3,
    "meal_enrichment": 4,
    "validating": 5,
    "saving": 6,
    "completed": 7,
    "failed": 7,
    "cancelled": 7,
    "expired": 7,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed_ms(start: datetime | None, end: datetime | None) -> int:
    if start is None or end is None:
        return 0
    return max(0, int((end - start).total_seconds() * 1000))


def transition_task(
    task: TripPlanTask,
    *,
    status: str,
    phase: str,
    progress: int,
    message: str,
    now: datetime | None = None,
) -> None:
    now = now or utc_now()
    current_phase = task.phase or "queued"
    if PHASE_RANK.get(phase, 0) < PHASE_RANK.get(current_phase, 0):
        phase = current_phase
        message = task.message
    timings = dict(task.phase_timings or {})
    if task.phase_started_at and task.phase and task.phase != phase:
        timings[task.phase] = _elapsed_ms(task.phase_started_at, now)
        task.phase_started_at = now
    elif task.phase_started_at is None:
        task.phase_started_at = now

    task.status = status
    task.phase = phase
    task.progress = max(task.progress or 0, min(progress, 100))
    task.message = message
    task.phase_timings = timings
    task.updated_at = now
    if status == "running" and task.started_at is None:
        task.started_at = now


class TripPlanTaskService:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def create_task(self, request: TripPlanRequest) -> TripPlanTaskCreatedResponse:
        now = utc_now()
        task_id = f"tp_{now:%Y%m%d}_{secrets.token_hex(6)}"
        if not request.session_id:
            raise HTTPException(status_code=422, detail="创建任务需要有效会话")
        user_id = await self._db.scalar(select(User.id).where(User.session_token == request.session_id))
        if user_id is None:
            raise HTTPException(status_code=404, detail="会话不存在")

        settings = get_settings()
        task = TripPlanTask(
            id=uuid.uuid4(),
            task_id=task_id,
            user_id=user_id,
            request_payload=request.model_dump(mode="json"),
            status="queued",
            phase="queued",
            progress=0,
            message="任务已进入队列",
            queued_at=now,
            phase_started_at=now,
            updated_at=now,
            expires_at=now + timedelta(days=settings.task_result_ttl_days),
            orchestration_backend=settings.orchestration_backend,
            workflow_version=(
                settings.langgraph_workflow_version
                if settings.orchestration_backend == "langgraph"
                else "legacy_v1"
            ),
            state_schema_version=settings.langgraph_state_schema_version,
            recovery_state="none",
        )
        self._db.add(task)
        await self._db.flush()
        return TripPlanTaskCreatedResponse(
            task_id=task_id,
            status="queued",
            status_url=f"/api/trip/tasks/{task_id}",
            events_url=f"/api/trip/tasks/{task_id}/events",
            result_url=f"/api/trip/tasks/{task_id}/result",
        )

    async def commit(self) -> None:
        await self._db.commit()

    async def mark_enqueue_failed(self, task_id: str, error: Exception) -> None:
        task = await self._get_task(task_id)
        now = utc_now()
        transition_task(
            task,
            status="failed",
            phase="failed",
            progress=task.progress,
            message="后台任务投递失败",
            now=now,
        )
        task.error_code = "QUEUE_UNAVAILABLE"
        task.error_message = str(error)[:2000]
        task.finished_at = now
        await self._db.flush()

    async def get_status(self, task_id: str, session_id: str | None = None) -> TripPlanTaskStatusResponse:
        return self.to_status_response(await self._get_task(task_id, session_id=session_id))

    async def get_result(
        self,
        task_id: str,
        session_id: str | None = None,
    ) -> TripPlanResponse | TripPlanTaskPendingResult:
        task = await self._get_task(task_id, session_id=session_id)
        if task.status == "succeeded" and task.result_payload:
            return TripPlanResponse.model_validate(task.result_payload)
        return TripPlanTaskPendingResult(status=task.status, message=task.error_message or task.message)

    async def _get_task(self, task_id: str, session_id: str | None = None) -> TripPlanTask:
        statement = select(TripPlanTask).where(TripPlanTask.task_id == task_id)
        if session_id is not None:
            statement = statement.join(User, TripPlanTask.user_id == User.id).where(
                User.session_token == session_id
            )
        task = await self._db.scalar(statement)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return task

    @staticmethod
    def to_status_response(task: TripPlanTask, now: datetime | None = None) -> TripPlanTaskStatusResponse:
        now = now or utc_now()
        elapsed_end = task.finished_at or now
        elapsed_start = task.queued_at
        phase_end = task.finished_at if task.status in TERMINAL_TASK_STATUSES else now
        result_url = f"/api/trip/tasks/{task.task_id}/result" if task.status == "succeeded" else None
        return TripPlanTaskStatusResponse(
            task_id=task.task_id,
            status=task.status,
            phase=task.phase,
            progress=task.progress,
            message=task.message,
            queued_at=task.queued_at.isoformat(),
            started_at=task.started_at.isoformat() if task.started_at else None,
            finished_at=task.finished_at.isoformat() if task.finished_at else None,
            updated_at=task.updated_at.isoformat(),
            elapsed_ms=_elapsed_ms(elapsed_start, elapsed_end),
            phase_elapsed_ms=_elapsed_ms(task.phase_started_at, phase_end),
            phase_timings={key: int(value) for key, value in (task.phase_timings or {}).items()},
            result_url=result_url,
            error_code=task.error_code,
            error_message=task.error_message,
        )


class TripPlanTaskReader:
    """Reads task snapshots with short-lived sessions, including for SSE streams."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def get_status(self, task_id: str, session_id: str | None = None) -> TripPlanTaskStatusResponse:
        async with self._session_factory() as db:
            return await TripPlanTaskService(db).get_status(task_id, session_id=session_id)


async def cleanup_expired_tasks(
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
) -> int:
    """Mark non-terminal tasks past expires_at as expired."""
    now = now or utc_now()
    async with session_factory() as db:
        tasks = (
            await db.scalars(
                select(TripPlanTask)
                .where(
                    TripPlanTask.expires_at.is_not(None),
                    TripPlanTask.expires_at < now,
                    TripPlanTask.status.not_in(TERMINAL_TASK_STATUSES),
                    TripPlanTask.orchestration_backend == "legacy",
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
        for task in tasks:
            transition_task(
                task,
                status="expired",
                phase="expired",
                progress=task.progress or 0,
                message="任务已过期",
                now=now,
            )
            task.finished_at = now
            task.error_code = "TASK_EXPIRED"
            task.error_message = "任务结果已超过保留期限"
        if tasks:
            await db.commit()
        return len(tasks)


async def expire_langgraph_tasks(
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
) -> list[str]:
    """Expire due LangGraph tasks and claim checkpoint threads that still need deletion."""
    now = now or utc_now()
    async with session_factory() as db:
        tasks = (
            await db.scalars(
                select(TripPlanTask)
                .where(
                    TripPlanTask.expires_at.is_not(None),
                    TripPlanTask.expires_at < now,
                    TripPlanTask.orchestration_backend == "langgraph",
                    TripPlanTask.checkpoint_deleted_at.is_(None),
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
        for task in tasks:
            if task.status not in TERMINAL_TASK_STATUSES:
                transition_task(
                    task,
                    status="expired",
                    phase="expired",
                    progress=task.progress or 0,
                    message="任务已过期",
                    now=now,
                )
                task.finished_at = now
                task.error_code = "TASK_EXPIRED"
                task.error_message = "任务结果已超过保留期限"
                task.lease_owner = None
                task.recovery_state = "none"
        if tasks:
            await db.commit()
        return [task.task_id for task in tasks]


async def mark_checkpoint_deleted(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
) -> None:
    async with session_factory() as db:
        await db.execute(
            update(TripPlanTask)
            .where(
                TripPlanTask.task_id == task_id,
                TripPlanTask.checkpoint_deleted_at.is_(None),
            )
            .values(checkpoint_deleted_at=utc_now())
        )
        await db.commit()


async def recover_stale_queued_tasks(
    session_factory: async_sessionmaker[AsyncSession],
    stale_threshold_seconds: int = 300,
    now: datetime | None = None,
) -> int:
    """Fail queued tasks that never started within the recovery threshold."""
    now = now or utc_now()
    threshold = now - timedelta(seconds=stale_threshold_seconds)
    async with session_factory() as db:
        tasks = (
            await db.scalars(
                select(TripPlanTask)
                .where(
                    TripPlanTask.status == "queued",
                    TripPlanTask.queued_at < threshold,
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
        for task in tasks:
            transition_task(
                task,
                status="failed",
                phase="failed",
                progress=task.progress or 0,
                message="任务排队超时",
                now=now,
            )
            task.finished_at = now
            task.error_code = "ENQUEUE_LOST"
            task.error_message = "任务长时间未开始，可能未成功进入队列，请重新提交"
        if tasks:
            await db.commit()
        return len(tasks)


async def recover_stale_running_tasks(
    session_factory: async_sessionmaker[AsyncSession],
    stale_threshold_seconds: int,
    now: datetime | None = None,
) -> int:
    """Fail running tasks that stopped reporting progress beyond the worker deadline."""
    now = now or utc_now()
    threshold = now - timedelta(seconds=stale_threshold_seconds)
    async with session_factory() as db:
        tasks = (
            await db.scalars(
                select(TripPlanTask)
                .where(
                    TripPlanTask.status == "running",
                    TripPlanTask.updated_at < threshold,
                    TripPlanTask.orchestration_backend == "legacy",
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
        for task in tasks:
            transition_task(
                task,
                status="failed",
                phase="failed",
                progress=task.progress or 0,
                message="任务执行超时",
                now=now,
            )
            task.finished_at = now
            task.error_code = "WORKER_LOST"
            task.error_message = "Worker 长时间未上报进度，请重新提交任务"
        if tasks:
            await db.commit()
        return len(tasks)


async def update_task_progress(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
    phase: str,
    progress: int,
    message: str,
    lease_owner: str | None = None,
) -> None:
    async with session_factory() as db:
        statement = select(TripPlanTask).where(TripPlanTask.task_id == task_id)
        if lease_owner is not None:
            statement = statement.where(TripPlanTask.lease_owner == lease_owner)
        task = await db.scalar(statement.with_for_update())
        if task is None or task.status in TERMINAL_TASK_STATUSES:
            return
        transition_task(
            task,
            status="running",
            phase=phase,
            progress=progress,
            message=message,
        )
        await db.commit()


def progress_from_graph_state(state: dict[str, Any]) -> tuple[str, int, str]:
    """Derive a monotonic public projection from durable graph values."""
    collectors = sum(
        key in state
        for key in ("attraction_search", "weather_query", "hotel_recommendation")
    )
    phase = "collecting_context"
    progress = 10 + (25 * collectors // 3)
    message = f"已完成信息采集 {collectors}/3"
    if "trip_planner" in state:
        phase, progress, message = "llm_planning", 75, "每日行程已生成"
    if "meal_enriched" in state:
        phase, progress, message = "meal_enrichment", 90, "餐饮信息处理完成"
    if state.get("validation_passed") or (
        isinstance(state.get("terminal_error"), dict)
        and state["terminal_error"].get("node") == "validate"
    ):
        phase, progress, message = "validating", 95, "行程质量校验完成"
    return phase, progress, message


async def reconcile_task_progress(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
    state: dict[str, Any],
    *,
    lease_owner: str,
) -> None:
    phase, progress, message = progress_from_graph_state(state)
    await update_task_progress(
        session_factory,
        task_id,
        phase,
        progress,
        message,
        lease_owner=lease_owner,
    )


async def acquire_task_lease(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
    lease_owner: str,
) -> dict[str, Any] | None:
    async with session_factory() as db:
        task = await db.scalar(
            select(TripPlanTask).where(TripPlanTask.task_id == task_id).with_for_update()
        )
        if task is None or task.status in TERMINAL_TASK_STATUSES:
            return None
        now = utc_now()
        transition_task(
            task,
            status="running",
            phase=task.phase if task.phase != "queued" else "preparing",
            progress=max(task.progress or 0, 5),
            message="正在准备行程请求" if task.phase == "queued" else task.message,
            now=now,
        )
        task.heartbeat_at = now
        task.lease_owner = lease_owner
        task.recovery_state = "none"
        task.recovery_enqueued_at = None
        await db.commit()
        return {
            "request_payload": dict(task.request_payload),
            "result_plan_id": str(task.result_plan_id) if task.result_plan_id else None,
            "workflow_version": task.workflow_version,
            "state_schema_version": task.state_schema_version,
        }


async def refresh_task_heartbeat(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
    lease_owner: str,
) -> bool:
    now = utc_now()
    async with session_factory() as db:
        result = await db.execute(
            update(TripPlanTask)
            .where(
                TripPlanTask.task_id == task_id,
                TripPlanTask.lease_owner == lease_owner,
                TripPlanTask.status == "running",
            )
            .values(heartbeat_at=now)
        )
        await db.commit()
        return bool(result.rowcount)


async def mark_task_recovery_pending(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
    lease_owner: str,
) -> None:
    async with session_factory() as db:
        await db.execute(
            update(TripPlanTask)
            .where(
                TripPlanTask.task_id == task_id,
                TripPlanTask.lease_owner == lease_owner,
                TripPlanTask.status == "running",
            )
            .values(
                lease_owner=None,
                recovery_state="pending",
                recovery_enqueued_at=utc_now(),
            )
        )
        await db.commit()


@dataclass(frozen=True)
class RecoveryCandidate:
    task_id: str
    retry_count: int
    plan_id: str | None
    exhausted: bool = False


async def claim_recovery_candidates(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stale_threshold_seconds: int,
    queue_stale_seconds: int,
    max_recoveries: int,
    workflow_version: str,
    state_schema_version: int,
    now: datetime | None = None,
) -> list[RecoveryCandidate]:
    now = now or utc_now()
    heartbeat_threshold = now - timedelta(seconds=stale_threshold_seconds)
    queue_threshold = now - timedelta(seconds=queue_stale_seconds)
    async with session_factory() as db:
        tasks = (
            await db.scalars(
                select(TripPlanTask)
                .where(
                    TripPlanTask.status == "running",
                    TripPlanTask.orchestration_backend == "langgraph",
                    or_(
                        and_(
                            TripPlanTask.recovery_state == "none",
                            or_(
                                TripPlanTask.heartbeat_at.is_(None),
                                TripPlanTask.heartbeat_at < heartbeat_threshold,
                            ),
                        ),
                        TripPlanTask.recovery_state == "pending",
                        and_(
                            TripPlanTask.recovery_state == "queued",
                            TripPlanTask.recovery_enqueued_at < queue_threshold,
                        ),
                    ),
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
        candidates: list[RecoveryCandidate] = []
        for task in tasks:
            compatible = (
                task.workflow_version == workflow_version
                and task.state_schema_version == state_schema_version
            )
            if not compatible or task.retry_count >= max_recoveries:
                candidates.append(
                    RecoveryCandidate(
                        task_id=task.task_id,
                        retry_count=task.retry_count,
                        plan_id=str(task.result_plan_id) if task.result_plan_id else None,
                        exhausted=True,
                    )
                )
                continue
            task.retry_count += 1
            task.recovery_state = "queued"
            task.recovery_enqueued_at = now
            task.lease_owner = None
            candidates.append(
                RecoveryCandidate(
                    task_id=task.task_id,
                    retry_count=task.retry_count,
                    plan_id=str(task.result_plan_id) if task.result_plan_id else None,
                )
            )
        if tasks:
            await db.commit()
        return candidates


async def mark_recovery_enqueue_failed(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
    retry_count: int,
) -> None:
    async with session_factory() as db:
        await db.execute(
            update(TripPlanTask)
            .where(
                TripPlanTask.task_id == task_id,
                TripPlanTask.status == "running",
                TripPlanTask.retry_count == retry_count,
                TripPlanTask.recovery_state == "queued",
            )
            .values(recovery_state="pending", recovery_enqueued_at=utc_now())
        )
        await db.commit()


async def fail_task(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
    error: Exception,
) -> None:
    detail: Any = getattr(error, "detail", None)
    error_code = error.__class__.__name__.upper()
    error_message = str(error)
    if isinstance(detail, dict):
        error_code = str(detail.get("code") or error_code)
        issues = detail.get("issues")
        error_message = str(detail.get("message") or ("；".join(issues) if isinstance(issues, list) else detail))
    elif detail:
        error_message = str(detail)

    async with session_factory() as db:
        task = await db.scalar(select(TripPlanTask).where(TripPlanTask.task_id == task_id).with_for_update())
        if task is None or task.status in TERMINAL_TASK_STATUSES:
            return
        now = utc_now()
        transition_task(
            task,
            status="failed",
            phase="failed",
            progress=task.progress,
            message="行程生成失败",
            now=now,
        )
        task.error_code = error_code[:64]
        task.error_message = error_message[:2000]
        task.finished_at = now
        await db.commit()
