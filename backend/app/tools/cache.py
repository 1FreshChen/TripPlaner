from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class ToolCache:
    """Redis-compatible cache wrapper for tool results."""

    def __init__(self, redis_client):
        self._redis = redis_client

    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            raw = await self._redis.get(key)
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.warning("缓存读取失败: %s", exc)
            return None

    async def set(self, key: str, value: Dict[str, Any], ttl: int = 300) -> None:
        try:
            await self._redis.setex(key, ttl, json.dumps(value, ensure_ascii=False))
        except Exception as exc:
            logger.warning("缓存写入失败: %s", exc)
