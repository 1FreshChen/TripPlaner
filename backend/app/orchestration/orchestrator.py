from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict

from app.orchestration.base import AgentResult, AgentStatus, RetryPolicy
from app.orchestration.fallback import FallbackChain
from app.orchestration.registry import AgentRegistry
from app.orchestration.trace import ExecutionTracer

logger = logging.getLogger(__name__)


class AgentOrchestrator:
    """Runs registered agents stage by stage, with parallelism inside each stage."""

    def __init__(self, registry: AgentRegistry, fallback_chain: FallbackChain, tracer: ExecutionTracer):
        self._registry = registry
        self._fallback_chain = fallback_chain
        self._tracer = tracer

    async def run(self, plan_id: str, context: Dict[str, Any]):
        run_id = str(uuid.uuid4())
        trace = self._tracer.start_run(run_id, plan_id)

        try:
            stages = self._registry.resolve_execution_plan()
        except ValueError as exc:
            logger.error("无法解析执行计划: %s", exc)
            trace.overall_status = AgentStatus.FAILED
            trace.finished_at = time.time()
            self._tracer.finish_run(run_id)
            return trace

        for stage_index, stage in enumerate(stages):
            logger.info("Stage %d: 并行执行 %s", stage_index, stage)
            results = await asyncio.gather(
                *(self._execute_with_retry(agent_name, context, trace) for agent_name in stage),
                return_exceptions=True,
            )

            stage_failed = False
            for agent_name, result in zip(stage, results):
                if isinstance(result, BaseException):
                    logger.exception("Agent '%s' 彻底失败", agent_name, exc_info=result)
                    trace.agent_results.append(self._failed_result(agent_name, result))
                    stage_failed = True
                    continue
                context[agent_name] = result.output
                if result.status == AgentStatus.FAILED:
                    stage_failed = True

            if stage_failed:
                self._skip_remaining_agents(trace, stages[stage_index + 1 :])
                break

        trace.finished_at = time.time()
        trace.overall_status = AgentStatus.COMPLETED if trace.failure_count == 0 else AgentStatus.FAILED
        self._tracer.finish_run(run_id)
        return trace

    async def _execute_with_retry(self, agent_name: str, context: Dict[str, Any], trace) -> AgentResult:
        agent_def = self._registry.get(agent_name)
        agent = agent_def.create_agent()
        retry_policy = agent_def.retry_policy or RetryPolicy()

        result = AgentResult(agent_name=agent_name, status=AgentStatus.RUNNING, started_at=time.time())
        last_error: Exception | None = None
        attempts_made = 0

        for attempt in range(retry_policy.max_attempts):
            attempts_made = attempt + 1
            try:
                output = await asyncio.wait_for(agent.execute(context), timeout=agent_def.timeout_seconds)
                result.status = AgentStatus.COMPLETED
                result.output = output
                result.retries_used = attempt
                self._clear_agent_failure_flags(context, agent_name)
                self._finish_result(result, trace)
                return result
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Agent '%s' 第 %d/%d 次尝试失败: %s",
                    agent_name,
                    attempt + 1,
                    retry_policy.max_attempts,
                    exc,
                )
                should_retry = attempt + 1 < retry_policy.max_attempts and isinstance(
                    exc, retry_policy.retryable_exceptions
                )
                if should_retry:
                    delay = retry_policy.delay_for_attempt(attempt)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue
                break

        result.retries_used = max(attempts_made - 1, 0)
        fallback_output = await self._fallback_chain.execute(agent_name, context, last_error=last_error)
        if fallback_output is not None:
            result.status = AgentStatus.COMPLETED
            result.output = fallback_output
            result.fallback_used = self._fallback_chain.last_level_used
            result.error_message = str(last_error) if last_error else None
            self._clear_agent_failure_flags(context, agent_name, clear_candidate_metadata=True)
        else:
            result.status = AgentStatus.FAILED
            result.error_message = str(last_error) if last_error else "unknown error"

        self._finish_result(result, trace)
        return result

    @staticmethod
    def _finish_result(result: AgentResult, trace) -> None:
        result.finished_at = time.time()
        result.duration_ms = (result.finished_at - result.started_at) * 1000
        trace.agent_results.append(result)

    @staticmethod
    def _clear_agent_failure_flags(
        context: Dict[str, Any],
        agent_name: str,
        *,
        clear_candidate_metadata: bool = False,
    ) -> None:
        context.pop(f"{agent_name}_quality_failed", None)
        if agent_name == "trip_planner" and clear_candidate_metadata:
            critique_events = context.pop("plan_critique_events", None)
            if critique_events:
                context.setdefault("failed_plan_critique_events", []).extend(critique_events)

    @staticmethod
    def _failed_result(agent_name: str, exc: Exception) -> AgentResult:
        now = time.time()
        return AgentResult(
            agent_name=agent_name,
            status=AgentStatus.FAILED,
            error_message=str(exc),
            started_at=now,
            finished_at=now,
        )

    @staticmethod
    def _skip_remaining_agents(trace, remaining_stages: list[list[str]]) -> None:
        now = time.time()
        for stage in remaining_stages:
            for agent_name in stage:
                trace.agent_results.append(
                    AgentResult(
                        agent_name=agent_name,
                        status=AgentStatus.SKIPPED,
                        error_message="dependency failed",
                        started_at=now,
                        finished_at=now,
                    )
                )
