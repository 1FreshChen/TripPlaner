import asyncio
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.api.deps import get_state_service, get_trip_task_reader, get_trip_task_service
from app.api.middlewares.rate_limit import limiter
from app.config import get_settings
from app.models.schemas import (
    PlanVersionsResponse,
    TripPlanRequest,
    TripPlanResponse,
    TripPlanTaskCreatedResponse,
    TripPlanTaskPendingResult,
    TripPlanTaskStatusResponse,
    TripPlanUpdateRequest,
)
from app.services.state_service import StateService
from app.services.task_service import TERMINAL_TASK_STATUSES, TripPlanTaskReader, TripPlanTaskService
from app.tasks.queue import TaskQueue, get_task_queue


router = APIRouter(prefix="/trip", tags=["trip"])


@router.post("/plan", response_model=TripPlanTaskCreatedResponse, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("5/minute")
@limiter.limit("50/hour")
async def create_trip_plan(
    request: Request,
    trip_request: TripPlanRequest,
    tasks: TripPlanTaskService = Depends(get_trip_task_service),
    queue: TaskQueue = Depends(get_task_queue),
) -> TripPlanTaskCreatedResponse:
    created = await tasks.create_task(trip_request)
    await tasks.commit()
    try:
        job = await queue.enqueue_job(
            "generate_trip_plan_task",
            created.task_id,
            _job_id=created.task_id,
        )
        if job is None:
            raise RuntimeError("同一任务已在队列中")
    except Exception as exc:
        await tasks.mark_enqueue_failed(created.task_id, exc)
        await tasks.commit()
        raise HTTPException(status_code=503, detail="后台任务队列暂时不可用") from exc
    return created


@router.post("/plan/sync", response_model=TripPlanResponse, status_code=status.HTTP_201_CREATED, include_in_schema=False)
@limiter.limit("5/minute")
@limiter.limit("50/hour")
async def create_trip_plan_sync(
    request: Request,
    trip_request: TripPlanRequest,
    state: StateService = Depends(get_state_service),
) -> TripPlanResponse:
    """Compatibility endpoint for restoring the original synchronous workflow."""
    return await state.create_trip_plan(trip_request)


@router.get("/tasks/{task_id}", response_model=TripPlanTaskStatusResponse)
@limiter.limit("120/minute")
async def get_trip_plan_task(
    request: Request,
    task_id: str,
    tasks: TripPlanTaskService = Depends(get_trip_task_service),
) -> TripPlanTaskStatusResponse:
    return await tasks.get_status(task_id)


@router.get("/tasks/{task_id}/result", response_model=TripPlanResponse | TripPlanTaskPendingResult)
@limiter.limit("120/minute")
async def get_trip_plan_task_result(
    request: Request,
    task_id: str,
    tasks: TripPlanTaskService = Depends(get_trip_task_service),
) -> TripPlanResponse | TripPlanTaskPendingResult:
    return await tasks.get_result(task_id)


@router.get("/tasks/{task_id}/events")
@limiter.limit("30/minute")
async def stream_trip_plan_task_events(
    request: Request,
    task_id: str,
    tasks: TripPlanTaskReader = Depends(get_trip_task_reader),
) -> StreamingResponse:
    initial_status = await tasks.get_status(task_id)
    connection_timeout = get_settings().task_sse_timeout_seconds

    async def event_stream():
        current = initial_status
        last_payload = ""
        stream_started = time.monotonic()
        last_heartbeat = stream_started
        while True:
            now = time.monotonic()
            if now - stream_started >= connection_timeout:
                timeout_payload = json.dumps(
                    {"task_id": task_id, "message": "进度连接已超时，已切换为状态轮询"},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield f"event: timeout\ndata: {timeout_payload}\n\n"
                return

            payload = current.model_dump(mode="json")
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if serialized != last_payload:
                event_name = "progress"
                if current.status == "succeeded":
                    event_name = "completed"
                elif current.status == "failed":
                    event_name = "failed"
                yield f"id: {current.updated_at}\nevent: {event_name}\ndata: {serialized}\n\n"
                last_payload = serialized
                if current.status in TERMINAL_TASK_STATUSES:
                    return
            elif now - last_heartbeat >= 15:
                yield ": keep-alive\n\n"
                last_heartbeat = now

            if await request.is_disconnected():
                return
            await asyncio.sleep(0.75)
            current = await tasks.get_status(task_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/plan/{plan_id}", response_model=TripPlanResponse)
@limiter.limit("60/minute")
async def get_trip_plan(
    request: Request,
    plan_id: str,
    state: StateService = Depends(get_state_service),
) -> TripPlanResponse:
    return await state.get_trip_plan(plan_id)


@router.put("/plan/{plan_id}", response_model=TripPlanResponse)
@limiter.limit("10/minute")
async def update_trip_plan(
    request: Request,
    plan_id: str,
    trip_update: TripPlanUpdateRequest,
    state: StateService = Depends(get_state_service),
) -> TripPlanResponse:
    return await state.update_trip_plan(plan_id, trip_update)


@router.get("/plan/{plan_id}/versions", response_model=PlanVersionsResponse)
@limiter.limit("60/minute")
async def get_plan_versions(
    request: Request,
    plan_id: str,
    state: StateService = Depends(get_state_service),
) -> PlanVersionsResponse:
    return await state.list_plan_versions(plan_id)


@router.post("/plan/{plan_id}/revert/{version}", response_model=TripPlanResponse)
@limiter.limit("10/minute")
async def revert_plan_version(
    request: Request,
    plan_id: str,
    version: int,
    state: StateService = Depends(get_state_service),
) -> TripPlanResponse:
    return await state.revert_plan(plan_id, version)


@router.delete("/plan/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def archive_trip_plan(
    request: Request,
    plan_id: str,
    state: StateService = Depends(get_state_service),
) -> None:
    await state.archive_plan(plan_id)
