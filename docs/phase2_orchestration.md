# Phase 2: 编排引擎

> **所属项目**: 智能旅行助手 Harness 架构升级
> **依赖**: Phase 1（数据库和配置，编排器本身是纯内存抽象，但 Agent 执行时需要访问 DB 存储的执行结果）
> **被依赖**: Phase 4（API 层调用编排器）

---

## 目标

将当前 `TripPlannerAgent` 中顺序调用子 Agent 的硬编码逻辑，升级为正式的 **AgentOrchestrator**，支持 pipeline/parallel 执行、拓扑排序依赖解析、指数退避重试、以及 LLM→确定性算法→Mock 三级回退链。

---

## 核心设计

```
                    AgentOrchestrator
                           │
              ┌────────────┼────────────┐
              │            │            │
         Registry    FallbackChain   Tracer
    (Agent注册+依赖)  (LLM→确定→Mock)  (执行追踪)


执行计划（拓扑排序）:
  Stage 1 (parallel): AttractionSearch │ WeatherQuery │ HotelAgent
                           │            │            │
                           └────────────┼────────────┘
                                        │
                    Stage 2 (pipeline): PlannerAgent
```

---

## 2.1 核心数据类

```python
# backend/app/orchestration/base.py

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
import time


class AgentStatus(Enum):
    PENDING    = "pending"
    RUNNING    = "running"
    COMPLETED  = "completed"
    FAILED     = "failed"
    SKIPPED    = "skipped"


class ExecutionMode(Enum):
    PIPELINE = "pipeline"   # 串行（逐 stage 执行）
    PARALLEL = "parallel"   # 并行（同一 stage 内并发）


class FallbackLevel(Enum):
    LLM           = "llm"
    DETERMINISTIC = "deterministic"
    MOCK          = "mock"


@dataclass
class RetryPolicy:
    """重试策略"""
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    backoff_multiplier: float = 2.0
    retryable_exceptions: tuple = (TimeoutError, ConnectionError)

    def delay_for_attempt(self, attempt: int) -> float:
        return min(self.base_delay * (self.backoff_multiplier ** attempt), self.max_delay)


@dataclass
class AgentDefinition:
    """Agent 注册元数据"""
    name: str
    agent_class: type
    description: str = ""
    depends_on: List[str] = field(default_factory=list)
    execution_mode: ExecutionMode = ExecutionMode.PIPELINE
    retry_policy: Optional[RetryPolicy] = None
    timeout_seconds: float = 30.0
    fallback_levels: List[FallbackLevel] = field(
        default_factory=lambda: [FallbackLevel.LLM, FallbackLevel.DETERMINISTIC, FallbackLevel.MOCK]
    )


@dataclass
class AgentResult:
    """单个 Agent 执行结果"""
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
    """一次编排运行的完整追踪"""
    run_id: str
    plan_id: str
    agent_results: List[AgentResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    overall_status: AgentStatus = AgentStatus.PENDING

    @property
    def total_duration_ms(self) -> float:
        return (self.finished_at - self.started_at) * 1000

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.agent_results if r.status == AgentStatus.COMPLETED)

    @property
    def failure_count(self) -> int:
        return sum(1 for r in self.agent_results if r.status == AgentStatus.FAILED)

    @property
    def any_fallback_used(self) -> bool:
        return any(r.fallback_used is not None for r in self.agent_results)


class BaseAgent(ABC):
    """Agent 抽象基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """唯一标识"""
        ...

    @abstractmethod
    async def execute(self, context: Dict[str, Any]) -> Any:
        """
        执行 Agent 逻辑。
        context 为共享上下文字典，包含：
          - 'request': TripPlanRequest
          - '<agent_name>': 前置 Agent 的输出
          - 'user_preferences': 用户偏好
          - 'tool_registry': ToolRegistry 实例（用于工具调用）
        """
        ...
```

---

## 2.2 AgentRegistry — 注册中心 + 拓扑排序

```python
# backend/app/orchestration/registry.py

from collections import defaultdict, deque
from typing import Dict, List


class AgentRegistry:
    """Agent 注册中心，管理 Agent 的注册和依赖解析"""

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
        """
        拓扑排序，将 Agent 依赖图分组成 stage。
        返回: [[stage1_agents], [stage2_agents], ...]

        示例：
          注册: A(dep=[]), B(dep=[]), C(dep=[]), D(dep=[A,B,C])
          输出: [['A','B','C'], ['D']]
        """
        in_degree = {name: len(def_.depends_on) for name, def_ in self._agents.items()}
        dependents = defaultdict(list)
        for name, def_ in self._agents.items():
            for dep in def_.depends_on:
                dependents[dep].append(name)

        queue = deque([name for name, deg in in_degree.items() if deg == 0])
        stages = []

        while queue:
            stage = list(queue)
            stages.append(stage)
            queue.clear()
            for name in stage:
                for dependent in dependents[name]:
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        queue.append(dependent)

        if len(stages) == 0 or sum(len(s) for s in stages) != len(self._agents):
            remaining = [n for n, d in in_degree.items() if d > 0]
            raise ValueError(f"检测到循环依赖: {remaining}")

        return stages
```

---

## 2.3 AgentOrchestrator — 编排器

```python
# backend/app/orchestration/orchestrator.py

import asyncio
import uuid
import time
import logging
from typing import Any, Dict, List, Optional

from app.orchestration.base import AgentStatus, AgentResult, ExecutionTrace, RetryPolicy

logger = logging.getLogger(__name__)


class AgentOrchestrator:
    """多 Agent 编排器，支持 pipeline/parallel 执行"""

    def __init__(
        self,
        registry: 'AgentRegistry',
        fallback_chain: 'FallbackChain',
        tracer: 'ExecutionTracer',
    ):
        self._registry = registry
        self._fallback_chain = fallback_chain
        self._tracer = tracer

    async def run(
        self,
        plan_id: str,
        context: Dict[str, Any],
    ) -> ExecutionTrace:
        """
        执行完整的 Agent 图。
        1. 解析依赖 → 生成 stage 列表
        2. 按 stage 执行（stage 内 asyncio.gather 并行）
        3. 单 Agent 失败 → 走 FallbackChain
        """
        run_id = str(uuid.uuid4())
        trace = self._tracer.start_run(run_id, plan_id)

        try:
            stages = self._registry.resolve_execution_plan()
        except ValueError as e:
            logger.error("无法解析执行计划: %s", e)
            trace.overall_status = AgentStatus.FAILED
            return trace

        for stage_index, stage in enumerate(stages):
            logger.info("Stage %d: 并行执行 %s", stage_index, stage)

            tasks = [
                self._execute_with_retry(agent_name, context, trace)
                for agent_name in stage
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for agent_name, result in zip(stage, results):
                if isinstance(result, Exception):
                    logger.error("Agent '%s' 彻底失败: %s", agent_name, result)
                else:
                    context[agent_name] = result.output

        trace.finished_at = time.time()
        trace.overall_status = AgentStatus.COMPLETED if trace.failure_count == 0 else AgentStatus.FAILED
        return trace

    async def _execute_with_retry(
        self,
        agent_name: str,
        context: Dict[str, Any],
        trace: ExecutionTrace,
    ) -> AgentResult:
        """带重试和回退的单 Agent 执行"""
        agent_def = self._registry.get(agent_name)
        agent = agent_def.agent_class()
        retry_policy = agent_def.retry_policy or RetryPolicy()

        result = AgentResult(
            agent_name=agent_name,
            status=AgentStatus.RUNNING,
            started_at=time.time(),
        )

        last_error = None
        for attempt in range(retry_policy.max_attempts):
            try:
                output = await asyncio.wait_for(
                    agent.execute(context),
                    timeout=agent_def.timeout_seconds,
                )
                result.status = AgentStatus.COMPLETED
                result.output = output
                result.retries_used = attempt
                result.finished_at = time.time()
                result.duration_ms = (result.finished_at - result.started_at) * 1000
                trace.agent_results.append(result)
                return result

            except Exception as e:
                last_error = e
                logger.warning(
                    "Agent '%s' 第 %d/%d 次尝试失败: %s",
                    agent_name, attempt + 1, retry_policy.max_attempts, e,
                )
                if attempt + 1 < retry_policy.max_attempts:
                    delay = retry_policy.delay_for_attempt(attempt)
                    await asyncio.sleep(delay)

        # 所有重试耗尽 → 走 FallbackChain
        logger.warning("Agent '%s' 所有重试耗尽，进入回退链", agent_name)
        fallback_output = await self._fallback_chain.execute(
            agent_name=agent_name,
            context=context,
            last_error=last_error,
        )
        if fallback_output is not None:
            result.status = AgentStatus.COMPLETED
            result.output = fallback_output
            result.fallback_used = self._fallback_chain.last_level_used
        else:
            result.status = AgentStatus.FAILED
            result.error_message = str(last_error)
            result.fallback_used = None

        result.finished_at = time.time()
        result.duration_ms = (result.finished_at - result.started_at) * 1000
        trace.agent_results.append(result)
        return result
```

---

## 2.4 FallbackChain — 回退链

```python
# backend/app/orchestration/fallback.py

from typing import Any, Callable, Dict, List, Optional
from app.orchestration.base import FallbackLevel


class FallbackChain:
    """
    回退链：按优先级依次尝试不同等级的处理方式。
    默认路径：LLM → 确定性算法 → Mock 数据
    """

    def __init__(self):
        self._handlers: Dict[str, List[tuple]] = {}
        self.last_level_used: Optional[FallbackLevel] = None

    def register_handler(
        self, agent_name: str, level: FallbackLevel, handler: Callable
    ) -> None:
        """为指定 Agent 注册某等级的回退处理器"""
        if agent_name not in self._handlers:
            self._handlers[agent_name] = []
        self._handlers[agent_name].append((level, handler))

    async def execute(
        self,
        agent_name: str,
        context: Dict[str, Any],
        last_error: Optional[Exception] = None,
    ) -> Any:
        """依次尝试回退处理器，返回第一个成功的结果"""
        handlers = self._handlers.get(agent_name, [])
        # 按 FallbackLevel 排序：LLM → DETERMINISTIC → MOCK
        level_order = [FallbackLevel.LLM, FallbackLevel.DETERMINISTIC, FallbackLevel.MOCK]
        handlers.sort(key=lambda x: level_order.index(x[0]) if x[0] in level_order else 99)

        for level, handler in handlers:
            try:
                result = await handler(context)
                if result is not None:
                    self.last_level_used = level
                    return result
            except Exception:
                continue

        return None
```

---

## 2.5 ExecutionTracer — 执行追踪

```python
# backend/app/orchestration/trace.py

import time
import logging
from typing import Dict, Optional
from app.orchestration.base import AgentStatus, ExecutionTrace

logger = logging.getLogger(__name__)


class ExecutionTracer:
    """管理编排执行的追踪记录"""

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
                run_id, trace.success_count, trace.failure_count, trace.total_duration_ms,
            )
        return trace

    def get_trace(self, run_id: str) -> Optional[ExecutionTrace]:
        return self._active_traces.get(run_id)
```

---

## 2.6 Agent 注册示例（应用启动时执行）

```python
# backend/app/orchestration/bootstrap.py（或放在 __init__.py）

from app.orchestration.base import AgentDefinition, RetryPolicy
from app.orchestration.registry import AgentRegistry
from app.agents.trip_planner import AttractionSearchAgent, WeatherQueryAgent, HotelAgent, PlannerAgent


def bootstrap_orchestration() -> AgentRegistry:
    registry = AgentRegistry()

    registry.register(AgentDefinition(
        name="attraction_search",
        agent_class=AttractionSearchAgent,
        description="搜索目的地城市的景点信息",
        depends_on=[],
        retry_policy=RetryPolicy(max_attempts=3, base_delay=1.0),
        timeout_seconds=15.0,
    ))

    registry.register(AgentDefinition(
        name="weather_query",
        agent_class=WeatherQueryAgent,
        description="查询目的地城市的天气预报",
        depends_on=[],
        retry_policy=RetryPolicy(max_attempts=2, base_delay=1.0),
        timeout_seconds=10.0,
    ))

    registry.register(AgentDefinition(
        name="hotel_recommendation",
        agent_class=HotelAgent,
        description="推荐目的地城市的酒店",
        depends_on=[],
        retry_policy=RetryPolicy(max_attempts=3, base_delay=1.0),
        timeout_seconds=15.0,
    ))

    registry.register(AgentDefinition(
        name="trip_planner",
        agent_class=PlannerAgent,
        description="综合所有信息生成完整旅行计划",
        depends_on=["attraction_search", "weather_query", "hotel_recommendation"],
        retry_policy=RetryPolicy(max_attempts=2, base_delay=2.0),
        timeout_seconds=60.0,
    ))

    return registry
```

---

## 2.7 重构现有 Agent

现有 `backend/app/agents/trip_planner.py` 中的 4 个 Agent 需要重构为继承 `BaseAgent`：

```python
# 重构后的 AttractionSearchAgent 示例
from app.orchestration.base import BaseAgent

class AttractionSearchAgent(BaseAgent):
    name = "attraction_search"

    def __init__(self):
        settings = get_settings()
        self.amap_service = AmapService(settings.amap_api_key)
        self.enable_external = settings.enable_external_services

    async def execute(self, context: Dict[str, Any]) -> List[Attraction]:
        request = context["request"]
        # ... 现有逻辑 ...
```

---

## 关键文件清单

| 文件 | 操作 |
|---|---|
| `backend/app/orchestration/__init__.py` | 新建 |
| `backend/app/orchestration/base.py` | 新建：核心抽象（BaseAgent, AgentDefinition, RetryPolicy 等） |
| `backend/app/orchestration/orchestrator.py` | 新建：AgentOrchestrator |
| `backend/app/orchestration/registry.py` | 新建：AgentRegistry + 拓扑排序 |
| `backend/app/orchestration/fallback.py` | 新建：FallbackChain |
| `backend/app/orchestration/trace.py` | 新建：ExecutionTracer |
| `backend/app/orchestration/bootstrap.py` | 新建：Agent 注册引导 |
| `backend/app/agents/trip_planner.py` | 重构：Agent 继承 BaseAgent，异步化 |
| `backend/tests/test_orchestrator.py` | 新建：编排器测试 |

---

## 验证方式

1. 单元测试拓扑排序：构造有依赖的 Agent 图，验证 `resolve_execution_plan()` 输出正确的 stage 分组
2. 并行执行验证：mock 3 个耗时 1s 的无依赖 Agent，验证 `asyncio.gather` 下总耗时 < 1.5s（而非 3s）
3. 回退链测试：mock LLM Agent 始终抛异常，验证自动 fallback 到确定性算法 → Mock
4. 重试测试：mock 前 2 次失败第 3 次成功，验证 `retries_used=2` 且 `status=COMPLETED`
5. `pytest tests/test_orchestrator.py -v`
