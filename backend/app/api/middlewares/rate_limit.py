from __future__ import annotations

import inspect
import time
from collections import defaultdict, deque
from collections.abc import Callable
from functools import wraps
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

try:
    from slowapi import Limiter as SlowAPILimiter
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address

    limiter = SlowAPILimiter(key_func=get_remote_address, default_limits=["100/hour"])
except ImportError:

    class RateLimitExceeded(Exception):
        def __init__(self, detail: str = "请求过于频繁，请稍后再试", retry_after: int = 60):
            super().__init__(detail)
            self.detail = detail
            self.retry_after = retry_after

    def get_remote_address(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    class InMemoryLimiter:
        def __init__(self, key_func: Callable[[Request], str], default_limits: list[str] | None = None):
            self.key_func = key_func
            self.default_limits = default_limits or []
            self._hits: dict[tuple[str, str, str], deque[float]] = defaultdict(deque)

        def limit(self, limit_value: str):
            amount, window_seconds = self._parse_limit(limit_value)

            def decorator(func):
                signature = inspect.signature(func)

                @wraps(func)
                async def wrapper(*args, **kwargs):
                    request = self._find_request(args, kwargs)
                    key = (self.key_func(request), f"{func.__module__}.{func.__qualname__}", limit_value)
                    now = time.monotonic()
                    hits = self._hits[key]
                    while hits and now - hits[0] >= window_seconds:
                        hits.popleft()
                    if len(hits) >= amount:
                        raise RateLimitExceeded(retry_after=max(1, int(window_seconds - (now - hits[0]))))
                    hits.append(now)
                    return await func(*args, **kwargs)

                wrapper.__signature__ = signature
                return wrapper

            return decorator

        def reset(self) -> None:
            self._hits.clear()

        @staticmethod
        def _parse_limit(limit_value: str) -> tuple[int, int]:
            amount_text, period = limit_value.split("/", 1)
            amount = int(amount_text)
            period_seconds = {
                "second": 1,
                "minute": 60,
                "hour": 3600,
                "day": 86400,
            }
            return amount, period_seconds[period.rstrip("s")]

        @staticmethod
        def _find_request(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Request:
            for value in kwargs.values():
                if isinstance(value, Request):
                    return value
            for value in args:
                if isinstance(value, Request):
                    return value
            raise RuntimeError("Rate-limited endpoints must accept a fastapi.Request argument")

    limiter = InMemoryLimiter(key_func=get_remote_address, default_limits=["100/hour"])


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    retry_after = str(getattr(exc, "retry_after", 60))
    return JSONResponse(
        status_code=429,
        content={"detail": "请求过于频繁，请稍后再试"},
        headers={"Retry-After": retry_after, "X-Rate-Limit-Exceeded": "true"},
    )
