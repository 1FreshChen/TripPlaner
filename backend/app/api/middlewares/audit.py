from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("audit")


class AuditMiddleware(BaseHTTPMiddleware):
    """Record HTTP request metadata into audit.event_log when the database is available."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = getattr(request.state, "request_id", None) or request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000

        await self._log_event(
            event_type="http_request",
            action=f"{request.method} {request.url.path}",
            details_json={
                "method": request.method,
                "path": request.url.path,
                "query_string": str(request.query_params),
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 2),
                "request_id": request_id,
            },
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("User-Agent", ""),
            duration_ms=duration_ms,
        )

        response.headers["X-Request-ID"] = request_id
        return response

    async def _log_event(self, **kwargs) -> None:
        try:
            from app.database import get_session_factory
            from app.models.db_models import AuditEvent

            factory = get_session_factory()
            async with factory() as db:
                db.add(AuditEvent(**kwargs))
                await db.commit()
        except Exception as exc:
            logger.warning("审计日志写入失败: %s", exc)
