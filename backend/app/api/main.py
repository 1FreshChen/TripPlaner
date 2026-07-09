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
from app.api.routes.sessions import router as sessions_router
from app.api.routes.trip import router as trip_router
from app.config import get_settings


settings = get_settings()

app = FastAPI(title="智能旅行助手 API", version="0.2.0")

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
