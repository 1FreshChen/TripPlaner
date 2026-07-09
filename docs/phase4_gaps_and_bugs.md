# Phase 4 缺漏与 Bug 修复清单

> **来源**: 对 `docs/phase4_state_management.md` 规范的逐项审查
> **审查日期**: 2026-06-30
> **整体进度**: 约 90% 完成 — 核心 API 路由、Schema、DB Models、Pinia Store、Composables 均已到位，存在 2 个功能缺漏 + 3 个 Bug

---

## 一、功能缺漏（2 项）

### 缺漏 #1：Conversation 后端是 Stub — 无 LLM / Tool-Calling 集成

- **严重程度**: 🔴 P0 — 对话调整功能完全不可用
- **涉及文件**: `backend/app/services/state_service.py` → `send_conversation_message()` (L218-245)

#### 问题描述

规范 §4.3 `conversation.py 关键逻辑` 定义了完整的 6 步流程，当前实现只完成了步骤 1 和 6（保存消息），步骤 2-5 全部缺失：

```
步骤 1 ─ ✅ 保存用户消息
步骤 2 ─ ❌ 加载最近对话上下文（history）作为 LLM 上下文
步骤 3 ─ ❌ 调用 LLM（带工具定义）
步骤 4 ─ ❌ 追溯 token 用量到 token_usage 表
步骤 5 ─ ❌ 执行工具并将结果回传 LLM 生成最终回复
步骤 6 ─ ✅ 保存 assistant 回复
```

当前实现直接返回硬编码字符串：

```python
# backend/app/services/state_service.py:229
content = "我已收到你的调整需求。当前版本会先记录对话，后续记忆系统会把它接入自动改行程。"
```

#### 修复方案

将 `send_conversation_message()` 替换为完整实现：

```python
async def send_conversation_message(
    self, session_id: str, request: ConversationRequest
) -> ConversationResponse:
    session_uuid = _parse_uuid(session_id, "session_id")

    # 1. 保存用户消息
    user_msg = ConversationMessage(
        id=uuid.uuid4(),
        session_id=session_uuid,
        role="user",
        content=request.message,
        metadata_json=(
            {"referenced_plan_id": request.referenced_plan_id}
            if request.referenced_plan_id
            else {}
        ),
    )
    self._db.add(user_msg)
    await self._db.flush()

    # 2. 加载最近对话上下文
    history = await self._load_recent_messages(session_uuid, limit=20)
    context_messages = [{"role": m.role, "content": m.content} for m in history]

    # 3. 如果引用了计划，加载计划上下文
    plan_context = ""
    updated_plan = None
    if request.referenced_plan_id:
        plan_uuid = _parse_uuid(request.referenced_plan_id, "referenced_plan_id")
        plan = await self._db.scalar(
            select(TripPlanModel).where(TripPlanModel.id == plan_uuid)
        )
        if plan and plan.plan_json:
            plan_context = json.dumps(plan.plan_json, ensure_ascii=False)

    # 4. 调用 LLM（带工具）
    from app.config import get_settings
    from app.services.llm_service import LLMService
    from app.tools.bootstrap import bootstrap_tools

    settings = get_settings()
    llm = LLMService(settings.llm_api_key, settings.llm_base_url, settings.llm_model)

    system_prompt = (
        "你是一个智能旅行助手，帮助用户调整他们的旅行计划。"
        "你可以使用工具来搜索景点、查询天气、推荐酒店、计算预算。"
        "当用户要求修改行程时，先理解他们的需求，然后调用合适的工具获取信息，"
        "最后给出修改建议。"
    )

    user_prompt = request.message
    if plan_context:
        user_prompt = (
            f"当前旅行计划:\n{plan_context}\n\n用户要求: {request.message}"
        )

    tool_registry = bootstrap_tools()
    tools = tool_registry.get_openai_functions()

    content, tool_calls, usage = await llm.chat_with_tools(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        tools=tools,
        max_tool_rounds=5,
    )

    # 5. 追溯 token 用量
    if usage.total_tokens > 0:
        self._db.add(
            TokenUsage(
                session_id=session_uuid,
                model=usage.model,
                provider=usage.provider,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
            )
        )

    # 6. 如果 LLM 调用了工具，执行工具并再次调用 LLM
    if tool_calls:
        from app.tools.executor import ToolExecutor

        executor = ToolExecutor()
        tool_results = []
        for tc in tool_calls:
            tool = tool_registry.get(tc["tool"])
            if tool:
                result = await executor.execute(tool, **tc["arguments"])
                tool_results.append({
                    "tool_call_id": tc["id"],
                    "role": "tool",
                    "content": json.dumps(result, ensure_ascii=False),
                })

        if tool_results:
            # 将工具结果回传 LLM 生成最终回复
            context_messages.append({
                "role": "assistant",
                "content": content or "",
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["tool"],
                            "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                        },
                    }
                    for tc in tool_calls
                ],
            })
            context_messages.extend(tool_results)

            content, _, usage2 = await llm.chat_with_tools(
                system_prompt=system_prompt,
                user_prompt="请根据工具执行结果，给出最终回复。",
                tools=tools,
                max_tool_rounds=1,
            )

    # 7. 保存 assistant 回复
    assistant_msg = ConversationMessage(
        id=uuid.uuid4(),
        session_id=session_uuid,
        role="assistant",
        content=content or "抱歉，我无法处理你的请求。",
        tool_calls_json=tool_calls if tool_calls else None,
    )
    self._db.add(assistant_msg)
    await self._db.flush()

    return ConversationResponse(
        message_id=str(assistant_msg.id),
        role="assistant",
        content=content or "",
        tool_calls=tool_calls or [],
        updated_plan=updated_plan,
    )
```

同时需要新增辅助方法：

```python
async def _load_recent_messages(
    self, session_uuid: uuid.UUID, limit: int = 20
) -> list[ConversationMessage]:
    """加载会话最近的消息（正序）"""
    stmt = (
        select(ConversationMessage)
        .where(ConversationMessage.session_id == session_uuid)
        .order_by(ConversationMessage.created_at.desc())
        .limit(limit)
    )
    messages = (await self._db.execute(stmt)).scalars().all()
    return list(reversed(messages))
```

---

### 缺漏 #2：History.vue — 纯静态占位符

- **严重程度**: 🔴 P1 — 历史记录页面完全不可用
- **涉及文件**: `frontend/src/views/History.vue`

#### 问题描述

规范 §4.6 要求 History.vue 提供 *"历史计划列表，支持查看/继续编辑/归档"*。当前实现仅有一个空的 `<a-empty>` 占位，没有任何数据加载、列表渲染、交互逻辑。

```html
<!-- 当前实现 (History.vue:11) -->
<a-empty description="创建计划后，历史记录会在这里显示" />
```

#### 修复方案

完整重写 `History.vue`：

```vue
<template>
  <main class="app-shell">
    <div class="workspace">
      <header class="topbar">
        <div class="brand">
          <MapPinned :size="30" />
          <h1>历史计划</h1>
        </div>
        <a-button @click="router.push('/')">
          <template #icon><ArrowLeft :size="16" /></template>
          返回
        </a-button>
      </header>

      <section class="panel">
        <a-spin :spinning="loading">
          <div v-if="!sessionId" class="empty-state">
            <a-empty description="暂无会话，请先创建旅行计划" />
          </div>

          <div v-else-if="plans.length === 0" class="empty-state">
            <a-empty description="暂无历史计划" />
          </div>

          <a-list
            v-else
            :data-source="plans"
            item-layout="horizontal"
          >
            <template #renderItem="{ item }">
              <a-list-item>
                <a-list-item-meta>
                  <template #title>
                    <a-space>
                      <span>{{ item.city }} · {{ item.days_count || item.days?.length }} 天</span>
                      <a-tag :color="statusColor(item.status)">{{ statusLabel(item.status) }}</a-tag>
                    </a-space>
                  </template>
                  <template #description>
                    {{ item.start_date }} 至 {{ item.end_date }}
                    <br />
                    偏好: {{ item.preferences }} · 预算: {{ item.budget_level || item.budget }}
                  </template>
                </a-list-item-meta>
                <template #actions>
                  <a-button size="small" @click="viewPlan(item.id)">查看</a-button>
                  <a-button size="small" type="primary" @click="continueEdit(item.id)">
                    继续编辑
                  </a-button>
                  <a-popconfirm
                    title="确定要归档这个计划吗？"
                    @confirm="archivePlan(item.id)"
                  >
                    <a-button size="small" danger>归档</a-button>
                  </a-popconfirm>
                </template>
              </a-list-item>
            </template>
          </a-list>
        </a-spin>
      </section>
    </div>
  </main>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { ArrowLeft, MapPinned } from 'lucide-vue-next'
import { useRouter } from 'vue-router'
import { archiveTripPlan, getTripPlan } from '../services/api'
import { getSession } from '../services/conversationApi'
import { useSessionStore } from '../stores/sessionStore'
import type { SessionTripPlanSummary } from '../types'

// 扩展类型 —— 前端可补充到 types/index.ts
interface PlanSummary {
  id: string
  city: string
  start_date: string
  end_date: string
  status: string
  version: number
  created_at: string
  updated_at: string
  preferences?: string
  budget_level?: string
  budget?: string
  days_count?: number
  days?: { length: number }
}

const router = useRouter()
const sessionStore = useSessionStore()
const plans = ref<PlanSummary[]>([])
const loading = ref(false)
const sessionId = ref(sessionStore.sessionId)

const statusColor = (status: string) => {
  const map: Record<string, string> = {
    completed: 'green',
    editing: 'blue',
    generating: 'orange',
    archived: 'default',
  }
  return map[status] || 'default'
}

const statusLabel = (status: string) => {
  const map: Record<string, string> = {
    completed: '已完成',
    editing: '编辑中',
    generating: '生成中',
    archived: '已归档',
  }
  return map[status] || status
}

const loadPlans = async () => {
  if (!sessionId.value) {
    await sessionStore.initSession()
    sessionId.value = sessionStore.sessionId
  }
  if (!sessionId.value) return

  loading.value = true
  try {
    const session = await getSession(sessionId.value)
    plans.value = (session as any).trip_plans || []
  } catch {
    message.error('加载历史计划失败')
  } finally {
    loading.value = false
  }
}

const viewPlan = async (planId: string) => {
  try {
    const plan = await getTripPlan(planId)
    // 将计划写入 store 后跳转
    const { useTripPlanStore } = await import('../stores/tripPlanStore')
    const tripPlanStore = useTripPlanStore()
    tripPlanStore.currentPlan = plan
    tripPlanStore.planId = plan.plan_id
    tripPlanStore.planStatus = plan.status
    router.push({ name: 'result' })
  } catch {
    message.error('加载计划失败')
  }
}

const continueEdit = async (planId: string) => {
  await viewPlan(planId)
  const { useTripPlanStore } = await import('../stores/tripPlanStore')
  useTripPlanStore().startEdit()
}

const archivePlan = async (planId: string) => {
  try {
    await archiveTripPlan(planId)
    plans.value = plans.value.filter(p => p.id !== planId)
    message.success('已归档')
  } catch {
    message.error('归档失败')
  }
}

onMounted(loadPlans)
</script>

<style scoped>
.empty-state {
  padding: 64px 0;
}
</style>
```

同时需在 `frontend/src/types/index.ts` 中补充会话计划摘要类型：

```typescript
// 追加到 types/index.ts
export interface SessionTripPlanSummary {
  id: string
  city: string
  start_date: string
  end_date: string
  status: string
  version: number
  created_at: string
  updated_at: string
}
```

以及更新 `conversationApi.ts` 中 `getSession` 的返回类型使用正确的 `SessionDetailResponse`：

```typescript
// conversationApi.ts — getSession 返回完整 session 详情
export const getSession = async (sessionId: string): Promise<SessionDetailResponse> => {
  const response = await api.get<SessionDetailResponse>(`/sessions/${sessionId}`)
  return response.data
}
```

---

## 二、Bug 修复（3 项）

### Bug #1：`_plan_response` 中 `plan_json` 为 `None` 时抛出 `ValidationError`

- **严重程度**: 🟡 P2 — 边缘情况导致 HTTP 500
- **涉及文件**: `backend/app/services/state_service.py:323-330`

#### 问题描述

`TripPlanModel.plan_json` 字段定义为 `Optional[dict]`（`db_models.py:88`），但 `_plan_response()` 方法在 `trip_plan` 参数为 `None` 时直接对 `plan.plan_json` 调用 `model_validate()`，若该字段为 `None` 会触发 Pydantic `ValidationError`。

```python
# 当前实现 (L323-330)
def _plan_response(self, plan: TripPlanModel, trip_plan: Optional[TripPlan] = None) -> TripPlanResponse:
    payload = trip_plan or TripPlan.model_validate(plan.plan_json)  # ← plan.plan_json 可能为 None
    return TripPlanResponse(
        **payload.model_dump(),
        plan_id=str(plan.id),
        status=plan.status,
        version=plan.version,
    )
```

#### 修复

```python
def _plan_response(
    self, plan: TripPlanModel, trip_plan: Optional[TripPlan] = None
) -> TripPlanResponse:
    if trip_plan is not None:
        payload = trip_plan
    elif plan.plan_json is not None:
        payload = TripPlan.model_validate(plan.plan_json)
    else:
        raise HTTPException(
            status_code=500,
            detail=f"计划 {plan.id} 缺少 plan_json 数据",
        )
    return TripPlanResponse(
        **payload.model_dump(),
        plan_id=str(plan.id),
        status=plan.status,
        version=plan.version,
    )
```

---

### Bug #2：TypeScript — `window._AMapSecurityConfig` 缺少类型声明

- **严重程度**: 🟡 P3 — 严格 TS 编译失败（`noImplicitAny` / `strict` 模式下报错）
- **涉及文件**: `frontend/src/composables/useMap.ts:23` + `frontend/src/env.d.ts`

#### 问题描述

```typescript
// useMap.ts:23
window._AMapSecurityConfig = { securityJsCode: securityCode }
//     ^^^^^^^^^^^^^^^^^^^^^ Property '_AMapSecurityConfig' does not exist on type 'Window & typeof globalThis'.
```

#### 修复

在 `frontend/src/env.d.ts`（或 `src/vite-env.d.ts`）中添加全局类型扩展：

```typescript
// frontend/src/env.d.ts (追加)
declare global {
  interface Window {
    _AMapSecurityConfig?: {
      securityJsCode: string
    }
  }
}

export {}
```

---

### Bug #3：`list_conversation` 未实现 `before_id` 游标分页

- **严重程度**: 🟡 P3 — 功能缺失（规范要求但未影响基本可用性）
- **涉及文件**: `backend/app/services/state_service.py:247-274`

#### 问题描述

规范 §4.1 定义 `GET /api/conversation/{session_id}` 支持 `?limit=50&before_id=uuid` 游标分页。路由层 (`conversation.py:26`) 已接收 `before_id` 参数，但 `list_conversation()` 方法未使用该参数，导致分页功能缺失。

#### 修复

```python
async def list_conversation(
    self,
    session_id: str,
    limit: int = 50,
    before_id: Optional[str] = None,
) -> ConversationListResponse:
    session_uuid = _parse_uuid(session_id, "session_id")

    # 如果提供了 before_id，先查出该消息的 created_at
    before_timestamp = None
    if before_id:
        before_uuid = _parse_uuid(before_id, "before_id")
        before_msg = await self._db.scalar(
            select(ConversationMessage).where(
                ConversationMessage.id == before_uuid
            )
        )
        if before_msg:
            before_timestamp = before_msg.created_at

    stmt = select(ConversationMessage).where(
        ConversationMessage.session_id == session_uuid
    )

    # 应用游标过滤
    if before_timestamp:
        stmt = stmt.where(
            ConversationMessage.created_at < before_timestamp
        )

    stmt = stmt.order_by(desc(ConversationMessage.created_at)).limit(limit + 1)

    messages = (await self._db.execute(stmt)).scalars().all()
    visible = list(reversed(messages[:limit]))

    return ConversationListResponse(
        messages=[
            ConversationMessageResponse(
                id=str(message.id),
                role=message.role,
                content=message.content,
                tool_calls=message.tool_calls_json,
                created_at=_iso(message.created_at),
            )
            for message in visible
        ],
        has_more=len(messages) > limit,
    )
```

---

## 三、修复汇总

| 序号 | 类型 | 文件 | 修复内容 | 优先级 |
|------|------|------|---------|--------|
| 1 | 缺漏 | `backend/app/services/state_service.py` | `send_conversation_message()` 集成 LLM + Tool-Calling | 🔴 P0 |
| 2 | 缺漏 | `frontend/src/views/History.vue` | 完整实现历史列表（加载/查看/编辑/归档） | 🔴 P1 |
| 3 | Bug | `backend/app/services/state_service.py` | `_plan_response()` 防御 `plan_json=None` | 🟡 P2 |
| 4 | Bug | `frontend/src/env.d.ts` | 扩展 `Window` 类型声明 `_AMapSecurityConfig` | 🟡 P3 |
| 5 | Bug | `backend/app/services/state_service.py` | `list_conversation()` 实现 `before_id` 游标分页 | 🟡 P3 |

### 连锁改动

| 改动 | 关联文件 |
|------|---------|
| History.vue 需要的类型 `SessionTripPlanSummary` | `frontend/src/types/index.ts` — 新增接口 |
| History.vue 需要的 API `getSession` 返回完整详情 | `frontend/src/services/conversationApi.ts` — `getSession` 返回类型改为 `SessionDetailResponse` |
| Conversation 集成 LLM 需要 `json` import | `backend/app/services/state_service.py` — 添加 `import json` |
| Conversation 集成 LLM 需要 `TokenUsage` model | `backend/app/services/state_service.py` — 添加 `from app.models.db_models import TokenUsage` |
