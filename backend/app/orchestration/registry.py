from __future__ import annotations

from collections import defaultdict, deque
from typing import Dict, List

from app.orchestration.base import AgentDefinition


class AgentRegistry:
    """Registry for agent metadata and dependency resolution."""

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

    def resolve_execution_plan(self) -> List[List[str]]:
        """Topologically sort registered agents into parallel execution stages."""
        missing_dependencies = {
            dependency
            for definition in self._agents.values()
            for dependency in definition.depends_on
            if dependency not in self._agents
        }
        if missing_dependencies:
            missing = ", ".join(sorted(missing_dependencies))
            raise ValueError(f"检测到未注册依赖: {missing}")

        in_degree = {name: len(definition.depends_on) for name, definition in self._agents.items()}
        dependents = defaultdict(list)
        for name, definition in self._agents.items():
            for dependency in definition.depends_on:
                dependents[dependency].append(name)

        queue = deque(name for name, degree in in_degree.items() if degree == 0)
        stages: List[List[str]] = []

        while queue:
            stage = list(queue)
            stages.append(stage)
            queue.clear()
            for name in stage:
                for dependent in dependents[name]:
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        queue.append(dependent)

        resolved_count = sum(len(stage) for stage in stages)
        if resolved_count != len(self._agents):
            remaining = [name for name, degree in in_degree.items() if degree > 0]
            raise ValueError(f"检测到循环依赖: {remaining}")

        return stages
