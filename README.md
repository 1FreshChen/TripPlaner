# 智能旅行助手

一个基于 Vue 3、FastAPI、PostgreSQL/pgvector、Redis/arq、Pydantic AI 与 LangGraph 的多 Agent 旅行规划系统。

用户输入目的地、日期、偏好、预算、交通和住宿要求后，系统会并行采集景点、天气和酒店信息，由规划 Agent 生成逐日行程，补充餐饮并执行质量校验。生成过程以后台任务运行，前端可查看真实进度；完成后的计划支持手工编辑、自然语言调整、版本回退、历史记录和收藏。

## 当前状态

- 唯一编排主干是带 PostgreSQL checkpoint 的 LangGraph；任务租约、心跳、恢复和最终发布都在同一状态机中完成。
- 默认 Planner 节点为 `pydantic_ai`；也可选择 `openai_tools` 或纯本地 `deterministic`，外部能力不可用时自动降级为确定性 Planner。
- `ENABLE_EXTERNAL_SERVICES=true` 时，最终发布的每个景点必须绑定真实高德 POI；高德不可用或无法匹配时任务明确失败，不再把 LLM 景点作为结果发布。
- 无外部 API Key 时可将 `ENABLE_EXTERNAL_SERVICES=false`，景点、天气、酒店和规划流程使用本地 fallback/mock，仅用于开发和测试。
- 长期记忆使用 PostgreSQL + pgvector：行程和收藏持久化为用户隔离的向量，下一次规划按语义相关度召回；嵌入端点不可用时自动退回结构化偏好。
- 当前后端全量测试：`214 passed`（2026-09-10）。
- 当前 API 版本：`0.4.0`。

## 主要能力

- 旅行需求表单：城市、日期、偏好、预算、交通方式、住宿类型。
- 多 Agent 并行采集：景点、天气、酒店三个节点并行执行。
- 工具调用：高德 MCP/HTTP、百度地图、预算、酒店、路线、Unsplash 图片。
- 智能规划：统一 Planner 契约、受限工具调用、Pydantic AI 类型化输出和共享发布校验。
- 质量控制：Plan Critic 有限修正循环 + 确定性计划质量校验。
- 异步任务：Redis/arq Worker、PostgreSQL 任务状态、SSE 进度和轮询降级。
- 耐久工作流：LangGraph + PostgreSQL Checkpointer、租约、心跳和恢复扫描。
- 计划管理：CRUD、乐观锁、版本历史、版本回退和归档。
- 对话式调整：识别修改意图，通过自然语言更新指定计划并保存新版本。
- 记忆与个性化：短期会话、结构化长期偏好、pgvector 行程/收藏语义召回。
- 前端体验：地图、天气、预算、带图片的逐日行程、编辑、导出图片/PDF、历史和对话页。
- 安全治理：内容过滤、Prompt 注入检测、限流、请求 ID、审计和密钥加密。
- 生产化：Docker、生产 Compose、CI/CD、Caddy 示例和 PostgreSQL 备份脚本。

## 系统架构

```text
Vue 3 / Pinia
  │
  │ POST /api/trip/plan → 202 + task_id
  ▼
FastAPI ── PostgreSQL + pgvector（会话、计划、版本、任务、向量记忆、审计）
  │
  └── Redis / arq ── Worker
                       │
                       └── LangGraph（唯一主干、可恢复）
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
          景点 Agent       天气 Agent       酒店 Agent
              └───────────────┼───────────────┘
                              ▼
          PlannerBackend（Pydantic AI / OpenAI Tools / Deterministic）
                              ▼
                 Critic → 百度 HTTP 餐厅推荐
                              ▼
                         质量校验
                              ▼
                   保存计划与版本 / SSE 进度
```

## 旅行计划生成流程

1. 前端创建或恢复会话，通过 `X-Session-ID` 绑定请求。
2. `POST /api/trip/plan` 在 PostgreSQL 创建任务并返回 `202 Accepted`。
3. arq 将任务写入 Redis；Worker 获取任务并维护状态、租约和心跳。
4. 后端读取用户偏好和短期历史，并用 pgvector 召回与当前城市/偏好相关的长期记忆，准备规划上下文。
5. 景点、天气、酒店 Agent 并行采集信息，各自具有超时、重试和 fallback。
6. Planner 汇总数据生成 `TripPlan`：
   - `PLANNER_BACKEND=pydantic_ai` 使用有调用预算、工具去重和输出重试的类型化 Planner；
   - `PLANNER_BACKEND=openai_tools` 使用 OpenAI-compatible function calling；
   - `PLANNER_BACKEND=deterministic` 或外部能力不可用时使用确定性 Planner。
7. 启用 Critic 时执行有限轮次的审视与修正。
8. 定稿阶段将 Planner 工具调用补充的 POI 合并回候选集，并把每个计划景点绑定到真实高德 POI；没有候选时按景点名定向重查高德，仍无法核验则终止发布。
9. 丢弃 Planner 产生的所有餐厅内容，按每天酒店/景点位置通过百度 HTTP 搜索早、午、晚餐 POI，并通过 UID 查询详情；未找到时显示明确占位，不保留 LLM 餐厅。
10. 确定性校验检查城市、日期、天数、天气、交通、住宿和每日内容。
11. 合格计划保存为版本 1，任务标记成功；前端通过 SSE 获取进度，连接异常时自动轮询。

完成后的计划可手工编辑或通过对话修改。每次更新都使用 `expected_version` 防止并发覆盖，并保存独立版本；回退旧版本也会生成一个新版本，不覆盖历史。

## 技术栈

### 后端

- Python 3.11+
- FastAPI、Pydantic V2、SQLAlchemy Async、Alembic
- PostgreSQL 16 + pgvector、asyncpg、psycopg
- Redis、arq
- LangGraph + PostgreSQL Checkpointer
- Pydantic AI、OpenAI-compatible Chat Completions
- hello-agents / FastMCP、高德、百度地图、Unsplash

### 前端

- Vue 3、TypeScript、Vite
- Pinia、Vue Router、Ant Design Vue
- Axios、高德 JS API
- html2canvas、jsPDF

## 快速开始：Docker Compose

### 1. 准备配置

PowerShell：

```powershell
Copy-Item .env.example .env
Copy-Item .env.example backend\.env
Copy-Item frontend\.env.example frontend\.env
```

至少在根目录 `.env` 中设置非空的 Compose 密码：

```dotenv
POSTGRES_PASSWORD=replace-with-a-long-url-safe-password
REDIS_PASSWORD=replace-with-a-long-url-safe-password
```

真实规划按需在 `backend/.env` 中填写：

```dotenv
LLM_API_KEY=
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
VECTOR_MEMORY_ENABLED=true
EMBEDDING_MODEL=text-embedding-3-small
# EMBEDDING_API_KEY/EMBEDDING_BASE_URL 留空时复用 LLM_*。
EMBEDDING_API_KEY=
EMBEDDING_BASE_URL=
AMAP_API_KEY=
BAIDU_MAP_API_KEY=
UNSPLASH_ACCESS_KEY=
```

前端地图按需在 `frontend/.env` 中填写：

```dotenv
VITE_API_BASE_URL=http://localhost:8000/api
VITE_AMAP_JS_KEY=
VITE_AMAP_SECURITY_CODE=
```

不要提交任何 `.env` 文件。

### 2. 启动

```powershell
docker compose up --build
```

访问：

- 前端：<http://localhost:5173>
- API 文档：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/api/health>

停止服务：

```powershell
docker compose down
```

如需同时删除本地数据库卷，请先确认数据不再需要，再显式执行 `docker compose down -v`。

## 本地开发

本地开发仍需要可用的 PostgreSQL 和 Redis。可只用 Compose 启动基础设施，再分别启动 API、Worker 和前端。

### 后端 API

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item ..\.env.example .env
alembic upgrade head
uvicorn app.api.main:app --reload
```

### Worker

另开终端：

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
arq app.tasks.worker.WorkerSettings
```

### 前端

```powershell
cd frontend
npm install
Copy-Item .env.example .env
npm run dev
```

## 关键功能开关

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `ENABLE_EXTERNAL_SERVICES` | `true` | 是否调用真实外部服务；不可用时仍有 fallback |
| `PLANNER_BACKEND` | `pydantic_ai` | Planner 节点实现：`pydantic_ai`、`openai_tools` 或 `deterministic` |
| `PYDANTIC_AI_REQUEST_LIMIT` | `12` | 单次 Pydantic AI 运行请求上限 |
| `PYDANTIC_AI_TOOL_CALL_LIMIT` | `8` | 单次运行工具调用上限 |
| `PYDANTIC_AI_TOOL_ROUND_LIMIT` | `3` | 工具轮次上限 |
| `PYDANTIC_AI_MODEL_REQUEST_TIMEOUT_SECONDS` | `90` | 单次模型 HTTP 请求硬超时（秒） |
| `TRIP_PLANNER_ATTEMPT_TIMEOUT_SECONDS` | `150` | 单次 Planner 尝试的总超时（秒） |
| `TRIP_PLANNER_MAX_ATTEMPTS` | `2` | Planner 超时或连接失败时的最大尝试次数 |
| `PLANNER_DRAFT_TIMEOUT_SECONDS` | `90` | LangGraph 行程草案单次超时；仅草案阶段允许重试 |
| `PLANNER_DRAFT_MAX_ATTEMPTS` | `2` | 草案阶段最大尝试次数 |
| `PLANNER_CRITIQUE_TIMEOUT_SECONDS` | `35` | 质量审查超时；`0` 真正关闭内外层硬超时，有限值超时后保留有效草案 |
| `PLANNER_REFINE_TIMEOUT_SECONDS` | `55` | 单次修订超时；超时后保留有效草案 |
| `PLANNER_GLOBAL_REQUEST_LIMIT` | `4` | 草案、审查和修订共享的模型请求预算 |
| `ENABLE_PLAN_CRITIQUE` | `true` | 启用计划审视与修正 |
| `MAX_REFINEMENT_ROUNDS` | `1` | 最大修正轮次；默认只做一次有针对性的重写 |
| `MIN_PASS_SCORE` | `7.0` | Critic 最低通过分 |
| `VECTOR_MEMORY_ENABLED` | `true` | 启用 pgvector 长期语义记忆；缺少嵌入 Key 时自动退回结构化记忆 |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI-compatible 的 1536 维嵌入模型 |
| `VECTOR_MEMORY_TOP_K` | `5` | 每次规划最多召回的语义记忆数 |
| `VECTOR_MEMORY_MIN_SIMILARITY` | `0.3` | 余弦相似度最低阈值 |
| `VECTOR_MEMORY_MAX_ENTRIES_PER_USER` | `1000` | 每个用户保留的向量记忆上限 |

Pydantic AI Planner 只有在外部服务和 LLM 都可用时才会生效；否则系统自动使用确定性 Planner。

### LangGraph 工作流版本

LangGraph 是唯一任务编排主干。部署本版本前必须停止旧 Worker、备份数据库并执行 Alembic migration `004_unified_langgraph_workflow`，然后让 API 与 Worker 使用一致的版本配置：

```dotenv
LANGGRAPH_WORKFLOW_VERSION=trip_planning_v2
LANGGRAPH_STATE_SCHEMA_VERSION=2
```

`trip_planning_v2` 将 Planner 拆为 `draft_plan → critique_plan → refine_plan（按需）→ finalize_plan`。草案写入 checkpoint 后，审查或修订即使超时也会保留草案继续执行，不再把整份行程替换为确定性降级结果；最坏模型阶段预算控制在约 270 秒以内。

诊断模型自然完成耗时时，可临时将 `PYDANTIC_AI_MODEL_REQUEST_TIMEOUT_SECONDS`、`PLANNER_DRAFT_TIMEOUT_SECONDS`、`PLANNER_CRITIQUE_TIMEOUT_SECONDS` 与 `PLANNER_REFINE_TIMEOUT_SECONDS` 全部设为 `0`。`0` 表示禁用相应 Planner 阶段的硬超时，但 Worker 的 `TASK_WORKER_TIMEOUT_SECONDS` 总保护仍然生效；完成诊断后应恢复有限超时。

`LANGGRAPH_CHECKPOINT_DSN` 可留空，Worker 会从 `DATABASE_URL` 派生 psycopg DSN。Planner 后端切换不会改变工作流或 checkpoint 格式。完整升级和恢复说明见 `docs/DEPLOYMENT.md`。

### pgvector 长期记忆

Compose 使用带 `vector` 扩展的 PostgreSQL 16 镜像。迁移 `005_pgvector_long_term_memory` 创建 1536 维 `memory_entries` 和 HNSW 余弦索引。行程成功后以计划 ID 幂等写入；收藏创建/删除会同步写入/清理向量记忆。查询始终按 `user_id` 隔离，并与结构化偏好、短期会话一起送入 Planner。

嵌入调用和向量 SQL 使用独立故障边界：服务超时、Key 缺失或数据库尚未迁移时只跳过语义记忆，不影响行程发布。配置、迁移、运维和隐私说明见 `docs/VECTOR_MEMORY.md`。

## API 概览

除创建会话外，计划接口通常需要 `X-Session-ID`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| POST | `/api/sessions` | 创建会话 |
| GET | `/api/sessions/{session_id}` | 会话详情及计划摘要 |
| POST | `/api/trip/plan` | 创建异步旅行规划任务 |
| GET | `/api/trip/tasks/{task_id}` | 查询任务状态 |
| GET | `/api/trip/tasks/{task_id}/events` | 订阅 SSE 任务进度 |
| GET | `/api/trip/tasks/{task_id}/result` | 获取任务结果 |
| GET | `/api/trip/plan/{plan_id}` | 获取已完成计划 |
| PUT | `/api/trip/plan/{plan_id}` | 更新计划，要求 `expected_version` |
| GET | `/api/trip/plan/{plan_id}/versions` | 查询版本历史 |
| POST | `/api/trip/plan/{plan_id}/revert/{version}` | 回退到指定版本 |
| DELETE | `/api/trip/plan/{plan_id}` | 归档计划 |
| POST | `/api/conversation/{session_id}` | 发送消息或调整计划 |
| GET | `/api/conversation/{session_id}` | 分页读取会话消息 |
| GET / PUT | `/api/preferences` | 读取或更新用户偏好 |
| GET / POST / DELETE | `/api/saved-items` | 收藏管理 |

`POST /api/trip/plan/sync` 仅用于兼容旧同步流程，不显示在 OpenAPI 中，新客户端应使用异步任务接口。

## 项目结构

```text
Agent-demo/
├── backend/
│   ├── app/
│   │   ├── agents/          # 采集 Agent、Planner、Critic、Pydantic AI
│   │   ├── api/             # FastAPI 路由与中间件
│   │   ├── memory/          # 短期/长期记忆与召回
│   │   ├── models/          # API 与数据库模型
│   │   ├── orchestration/   # Registry、LangGraph、checkpoint、fallback
│   │   ├── services/        # 状态、任务、LLM、地图、质量校验
│   │   ├── tasks/           # arq 队列与 Worker
│   │   └── tools/           # 工具注册、执行、缓存和实现
│   ├── alembic/             # 数据库迁移
│   ├── evals/               # Planner 评测
│   └── tests/
├── frontend/
│   └── src/
│       ├── views/           # 首页、任务、结果、历史、对话
│       ├── stores/          # Pinia 状态
│       ├── services/        # API 客户端
│       └── composables/     # 地图、导出、规划逻辑
├── docs/
├── ops/
├── docker-compose.yml
└── compose.production.yml
```

## 测试与构建

后端：

```powershell
cd backend
$env:PYTHONDONTWRITEBYTECODE='1'
python -m pytest tests -q -p no:cacheprovider
```

前端：

```powershell
cd frontend
npm run build
npm run test:api-timeout
```

2026-09-10 后端全量结果：

```text
214 passed, 1 warning
```

测试 warning 来自 `hello_agents` 对 Pydantic V2 旧式 class config 的弃用提示；容器启动日志另有 FastMCP 依赖的 Authlib JOSE 接口弃用提示。

## 生产部署

生产环境使用 `compose.production.yml`、预构建镜像和 `.env.production`：

```bash
docker compose --env-file .env.production -f compose.production.yml pull
docker compose --env-file .env.production -f compose.production.yml up -d --remove-orphans --wait
```

生产基线要求：

- 使用长随机 PostgreSQL/Redis 密码和独立 Fernet `ENCRYPTION_KEY`；
- 通过反向代理或负载均衡器终止 TLS；
- 不在镜像或仓库中保存 Key；
- 发布时先执行数据库迁移；
- 确认 PostgreSQL 已启用 `vector` 扩展，并完成 migration `005_pgvector_long_term_memory`；
- 定期运行 `ops/backup-postgres.sh` 并验证恢复；
- 监控 Worker 心跳、恢复次数、任务阶段耗时和 checkpoint 表增长。

CI/CD、生产环境变量、回滚、备份和 LangGraph 灰度步骤详见 `docs/DEPLOYMENT.md`。

## 当前限制

- README 描述的是当前代码；真实地图、LLM、MCP、图片和餐饮质量仍取决于有效 Key、配额与网络。
- 高德 MCP 客户端固定为已验证的 `fastmcp==2.14.7`；MCP 进程不可用时可使用同一 Key 的高德 HTTP fallback。
- LangGraph 始终启用；Pydantic AI 是默认 Planner，但仍需在目标模型和网络环境完成效果、时延与费用评测。
- 向量记忆固定为 1536 维；更换嵌入模型时必须保证维度兼容。自建 LLM 端点不支持 embeddings 时应配置独立 `EMBEDDING_*` 或关闭向量记忆。
- 测试覆盖控制流和故障边界，但不替代真实外部 API 的延迟、费用和内容质量评测。
- 计划质量仍属于生成式问题，应持续维护固定场景数据集并跟踪路线、预算、重复率和天气适配指标。

## 相关文档

- `docs/ENV_SETUP_GUIDE.md`：外部服务和 Key 配置
- `docs/DEPLOYMENT.md`：CI/CD 与生产部署
- `docs/VECTOR_MEMORY.md`：pgvector 长期记忆、迁移与故障处理
- `docs/LangGraph_状态机集成计划.md`：耐久工作流设计
- `docs/Pydantic_AI_集成技术报告.md`：类型化 Planner 设计与验证
- `docs/async-task-optimization-plan.md`：异步任务模型
- `docs/conversation-adjustment-plan.md`：对话式计划修改

## 高德地图 QPS 治理

个人 Key 通常只有约 3 QPS 配额，而 MCP 文本搜索会为每个 POI 逐条拉取详情，多节点并行时容易触发 `CUQPS_HAS_EXCEEDED_THE_LIMIT`（错误码 10021）。代码内置了进程级限流与 QPS 错误退避重试：

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `AMAP_QPS_BUDGET` | `2.0` | 进程级每秒允许的高德调用额度；`0` 关闭限流 |
| `AMAP_QPS_RETRY_ATTEMPTS` | `2` | 遇到 QPS 限流错误时的退避重试次数 |
| `AMAP_QPS_RETRY_DELAY_SECONDS` | `1.0` | QPS 重试前的等待秒数 |
| `AMAP_ENRICH_DETAIL_LIMIT` | `3` | 每次 POI 搜索最多拉取的详情条数；`0` 禁用详情富化 |

MCP POI 详情超时或工具不可用时会改用高德 HTTP detail 补拉；仍无法得到坐标的结果不会进入生产候选集。`AMAP_ENRICH_DETAIL_LIMIT=0` 会完全禁用详情补拉。限流只在单进程内生效，多 Worker 部署时应按 Worker 数相应调低 `AMAP_QPS_BUDGET`。

高德 HTTP 对网络超时和连接中断使用同一重试预算；服务端错误会在日志和任务错误中保留 `info`、`infocode` 与来源，便于区分无效 Key、QPS 超限和网络故障。MCP Python 客户端由 `requirements.txt` 显式安装；如果出现 `No module named 'fastmcp'`，说明运行环境没有按最新版依赖重新构建。

## 景点数据来源与宽松匹配

LLM 只负责在真实候选中安排顺序、游览时长和行程描述，不再拥有新增景点事实的权限。最终景点的 `poi_id`、名称、地址、坐标、类别、评分、图片和 `data_source` 都由绑定后的高德记录覆盖，且必须满足：存在 POI ID、存在坐标、`data_source` 以 `amap` 开头。Planner 额外执行 `amap_poi_search` 得到的 POI 会被保存，并在 LangGraph 定稿节点合并到候选集，而不是只记录调用次数或再次重复查询。

名称匹配采用宽松但有边界的规则：忽略大小写、空格和符号；允许“景区”“风景区”“公园”等常见景区后缀差异；结合名称相似度和地址选择最佳候选；同一个 POI 不会分配给多个计划景点。为了避免坐标和图片串用，“明孝陵博物馆”与“明孝陵景区”这类独立场馆不会仅因共享前缀而视为同一地点。未匹配项会按原计划名称向高德定向搜索一次，仍未找到真实 POI 时抛出 `AttractionVerificationError`，不会降级为 `data_source=llm`。

## 餐饮数据来源

最终餐厅完全由百度地图 HTTP API 生成，不使用 LLM 给出的餐厅名称、地址、评分或价格。Planner 和 Critic 阶段要求 `days[].meals=[]`，也不会向模型暴露餐厅查询工具；定稿阶段固定生成早餐、午餐和晚餐三个餐位，以当天酒店、首个/中间/最后景点为中心，在 8 公里范围内分别搜索“早餐”“中餐”“特色餐厅”，周边无结果时再降级为城市范围搜索。

每个搜索最多读取 10 个候选，按评分、评论数和距离选择尚未使用的 POI，然后使用百度 UID 调用 `/place/v2/detail?scope=2` 补充详情。最终 `Meal` 保存 `poi_id`、`data_source=baidu`、地址、百度坐标，以及百度实际返回的评分、人均、营业时间和评论数；百度未返回的字段保持空值，不由模型补写。搜索不到时显示“未找到可核验餐厅”，标记 `data_source=unavailable`，同时清除所有 LLM 餐厅内容。预算中的 `estimated_cost` 在百度有 `price` 时使用该值，否则只使用固定的餐位预算估算，并重新计算总预算。

百度的官方配额表显示，Place Search Web API 的个人开发者额度为 3 QPS，企业试用为 30 QPS，企业认证为 200 QPS；实际额度还可能受账号配额包影响，应以控制台为准。项目无法从 AK 自动识别账号等级，并且历史日志曾在 3 并发下收到状态 401“超过约定并发配额”，因此默认按保守值运行：全进程 2 QPS、餐厅任务并发 2。参考：[百度地图开放平台配额说明](https://lbsyun.baidu.com/cashier/quota?from=privilege)、[并发量 FAQ](https://lbsyun.baidu.com/index.php?title=FAQ%2FConcurrent)。

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `BAIDU_QPS_BUDGET` | `2.0` | 进程级百度请求速率；`0` 关闭节流 |
| `BAIDU_MEAL_CONCURRENCY` | `2` | 餐厅搜索/详情任务并发上限 |
| `BAIDU_QPS_RETRY_ATTEMPTS` | `2` | 状态 401 配额错误或瞬时网络错误的重试次数 |
| `BAIDU_QPS_RETRY_DELAY_SECONDS` | `0.5` | 重试基础等待时间 |

所有百度搜索、详情、地理编码和路线请求共用同一个进程级节流器，避免“并发数不高但一秒内突发调用过多”。多 Worker 部署时该限制不是跨进程共享，应把单 Worker 的 `BAIDU_QPS_BUDGET` 设为总额度除以 Worker 数，或改用 Redis 分布式限流。前端仅把 `baidu` 标为“百度实查”，无结果时显示“暂无实查结果”；历史计划里原有的 `llm` 数据只标记为“历史模型建议”。

## 景点图片

高德 HTTP POI 搜索使用 `extensions=all` 请求扩展字段，并将首张 `photos[].url` 映射到景点的 `image_url`。Planner 定稿时通过上述宽松匹配复用同一高德 POI 的 ID、地址、坐标和图片，并记录 `data_source`、`image_source` 与 `coordinate_verified`；生产模式不再保留未核验的模型坐标。仍缺图且配置了 `UNSPLASH_ACCESS_KEY` 时，通过 Unsplash 并发补齐；图片缺失不会改变 POI 已核验状态。

结果页景点卡片会展示坐标和图片来源。点击地图编号标记会打开对应景点的图片、地址及来源弹窗，实现“图片跟随坐标展示”，同时避免常驻缩略图遮挡地图。图片服务不可用时显示“暂无图片”；远程图片加载失败时也会切换为占位。百度地图当前只用于餐厅和路线，不是景点图片来源。
