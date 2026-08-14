# Pydantic AI 局部集成计划

## 摘要

- 实际引入 Pydantic AI，只迁移 `LLMPlannerAgent` 的模型—工具—输出循环。
- 保留现有 orchestrator、搜索 Agent、fallback、API、数据库及领域模型。
- 新旧 Planner 通过功能开关双轨运行，默认继续使用旧实现。
- CI 使用完全离线评测；另提供手动真实模型对比评测。
- 固定使用当前稳定版 `pydantic-ai-slim[openai]==2.30.0` 和 `pydantic-evals==2.30.0`，避免快速更新造成接口漂移。

## 核心实现

### 1. 类型化 Planner

新增 `PydanticAIPlannerAgent`，继续实现现有 `BaseAgent.execute(context) -> TripPlan`，保证外层编排接口不变。

内部定义：

- `PlannerDeps`：包含请求、景点、天气、酒店、记忆、近期对话、`ToolExecutor`、Critic 和运行记录器。
- `PlannerRunState`：记录工具调用、token 用量、Critic 事件和修改次数。
- `Agent[PlannerDeps, TripPlan]`：声明类型化依赖和最终输出。
- 在共享 `context` 与 `PlannerDeps` 的边界执行显式类型校验，缺字段或类型错误立即失败，不把错误延迟到提示词生成阶段。

模型工厂复用现有 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`：

- DeepSeek 地址使用 `DeepSeekProvider`。
- OpenAI 和其他 OpenAI-compatible 地址使用 `OpenAIChatModel + OpenAIProvider`。
- 保持 Chat Completions 协议，不切换 Responses API。
- 温度保持 `0.4`，外层 300 秒超时保持不变。

### 2. 七个工具的类型化适配

为以下工具建立 Pydantic 参数模型和 Pydantic AI 函数包装器，名称及功能保持不变：

- `amap_poi_search`
- `amap_weather`
- `hotel_search`
- `baidu_poi_search`
- `baidu_direction`
- `budget_calculator`
- `unsplash_image`

包装器负责：

- 从类型签名、`Field` 约束和说明生成工具 Schema，不再给新 Planner 使用手写 JSON Schema。
- 继续调用现有 `ToolExecutor`，保留缓存、超时和指数退避。
- `success=False` 转换成 `ToolFailed`，让模型选择替代工具；参数错误由 Pydantic 自动校验并反馈模型。
- 通过事件处理器记录调用，转换成现有 `{tool, arguments, id, error?}` 格式，保证审计、短期记忆和 API 不变。
- DeepSeek 连续调用工具时，最多允许五个带工具的模型轮次；达到上限后隐藏工具并要求生成最终 `TripPlan`。
- 每次运行默认限制为 20 次模型请求和 20 次成功工具调用，超过限制进入现有 fallback。

旧 `ToolRegistry` 和手写 Schema 暂不删除，继续服务旧 Planner 和其他现有功能。

### 3. 结构化输出与 Critic

使用 `output_type=TripPlan` 替换新 Planner 中的：

- `response_format={"type": "json_object"}`
- Markdown fence 清理
- `json.loads`
- 手工 `TripPlan.model_validate`
- 手工 correction prompt

输出验证顺序：

1. Pydantic 自动完成结构校验。
2. 调用现有 `validate_trip_plan_for_request()` 检查城市、日期、天数、天气、交通、住宿等硬规则。
3. 启用 Critic 时调用现有 `PlanCritic`，保持原有评分、事件格式、最低分和修改轮数。
4. 可修复问题通过 `ModelRetry` 返回模型；达到 `MAX_REFINEMENT_ROUNDS` 后仍不合格则抛出 `PlanQualityError`，交给现有 deterministic fallback。
5. 输出重试预算设为 `MAX_REFINEMENT_ROUNDS + 1`；Critic 修改次数单独记录，避免格式错误消耗后错误计算 Critic 轮次。

运行结束后将 Pydantic AI 的使用量转换为现有 `TokenUsage`，继续写入：

- `context["trip_planner_tool_calls"]`
- `context["trip_planner_token_usage"]`
- `context["plan_critique_events"]`

### 4. 功能开关和兼容性

新增配置：

```text
ENABLE_PYDANTIC_AI_PLANNER=false
PYDANTIC_AI_REQUEST_LIMIT=20
PYDANTIC_AI_TOOL_CALL_LIMIT=20
```

Planner 选择规则：

```text
未启用外部服务或 LLM
  → Deterministic Planner

启用 LLM，但 ENABLE_PYDANTIC_AI_PLANNER=false
  → 旧 LLMPlannerAgent

启用 LLM，且 ENABLE_PYDANTIC_AI_PLANNER=true
  → 新 PydanticAIPlannerAgent
```

主要改动集中在新 Planner 模块、`config.py` 和 `orchestration/bootstrap.py`。不修改前端、API Schema或数据库结构，也不删除 `LLMService`，因为旧 Planner、Critic 和状态服务仍需要它。

## 评测与测试

### 离线 CI 门禁

使用 Pydantic AI `TestModel/FunctionModel`、Fake 服务和 Fake `ToolExecutor`，禁止真实网络请求。

建立至少 24 个场景，覆盖：

- 不同城市、天数、预算、交通和住宿。
- 雨天户外冲突、日期和天数错误、空景点、预算异常。
- 记忆和近期对话注入。
- 七个工具的参数默认值、范围和必填校验。
- 工具缓存、超时、上游失败和替代工具选择。
- 非法结构输出后的自动修正。
- Critic 通过、要求修改、达到修改上限。
- DeepSeek 重复工具调用后的强制收尾。
- 请求和工具调用达到上限。
- 工具调用、token 和 Critic 审计格式兼容。

使用 `pydantic-evals` 建立：

- `RequestFitEvaluator`：复用现有领域质量规则。
- `ToolPolicyEvaluator`：检查工具数量、参数和失败处理。
- `CritiqueEvaluator`：记录平均分及是否仍需修改。
- `MaxDuration`：检测离线任务耗时异常。

合并门禁：

- 功能开关关闭时，现有 pytest 全部通过。
- 新 Planner 离线场景结构和硬业务规则通过率为 100%。
- 七个工具生成的 Schema 与参数约束测试全部通过。
- 任何默认 CI 流程都不能访问真实 LLM、地图或图片服务。
- 新旧 Planner 对外返回类型及审计字段完全兼容。

### 可选真实模型评测

提供手动命令，通过 `RUN_LIVE_AGENT_EVALS=1` 启用，使用现有 LLM 配置，不在 CI 默认执行。

对 12 个代表性场景分别运行旧、新 Planner 各三次，报告：

- 完成率和硬规则通过率。
- Critic 平均分。
- 工具调用数与失败率。
- 延迟、输入/输出 token 和估算成本。
- fallback 使用率。

切换生产默认值前，新 Planner 应满足：

- 硬规则通过率不低于 95%，且不低于旧 Planner。
- Critic 平均分相对旧 Planner下降不超过 0.3。
- P95 延迟增加不超过 20%。
- 平均 token 成本增加不超过 25%。
- 无新增未处理异常或审计数据缺失。

不满足时保持开关关闭，不阻塞旧 Planner 正常运行。

## 发布顺序与假设

1. 加入固定版本依赖、类型和模型适配层，开关保持关闭。
2. 完成七个工具包装、结构化输出和 Critic 集成。
3. 完成离线测试与 eval，运行完整回归。
4. 在开发环境开启新 Planner，执行真实模型对比。
5. 指标达标后再单独决定是否将开关默认改为开启；本阶段不删除旧 Planner。

默认假设：

- 本阶段不引入 LangGraph，不改记忆检索、MCP 架构或多 Agent 协议。
- 保留现有七个工具、提示词、`TripPlan` 模型、fallback 和审计格式。
- 不记录或输出 API Key；评测报告只保存模型名、provider、用量和性能数据。
- 旧 Planner 的删除和其他 Agent 的迁移属于后续独立阶段。
