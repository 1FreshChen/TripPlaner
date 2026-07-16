from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.tools.base import BaseTool


class ToolRegistry:
    """Registry for one coherent set of tool instances."""

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具 '{tool.name}' 已注册")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def list_all(self) -> List[BaseTool]:
        return list(self._tools.values())

    def get_openai_functions(self) -> List[Dict[str, Any]]:
        return [tool.to_openai_function() for tool in self._tools.values()]

    def find_by_keyword(self, keyword: str) -> List[BaseTool]:
        normalized = keyword.lower()
        return [
            tool
            for tool in self._tools.values()
            if normalized in tool.name.lower() or normalized in tool.description.lower()
        ]

    def clear(self) -> None:
        self._tools.clear()
