# Agent-demo 异步任务模型优化方案

## 目标

将当前“用户提交后一直等待完整旅行计划生成”的同步链路，改造成真正的异步任务模型。

核心目标不是单纯让接口更快返回，而是减少用户感知等待时间，并为后续缩短真实生成耗时创造条件：

- `POST /trip/plan` 在 200～300ms 内返回 `202 Accepted + task_id`
- 后端任务状态持久化到 PostgreSQL，服务重启后不丢任务记录
- Redis + ARQ、Dramatiq 或 Celery 承担后台执行
- SSE 或 WebSocket 推送真实阶段、耗时、错误和最终结果
- 前端可离开页面，稍后从历史记录恢复任务
- 为后续缓存、并发工具调用、分阶段返回、性能统计打基础

## 当前问题

现在的旅行计划生成更接近“长请求同步等待”：

1. 前端提交请求后，要等后端完整生成计划才返回。
2. LLM 规划、工具调用、计划校验、餐饮补全等步骤都容易进入同一个等待链路。
3. 前端虽然可以显示进度，但如果进度不是后端真实阶段，就只是“假进度”。
4. 请求超时、页面刷新、网络断开后，用户难以恢复原任务。
5. 后端无法稳定统计每个阶段耗时，也不容易定位到底是 LLM 慢、地图服务慢、餐饮补全慢，还是批评修订慢。

这类问题的结果是：即使真实生成只慢在某几个步骤上，用户看到的是整个系统都“卡住了”。

## TripStar 可借鉴点与不能照搬点

TripStar 的优点是用户提交后先拿到任务 ID，再通过状态接口或 WebSocket 观察进度。这种体验比长时间等待一个 HTTP 请求更成熟。

但生产环境不建议直接照搬进程内 `asyncio.create_task`：

- 服务重启后，内存任务会丢失。
- 多实例部署时，不同实例之间无法共享任务执行状态。
- 任务失败、重试、超时、排队、取消都不好治理。
- 无法可靠做任务恢复和历史记录。

Agent-demo 更适合采用“持久化任务表 + Redis 队列 + 独立 Worker”的模型。

## 推荐架构

```text
Frontend
  |
  | POST /trip/plan
  v
FastAPI API Server
  |
  | 1. 创建 task 记录
  | 2. 投递 Redis 队列
  | 3. 立即返回 202 + task_id
  v
PostgreSQL <---- Worker 更新任务状态、阶段、耗时、结果
  ^
  |
Redis Queue / PubSub
  ^
  |
ARQ / Dramatiq / Celery Worker
  |
  | 执行：工具采集 -> LLM 生成 -> 校验 -> 补全 -> 保存结果
  v
SSE / WebSocket 推送进度到前端
```

## 为什么这能减少任务时间

它会从两个层面减少时间。

第一层是减少用户感知等待时间。用户不再卡在一个长 HTTP 请求里，而是几百毫秒内进入任务详情页，看到真实阶段进度。

第二层是减少真实执行时间。任务进入 Worker 后，可以更清晰地拆阶段、打点、并发执行、缓存复用，并把非核心步骤延后处理。

也就是说，异步任务模型本身不是魔法加速器，但它会把慢任务从一团糊状等待，拆成可观测、可并发、可缓存、可恢复的流水线。

## 技术选型建议

### 首选：ARQ

如果当前项目已经是 FastAPI + async Python，推荐优先考虑 ARQ。

优点：

- 原生 async，适合现有异步服务。
- 基于 Redis，部署成本低。
- 写法比 Celery 轻，和 FastAPI 心智更接近。
- 适合旅行计划这类 I/O 密集型任务。

缺点：

- 生态不如 Celery 大。
- 复杂工作流、任务编排、管理后台能力弱一些。

### 备选：Dramatiq

Dramatiq 比 Celery 轻，比 ARQ 更传统。

适合希望保留较简单 Worker 模型，但又不想引入 Celery 全家桶的情况。

### 备选：Celery

Celery 成熟、生态大，但对当前项目可能偏重。

只有在后续需要复杂重试策略、任务路由、定时任务、大规模 Worker 管理时，再考虑 Celery。

建议结论：先用 ARQ。够用、轻、贴合 async 代码。

## API 设计

### 创建任务

```http
POST /trip/plan
```

返回：

```json
{
  "task_id": "tp_20260713_xxxxx",
  "status": "queued",
  "status_url": "/trip/tasks/tp_20260713_xxxxx",
  "events_url": "/trip/tasks/tp_20260713_xxxxx/events",
  "result_url": "/trip/tasks/tp_20260713_xxxxx/result"
}
```

HTTP 状态码使用：

```text
202 Accepted
```

### 查询任务状态

```http
GET /trip/tasks/{task_id}
```

返回：

```json
{
  "task_id": "tp_20260713_xxxxx",
  "status": "running",
  "phase": "llm_planning",
  "progress": 55,
  "message": "正在生成每日行程",
  "started_at": "2026-07-13T10:21:30+08:00",
  "updated_at": "2026-07-13T10:22:15+08:00",
  "elapsed_ms": 45000,
  "phase_elapsed_ms": 18000
}
```

### 订阅任务事件

推荐先实现 SSE：

```http
GET /trip/tasks/{task_id}/events
```

SSE 比 WebSocket 简单，适合单向推送进度。

事件示例：

```text
event: progress
data: {"phase":"collecting_context","progress":20,"message":"正在查询景点和天气","elapsed_ms":4300}

event: progress
data: {"phase":"llm_planning","progress":55,"message":"正在生成每日行程","elapsed_ms":22000}

event: completed
data: {"phase":"completed","progress":100,"result_url":"/trip/tasks/tp_20260713_xxxxx/result"}
```

如果后续需要双向能力，例如取消任务、用户实时补充要求、协同编辑，再升级 WebSocket。

### 获取结果

```http
GET /trip/tasks/{task_id}/result
```

任务完成前返回：

```json
{
  "status": "running",
  "message": "任务仍在生成中"
}
```

任务完成后返回完整 `TripPlan`。

## 数据库设计

建议新增任务表 `trip_plan_tasks`。

字段建议：

```text
id
task_id
user_id
request_payload
status
phase
progress
message
result_plan_id
result_payload
error_code
error_message
retry_count
queued_at
started_at
finished_at
updated_at
expires_at
```

状态建议：

```text
queued
running
succeeded
failed
cancelled
expired
```

阶段建议：

```text
queued
preparing
collecting_context
llm_planning
validating
meal_enrichment
saving
completed
failed
```

## Worker 执行流程

Worker 不应该只是把原来的同步函数搬进去。建议顺便拆成可观测阶段。

```text
1. mark running
2. preparing
3. collecting_context
   - 景点
   - 天气
   - 酒店
   - 地图路线
   - 可并发执行
4. llm_planning
   - 尽量控制为 1 次主规划调用
   - 质量模式下才启用 critique/revision
5. validating
   - 本地 schema 校验
   - 本地规则校验
6. meal_enrichment
   - 可降级为后台补全
   - 不一定阻塞核心计划返回
7. saving
8. mark succeeded
```

关键点：核心计划优先返回，餐饮、图片、路线、知识图谱等增强信息可以分阶段补全。

## 进度计算原则

不要用纯前端定时器模拟进度。进度应该来自后端阶段。

建议权重：

```text
queued: 0%
preparing: 5%
collecting_context: 10% - 35%
llm_planning: 35% - 75%
validating: 75% - 85%
meal_enrichment: 85% - 95%
saving: 95% - 99%
completed: 100%
```

阶段内可以继续记录：

```text
phase_started_at
phase_elapsed_ms
tool_name
llm_round
cache_hit
```

这样前端显示的是“真实慢在哪里”，不是装饰性进度。

## 前端改造

前端提交表单后不再等待完整计划。

新流程：

```text
1. 用户提交表单
2. 调用 POST /trip/plan
3. 立即拿到 task_id
4. 跳转到 /trip/tasks/{task_id}
5. 建立 SSE 连接
6. 显示真实阶段、耗时、日志摘要
7. completed 后自动拉取 result
8. 页面刷新后用 task_id 恢复
```

需要新增：

- 任务详情页
- 任务历史列表
- 任务失败重试按钮
- 任务取消按钮，可第二阶段实现
- SSE 断线自动重连
- completed 后缓存结果到前端 store

## 减少真实任务时间的重点

异步任务模型解决“等待方式”，真正缩短任务时间还要做这些。

### 1. 限制 LLM 轮次

当前最应该控制的是 LLM 多轮规划、工具调用、批评修订。

建议：

- 默认快速模式：1 次主规划 LLM 调用
- 本地 deterministic validators 负责基础校验
- critique/revision 只在质量模式、校验失败、或用户手动要求时开启
- 限制工具调用轮次，例如默认最多 1～2 轮

### 2. 工具调用并发化

景点、天气、酒店、地图、图片等外部 I/O 应尽量并发。

建议：

- 使用 `asyncio.gather`
- 使用 semaphore 控制并发，例如 4～6
- 单工具设置 timeout
- 单工具失败允许降级，不拖死整个任务

### 3. Redis 缓存真正接入

缓存应该优先覆盖这些内容：

```text
地理编码：7～30 天
POI 搜索：6～24 小时
酒店搜索：6～24 小时
天气：10～30 分钟
路线规划：1～6 小时
图片搜索：24 小时
同请求计划草稿：15～60 分钟
```

缓存命中后，可以显著减少外部 API 时间和失败率。

### 4. 非核心增强信息后置

第一版返回可以只包含核心旅行计划：

```text
每日行程
主要景点
时间安排
预算粗估
交通建议
```

这些可以异步补全：

```text
餐厅详情
图片
更细路线
知识图谱
预订提醒
导出文件
```

这样用户更快看到结果，增强内容随后逐步出现。

### 5. 共享 HTTP Client

外部服务调用不要每次新建客户端。

建议：

- FastAPI lifespan 中创建共享 `httpx.AsyncClient`
- 设置连接池
- 设置合理 timeout
- 统一重试和熔断

## 里程碑

### 第 1 阶段：异步任务骨架

目标：先把长请求拆掉。

交付：

- `trip_plan_tasks` 表
- `POST /trip/plan` 返回 `202 + task_id`
- Worker 执行原有生成逻辑
- `GET /trip/tasks/{task_id}` 查询状态
- 前端任务页可轮询状态

验收：

- 创建任务接口 300ms 内返回
- 刷新页面后能恢复任务状态
- 后端重启后任务记录仍存在

### 第 2 阶段：真实进度推送

目标：让用户看到真实阶段。

交付：

- SSE `/trip/tasks/{task_id}/events`
- Worker 写入阶段耗时
- 前端显示阶段、耗时、错误

验收：

- 进度来自后端阶段，不再由前端假定
- 任一阶段失败能展示明确错误

### 第 3 阶段：缩短真实执行时间

目标：让任务真的更快。

交付：

- 限制默认 LLM 轮次
- 工具调用并发化
- Redis 缓存接入工具层
- 餐饮、图片、路线等增强信息后置

验收：

- p50 生成耗时明显下降
- p95 超时率下降
- 外部工具缓存命中率可见
- LLM 调用次数可统计

### 第 4 阶段：生产化治理

目标：任务可恢复、可重试、可观测。

交付：

- 任务超时控制
- 失败重试策略
- 取消任务
- 任务历史记录
- 指标面板
- Docker Compose 增加 worker 服务

验收：

- Worker 重启后不会丢任务记录
- 失败任务可追踪原因
- 能看到各阶段 p50 / p95 耗时

## Compose 改造方向

最终 Compose 不应只启动 PostgreSQL 和 Redis，还应该包含：

```text
backend
worker
frontend
postgres
redis
```

示例服务结构：

```yaml
services:
  backend:
    build: ./backend
    depends_on:
      - postgres
      - redis

  worker:
    build: ./backend
    command: arq app.worker.WorkerSettings
    depends_on:
      - postgres
      - redis

  frontend:
    build: ./frontend
    depends_on:
      - backend

  postgres:
    image: postgres:16

  redis:
    image: redis:7
```

这样本地和部署环境都能用同一套命令启动完整系统：

```bash
docker compose up --build
```

## 监控指标

为了真正减少任务时间，必须先能量化慢在哪里。

建议记录：

```text
task_total_ms
queue_wait_ms
collecting_context_ms
llm_planning_ms
validation_ms
meal_enrichment_ms
saving_ms
llm_call_count
llm_token_count
tool_call_count
tool_cache_hit_count
tool_cache_hit_rate
external_api_timeout_count
task_success_rate
task_failed_rate
task_cancelled_rate
```

前期不一定要上完整 Prometheus，可以先写数据库和结构化日志。

## 风险与处理

### 任务状态和结果不一致

处理方式：

- 任务状态更新和结果保存尽量在同一事务中完成。
- `succeeded` 状态必须保证 `result_plan_id` 或 `result_payload` 已存在。

### SSE 连接断开

处理方式：

- 前端自动重连。
- 重连后先调用状态接口补齐当前状态。
- 不依赖 SSE 保存最终状态，PostgreSQL 才是准确信息源。

### Worker 重复执行

处理方式：

- 任务执行前检查状态。
- 使用幂等 task_id。
- 保存结果时防止重复写入。

### 外部 API 慢或失败

处理方式：

- 每个工具单独 timeout。
- 设置降级结果。
- 优先使用缓存。
- 不让非核心增强信息阻塞核心计划。

## 推荐最终路线

最适合 Agent-demo 的路线是：

```text
长请求同步生成
  -> 202 + task_id
  -> PostgreSQL 任务表
  -> Redis + ARQ Worker
  -> SSE 真实进度
  -> 核心计划优先完成
  -> 增强信息后台补全
  -> 指标驱动继续压缩耗时
```

优先级最高的不是先做复杂 WebSocket，也不是马上上 Celery，而是先把“同步等待完整结果”拆成“可恢复的后台任务”。

完成这一步后，项目的速度优化会从凭感觉变成看数据：哪一阶段慢，就优化哪一阶段。
