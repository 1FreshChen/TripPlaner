from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class AgentStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ExecutionMode(Enum):
    PIPELINE = "pipeline"
    PARALLEL = "parallel"


class FallbackLevel(Enum):
    LLM = "llm"
    DETERMINISTIC = "deterministic"
    MOCK = "mock"


@dataclass
class RetryPolicy:
    """Retry policy with exponential backoff."""

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    backoff_multiplier: float = 2.0
    retryable_exceptions: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError)

    def delay_for_attempt(self, attempt: int) -> float:
        return min(self.base_delay * (self.backoff_multiplier**attempt), self.max_delay)


@dataclass
class AgentDefinition:
    """Metadata used by the orchestrator to instantiate and schedule an agent."""

    name: str
    agent_class: type | Callable[[], Any]
    description: str = ""
    depends_on: List[str] = field(default_factory=list)
    execution_mode: ExecutionMode = ExecutionMode.PIPELINE
    retry_policy: Optional[RetryPolicy] = None
    timeout_seconds: float = 30.0
    fallback_levels: List[FallbackLevel] = field(
        default_factory=lambda: [FallbackLevel.LLM, FallbackLevel.DETERMINISTIC, FallbackLevel.MOCK]
    )

    def create_agent(self) -> Any:
        return self.agent_class()


@dataclass
class AgentResult:
    """Execution result for a single agent."""

    agent_name: str
    status: AgentStatus
    output: Any = None
    error_message: Optional[str] = None
    duration_ms: float = 0.0
    retries_used: int = 0
    fallback_used: Optional[FallbackLevel] = None
    started_at: float = 0.0
    finished_at: float = 0.0


@dataclass
class ExecutionTrace:
    """Trace for one orchestrator run."""

    run_id: str
    plan_id: str
    agent_results: List[AgentResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    overall_status: AgentStatus = AgentStatus.PENDING

    @property
    def total_duration_ms(self) -> float:
        finished_at = self.finished_at or time.time()
        return (finished_at - self.started_at) * 1000

    @property
    def success_count(self) -> int:
        return sum(1 for result in self.agent_results if result.status == AgentStatus.COMPLETED)

    @property
    def failure_count(self) -> int:
        return sum(1 for result in self.agent_results if result.status == AgentStatus.FAILED)

    @property
    def any_fallback_used(self) -> bool:
        return any(result.fallback_used is not None for result in self.agent_results)


class BaseAgent(ABC):
    """Base class for agents scheduled by AgentOrchestrator."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    async def execute(self, context: Dict[str, Any]) -> Any:
        ...
