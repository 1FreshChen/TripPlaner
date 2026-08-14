# Pydantic AI 局部集成与 DeepSeek 工具收敛技术报告

## 1. 结论

本次工作已完成计划中的局部集成：仅为 `trip_planner` 新增一条真实的 Pydantic AI 执行路径，保留原有编排器、工具实现、`TripPlan` 公共模型、API、数据库和前端契约。

新路径针对 DeepSeek 可能持续调用工具的五类诱因，增加了分阶段工具暴露、明确的信息充分性判断、停止提示、相同参数去重、工具轮次/调用次数/模型请求次数硬限制，以及无工具 JSON 强制收尾。

最终验收结果：

- 修改前后端基线：`154 passed, 2 warnings`。
- 修改后完整后端回归：`161 passed, 1 warning`。
- Pydantic Evals：5 个收敛场景，断言通过率 100%。
- 前端：TypeScript 检查和 Vite 生产构建通过；API 超时检查通过，仍为 300000 ms。
- 真实 DeepSeek 测试：baseline 足够时 0 次业务工具调用；baseline 缺景点时 1 个工具轮后强制结束并成功输出 `TripPlan`。

新路径默认关闭，因此部署本次代码不会自动切换原有 Planner。确认环境依赖和模型兼容后，可通过 `ENABLE_PYDANTIC_AI_PLANNER=true` 灰度启用。

## 2. 回退基线

- 实施分支：`codex/pydantic-ai-planner`
- 修改前快照提交：`e901139 chore: snapshot before pydantic ai integration`
- 快照包含开始本次集成前的完整项目状态。

回退有两层：

1. 运行时立即回退：设置 `ENABLE_PYDANTIC_AI_PLANNER=false`，继续使用原 `LLMPlannerAgent`。
2. 代码级回退：回到提交 `e901139`，即可恢复本次集成前的完整状态。

## 3. 修改前的真实形式

原 `LLMPlannerAgent` 调用 `LLMService.chat_with_tools()`，该循环具有以下行为：

1. 每次模型请求都发送注册表中的全部 7 个工具。
2. 只要有工具，就始终设置 `tool_choice="auto"`。
3. Prompt 主动要求使用 2～4 组不同关键词扩展候选，并建议替换 40%～60% baseline 景点。
4. 工具返回后，没有基于 baseline 和返回数量计算“信息是否已经足够”。
5. 没有对 `工具名 + 参数` 做规范化去重。
6. 最终依赖模型自行决定停止；项目只在达到工具轮上限后另发一次无工具 JSON 请求。

旧实现已经有 `max_tool_rounds=5`，所以严格说不是代码层面的真正“无限循环”，但可能连续消耗多个工具轮，并且 Critic 修订时会重新开始一轮工具循环。不同模型的收敛能力不同，DeepSeek 更容易暴露这个设计问题。

## 4. 修改后的架构

### 4.1 双路径选择

```text
ENABLE_LLM_TOOL_PLANNING=false
  -> 原确定性/普通 LLM Planner

ENABLE_LLM_TOOL_PLANNING=true
ENABLE_PYDANTIC_AI_PLANNER=false
  -> 原 LLMPlannerAgent（行为保持不变）

ENABLE_LLM_TOOL_PLANNING=true
ENABLE_PYDANTIC_AI_PLANNER=true
  -> 新 PydanticAIPlannerAgent
```

新 Planner 仍实现原 `BaseAgent.execute(context) -> TripPlan` 契约，外层 `AgentOrchestrator`、fallback、trace 和 `TripPlannerAgent` 不需要改变调用方式。

### 4.2 类型化 Agent 和依赖

新路径使用：

- `Agent[PlannerDeps, TripPlan]`
- `PlannerDeps`：持有请求、原 `ToolExecutor` 和本轮收敛状态。
- `PromptedOutput(TripPlan)`：模型返回 JSON，由 Pydantic AI 解析为现有 `TripPlan`。
- output validator：继续调用 `validate_trip_plan_for_request()`；校验失败时用 `ModelRetry` 要求只修正 JSON，不再开放工具。
- `UsageLimits`：限制模型请求次数和工具调用次数。

`TripPlan`、`DayPlan`、`Attraction`、`WeatherInfo` 等公共 Pydantic 模型没有改字段，因此前端地图坐标、持久化和 API 响应契约不变。

### 4.3 七个类型化工具

七个 Pydantic AI 工具使用 Python 参数类型、`Literal` 和 `Field` 约束自动生成 JSON Schema，同时继续委托给原 `ToolExecutor`，保留原工具缓存、超时、重试和服务实现。

| 工具 | 类型约束示例 | 运行时暴露条件 |
| --- | --- | --- |
| `amap_poi_search` | `offset: 1..25` | baseline 景点候选不足 |
| `amap_weather` | 必填 `city` | 天气没有覆盖全部日期 |
| `hotel_search` | `limit: 1..10` | 没有酒店候选 |
| `baidu_poi_search` | 排序字段为 `Literal` | 用户明确强调美食/餐厅 |
| `baidu_direction` | mode 为 driving/walking/transit/riding | 用户明确要求路线、换乘或距离 |
| `budget_calculator` | 交通和餐饮档次为 `Literal` | 用户有精确预算、上限或省钱诉求 |
| `unsplash_image` | `count: 1..5` | 用户明确强调摄影、图片或出片 |

任一模型轮最多暴露 3 个当前阶段真正需要的工具，不再每轮发送全部 7 个。

## 5. 五类循环诱因的前后对比

| 诱因 | 修改前 | 修改后 | 直接优势 |
| --- | --- | --- | --- |
| 每轮全部 7 工具 + `auto` | 每轮固定发送 7 个，模型持续看到新的行动空间 | 运行时动态暴露 0～3 个；收尾请求不发送工具，也不发送 `tool_choice`，只启用 JSON mode | 缩小模型决策空间，停止动作更确定 |
| Prompt 鼓励 2～4 组扩展 | 即使 baseline 足够也鼓励主动扩展和替换 | 只有景点不足才扩展，最多 2 组；一次成功补齐后必须停止 | 降低无价值搜索和 token/接口消耗 |
| 没有“何时足够” | 仅模型主观判断 | 代码计算景点、天气、酒店三类缺口，并把机器判定写入 Prompt | 停止条件从概率判断变成可测试规则 |
| 没有相同参数去重 | 相同工具参数会再次访问外部服务 | 对字符串空白、大小写、关键词顺序和分隔符规范化，生成 `工具名 + JSON 参数` 签名；重复调用返回已有结果摘要，不执行外部服务 | 避免完全相同或等价参数重复消费 |
| 模型是否提交结果是概率性的 | 主要依赖模型自行停止，最后才由旧循环兜底 | 同时限制请求数、工具调用数和工具轮数；达到上限后下一轮强制隐藏业务工具并要求 JSON | 即使模型不收敛，代码也会终止工具阶段 |

默认限制：

- `PYDANTIC_AI_REQUEST_LIMIT=12`
- `PYDANTIC_AI_TOOL_CALL_LIMIT=8`
- `PYDANTIC_AI_TOOL_ROUND_LIMIT=3`

景点关键词另外限制为最多 2 组，因此即使工具轮限制配置较大，也不会无限换近义词搜索景点。

## 6. DeepSeek 专项兼容

### 6.1 `reasoning_content` 回放

DeepSeek 思考模式的工具轮需要在后续请求中回放先前 assistant 消息的 `reasoning_content`。新 HTTP Model 适配器将其转换为 Pydantic AI `ThinkingPart` 保存，并在下一轮恢复为 DeepSeek 所需字段。

离线测试验证了：

- 第一次响应中的 `reasoning_content` 被解析为 `ThinkingPart`。
- 下一次请求的对应 assistant 消息仍包含原 `reasoning_content`。
- 达到工具轮上限后，请求不再携带业务工具，并启用 `response_format={"type":"json_object"}`。

### 6.2 工具消息必须连续

真实 DeepSeek 测试最初发现一个 `400 Bad Request`：第二轮重复的 system instructions 被放在 assistant tool_calls 和 tool 结果之间。DeepSeek 要求所有对应 tool 消息紧跟 assistant tool_calls。

修复后：

- 所有 system instructions 去重后统一放到消息序列最前面。
- assistant tool_calls 后连续发送全部对应 tool 消息。
- 再添加强制结束的 user 消息。

该问题已增加离线回归断言，并通过真实 DeepSeek 复测。

## 7. 依赖兼容决策

原计划拟使用 `pydantic-ai-slim[openai]==2.30.0`。实际安装探针发现：

- Pydantic AI 2.30 的 OpenAI extra 要求较新的 OpenAI SDK（安装时解析为 3.0.0）。
- 项目现有 `hello-agents==0.2.9` 明确要求 `openai<2.0.0`。

直接安装 `[openai]` 会破坏现有依赖，因此最终使用：

- `pydantic-ai-slim==2.30.0`
- `pydantic-evals==2.30.0`
- 保持 `hello-agents==0.2.9` 和 `openai 1.x` 兼容关系
- 新增 `OpenAICompatiblePydanticModel`，通过项目原有 HTTP 方式请求 `/chat/completions`

这不是模拟 Pydantic AI：Agent 循环、RunContext、类型化 Tool、动态 Tool prepare、PromptedOutput、ModelRetry、消息对象、UsageLimits 和 eval 都由 Pydantic AI 真实执行；只替换了与 OpenAI-compatible 服务通信的 provider transport。

## 8. 审计与原行为兼容

新路径继续写入原有 context 键：

- `trip_planner_tool_calls`
- `trip_planner_token_usage`
- `plan_critique_events`
- `event_log`

工具审计项继续包含：

```json
{
  "tool": "amap_poi_search",
  "arguments": {"keywords": "博物馆", "city": "杭州", "offset": 10},
  "id": "call_xxx"
}
```

重复或失败时增加 `duplicate` / `error` 字段，但不删除旧字段。

新增 `planner_convergence` 事件记录：

- 模型请求数
- 工具轮数
- 模型请求的工具调用数
- 实际唯一外部调用数
- 被抑制的重复调用数
- 是否进入强制结束

Critic 仍使用原 `PlanCritic`。需要修订时，新 Planner 会关闭工具，仅基于原请求、上一版计划和 Critic 意见修正结构化结果，避免 Critic 再触发一轮搜索。

## 9. 测试结果

### 9.1 修改前基线

```text
154 passed, 2 warnings in 162.16s
```

最初环境缺少 requirements 中已有的 `hello-agents`，恢复锁定版本后得到上述有效基线；这不是本次代码缺陷。

### 9.2 修改后完整回归

```text
161 passed, 1 warning in 162.73s
```

剩余警告来自 `hello-agents 0.2.9` 内部仍使用 Pydantic v1 风格 class Config，与本次改动无关。

新增测试覆盖：

- 功能开关选择新旧 Planner。
- 七个工具自动生成的类型化 JSON Schema。
- baseline 足够时不暴露工具。
- 数据缺口时只暴露当前阶段工具。
- 等价关键词参数去重，不重复执行外部服务。
- DeepSeek `reasoning_content` 保存和回放。
- assistant tool_calls 与 tool 消息连续配对。
- 工具轮上限后无工具 JSON 强制收尾。
- Pydantic AI 离线结构化输出和原 context 审计契约。
- Pydantic Evals 五场景数据集。

### 9.3 Pydantic Evals

```text
5 cases, 100.0% assertions passed
```

场景包括：

1. baseline 足够时立即停止。
2. 三类数据都缺失时只显示三个采集工具。
3. 明确美食偏好时只显示餐厅工具。
4. 明确路线和摄影诉求时只显示两个对应工具。
5. 硬停止状态下即使数据缺失也不再显示工具。

### 9.4 真实 DeepSeek 冒烟测试

使用项目现有 DeepSeek 配置测试，未输出密钥或完整行程。

场景 A：1 日杭州，baseline 景点/天气/酒店都足够。

- 成功返回合法 `TripPlan`。
- 业务工具调用：0。
- 总 token：13705。

场景 B：1 日杭州，景点 baseline 为空，本地假工具返回候选，工具轮限制为 1。

- 模型在第一轮选择两组互补关键词。
- 外部执行：2 次；都发生在同一个工具轮。
- 模型请求：2 次。
- 工具轮：1。
- `forced_finalize=true`。
- 成功返回合法 `TripPlan`。
- 总 token：19569。

该测试证明真实 DeepSeek 在一次工具轮后进入无工具 JSON 收尾，没有继续循环调用工具。

### 9.5 前端和工程检查

- `npm run build`：通过，5049 个模块完成生产构建。
- `npm run test:api-timeout`：通过，仍为 300000 ms。
- `python -m compileall app evals tests -q`：通过。
- `git diff --check`：通过，仅有 Windows 下未来 LF/CRLF 转换提示，无空白错误。

## 10. 文件变更

核心新增：

- `backend/app/agents/pydantic_planner.py`：类型化 Planner、七工具、动态暴露、充分性状态、去重、Critic 修订。
- `backend/app/services/pydantic_ai_model.py`：OpenAI-compatible HTTP Model、DeepSeek 思考回放、消息序列和强制收尾。
- `backend/evals/pydantic_planner_eval.py`：五场景离线 eval 数据集。
- `backend/tests/test_pydantic_planner.py`：类型、工具策略、DeepSeek 协议和离线端到端测试。
- `backend/tests/test_pydantic_planner_eval.py`：eval 数据集回归测试。

兼容性修改：

- `backend/app/orchestration/bootstrap.py`：增加 feature flag 路由。
- `backend/app/config.py`：增加开关和三类限制。
- `backend/app/agents/trip_planner.py`：新路径使用收敛版 Prompt；旧路径保留原 Prompt。
- `backend/requirements.txt`：锁定 Pydantic AI/Evals 2.30.0。
- `.env.example`、`.env.production.example`：增加新配置，默认关闭。
- `docs/Pydantic_AI_局部集成计划.md`：记录 OpenAI SDK 冲突后的兼容实现决策。

## 11. 启用方式

先安装更新后的后端依赖，然后在测试或灰度环境配置：

```env
ENABLE_EXTERNAL_SERVICES=true
ENABLE_LLM_TOOL_PLANNING=true
ENABLE_PYDANTIC_AI_PLANNER=true
PYDANTIC_AI_REQUEST_LIMIT=12
PYDANTIC_AI_TOOL_CALL_LIMIT=8
PYDANTIC_AI_TOOL_ROUND_LIMIT=3
```

建议先观察 `planner_convergence` 事件和 `trip_planner_tool_calls`：

- baseline 足够的请求应大多数为 0 次工具调用。
- 景点不足的请求通常应在 1～2 组关键词内结束。
- `forced_finalize` 可以为 true；它表示代码明确关闭了工具阶段，不代表执行失败。
- 如果出现重复参数，审计中应有 `duplicate=true`，实际唯一外部调用数不增加。

## 12. 优势与当前限制

优势：

- 工具是否可见由代码规则决定，不再完全交给模型概率判断。
- 输出直接验证为现有 `TripPlan`，格式错误和业务质量错误可以区分处理。
- 相同/等价参数不会重复访问外部服务。
- DeepSeek 思考工具轮消息可正确回放。
- Critic 修订不再重新开放搜索工具。
- 新旧路径可运行时切换，回退成本低。
- 收敛行为有独立 eval 和审计指标，可以持续迭代。

当前限制：

- 新路径默认关闭；只有启用 `ENABLE_PYDANTIC_AI_PLANNER=true` 后才使用本报告中的收敛策略。旧 Planner 为保证兼容仍保持原行为。
- 完整 Planner Prompt 加 `TripPlan` JSON Schema 较长，真实冒烟测试 token 数较高。后续可在不改变公共模型的前提下压缩重复示例和输出说明。
- 工具充分性目前主要按候选数量、天气日期覆盖和酒店是否存在判断，尚未对 POI 质量做复杂评分。
- 真实工具轮冒烟测试使用本地假地图结果，避免测试触发真实地图费用；原工具执行器和各地图工具仍由现有单元测试覆盖。
- Pydantic AI 仅接入 trip planner；会话修改等其他 `LLMService.chat_with_tools()` 调用未迁移，不受本次改动影响。

综合判断：本次实现已经把 DeepSeek 工具循环问题从“依赖模型自行收敛”改为“代码控制可见工具、去重、预算和最终收尾”，同时通过默认关闭的新路径保持原项目功能不变，适合进入灰度启用阶段。
