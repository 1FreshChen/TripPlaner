from __future__ import annotations

import logging
from typing import Dict, Optional

from app.orchestration.base import ExecutionTrace

logger = logging.getLogger(__name__)


class ExecutionTracer:
    """Tracks active orchestrator runs in memory."""

    def __init__(self):
        self._active_traces: Dict[str, ExecutionTrace] = {}

    def start_run(self, run_id: str, plan_id: str) -> ExecutionTrace:
        trace = ExecutionTrace(run_id=run_id, plan_id=plan_id)
        self._active_traces[run_id] = trace
        logger.info("编排运行开始: run_id=%s, plan_id=%s", run_id, plan_id)
        return trace

    def finish_run(self, run_id: str) -> Optional[ExecutionTrace]:
        trace = self._active_traces.pop(run_id, None)
        if trace:
            logger.info(
                "编排运行结束: run_id=%s, 成功=%d, 失败=%d, 总耗时=%.0fms",
                run_id,
                trace.success_count,
                trace.failure_count,
                trace.total_duration_ms,
            )
        return trace

    def get_trace(self, run_id: str) -> Optional[ExecutionTrace]:
        return self._active_traces.get(run_id)
