from __future__ import annotations

from typing import Dict, List

from app.orchestration.base import AgentDefinition


class AgentRegistry:
    """Registry for Agent factories used by LangGraph nodes."""

    def __init__(self):
        self._agents: Dict[str, AgentDefinition] = {}

    def register(self, definition: AgentDefinition) -> None:
        if definition.name in self._agents:
            raise ValueError(f"Agent '{definition.name}' 已注册")
        self._agents[definition.name] = definition

    def get(self, name: str) -> AgentDefinition:
        if name not in self._agents:
            raise KeyError(f"Agent '{name}' 未注册")
        return self._agents[name]

    def list_all(self) -> List[AgentDefinition]:
        return list(self._agents.values())
