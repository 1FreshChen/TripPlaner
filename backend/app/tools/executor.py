from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Dict, Optional

from app.tools.base import BaseTool
from app.tools.cache import ToolCache

logger = logging.getLogger(__name__)


class ToolExecutor:
    """Runs tools with cache lookup, timeout control, and retry."""

    def __init__(
        self,
        cache: Optional[ToolCache] = None,
        default_timeout: float = 30.0,
        retry_attempts: int = 3,
        retry_base_delay: float = 1.0,
        registry: Optional[Any] = None,
    ):
        self._cache = cache
        self._default_timeout = default_timeout
        self._retry_attempts = retry_attempts
        self._retry_base_delay = retry_base_delay
        self._registry = registry

    async def execute_by_name(self, tool_name: str, **kwargs) -> Dict[str, Any]:
        if self._registry is None:
            logger.error("工具 '%s' 无法执行: 未配置工具注册表", tool_name)
            return {"error": "Tool registry is not configured.", "tool": tool_name, "success": False}

        tool = self._registry.get(tool_name)
        if tool is None:
            logger.error("工具 '%s' 不存在", tool_name)
            return {"error": "Tool not found.", "tool": tool_name, "success": False}

        return await self.execute(tool, **kwargs)

    async def execute(self, tool: BaseTool, **kwargs) -> Dict[str, Any]:
        cache_key = self._build_cache_key(tool.name, kwargs)

        if self._cache:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return cached

        last_error: Exception | None = None
        for attempt in range(self._retry_attempts):
            try:
                start = time.time()
                result = await asyncio.wait_for(tool.execute(**kwargs), timeout=self._default_timeout)
                duration_ms = (time.time() - start) * 1000
                logger.info("工具 '%s' 执行成功, 耗时 %.0fms", tool.name, duration_ms)

                if self._cache and result:
                    await self._cache.set(cache_key, result, ttl=300)

                return result
            except asyncio.TimeoutError:
                last_error = TimeoutError(f"工具 '{tool.name}' 超时 ({self._default_timeout}s)")
            except Exception as exc:
                last_error = exc

            if attempt + 1 < self._retry_attempts:
                delay = self._retry_base_delay * (2**attempt)
                logger.warning(
                    "工具 '%s' 第 %d 次失败, %.1fs后重试: %s",
                    tool.name,
                    attempt + 1,
                    delay,
                    last_error,
                )
                if delay > 0:
                    await asyncio.sleep(delay)

        logger.error("工具 '%s' 所有重试耗尽: %s", tool.name, last_error)
        return {"error": str(last_error), "tool": tool.name, "success": False}

    def _build_cache_key(self, tool_name: str, kwargs: Dict[str, Any]) -> str:
        raw = json.dumps({"tool": tool_name, "args": kwargs}, sort_keys=True, ensure_ascii=False)
        return f"tool_cache:{hashlib.md5(raw.encode()).hexdigest()}"
