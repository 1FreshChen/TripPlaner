# LangGraph 状态机集成计划（异步任务断点续跑）

> 状态：最终实施版；进入阶段 0 前仍须以技术 Spike 验证锁定依赖与 PostgreSQL 恢复语义。
>
> 覆盖范围：仅异步 `/plan` 路径；`/plan/sync` 和对话修改路径保持现有外部 API 行为。
>
> 核心目标：worker 重启后，任务从最后一个已持久化的 LangGraph 执行点继续；已经完成并写入 checkpoint 的 Agent 不重跑，尤其避免已经完成的 LLM Planner 重复计费。

## 1. 结论与可行性

方案总体可行，适合当前项目的静态 DAG。推荐采用“LangGraph 替换调度内核，保留现有 Agent、工具、FastAPI、ARQ 和领域模型”的局部集成方式，不将整个项目改造成 LangGraph Agent Server 工程。

原始方案的方向正确，但实施前必须接受以下边界：

1. **业务算法可以不改，事务和调用边界必须改。** `StateService.create_trip_plan()` 需要拆成准备、图执行、幂等落库三个阶段，不能在整个 LLM 工作流期间持有 SQLAlchemy 长事务。
2. **LangGraph 只能保证已完成并写入 checkpoint 的节点不重跑。** Worker 在 Planner 节点执行中退出时，Planner 会从节点开头重跑；如需 Critic 轮次级恢复，必须在后续阶段把 Planner 内部循环拆成多个节点。
3. **并行节点仍可保留。** 完整状态快照在 super-step 边界生成，但成功节点的 pending writes 会单独持久化；同一并行层中一个节点失败时，已经成功的节点通常不需要重跑。
4. **Checkpoint 是恢复机制，不等于业务审计。** token、tool call、Critic、fallback 和任务进度仍需保留现有业务记录。

### 1.1 工程评价

| 维度 | 评价 | 说明 |
|---|---|---|
| 技术可行性 | 高 | 现有 `[[景点, 天气, 酒店], [Planner]]` 可直接映射为 StateGraph |
| 实施难度 | 中高 | 图构建简单，难点在事务拆分、恢复调度、并发锁和幂等落库 |
| 代码破坏性 | 中低 | Agent、工具、API、前端基本不动；Worker、StateService 和任务维护逻辑需要调整 |
| 正常路径性能 | 基本持平 | 每个 super-step 增加少量 PostgreSQL checkpoint 写入 |
| 故障恢复收益 | 高 | 已完成节点可复用，避免从 stage 0 重跑 |
| LLM 成本收益 | 中 | 已完成 Planner 可复用；执行中的 Planner 仍可能重跑 |
| 可扩展性 | 高 | 后续容易增加条件边、Critic 节点、HITL 和子图 |
| 运维复杂度 | 中等增加 | 新增 psycopg 连接池、checkpoint 表、恢复任务和清理策略 |

### 1.2 工程量

- 最小验证版：2–3 人日。
- 行为等价的 LangGraph 图：3–5 人日。
- 生产级断点续跑总计：12–18 人日。
- 如进一步拆分 Planner、Critic、修订轮次：额外 5–10 人日。

### 1.3 终审发现与最终处理

本计划经终审后确认可实施，但原稿存在若干会导致数据库约束失败、重复落库或恢复行为不确定的问题。本最终版统一按以下决策执行：

| 原问题 | 风险 | 最终处理 |
|---|---|---|
| 失败草稿计划写入 `status="failed"`，但现有数据库约束不允许 | 失败终结事务直接报错，遗留 generating 草稿 | Alembic migration 将 `failed` 加入 `ck_trip_plans_status` |
| prepare 提前提交 generating 草稿，且新增 failed 后现有查询仍普遍使用 `status != "archived"` | generating/failed 草稿会进入会话列表；用户可按 ID 读取、编辑、回退、归档或对话引用，甚至与运行中 finalize 竞态 | 用户查询改为公开状态白名单；公开读取与工作流内部读取使用不同方法，禁止负向过滤自动暴露新状态 |
| `finalize_trip_plan()` 与 `TripPlanTask=succeeded` 分成两个提交点 | 两次提交之间宕机时，审计、token 或长期记忆可能重复写入 | 合并为同一个短事务，并在事务内锁定 Task 和 Plan |
| “meal 节点恢复时不重复调用”表述过于绝对 | 节点执行中宕机仍会整节点重跑 | 明确只保证“节点成功并 checkpoint 后不重跑”，失败时降级使用原计划 |
| validate 节点直接标记任务失败 | 图节点引入数据库副作用，恢复时难以保证原子性 | validate 只写 `terminal_error`；Worker 执行统一失败终结事务 |
| 并行节点按固定节点进度上报 | 节点完成顺序不同会导致 phase/message 回退 | 按“已完成采集节点数量”投影同一 collecting phase，progress 和 phase 均单调 |
| heartbeat 只说明使用后台协程 | 旧 lease 可能继续刷新，退出时可能泄漏协程或连接 | 独立短 Session、带 `lease_owner` 条件更新、finally 中 cancel 并 await |
| 使用 `resume_pending/resume_queued` 等新 phase 表达恢复调度 | 现有 TaskPhase API 不接受这些值，状态响应会校验失败 | 新增不对外暴露的 `recovery_state`，用户可见 phase 继续使用现有枚举 |
| 恢复扫描归属写成“worker 或 supervisor”，文件清单又保留 API 扫描 | 部署后可能没有扫描器或多处重复扫描 | 第一阶段固定由 Worker 后台协程负责，并用全局 advisory lock 选举单一 scanner |
| snapshot 虽规定锁后读取，但未要求锁后重读任务 | 可能使用加锁前的旧任务状态继续执行 | 获取任务锁后重新加载 Task、检查终态和版本，再读取 snapshot |
| prepare/finalize 同时服务异步任务和 `/plan/sync`，但签名默认存在 task_id | 同步路径没有 TripPlanTask，接口无法统一 | `task_id` 显式可选；异步路径原子更新 Task，同步路径只更新业务计划 |
| 阶段 2 仍写“四个节点适配器” | 实施范围与最终图不一致 | 改为六个主节点，并补 meal、validate 和进度投影测试 |

上述事项属于本计划的硬性实施约束，不再作为编码阶段的可选方案。

## 2. 背景与现状

异步规划任务 `/plan` 在独立 arq worker 中执行。Worker 被部署或重启取消时，当前代码捕获 `asyncio.CancelledError` 后直接将任务标为失败，用户只能重新提交。

### 2.1 已持久化

- `TripPlanTask` 的 `status`、`phase`、`progress`、`message`、请求和最终结果。
- 阶段进度由独立短事务写入 PostgreSQL。

### 2.2 尚未持久化

- `ExecutionTracer._active_traces`。
- 编排 `context` 中的景点、天气、酒店、Planner 输出。
- Planner 的 token、tool call、Critic 中间结果。
- 当前 DAG 的待执行节点、失败节点和恢复位置。

### 2.3 需要修正的现状描述

`StateService.create_trip_plan()` 开头创建的 `TripPlan(status="generating")` 当前只执行 `flush()`，最终由 Worker 成功路径统一 `commit()`。因此 Worker 中断时该行会随事务回滚，不能视为已经持久化的草稿计划。

## 3. 已确定决策

- 真正引入 `langgraph`，不自研 checkpoint 状态机。
- 使用 `StateGraph` + `AsyncPostgresSaver` 替换异步 `/plan` 的调度内核。
- 第一阶段恢复粒度为 Agent/节点级；Planner 内部 Critic 循环暂不拆分。
- 保留三个采集 Agent 并行，不为 checkpoint 串行化。
- Agent 的业务算法和工具实现保持不变，通过节点适配器调用。
- `StateService` 的业务规则保持不变，但拆分事务和方法边界。
- `TripPlanTask` 继续作为前端任务查询的 read model；LangGraph checkpoint 是执行恢复状态。
- 每个任务持久化 `orchestration_backend`、`workflow_version` 和 `state_schema_version`，不能只依赖部署时的全局功能开关。
- Graph State 只保存 JSON 兼容数据；数据库 Session、LLMService、ToolRegistry、ToolExecutor、MCP client 通过运行时依赖注入，不进入 checkpoint。
- `meal_enrichment` 和 `validate` 都是正式图节点；finalize 位于图外，只做数据库终结。
- 异步路径在首次执行、图 invoke 之前创建并提交稳定 `plan_id`；不要求创建任务 API 立即创建 TripPlan。
- 成功终结必须在同一事务内完成 TripPlan、初始版本、审计、长期记忆和 TripPlanTask 更新。
- 失败终结必须在同一事务内将 TripPlan 草稿和 TripPlanTask 标为 failed。
- `generating` 和 `failed` 是工作流内部状态，不属于用户可见计划；第一阶段只有 `completed` 可以通过用户 API 查询和修改。
- 所有用户可见 TripPlan 查询使用显式状态白名单，禁止继续使用 `status != "archived"`；工作流使用独立内部查询读取 generating/failed。
- 恢复扫描第一阶段固定归属于 Worker 进程；API 进程不承担 running-task 恢复。

## 4. 目标架构

```text
ARQ Worker
  ├─ 加载 task_id 基础信息
  ├─ 获取 task_id advisory lock / lease
  ├─ 锁后重新加载 TripPlanTask，检查终态、backend 和 workflow version
  ├─ 启动 heartbeat
  ├─ 锁后读取 graph snapshot
  │    ├─ 无 snapshot：prepare_planning_context() → ainvoke(initial_state)
  │    ├─ snapshot.next 非空：ainvoke(None)
  │    └─ snapshot.next 为空：读取 snapshot.values
  ├─ LangGraph invoke/resume          # 图执行期间不持有 SQLAlchemy Session
  │    ├─ attraction_search ─┐
  │    ├─ weather_query ─────┼─→ trip_planner → meal_enrichment → validate → END
  │    └─ hotel_recommendation ┘
  ├─ finalize_trip_plan()             # 单一短事务、幂等，包含 Task=succeeded
  └─ finally：停止并 await heartbeat，释放锁
```

### 4.1 `StateService` 拆分

将当前 `create_trip_plan()` 重构为：

- `prepare_planning_context(request, task_id: str | None = None)`：读取用户、长期记忆和近期会话；创建 `TripPlan(status=generating)` 草稿行并提交（短事务），返回含稳定 `plan_id` 的可序列化上下文。异步路径传入 `task_id`，同步路径传入 `None`。
- 图节点 `meal_enrichment`：餐饮补充（调用百度 API），位于 `trip_planner` 之后。成功时覆盖 `trip_planner` 并写入 `meal_enriched=True`；失败时保留原始计划、记录 fallback 并继续 validate。
- 图节点 `validate`：执行 `validate_trip_plan_for_request()` 最终校验。成功时写入 `validation_passed=True`；失败不重试，由 error handler 写入可序列化 `terminal_error` 并路由到 END，节点本身不得更新业务数据库。
- `finalize_trip_plan(task_id: str | None, plan_id, graph_result)`：严格只含 DB 落库，不含校验和外部 API。异步路径在同一事务中更新计划、初始版本、token/tool/Critic/fallback 审计、长期记忆和 `TripPlanTask=succeeded`；同步路径不更新任务表。
- `fail_planning_run(task_id: str | None, plan_id: str | None, terminal_error)`：单一短事务；存在草稿时标记 TripPlan=failed，存在异步任务时同时标记 TripPlanTask=failed，并写入稳定错误码和错误摘要。这样也覆盖 Worker 在 prepare 前退出、尚无 `plan_id` 的情况。
- `create_trip_plan(request)`：同步路径兼容包装，依次执行 prepare、旧编排、校验、finalize；prepare 提交后发生异常时调用 `fail_planning_run(None, plan_id, error)`。对外 API 行为不变，但内部允许保留明确标记为 failed 的失败草稿。

不得跨整个图执行持有一个 `AsyncSession`。异步 finalize 必须先锁定对应 `TripPlanTask` 和 `TripPlan`；若任务已经 succeeded，则直接返回已保存结果。只有在所有业务数据写入完成后，才在同一事务内将任务更新为 succeeded。事务回滚时，计划、版本、审计、长期记忆和任务状态必须一起回滚。

`plan_id` 来自 `prepare_planning_context()` 短事务创建的 `TripPlan(status=generating)` 草稿行。异步路径创建后在同一事务内把 `TripPlan.id` 写入 `TripPlanTask.result_plan_id`；不能在 TripPlan 尚未存在时把预生成 UUID 写入该外键。prepare 以锁后读取的 `result_plan_id` 为幂等依据：已填则校验并复用草稿，未填才创建，避免“prepare 后、首次 invoke 前”重启造成重复计划。

现有 `ck_trip_plans_status` 不允许 `failed`。实施前必须通过 Alembic migration 将 `failed` 加入约束；迁移未完成前不得启用 LangGraph 路径。

meal 节点的“不重复”保证仅适用于节点成功并完成 checkpoint 之后。节点执行中发生进程硬退出时，节点会从头重跑，可能重复百度只读查询；该边界必须保留在验收说明中。

### 4.2 现有概念映射

| 现有概念 | LangGraph 对应 |
|---|---|
| `context: Dict[str, Any]` | `PlanningState` |
| `AgentRegistry.resolve_execution_plan()` | `StateGraph` 节点和边 |
| stage 内 `asyncio.gather` | 同一 super-step 并行节点 |
| `AgentDefinition.retry_policy` | 显式 `langgraph.types.RetryPolicy` |
| `timeout_seconds` | `TimeoutPolicy` 或兼容的 `asyncio.wait_for` 包装 |
| `FallbackChain` | 节点 `error_handler` 返回 `Command`，或节点包装器返回错误状态后路由 |
| `ExecutionTracer` | checkpoint + graph event；业务审计仍单独保留 |
| `run_id/task_id` | `thread_id=task_id` |

### 4.3 SSE 进度投影

LangGraph checkpoint 是执行真相，`TripPlanTask` 是 SSE/read model。节点不得为了上报进度持有或写入业务 Session；Worker 消费节点完成事件后，使用独立短事务投影进度。

| 完成事件 | phase | progress |
|---|---|---:|
| 任务入队 | `queued` | 0 |
| prepare 开始 | `preparing` | 5 |
| prepare 完成 | `collecting_context` | 10 |
| 每完成一个采集节点 | `collecting_context` | `10 + floor(25 × completed_collectors / 3)` |
| trip_planner 完成 | `llm_planning` | 75 |
| meal_enrichment 完成或降级 | `meal_enrichment` | 90 |
| validate 成功 | `validating` | 95 |
| finalize 开始 | `saving` | 95 |
| finalize 与任务成功事务提交 | `completed` | 100 |

三个并行采集节点继续使用现有 `collecting_context` phase，消息中可显示具体完成节点和 `completed/3`，不得新增 API phase 或按节点名称分配会受完成顺序影响的固定 progress。恢复调度状态使用任务表内部字段，不复用面向前端的 phase。

恢复时在 advisory lock 内读取 snapshot，根据三个采集结果、`trip_planner`、`meal_enriched`、`validation_passed` 和 `terminal_error` 计算 `derived_progress`。写回任务表时使用 `max(persisted_progress, derived_progress)`，phase 也按固定 rank 单调推进。若进程在 checkpoint 已提交、进度短事务尚未提交时退出，恢复对账只能向前补齐，不能回退或重复发送旧阶段。

### 4.4 TripPlan 可见性与查询边界

prepare 提前提交 `TripPlan(status=generating)` 后，TripPlan 表同时承担“工作流内部草稿”和“用户可见行程”两类数据。必须在查询层显式隔离，不能依赖 UUID 难猜或前端不展示。

第一阶段固定定义：

```python
PUBLIC_PLAN_STATUSES = frozenset({"completed"})
WORKFLOW_PLAN_STATUSES = frozenset({"generating", "failed"})
```

约束如下：

- 会话行程列表只能返回 `status IN PUBLIC_PLAN_STATUSES`。
- 行程详情、手动编辑、版本列表、版本回退、归档和对话 `referenced_plan_id` 必须调用统一的 `_get_public_plan()`，同时校验 session owner 和公开状态；非公开状态统一按不存在处理并返回 404。
- 禁止用户查询继续使用 `status != "archived"`。这种负向过滤会让以后新增的任意内部状态自动暴露。
- prepare、finalize、失败终结、恢复和清理使用独立 `_get_workflow_plan()` 或专用 repository 方法，允许按明确状态集合读取 generating/failed，并要求 task_id、lease 或内部调用上下文；内部方法不得暴露为 API 依赖。
- 异步 Task result/status 只有在 Task succeeded 时提供结果入口；failed、running 和恢复中的任务不得返回 `result_plan_id` 或草稿详情。
- 若未来真正启用 `editing` 状态，必须单独决定其公开、可修改和可归档语义，并补测试后才能加入公开白名单。

该隔离既防止失败草稿出现在用户界面，也防止用户在图执行期间修改或归档 generating 草稿，与 finalize 产生竞态。

## 5. PlanningState 设计

建议至少包含：

```python
class PlanningState(TypedDict, total=False):
    state_schema_version: int
    workflow_version: str
    task_id: str
    plan_id: str
    request: dict
    memory_context: dict
    conversation_context: list[dict]

    attraction_search: list[dict]
    weather_query: list[dict]
    hotel_recommendation: list[dict]
    trip_planner: dict
    meal_enriched: bool
    validation_passed: bool

    agent_results: Annotated[list[dict], operator.add]
    planner_tool_calls: list[dict]
    planner_token_usage: dict | None
    plan_critique_events: list[dict]
    failed_plan_critique_events: list[dict]
    fallback_events: list[dict]
    quality_failed: bool
    terminal_error: dict | None
```

Pydantic 对象在写入 State 前统一使用 `model_dump(mode="json")`，节点调用现有 Agent 前再执行 `model_validate()`。不要将服务实例或数据库对象写入 State。

`trip_planner` 字段承载最终 TripPlan：`trip_planner` 节点输出原始计划，`meal_enrichment` 成功时补餐饮后覆盖同一字段，失败降级时保持原值；`validate` 只设置验证标志或 `terminal_error`，不改写计划。`plan_id` 来自 prepare 阶段创建的 TripPlan 草稿行（见 §4.1），不在 State 内重新生成。

并行节点尽量写不同的 state key；多个节点写同一 key 时必须定义 reducer。`agent_results` 中每条记录需要稳定的 `agent_name`、状态、attempt、错误和 fallback 信息，最终投影时按稳定标识去重。

## 6. Checkpoint 与恢复语义

### 6.1 并行采集节点

LangGraph 在 super-step 边界生成完整 checkpoint，同时将单个节点完成后的输出保存为 pending writes。三个采集节点保持并行：

- 单一分支失败时，只重试或恢复失败分支。
- 已成功分支的 pending writes 可复用。
- 硬杀进程时是否已写入 pending writes 取决于节点完成和数据库写入时点，必须通过真实 PostgreSQL 集成测试验证。

### 6.2 Planner 节点

Planner 作为独立节点，节点成功并写入 checkpoint 后不再重复执行。以下边界必须写入文档和验收标准：

- Planner 开始前退出：恢复后正常执行 Planner。
- Planner 执行中退出：Planner 从节点开头重跑，可能重复 LLM 调用。
- Planner 完成并 checkpoint 后退出：恢复时不得重跑 Planner。
- 如果将来要求 Critic 轮次级恢复，需要将 `generate_plan`、`critic`、`revise_plan` 拆成独立节点。

### 6.3 首次调用、恢复和已完成图

不能只用“是否存在 checkpoint”判断调用方式。Worker 必须按固定顺序执行：

```text
1. 获取 task_id advisory lock
2. 锁后重新加载 TripPlanTask
3. 若任务已终态则退出；校验 backend/workflow/state version
4. 写入本次 lease_owner，启动 heartbeat
5. 读取 graph snapshot
6. 无 snapshot：幂等 prepare → 构造 initial_state → ainvoke(initial_state, config)
7. snapshot.next 非空：不重建 memory/conversation，不重复 prepare → ainvoke(None, config)
8. snapshot.next 为空：读取 snapshot.values，按 terminal_error 进入成功或失败终结事务
```

如果 prepare 后、首次 invoke 前宕机，恢复时仍属于“无 snapshot”，但 prepare 必须通过 `result_plan_id` 复用现有草稿。已有 snapshot 时不得使用重新查询到的长期记忆或会话覆盖 checkpoint State。

同一个 `task_id` 必须始终使用同一 `workflow_version` 的图恢复。锁后重读任务是强制步骤，不能继续使用获取锁前加载的 ORM 对象。

## 7. Retry、Timeout 与 Fallback

现有语义需要逐项等价映射，不能依赖 LangGraph 默认策略：

- 景点：最多 3 次，15 秒。
- 天气：最多 2 次，10 秒。
- 酒店：最多 3 次，15 秒。
- Planner：最多 1 次，300 秒。
- Meal enrichment：最多 1 次，节点总超时 60 秒；失败不使整个规划失败，由 error handler 保留原计划、记录 fallback 并转入 validate。若压测证明 60 秒不足，只能通过配置调整，不能取消超时。
- Validate：最多 1 次；`PlanQualityError` 明确不重试。失败时 error handler 写入 `terminal_error`、`validation_passed=False` 并转到 END。
- `retry_on` 显式限制为当前允许重试的异常及 LangGraph `NodeTimeoutError`。
- `base_delay`、backoff multiplier 和 max delay 映射到 LangGraph 对应字段。

节点抛异常后普通 conditional edge 不会自动执行。LangGraph 1.2+ 的节点级 `error_handler` 在 retry 耗尽后运行，并可返回 `Command(update=..., goto=...)`；阶段 0 必须用最终锁定版本验证此行为。若验证失败，则由节点包装器保留现有 `_execute_with_retry + FallbackChain` 语义，不能为了迁移改变 fallback 行为。

Meal error handler 不能把部分修改的 Pydantic 对象写回 State；节点异常时 checkpoint 中仍保留 Planner 原始结果。Validate error handler 不写数据库。图结束后，Worker 检查 `terminal_error`：为空且 `validation_passed=True` 时执行成功 finalize，否则执行失败终结事务。

必须保留现有 Planner deterministic fallback 对质量标志和失败 Critic 事件的清理语义。

## 8. PostgreSQL Checkpointer

### 8.1 依赖

实施时用干净环境解析并锁定精确版本：

- `langgraph==<verified>`（终审参考基线为 1.2.9，不直接视为最终锁定结果）
- `langgraph-checkpoint-postgres==<verified>`
- `psycopg[binary,pool]==<verified>`

与现有 SQLAlchemy `asyncpg` 并存两个连接池。需要执行完整依赖回归，尤其检查当前 Pydantic AI 和 OpenAI SDK 约束。

阶段 0 的 API 语义核对以 [LangGraph fault tolerance](https://docs.langchain.com/oss/python/langgraph/fault-tolerance)、[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 和对应版本 Python reference 为准，不依赖博客或旧示例。

### 8.2 生命周期

- Worker `on_startup` 创建 `AsyncConnectionPool`、`AsyncPostgresSaver` 和 compiled graph。
- Worker 内多个 Job 共用 saver/pool；节点使用 Agent factory，不能共享带可变 `last_*` 字段的 Agent 实例。
- Worker `on_shutdown` 关闭 psycopg pool；生命周期由 pool manager 管理，不假设 saver 本身拥有 `aclose()`。
- `setup()` 在开发环境可由启动逻辑执行；生产环境优先作为部署迁移步骤执行，避免运行账号长期拥有 DDL 权限。
- 多个 Worker 并发启动时，setup/migration 必须验证安全性。

### 8.3 DSN

优先显式配置 `LANGGRAPH_CHECKPOINT_DSN`。如果从 `DATABASE_URL` 派生，使用 SQLAlchemy URL 解析器处理 driver、密码转义、SSL 和 query 参数，不做简单字符串替换。

### 8.4 表与保留期

checkpoint 表名和迁移表名以最终锁定版本为准，不在业务代码中硬编码。默认可能创建于 `public` schema；如需隔离，使用独立数据库/DSN，或在验证连接池 `search_path` 后使用独立 schema。

checkpoint 含请求、偏好、工具结果和计划，属于用户数据：

- 保留期与 `TripPlanTask` TTL 对齐。
- 任务过期清理时同步删除对应 thread/checkpoint。
- 删除会话或用户数据时纳入 checkpoint 清理。
- 监控 checkpoint 表大小、写入延迟和连接池占用。

## 9. Worker 恢复控制

### 9.1 取消语义

当任务记录的 `orchestration_backend="langgraph"` 时，`asyncio.CancelledError` 不得调用 `fail_task()` 进入终态。不能依据部署时的全局开关判断在途任务。应：

1. 回滚当前短事务。
2. 停止 heartbeat。
3. 最佳努力将内部 `recovery_state` 标为 `pending`，保持原有用户可见 phase/progress 和非终态 status。
4. 释放 advisory lock。
5. 重新抛出取消异常，让 Worker 正常退出。

用户主动取消仍标记 `cancelled`；普通业务异常在 retry/fallback 全部失败后标记 `failed`。

### 9.2 Heartbeat 与 stale 判定

仅靠 phase 更新不足以区分慢 Planner 和死亡 Worker。建议为 `TripPlanTask` 增加：

- `heartbeat_at`
- `lease_owner`
- `orchestration_backend`
- `workflow_version`
- `state_schema_version`
- `recovery_state`：内部状态，取值 `none/pending/queued`，不加入 TaskPhase API 枚举。
- `recovery_enqueued_at`：用于判断 recovery Job 是否长期未启动。
- `checkpoint_deleted_at`：记录 TTL 清理已完成，避免 scanner 每轮重复删除同一 checkpoint；删除失败时保持为空以便重试。

Worker 在成功写入本次 `lease_owner` 后启动独立后台协程（`asyncio.create_task`），持锁期间每 15–30 秒更新 heartbeat。具体约束：

- 每次 heartbeat 使用独立、短生命周期 SQLAlchemy Session，不复用图执行或 finalize Session。
- 更新条件必须包含 `task_id`、本次 `lease_owner` 和 `status=running`；更新行数为 0 时视为 lease 已失效，旧 Worker 不得继续刷新。
- heartbeat 协程异常必须被记录并通知主工作流；不能静默停止后继续执行数分钟。
- 所有图节点必须使用异步非阻塞 I/O；同步 SDK 调用放入线程池，否则阻塞事件循环时独立 heartbeat 仍无法调度。
- 所有退出路径在释放 advisory lock 之前执行 `heartbeat_task.cancel()` 并 `await heartbeat_task`，吞掉预期的 `CancelledError`，不得泄漏协程或连接。

恢复扫描只根据 `heartbeat_at` 和 lease 状态判断运行 Worker 是否存活，不再使用 phase 更新时间代替心跳。数据库为 stale 查询建立 `(status, orchestration_backend, heartbeat_at)` 组合索引。

### 9.3 恢复投递

`recover_stale_running_tasks` 必须与 Queue 协作，不能只在 `task_service.py` 内修改数据库。第一阶段固定由 ARQ Worker 的 startup/shutdown 生命周期启动和停止恢复扫描协程，API 进程不负责 running-task 恢复。多 Worker 部署时，每轮扫描先竞争一个与业务 task lock 不同的全局 advisory lock；未获得 scanner lock 的 Worker 跳过本轮。

完整流程：

1. 获取全局 scanner advisory lock。
2. 使用 `FOR UPDATE SKIP LOCKED` 选取以下任务：heartbeat 超时的 running 任务、上次 enqueue 失败且 `recovery_state=pending` 的任务，或 `recovery_state=queued` 且 `recovery_enqueued_at` 超过 queue stale 阈值仍未启动的任务。
3. 检查 backend、workflow/state version 和 `retry_count`；超过上限时执行统一失败终结事务。
4. 对本次恢复执行 `retry_count += 1`，设置 `recovery_state=queued` 和 `recovery_enqueued_at=now()`，清除旧 `lease_owner`，保持用户可见 phase/progress 不变并提交。
5. 使用 `{task_id}:recovery:{retry_count}` enqueue recovery Job。
6. enqueue 失败时，用带当前 `retry_count` 条件的短事务把 `recovery_state` 改为 `pending`；下一轮在退避时间后重试。不能把普通 enqueue 失败立即标为终态失败。
7. Worker 开始执行 recovery Job 后，在 task advisory lock 内重读任务，设置 `recovery_state=none`，并重建 lease/heartbeat。
8. 释放 scanner advisory lock。

ARQ Job ID 只负责减少重复入队，真正的执行互斥由 task advisory lock/lease 保证。scanner 查询必须排除尚未达到 queue stale 阈值的 `recovery_state=queued` 任务，避免一次成功 enqueue 被下一轮立即生成新的 recovery attempt。

### 9.4 并发锁

Worker 入口使用 `pg_try_advisory_lock` 或等价 lease：

- 锁键由 `task_id` 稳定映射为 64 位整数。
- session 级 advisory lock 使用独立连接持有到 Job 结束。
- 在所有退出路径的 `finally` 中释放。
- 进程异常退出时依靠连接关闭自动释放。
- 两个 Worker 同时收到同一恢复任务时，未获取锁的一方直接退出，不改变任务终态。

## 10. 配置与任务版本

推荐使用枚举式 backend，而不是单一布尔开关：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `ORCHESTRATION_BACKEND` | `legacy` | 新任务使用 `legacy` 或 `langgraph`；在途任务以数据库记录为准 |
| `LANGGRAPH_CHECKPOINT_DSN` | `""` | 空时从 `DATABASE_URL` 安全派生 |
| `LANGGRAPH_WORKFLOW_VERSION` | `trip_planning_v1` | 新任务写入的图版本 |
| `LANGGRAPH_STATE_SCHEMA_VERSION` | `1` | State schema 版本 |
| `LANGGRAPH_MAX_RECOVERIES` | `3` | 恢复上限 |
| `LANGGRAPH_HEARTBEAT_SECONDS` | `20` | Worker 心跳间隔 |
| `LANGGRAPH_STALE_SECONDS` | `90` | 无心跳后允许恢复的阈值 |
| `LANGGRAPH_MEAL_TIMEOUT_SECONDS` | `60` | meal_enrichment 单次节点总超时 |
| `LANGGRAPH_RECOVERY_SCAN_SECONDS` | `30` | Worker recovery scanner 扫描间隔 |
| `LANGGRAPH_RECOVERY_QUEUE_STALE_SECONDS` | `180` | 已 enqueue 恢复任务长期未启动时允许重新投递的阈值 |

功能开关只决定新任务走哪条路径。任务创建时将 backend/version 固化到 `TripPlanTask`；部署切换开关不能改变在途任务的恢复实现。

## 11. 文件变更清单

### 11.1 新增

#### `backend/app/orchestration/checkpoint.py`

- psycopg pool manager。
- AsyncPostgresSaver 生命周期。
- 安全 DSN 转换。
- setup/health/delete thread 支持。

#### `backend/app/orchestration/langgraph_planning.py`

- `PlanningState`。
- 节点适配器。
- retry/timeout/error handler/fallback。
- `build_trip_planning_graph()`。
- snapshot 首次执行/恢复辅助函数。

#### `backend/app/services/planning_workflow_service.py`

- prepare/graph/finalize 的异步工作流入口。
- 成功 finalize 与失败终结的原子事务。
- snapshot→progress 恢复对账。
- legacy/langgraph backend 选择。

#### `backend/tests/test_langgraph_planning.py`

- 图顺序和并行。
- pending writes。
- retry、timeout、fallback。
- meal_enrichment 成功、执行中失败降级和 checkpoint 后不重跑。
- validate 成功及 terminal_error 路由。
- 三个并行采集节点所有完成顺序下的单调 progress/phase。
- snapshot 恢复。
- Planner checkpoint 后不重跑。

#### `backend/tests/test_langgraph_postgres_integration.py`

- 真实 Postgres saver。
- 新 saver 实例恢复。
- 序列化兼容。
- 两个 Worker 并发恢复。
- checkpoint 清理。

#### `backend/tests/test_trip_plan_visibility.py`

- session 列表只返回 completed。
- 用户详情、编辑、版本、回退、归档和对话引用拒绝 draft/generating/failed/archived。
- Task 未成功时不暴露 result_plan_id 或草稿内容。
- 工作流内部查询仍可在 task/lease 约束下读取 generating/failed。
- generating 草稿无法被用户操作，与 finalize 不发生用户侧竞态。

### 11.2 修改

- `backend/requirements.txt`：锁定依赖。
- `backend/app/config.py` 和 env 示例：新增配置。
- `backend/app/models/db_models.py`：任务 backend/version/heartbeat/lease/recovery/checkpoint-cleanup 字段，并允许 TripPlan 的 failed 状态。
- 新 Alembic migration：新增任务字段、`recovery_state` 约束、stale 查询组合索引，并重建 `ck_trip_plans_status` 纳入 failed。
- `backend/app/services/state_service.py`：拆 prepare/finalize/fail；新增公开状态白名单、`_get_public_plan()` 与受 task/lease 约束的内部工作流查询；保留 task_id 可选的同步兼容入口。
- `backend/app/orchestration/bootstrap.py`：增加独立 graph factory；不要让同一函数返回不稳定的 union 类型。
- `backend/app/tasks/worker.py`：backend 分流、saver lifecycle、task/scanner lock、heartbeat、恢复扫描协程和 CancelledError 语义。
- `backend/app/services/task_service.py`：stale 检测、恢复计数、内部 recovery_state 状态迁移和单调进度投影。
- `backend/app/api/main.py`：移除 running-task 恢复扫描；可保留与 LangGraph 无关的过期清理生命周期。
- `backend/app/api/routes/trip.py`：创建任务时写入 backend/workflow version；所有用户计划入口只使用公开查询，内部状态统一返回 404；API response schema 不变。
- `backend/app/api/routes/sessions.py` 和对话入口：列表及 `referenced_plan_id` 只接受 completed 计划。
- 现有测试：保留 legacy 回归并补充双轨测试。

### 11.3 业务实现保持不变

- `trip_planner.py` 中景点、天气、酒店和 deterministic planner 算法。
- `llm_planner.py`、`pydantic_planner.py`、`critic.py` 的第一阶段业务算法。
- ToolRegistry、ToolExecutor、地图/MCP/图片/预算工具。
- 前端和现有 API response schema。
- `/plan/sync` 和对话修改的外部行为。

“不修改 Agent 实现”指业务算法不改；节点适配器仍需构造旧 `context`，调用 `execute()`，并把 Agent 对 context 的 token/tool/Critic 修改显式提取为 State delta。

## 12. 实施顺序

### 阶段 0：技术 Spike

1. 锁定依赖版本。
2. 真实 PostgreSQL 上初始化 saver。
3. 验证 JSON State、Pydantic model_dump/model_validate。
4. 验证并行 pending writes 和跨 saver 实例恢复。
5. 验证 Worker max_jobs=4 下连接池容量。
6. 验证节点 timeout、RetryPolicy、error_handler、Command 路由和 NodeTimeoutError 的实际版本语义。

退出条件：Spike 全部通过，否则不进入业务接入。

### 阶段 1：事务与服务边界

1. 先执行模型和 Alembic migration：新增任务字段/索引，并允许 `TripPlan.status=failed`。
2. 拆 `prepare_planning_context(request, task_id=None)`。
3. 拆成功 `finalize_trip_plan()` 和失败 `fail_planning_run()`；草稿存在时保证 Plan 与 Task 同事务终结，草稿尚未创建时允许只终结 Task。
4. 将现有 `status != "archived"` 用户查询改为 completed 白名单；拆分公开 `_get_public_plan()` 与内部工作流查询，覆盖列表、详情、编辑、版本、回退、归档和对话引用。
5. 为 prepare 增加草稿行创建幂等，为成功/失败 finalize 增加事务原子性和孤儿草稿测试。
6. 保证 `/plan/sync` 对外行为和现有测试不变，并验证无 TripPlanTask 的可选 task_id 路径。

### 阶段 2：图行为等价迁移

1. 实现 PlanningState 和六个主节点适配器：三个采集节点、trip_planner、meal_enrichment、validate。
2. 保持三个采集节点并行。
3. 映射 retry、timeout、meal 降级、validate terminal_error 和质量标志清理。
4. 保留 memory、conversation、token、tool、Critic 和 fallback 元数据。
5. 实现节点完成事件到单调 SSE progress/phase 的投影。
6. 先使用 InMemorySaver 跑完离线测试。

### 阶段 3：Worker + PostgreSQL Saver

1. Worker startup/shutdown 管理 pool/saver/compiled graph。
2. 创建任务时固化 backend/workflow/state version。
3. Worker 根据任务记录选择 legacy/langgraph。
4. 接入“task lock → 锁后重读 Task → snapshot”首次/恢复/已完成分支。
5. 接入 snapshot→progress 对账。
6. 接入包含 Task=succeeded 的原子幂等 finalize，以及 terminal_error 失败终结。

### 阶段 4：恢复控制

1. 实现带 lease 条件和独立 Session 的 heartbeat 协程。
2. 在 Worker lifecycle 中启动 recovery scanner，并用全局 scanner advisory lock 选主。
3. 实现 stale heartbeat 与 `recovery_state=pending/queued` 扫描及 recovery enqueue。
4. 实现 task advisory lock/lease、retry_count 上限和 enqueue 退避。
5. 从 API lifespan 移除 running-task 恢复职责。
6. 修改 CancelledError 语义。
7. 验证两个 Worker 同时扫描、同时恢复时都只有一个有效执行者。

### 阶段 5：灰度和清理

1. 默认 `legacy` 发布。
2. 测试环境开启 `langgraph`。
3. 生产按新任务比例灰度，不迁移在途 legacy 任务。
4. 监控成功率、恢复次数、token、延迟、checkpoint 写入和连接数。
5. 任务过期时清理 checkpoint。
6. 所有 legacy 在途任务结束且指标稳定后，再评估删除旧 `AgentOrchestrator`。

## 13. 验证矩阵

### 13.1 行为等价

- [ ] memory_context 和 conversation_context 与旧路径一致。
- [ ] Planner 类型、内容和质量校验一致。
- [ ] retry 次数、timeout 和 fallback 顺序一致。
- [ ] fallback 后 stale quality flag 和失败 Critic 事件处理一致。
- [ ] token、tool call、Critic、fallback 审计没有丢失或重复。
- [ ] SSE phase/progress 与旧 API 契约一致。
- [ ] 恢复续跑时 SSE phase/progress 不跳变，与 checkpoint 记录的进度衔接。
- [ ] 三个并行采集节点按所有完成顺序排列时，progress 和 phase 均不回退。
- [ ] meal enrichment 失败时保留 Planner 原始计划并记录 fallback，validate 继续执行。
- [ ] validate 失败时图节点不写业务数据库，由 Worker 原子标记 Task 和草稿 Plan 为 failed。
- [ ] session 行程列表只返回 completed，不返回 draft/generating/failed/archived。
- [ ] 用户详情、编辑、版本、回退、归档和对话 referenced_plan_id 对 generating/failed 统一返回 404。
- [ ] Task 为 running/failed/cancelled/expired 时不返回 result_plan_id、result_url 或草稿内容。
- [ ] 工作流内部查询在 task/lease 校验后仍可读取 generating/failed，公开过滤不阻断 finalize 和失败终结。

### 13.2 Checkpoint 恢复

- [ ] 一个并行采集节点失败，已成功节点不重跑。
- [ ] 采集阶段硬杀 Worker，验证已写入 pending writes 的节点不重跑。
- [ ] Planner 执行中硬杀 Worker，允许 Planner 重跑且任务最终成功。
- [ ] Planner checkpoint 完成后硬杀 Worker，Planner 不重跑。
- [ ] meal_enrichment 执行中硬杀 Worker，允许节点重跑；节点成功并 checkpoint 后硬杀 Worker，恢复时不得重复调用百度 API。
- [ ] 图已完成、finalize 前硬杀 Worker，直接从 snapshot.values 幂等落库。
- [ ] finalize 事务中途失败时 Plan、版本、审计、长期记忆和 Task 全部回滚。
- [ ] finalize 提交后立即硬杀 Worker，Task 已同步 succeeded；恢复不会重复写版本、审计、token 或长期记忆。
- [ ] finalize 重复调用，只产生一条 TripPlan 和一个初始版本。
- [ ] 创建新的 saver/graph 实例后仍能恢复，不能只在同进程测试。
- [ ] prepare 之后、首次 invoke 之前硬杀 Worker，恢复后复用同一草稿行（result_plan_id 不变），不重复创建 TripPlan。
- [ ] 图执行最终失败时，在已迁移的状态约束下，generating 草稿和 Task 在同一事务中被标记为 failed。
- [ ] 已有 snapshot 时恢复不重新加载并覆盖 checkpoint 内的 memory/conversation。

### 13.3 并发和运维

- [ ] 两个 Worker 同时 resume，仅一个获得锁并执行。
- [ ] 用户无法在图运行期间通过公开 API 编辑、回退、归档或引用 generating 草稿。
- [ ] 恢复 Job 重复入队不会重复执行图或 finalize。
- [ ] heartbeat 健康时不会被 stale scanner 误恢复。
- [ ] heartbeat 停止后在目标时间内重新投递。
- [ ] trip_planner 阻塞期间 heartbeat 独立协程持续更新，不触发误恢复。
- [ ] 旧 Worker 的 lease_owner 失效后无法继续刷新 heartbeat。
- [ ] heartbeat 协程退出后没有遗留 Task 或数据库连接。
- [ ] 多 Worker 同时启动 scanner 时仅一个获得 scanner advisory lock。
- [ ] `recovery_state=queued` 未达到 queue stale 阈值时不会被重复投递；enqueue 失败后进入 pending 并可重试。
- [ ] 超过最大恢复次数后正确 fail。
- [ ] checkpoint pool 关闭后 Worker 正常退出，无连接泄漏。
- [ ] task TTL 到期后对应 checkpoints 被删除。
- [ ] workflow version 改版后旧任务仍使用旧图恢复，或按明确策略终止。

## 14. 风险与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| Worker 在 Planner 节点内部退出 | 重复 LLM 调用 | 明确保证边界；后续拆 Planner/Critic 节点 |
| Worker 在 meal 节点内部退出 | 重复百度只读查询 | 明确只保证 checkpoint 后不重跑；设置总超时并允许安全重跑 |
| asyncpg 与 psycopg 双池 | 连接数增加 | 独立小池、监控、容量测试 |
| saver 版本漂移 | checkpoint 无法恢复 | 精确锁版、workflow version、升级回归 |
| State schema 改动 | 在途任务反序列化失败 | JSON 状态、schema version、兼容默认值 |
| Agent 实例被多个 Job 共享 | `last_*` 数据竞争 | 图共享，Agent 用 factory 按节点执行创建 |
| recovery 重复投递 | 重复调用和计费 | scanner lock + attempt Job ID + task advisory lock + 原子 finalize |
| phase 不是心跳 | 慢任务被误判 | 独立 heartbeat_at |
| 同步调用阻塞事件循环 | heartbeat 无法调度 | 图节点异步化；同步 SDK 放入线程池 |
| checkpoint 与 SSE 短事务提交点不同 | 恢复后进度短暂不一致 | 锁内 snapshot→progress 对账，progress/phase 单调投影 |
| finalize 与 Task 分开提交 | 重复审计、token 或长期记忆 | 同一事务内完成全部业务落库和 Task=succeeded |
| failed 状态约束未迁移 | 失败终结事务回滚 | migration 作为启用 LangGraph 的前置门禁 |
| 用户查询继续使用 `status != "archived"` | generating/failed 草稿泄漏，运行中计划可被编辑或归档并与 finalize 竞态 | completed 公开白名单 + 公开/内部查询分离 + 非公开状态统一 404 |
| checkpoint 无限增长 | 数据库膨胀和隐私风险 | TTL、delete thread、监控和用户删除联动 |
| setup 需要 DDL 权限 | 生产启动失败 | 部署阶段迁移，运行账号仅 DML |
| 全局开关在部署时改变 | 在途任务走错 backend | backend/version 固化到 TripPlanTask |

## 15. 完成标准

本项目只有在同时满足以下条件后，才能称为“生产级 LangGraph 断点续跑”：

1. 真实 PostgreSQL saver 下可跨进程恢复。
2. 已 checkpoint 的 Agent 不重跑，Planner 中途退出的限制已有明确说明和测试。
3. prepare 为幂等短事务；成功 finalize 在一个事务内完成全部业务落库和 Task=succeeded，失败终结在一个事务内更新 Plan 与 Task。
4. legacy/langgraph 双轨可按任务版本稳定运行和回退。
5. Worker-owned scanner、heartbeat、scanner/task advisory lock、恢复退避和最大恢复次数形成闭环；API 不承担 running-task 恢复。
6. memory、质量、token、tool、Critic、fallback、SSE 行为无回归，所有并行完成顺序和恢复场景下 progress/phase 单调。
7. checkpoint 有版本兼容、监控、保留期和删除策略。
8. `TripPlan.status=failed` 数据库约束迁移已部署，并完成成功、失败和宕机边界的原子性测试。
9. 所有用户可见 TripPlan 查询使用 completed 白名单；draft/generating/failed/archived 不出现在列表且不能通过任何用户操作入口访问，内部 finalize/fail/recovery 查询仍正常。

达到以上标准后，LangGraph 路径可逐步成为默认；在所有 legacy 在途任务结束前，不删除旧 `AgentOrchestrator`。
