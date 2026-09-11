from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional


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
    """Metadata used by LangGraph nodes to instantiate an agent."""

    name: str
    agent_class: type | Callable[[], Any]
    description: str = ""
    retry_policy: Optional[RetryPolicy] = None
    timeout_seconds: float = 30.0

    def create_agent(self) -> Any:
        return self.agent_class()


class BaseAgent(ABC):
    """Base class for agents executed by LangGraph nodes."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    async def execute(self, context: Dict[str, Any]) -> Any:
        ...
