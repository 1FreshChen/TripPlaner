import asyncio
import time

import pytest

from app.orchestration.base import AgentDefinition, AgentStatus, FallbackLevel, RetryPolicy
from app.orchestration.fallback import FallbackChain
from app.orchestration.orchestrator import AgentOrchestrator
from app.orchestration.registry import AgentRegistry
from app.orchestration.trace import ExecutionTracer


class NoopAgent:
    name = "noop"

    async def execute(self, context):
        return self.name


def test_registry_resolves_dependencies_into_parallel_stages():
    registry = AgentRegistry()
    registry.register(AgentDefinition(name="A", agent_class=NoopAgent))
    registry.register(AgentDefinition(name="B", agent_class=NoopAgent))
    registry.register(AgentDefinition(name="C", agent_class=NoopAgent))
    registry.register(AgentDefinition(name="D", agent_class=NoopAgent, depends_on=["A", "B", "C"]))

    assert registry.resolve_execution_plan() == [["A", "B", "C"], ["D"]]


def test_registry_rejects_circular_dependencies():
    registry = AgentRegistry()
    registry.register(AgentDefinition(name="A", agent_class=NoopAgent, depends_on=["B"]))
    registry.register(AgentDefinition(name="B", agent_class=NoopAgent, depends_on=["A"]))

    with pytest.raises(ValueError, match="循环依赖"):
        registry.resolve_execution_plan()


def test_orchestrator_runs_same_stage_agents_in_parallel():
    class SlowA:
        name = "slow_a"

        async def execute(self, context):
            await asyncio.sleep(1)
            return "A"

    class SlowB:
        name = "slow_b"

        async def execute(self, context):
            await asyncio.sleep(1)
            return "B"

    class SlowC:
        name = "slow_c"

        async def execute(self, context):
            await asyncio.sleep(1)
            return "C"

    registry = AgentRegistry()
    registry.register(AgentDefinition(name="slow_a", agent_class=SlowA, retry_policy=RetryPolicy(max_attempts=1)))
    registry.register(AgentDefinition(name="slow_b", agent_class=SlowB, retry_policy=RetryPolicy(max_attempts=1)))
    registry.register(AgentDefinition(name="slow_c", agent_class=SlowC, retry_policy=RetryPolicy(max_attempts=1)))

    orchestrator = AgentOrchestrator(registry, FallbackChain(), ExecutionTracer())
    context = {}

    started = time.perf_counter()
    trace = asyncio.run(orchestrator.run("parallel-test", context))
    elapsed = time.perf_counter() - started

    assert elapsed < 1.5
    assert trace.overall_status == AgentStatus.COMPLETED
    assert trace.success_count == 3
    assert context["slow_a"] == "A"
    assert context["slow_b"] == "B"
    assert context["slow_c"] == "C"


def test_orchestrator_retries_failed_agent_until_success():
    class FlakyAgent:
        name = "flaky"
        attempts = 0

        async def execute(self, context):
            type(self).attempts += 1
            if type(self).attempts < 3:
                raise ConnectionError("temporary failure")
            return "ok"

    FlakyAgent.attempts = 0
    registry = AgentRegistry()
    registry.register(
        AgentDefinition(
            name="flaky",
            agent_class=FlakyAgent,
            retry_policy=RetryPolicy(max_attempts=3, base_delay=0),
        )
    )

    orchestrator = AgentOrchestrator(registry, FallbackChain(), ExecutionTracer())
    trace = asyncio.run(orchestrator.run("retry-test", {}))

    result = trace.agent_results[0]
    assert result.status == AgentStatus.COMPLETED
    assert result.output == "ok"
    assert result.retries_used == 2
    assert FlakyAgent.attempts == 3


def test_orchestrator_uses_fallback_chain_after_retries_are_exhausted():
    class FailingAgent:
        name = "failing"

        async def execute(self, context):
            raise ConnectionError("service unavailable")

    async def llm_fallback(context):
        raise RuntimeError("llm unavailable")

    async def deterministic_fallback(context):
        return "deterministic result"

    async def mock_fallback(context):
        return "mock result"

    registry = AgentRegistry()
    registry.register(
        AgentDefinition(
            name="failing",
            agent_class=FailingAgent,
            retry_policy=RetryPolicy(max_attempts=1, base_delay=0),
        )
    )
    fallback_chain = FallbackChain()
    fallback_chain.register_handler("failing", FallbackLevel.MOCK, mock_fallback)
    fallback_chain.register_handler("failing", FallbackLevel.LLM, llm_fallback)
    fallback_chain.register_handler("failing", FallbackLevel.DETERMINISTIC, deterministic_fallback)

    orchestrator = AgentOrchestrator(registry, fallback_chain, ExecutionTracer())
    trace = asyncio.run(orchestrator.run("fallback-test", {}))

    result = trace.agent_results[0]
    assert result.status == AgentStatus.COMPLETED
    assert result.output == "deterministic result"
    assert result.fallback_used == FallbackLevel.DETERMINISTIC
    assert trace.any_fallback_used is True
