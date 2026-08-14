# 安全与稳定性问题修复计划

> 审查日期：2026-07-16
> 原则：保持代码风格一致、功能行为一致、不破坏现有架构

---

## 概览

| 编号 | 级别 | 问题 | 影响 | 修复复杂度 |
|------|------|------|------|------------|
| P0-1 | P0 | 对话修改失败后仍可能提交部分数据 | 数据不一致 | 低 |
| P1-1 | P1 | 行程和任务接口存在 BOLA/IDOR 越权 | 越权访问 | 中 |
| P1-2 | P1 | 并发编辑会静默覆盖数据 | 数据丢失 | 中 |
| P1-3 | P1 | 异步任务可能永久停留在 queued/running | 任务僵死 | 中 |
| P1-4 | P1 | 高德 MCP 调用毒化整个进程 | 服务不可用 | 高 |
| P1-5 | P1 | 外部服务失败被伪装为成功 | 静默数据降级 | 中 |
| P1-6 | P1 | LLM 返回畸形工具参数造成 HTTP 500 | 接口崩溃 | 低 |
| P1-7 | P1 | Docker 部署数据服务暴露 | 数据泄露 | 低 |

---

## P0-1: 对话修改失败后仍可能提交部分数据

### 根因

`state_service.py` 的 `_modify_plan_via_conversation` 方法（行 756-764）采用**增量赋值**方式修改 SQLAlchemy 追踪对象。当中间步骤（如 `date.fromisoformat`）抛出异常时，已修改的字段（`version`, `status`, `city`）保持 dirty 状态。上层异常处理（行 501）没有执行 `rollback` 或 `refresh`，导致请求结束时的自动 flush/commit 将半修改状态持久化。

### 修复方案

在 `_modify_plan_via_conversation` 中，先完成所有校验和解析，确认全部合法后再一次性赋值。

**文件**: `backend/app/services/state_service.py`

**修改点 1** (行 743-788): 将 `_modify_plan_via_conversation` 中的增量赋值改为"先解析校验，后统一赋值"：

```python
# 替换行 743-788 的 try/except 块及后续赋值逻辑
# 当前代码模式（伪代码）：
#   try:
#       modified_plan = self._load_conversation_modified_plan(text)  # 可能抛异常
#   except Exception: ...
#   if modified_plan is None: ...
#   referenced_plan.version += 1          # ← 先改
#   referenced_plan.status = "completed"  # ← 先改
#   referenced_plan.city = modified_plan.city  # ← 先改
#   referenced_plan.start_date = date.fromisoformat(...)  # ← 这里才可能抛异常！
#   ...

# 修改为：在赋值给 referenced_plan 之前，先完成所有解析
try:
    modified_plan = self._load_conversation_modified_plan(text)
except Exception as exc:
    last_error = str(exc)
    continue

if modified_plan is None:
    last_error = "修改意图已确认，不能返回 no_change"
    continue
if modified_plan.model_dump(mode="json") == current_plan.model_dump(mode="json"):
    last_error = "修改后的 TripPlan 与原计划完全相同，用户要求尚未落实"
    continue

# ✅ 先完成所有可能失败的解析
try:
    new_start_date = date.fromisoformat(modified_plan.start_date)
    new_end_date = date.fromisoformat(modified_plan.end_date)
except (ValueError, TypeError) as exc:
    last_error = f"日期解析失败: {exc}"
    continue

# ✅ 全部解析成功后，一次性赋值
referenced_plan.version += 1
referenced_plan.status = "completed"
referenced_plan.city = modified_plan.city
referenced_plan.start_date = new_start_date
referenced_plan.end_date = new_end_date
referenced_plan.days_count = len(modified_plan.days)
referenced_plan.plan_json = modified_plan.model_dump()
referenced_plan.overall_suggestions = modified_plan.overall_suggestions
referenced_plan.budget_summary = modified_plan.budget.model_dump() if modified_plan.budget else None
```

**修改点 2** (行 501-518): 在异常处理中增加防御性 rollback：

```python
# 在 except Exception as exc: 块的开头增加：
except Exception as exc:
    plan_update_failed = True
    await self._db.rollback()  # ✅ 新增：回滚任何可能的 dirty 状态
    logger.warning("Conversation plan modification failed: %s", exc, exc_info=True)
    # ... 其余代码不变
```

### 验证方式

1. 构造一个 LLM 返回的 TripPlan JSON，其中 `start_date` 为非法值（如 `"2026-13-45"`）
2. 调用 conversation API 触发修改
3. 检查数据库：`version`、`status`、`city` 不应变化
4. 检查 API 响应：应提示"行程未变化"

---

## P1-1: 行程和任务接口存在 BOLA/IDOR 越权

### 根因

Trip 路由层和 Service 层均未校验请求者是否为资源的合法拥有者：
- `_get_plan()` 仅按 `plan_id` 查询，不检查 session/user 归属
- `_get_task()` 仅按 `task_id` 查询，不检查 user 归属
- 对比：`preferences.py` 和 `saved_items.py` 正确调用了 `_get_user_by_session` 进行归属校验

### 修复方案

建立统一的资源归属校验机制，在 Service 层为所有资源访问增加可选的 `session_id` 参数校验。

**文件**: `backend/app/services/state_service.py`

**修改点 1**: 扩展现有方法签名，增加 `session_id` 参数用于归属校验：

```python
# _get_plan 增加 session_id 可选参数
async def _get_plan(self, plan_id: str, session_id: str | None = None) -> TripPlanModel:
    plan_uuid = _parse_uuid(plan_id, "plan_id")
    plan = await self._db.scalar(select(TripPlanModel).where(TripPlanModel.id == plan_uuid))
    if plan is None or plan.status == "archived":
        raise HTTPException(status_code=404, detail="计划不存在")
    # ✅ 新增：如果提供了 session_id，校验归属
    if session_id is not None:
        session_uuid = _parse_uuid(session_id, "session_id")
        user = await self._get_or_create_user_by_session(str(session_uuid))
        if plan.user_id is not None and plan.user_id != user.id:
            raise HTTPException(status_code=404, detail="计划不存在")
    return plan
```

**修改点 2**: 为所有对外暴露的 trip plan 操作增加 session_id 参数：

- `get_trip_plan(plan_id, session_id=None)` → 调用 `_get_plan(plan_id, session_id)`
- `update_trip_plan(plan_id, update, session_id=None)` → 同上
- `list_plan_versions(plan_id, session_id=None)` → 同上
- `revert_plan(plan_id, version, session_id=None)` → 同上
- `archive_plan(plan_id, session_id=None)` → 同上

**文件**: `backend/app/services/task_service.py`

**修改点 3**: `_get_task` 增加可选的 session/user 校验：

```python
async def _get_task(self, task_id: str, session_id: str | None = None) -> TripPlanTask:
    task = await self._db.scalar(select(TripPlanTask).where(TripPlanTask.task_id == task_id))
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    # ✅ 新增归属校验
    if session_id is not None and task.user_id is not None:
        user = await self._db.scalar(
            select(User).where(User.session_token == session_id)
        )
        if user is None or task.user_id != user.id:
            raise HTTPException(status_code=404, detail="任务不存在")
    return task
```

**文件**: `backend/app/api/routes/trip.py`

**修改点 4**: 路由层增加 `session_id` 查询参数并传递到 service 层：

在各路由函数签名中增加 `session_id: str = Query(...)` 参数，并透传到 service 方法。对于已有 session 上下文的接口（如 create_trip_plan 通过 request.session_id），使用请求体中的 session_id 进行校验。

```python
# 示例：GET /plan/{plan_id}
@router.get("/plan/{plan_id}", response_model=TripPlanResponse)
async def get_trip_plan(
    request: Request,
    plan_id: str,
    session_id: str = Query(..., description="会话令牌"),
    state: StateService = Depends(get_state_service),
) -> TripPlanResponse:
    return await state.get_trip_plan(plan_id, session_id=session_id)
```

### 验证方式

1. 用户 A 创建一个 trip plan，获得 plan_id
2. 用户 B 使用自己的 session_id 尝试访问用户 A 的 plan_id
3. 应返回 404（而非 200 数据泄露）

---

## P1-2: 并发编辑会静默覆盖数据

### 根因

`TripPlanUpdateRequest` 没有 `expected_version` 字段，`update_trip_plan` 直接覆盖并递增版本号，无乐观锁保护。

### 修复方案

采用乐观锁（optimistic locking）：前端提交时带上当前版本号，后端在更新时校验版本是否匹配。

**文件**: `backend/app/models/schemas.py`

**修改点 1** (行 261-265): 在 `TripPlanUpdateRequest` 中增加 `expected_version` 字段：

```python
class TripPlanUpdateRequest(BaseModel):
    """旅行计划编辑请求"""

    plan_json: TripPlan = Field(..., description="修改后的完整旅行计划")
    expected_version: int = Field(..., ge=1, description="客户端当前持有的版本号，用于冲突检测")
    change_summary: str = Field(default="手动编辑", description="修改摘要")

    @field_validator("change_summary", mode="before")
    @classmethod
    def sanitize_change_summary(cls, value: str) -> str:
        return _sanitize_and_check_text(value)
```

**文件**: `backend/app/services/state_service.py`

**修改点 2** (行 281-291): `update_trip_plan` 增加版本冲突检测：

```python
async def update_trip_plan(self, plan_id: str, update: TripPlanUpdateRequest, session_id: str | None = None) -> TripPlanResponse:
    plan = await self._get_plan(plan_id, session_id)

    # ✅ 乐观锁：版本冲突检测
    if update.expected_version != plan.version:
        raise HTTPException(
            status_code=409,
            detail=f"版本冲突：您的修改基于版本 {update.expected_version}，"
                   f"但当前已是版本 {plan.version}。请刷新后重新编辑。",
        )

    plan.version += 1
    # ... 其余赋值逻辑不变
```

### 验证方式

1. 页面 A 和页面 B 同时加载 plan（version=1）
2. 页面 A 先提交修改 → 成功（version→2）
3. 页面 B 后提交修改（expected_version=1）→ 应返回 409 冲突错误
4. 页面 B 刷新后重新编辑 → 成功

---

## P1-3: 异步任务可能永久停留在 queued/running

### 根因

1. **expires_at 只写不读**：任务创建时设置了 `expires_at`，但没有后台调度器将过期任务标记为 `expired`
2. **双写窗口**：DB 提交成功 + Redis 入队前进程退出 → 任务在 DB 中 `queued` 但 Redis 中不存在
3. **CancelledError 处理**：arq Worker 超时/关闭时可能无法正确调用 `fail_task`

### 修复方案

**文件**: `backend/app/services/task_service.py`

**修改点 1**: 新增过期任务清理函数：

```python
async def cleanup_expired_tasks(
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
) -> int:
    """将超过 expires_at 的非终态任务标记为 expired，返回清理数量。"""
    now = now or utc_now()
    cleaned = 0
    async with session_factory() as db:
        expired = await db.scalars(
            select(TripPlanTask)
            .where(
                TripPlanTask.expires_at.isnot(None),
                TripPlanTask.expires_at < now,
                TripPlanTask.status.notin_(TERMINAL_TASK_STATUSES),
            )
        )
        for task in expired:
            transition_task(
                task,
                status="expired",
                phase="expired",
                progress=task.progress or 0,
                message="任务已过期",
                now=now,
            )
            task.finished_at = now
            task.error_code = "TASK_EXPIRED"
            cleaned += 1
        if cleaned:
            await db.commit()
    return cleaned
```

**修改点 2**: 新增孤儿任务恢复函数，处理双写窗口中丢失的任务：

```python
async def recover_stale_queued_tasks(
    session_factory: async_sessionmaker[AsyncSession],
    stale_threshold_seconds: int = 120,
) -> int:
    """将超过阈值仍未开始执行的 queued 任务标记为 failed。"""
    now = utc_now()
    threshold = now - timedelta(seconds=stale_threshold_seconds)
    cleaned = 0
    async with session_factory() as db:
        stale = await db.scalars(
            select(TripPlanTask)
            .where(
                TripPlanTask.status == "queued",
                TripPlanTask.queued_at < threshold,
            )
        )
        for task in stale:
            transition_task(
                task,
                status="failed",
                phase="failed",
                progress=task.progress or 0,
                message="任务入队失败，队列未收到请求",
                now=now,
            )
            task.finished_at = now
            task.error_code = "ENQUEUE_LOST"
            cleaned += 1
        if cleaned:
            await db.commit()
    return cleaned
```

**文件**: `backend/app/tasks/worker.py`

**修改点 3** (行 55-58): 确保 CancelledError 也被正确处理：

```python
except asyncio.CancelledError:
    # ✅ 新增：专门处理取消信号
    await db.rollback()
    try:
        await fail_task(session_factory, task_id, RuntimeError("Worker shutting down"))
    except Exception:
        pass
    raise
except Exception as exc:
    await db.rollback()
    await fail_task(session_factory, task_id, exc)
    raise
```

**修改点 4**: 添加一个轻量级定期清理任务（可通过 arq cron 或 FastAPI startup 事件注册）：

```python
# 在 app/api/main.py 的 lifespan 中注册一个后台 asyncio task，
# 每 60 秒执行一次 cleanup_expired_tasks + recover_stale_queued_tasks
# 或使用 arq 的 cron 功能
```

### 验证方式

1. 创建一个超过 `expires_at` 的测试任务，手动将 `queued_at` 设为 5 分钟前
2. 运行 `cleanup_expired_tasks` → 任务应被标记为 `expired`
3. 模拟双写窗口：直接在 DB 中插入一条 `queued` 状态但 Redis 中无对应 job 的记录
4. 运行 `recover_stale_queued_tasks` → 任务应被标记为 `failed`（错误码 `ENQUEUE_LOST`）

---

## P1-4: 高德 MCP 调用毒化整个进程

### 根因

`_PersistentMCPConnection.call_tool()` 在超时时只取消了 Python `Future`，没有取消底层 `client.call_tool()` 协程。单消费者循环 `_serve()` 因此被阻塞，且进程级单例不会自动重建。

### 修复方案

在超时发生后，主动取消底层协程并重建连接。

**文件**: `backend/app/services/amap_mcp_service.py`

**修改点 1** (类 `_PersistentMCPConnection`): 增加超时追踪与协程取消能力：

```python
class _PersistentMCPConnection:
    def __init__(self, ...):
        # ... 现有初始化 ...
        self._active_call: Optional[asyncio.Task] = None  # ✅ 追踪当前活跃的调用

    # ✅ 新增：带超时取消的 call_tool 方法
    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        self.start()
        if self._loop is None or self._queue is None or self._stopped.is_set():
            raise RuntimeError("AMap MCP server is not available")

        future: Future = Future()
        request = _MCPRequest(tool_name=tool_name, arguments=arguments, future=future)
        self._loop.call_soon_threadsafe(self._queue.put_nowait, request)
        try:
            return future.result(timeout=self._call_timeout)
        except FutureTimeoutError:
            future.cancel()
            # ✅ 新增：取消底层协程，防止毒化消费者循环
            self._loop.call_soon_threadsafe(self._cancel_active_call)
            raise TimeoutError(f"AMap MCP tool '{tool_name}' timed out") from None

    def _cancel_active_call(self):
        """在 event loop 线程上安全取消活跃的 client.call_tool() 协程。"""
        if self._active_call is not None and not self._active_call.done():
            self._active_call.cancel()
        # ✅ 注入哨兵值让消费者跳过被取消的请求
        if self._queue is not None:
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
```

**修改点 2** (行 171-193, `_serve` 方法): 追踪活跃调用并处理哨兵：

```python
async def _serve(self) -> None:
    self._loop = asyncio.get_running_loop()
    self._task = asyncio.current_task()
    client_context = self._create_client()
    async with client_context as client:
        self._tools = await client.list_tools()
        self._queue = asyncio.Queue()
        self._ready.set()

        while True:
            request = await self._queue.get()
            if request is None:
                # ✅ 哨兵值：跳过被取消的请求后继续服务
                # 重新创建 client 以清理可能的状态污染
                break  # 退出循环，触发外层重建
            if request.future.cancelled():
                continue
            try:
                # ✅ 追踪活跃调用
                self._active_call = asyncio.current_task()
                result = await client.call_tool(request.tool_name, request.arguments)
            except asyncio.CancelledError:
                # ✅ 被取消时不设置异常（future 已被 cancel）
                if not request.future.cancelled():
                    request.future.set_exception(
                        RuntimeError("Tool call was cancelled")
                    )
                # 不 break，继续处理下一个请求
            except Exception as exc:
                if not request.future.cancelled():
                    request.future.set_exception(exc)
            else:
                if not request.future.cancelled():
                    request.future.set_result(result)
            finally:
                self._active_call = None

    # ✅ 循环退出后自动重启
    if not self._closing.is_set():
        logger.warning("AMap MCP serve loop exited, will restart on next call")
        self._stopped.set()
```

**文件**: `backend/app/services/amap_mcp_service.py`

**修改点 3**: 进程级单例 `get_amap_mcp_service()` 增加健康检查和自动重建：

在现有的单例获取函数中增加对 `_stopped` 状态的检查，如果连接已停止，自动创建新实例。

### 验证方式

1. 发送一个会超时的 MCP 工具调用
2. 超时后立即发送另一个正常的 MCP 工具调用
3. 第二个调用应能正常执行（而非永久阻塞）

---

## P1-5: 外部服务失败被伪装为成功

### 根因

`AmapService` 的所有方法在遇到任何异常时返回空列表/空结果，调用方无法区分"API 不可用"和"无匹配结果"。TripPlannerAgent 在收到空结果时会生成 mock 数据，但 Trace 中不体现这一点。

### 修复方案

引入结构化结果类型，保留异常信息供上层决策。

**文件**: `backend/app/services/amap_service.py`

**修改点**: 修改 `search_pois` 和 `get_weather`，在返回空结果时区分不同的失败原因：

```python
from dataclasses import dataclass, field

@dataclass
class ServiceResult:
    """结构化外部服务调用结果"""
    data: list = field(default_factory=list)
    error: str | None = None
    from_cache: bool = False

    @property
    def is_error(self) -> bool:
        return self.error is not None

    @property
    def is_empty(self) -> bool:
        return len(self.data) == 0 and self.error is None


class AmapService:
    # ... 现有代码 ...

    def search_pois(self, keywords: str, city: str, offset: int = 10) -> ServiceResult:
        if not self.enabled:
            return ServiceResult(error="AMap API key not configured")
        try:
            response = requests.get(
                f"{self.base_url}/place/text",
                params={
                    "keywords": keywords,
                    "city": city,
                    "key": self.api_key,
                    "output": "json",
                    "offset": offset,
                    "page": 1,
                },
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "1":
                error_msg = payload.get("info", "Unknown error")
                logger.warning("Amap POI search failed: %s", payload)
                return ServiceResult(error=f"AMap API error: {error_msg}")
            return ServiceResult(data=payload.get("pois", []))
        except requests.Timeout:
            logger.warning("Amap POI request timed out")
            return ServiceResult(error="AMap API timeout")
        except requests.ConnectionError:
            logger.warning("Amap POI connection failed")
            return ServiceResult(error="AMap API connection failed")
        except Exception as exc:
            logger.warning("Amap POI request failed: %s", exc)
            return ServiceResult(error=f"AMap API unexpected error: {exc}")

    def get_weather(self, city: str) -> ServiceResult:
        # ... 同理改造，返回 ServiceResult ...
```

**文件**: 所有调用 `AmapService.search_pois` / `get_weather` 的地方

**修改点**: 调用方检查 `ServiceResult.is_error`，将错误信息传递到 Trace/日志中，确保 Agent 在生成 mock 数据时能标注数据来源为模拟。

> 注意：需要同步检查 `TripPlannerAgent` 和 `ToolExecutor` 中对 AmapService 的调用点，确保兼容 `ServiceResult` 类型。

### 验证方式

1. 断开网络或使用无效 API Key
2. 发起行程规划请求
3. 检查 Trace/响应：应明确标注哪些数据来自 mock/fallback，而非静默返回模拟数据

---

## P1-6: LLM 返回畸形工具参数造成 HTTP 500

### 根因

`llm_service.py` 行 142 的 `json.loads(arguments)` 在 `arguments` 为畸形 JSON 时会抛出 `json.JSONDecodeError`，但该异常未被 `chat_with_tools` 方法内任何 try/except 捕获。

### 修复方案

为 JSON 解析增加防御性异常处理。

**文件**: `backend/app/services/llm_service.py`

**修改点** (行 140-150): 将 `json.loads` 放入 try/except，失败时记录日志并使用空参数继续：

```python
parsed_calls: List[Tuple[Dict[str, Any], str, Dict[str, Any]]] = []
for tool_call in tool_calls:
    tool_name = tool_call.get("function", {}).get("name", "")
    raw_arguments = tool_call.get("function", {}).get("arguments") or "{}"
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError:
        logger.warning(
            "LLM returned malformed tool arguments for '%s': %s",
            tool_name, raw_arguments[:200]
        )
        arguments = {}  # ✅ 降级为空参数，避免 500
        # 同时记录到 tool_calls_log 供排查
    tool_calls_log.append(
        {
            "tool": tool_name,
            "arguments": arguments,
            "id": tool_call.get("id"),
        }
    )
    parsed_calls.append((tool_call, tool_name, arguments))
```

### 验证方式

1. Mock LLM 响应，将 `arguments` 设为非法 JSON 字符串（如 `"{bad json"`）
2. 调用 conversation API
3. API 不应返回 500；应正常返回并记录 warning 日志

---

## P1-7: Docker 部署数据服务暴露

### 根因

`docker-compose.yml` 将 PostgreSQL 和 Redis 端口直接映射到宿主机 `0.0.0.0`，且使用弱密码/无密码。

### 修复方案

移除不必要的宿主机端口映射，为 Redis 增加密码认证，PostgreSQL 使用环境变量注入密码。

**文件**: `docker-compose.yml`

**修改点 1** (行 39-53): PostgreSQL 服务仅对内暴露：

```yaml
postgres:
  image: postgres:16
  environment:
    POSTGRES_USER: ${POSTGRES_USER:-postgres}
    POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    POSTGRES_DB: ${POSTGRES_DB:-trip_planner}
  # ✅ 移除 ports 映射（或仅在需要本地调试时绑定 127.0.0.1）
  # ports:
  #   - "127.0.0.1:5432:5432"  # 仅本地调试时取消注释
  volumes:
    - pgdata:/var/lib/postgresql/data
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-postgres} -d ${POSTGRES_DB:-trip_planner}"]
    interval: 5s
    timeout: 5s
    retries: 10
```

**修改点 2** (行 55-63): Redis 增加密码认证并限制端口暴露：

```yaml
redis:
  image: redis:7-alpine
  command: redis-server --requirepass ${REDIS_PASSWORD}  # ✅ 增加密码
  # ✅ 移除 ports 映射（或仅在需要本地调试时绑定 127.0.0.1）
  # ports:
  #   - "127.0.0.1:6379:6379"  # 仅本地调试时取消注释
  healthcheck:
    test: ["CMD", "redis-cli", "-a", "${REDIS_PASSWORD}", "ping"]
    interval: 5s
    timeout: 3s
    retries: 10
```

**修改点 3** (行 9): 更新 backend 的 Redis 连接 URL：

```yaml
REDIS_URL: redis://:${REDIS_PASSWORD}@redis:6379/0
```

**修改点 4**: 确保 `.env.example` 文件包含必要的变量说明：

```env
# 数据库
POSTGRES_USER=postgres
POSTGRES_PASSWORD=<generate-a-strong-password>
POSTGRES_DB=trip_planner

# Redis
REDIS_PASSWORD=<generate-a-strong-password>
```

### 验证方式

1. `docker compose up -d` 启动服务
2. 从外部机器尝试连接 `{host_ip}:5432` 和 `{host_ip}:6379` → 应拒绝连接
3. 内部服务间（backend→postgres, backend→redis）通信正常

---

## 实施顺序建议

| 优先级 | 编号 | 任务 | 理由 |
|--------|------|------|------|
| 1 | P0-1 | 对话修改回滚 | 阻断上线，数据完整性 |
| 2 | P1-1 | BOLA/IDOR 越权 | 安全漏洞，影响所有多用户场景 |
| 3 | P1-7 | Docker 端口暴露 | 部署安全，实施简单 |
| 4 | P1-6 | LLM JSON 解析 | 影响稳定性，实施简单 |
| 5 | P1-5 | 外部服务错误传递 | 数据可观测性 |
| 6 | P1-2 | 并发编辑乐观锁 | 需要前端配合 `expected_version` |
| 7 | P1-3 | 任务僵死清理 | 需增加后台调度 |
| 8 | P1-4 | MCP 毒化恢复 | 实施复杂，需充分测试 |

---

## 不破坏现有结构的保障

1. **增量修改**：所有改动基于现有代码行级替换，不重构文件结构
2. **向后兼容**：
   - `session_id` 参数采用可选参数（默认值 `None`），不影响不传参的内部调用
   - `ServiceResult` 类型可通过 `result.data` 访问原始列表，原有调用方小幅适配即可
   - `expected_version` 可短期设为可选字段，未提供时降级为无锁模式
3. **渐进实施**：P0 和 P1 安全类问题优先，P1 稳定性问题可在后续迭代中完成
4. **保持命名和代码风格**：遵循项目现有的 `_parse_uuid`、`HTTPException` 模式、以及 async/await 异步风格
