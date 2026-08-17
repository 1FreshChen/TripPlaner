import asyncio
import logging
from contextlib import suppress
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middlewares.audit import AuditMiddleware
from app.api.middlewares.rate_limit import (
    RateLimitExceeded,
    limiter,
    rate_limit_exceeded_handler,
)

try:
    from slowapi.middleware import SlowAPIMiddleware
    _SLOWAPI_AVAILABLE = True
except ImportError:
    _SLOWAPI_AVAILABLE = False

from app.api.middlewares.request_id import RequestIDMiddleware
from app.api.routes.conversation import router as conversation_router
from app.api.routes.preferences import router as preferences_router
from app.api.routes.saved_items import router as saved_items_router
from app.api.routes.sessions import router as sessions_router
from app.api.routes.trip import router as trip_router
from app.config import get_settings
from app.database import get_session_factory
from app.services.amap_mcp_service import close_amap_mcp_service
from app.services.task_service import (
    cleanup_expired_tasks,
    recover_stale_queued_tasks,
    recover_stale_running_tasks,
)
from app.tasks.queue import close_task_queue


settings = get_settings()
logger = logging.getLogger(__name__)


async def _maintain_tasks() -> None:
    while True:
        await asyncio.sleep(settings.task_maintenance_interval_seconds)
        try:
            session_factory = get_session_factory()
            expired = await cleanup_expired_tasks(session_factory)
            recovered = await recover_stale_queued_tasks(
                session_factory,
                stale_threshold_seconds=settings.task_stale_queued_seconds,
            )
            legacy_running_failed = await recover_stale_running_tasks(
                session_factory,
                stale_threshold_seconds=(
                    settings.task_worker_timeout_seconds
                    + settings.task_maintenance_interval_seconds
                ),
            )
            if expired or recovered or legacy_running_failed:
                logger.warning(
                    "Task maintenance updated stale records: expired=%d enqueue_lost=%d legacy_worker_lost=%d",
                    expired,
                    recovered,
                    legacy_running_failed,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Task maintenance failed")


@asynccontextmanager
async def lifespan(application: FastAPI):
    maintenance_task = asyncio.create_task(_maintain_tasks(), name="trip-task-maintenance")
    try:
        yield
    finally:
        maintenance_task.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance_task
        await close_task_queue(getattr(application.state, "task_queue", None))
        await asyncio.to_thread(close_amap_mcp_service)


app = FastAPI(title="智能旅行助手 API", version="0.3.0", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

if _SLOWAPI_AVAILABLE:
    app.add_middleware(SlowAPIMiddleware)

app.add_middleware(RequestIDMiddleware)
app.add_middleware(AuditMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin, "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "service": "trip-planner"}


app.include_router(trip_router, prefix="/api")
app.include_router(sessions_router, prefix="/api")
app.include_router(conversation_router, prefix="/api")
app.include_router(preferences_router, prefix="/api")
app.include_router(saved_items_router, prefix="/api")
