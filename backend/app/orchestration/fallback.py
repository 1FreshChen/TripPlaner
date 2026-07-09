from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List, Optional

from app.orchestration.base import FallbackLevel


class FallbackChain:
    """Fallback chain ordered as LLM -> deterministic -> mock."""

    def __init__(self):
        self._handlers: Dict[str, List[tuple[FallbackLevel, Callable]]] = {}
        self.last_level_used: Optional[FallbackLevel] = None

    def register_handler(self, agent_name: str, level: FallbackLevel, handler: Callable) -> None:
        if agent_name not in self._handlers:
            self._handlers[agent_name] = []
        self._handlers[agent_name].append((level, handler))

    async def execute(
        self,
        agent_name: str,
        context: Dict[str, Any],
        last_error: Optional[Exception] = None,
    ) -> Any:
        self.last_level_used = None
        level_order = [FallbackLevel.LLM, FallbackLevel.DETERMINISTIC, FallbackLevel.MOCK]
        handlers = sorted(
            self._handlers.get(agent_name, []),
            key=lambda item: level_order.index(item[0]) if item[0] in level_order else 99,
        )

        fallback_context = dict(context)
        fallback_context["last_error"] = last_error

        for level, handler in handlers:
            try:
                result = handler(fallback_context)
                if inspect.isawaitable(result):
                    result = await result
                if result is not None:
                    self.last_level_used = level
                    return result
            except Exception:
                continue

        return None
