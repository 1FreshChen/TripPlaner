from __future__ import annotations

import asyncio
from urllib.parse import unquote, urlsplit

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import Request

from app.config import get_settings


def redis_settings_from_url(redis_url: str) -> RedisSettings:
    parsed = urlsplit(redis_url)
    if parsed.scheme not in {"redis", "rediss"}:
        raise ValueError("REDIS_URL must use redis:// or rediss://")
    database = int(parsed.path.lstrip("/") or 0)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=database,
        username=unquote(parsed.username) if parsed.username else None,
        password=unquote(parsed.password) if parsed.password else None,
        ssl=parsed.scheme == "rediss",
    )


class TaskQueue:
    """Lazily opens Redis so the database task record is committed first."""

    def __init__(self, application):
        self._application = application

    async def enqueue_job(self, function: str, *args, **kwargs):
        queue = getattr(self._application.state, "task_queue", None)
        if queue is None:
            lock = getattr(self._application.state, "task_queue_lock", None)
            if lock is None:
                lock = asyncio.Lock()
                self._application.state.task_queue_lock = lock
            async with lock:
                queue = getattr(self._application.state, "task_queue", None)
                if queue is None:
                    settings = get_settings()
                    queue = await create_pool(redis_settings_from_url(settings.redis_url))
                    self._application.state.task_queue = queue
        return await queue.enqueue_job(function, *args, **kwargs)


async def get_task_queue(request: Request) -> TaskQueue:
    return TaskQueue(request.app)


async def close_task_queue(queue: ArqRedis | None) -> None:
    if queue is None:
        return
    close = getattr(queue, "aclose", None) or queue.close
    try:
        await close(close_connection_pool=True)
    except TypeError:
        await close()
