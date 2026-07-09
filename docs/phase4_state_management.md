# Phase 4: 状态管理 + API 扩展

> **所属项目**: 智能旅行助手 Harness 架构升级
> **依赖**: Phase 1（数据库）、Phase 2（编排器）、Phase 3（工具注册中心）
> **被依赖**: Phase 5（记忆系统需要 Session 和 API 上下文）

---

## 目标

引入完整的状态管理体系：后端通过 PostgreSQL 持久化计划状态机 + 版本历史 + 会话管理；前端用 Pinia 替代 sessionStorage + ref 的分散状态管理，并新增历史记录和对话页面。

---

## 4.1 后端 API 请求/响应契约

### 会话管理

```
POST /api/sessions
  Request:  {}
  Response: {
    "session_id": "uuid",
    "user_id": "uuid",
    "created_at": "2026-06-26T10:00:00Z",
    "is_new": true
  }

GET /api/sessions/{session_id}
  Response: {
    "session_id": "uuid",
    "user_id": "uuid",
    "trip_plans": [
      {
        "id": "uuid",
        "city": "北京",
        "start_date": "2026-07-01",
        "end_date": "2026-07-03",
        "status": "completed",
        "version": 1,
        "created_at": "...",
        "updated_at": "..."
      }
    ],
    "conversation_count": 5,
    "created_at": "...",
    "updated_at": "..."
  }
```

### 行程计划 CRUD

```
POST /api/trip/plan
  Request: {
    "session_id": "uuid",
    "city": "北京",
    "start_date": "2026-07-01",
    "end_date": "2026-07-03",
    "days": 3,
    "preferences": "历史文化,美食",
    "budget": "中等",
    "transportation": "公共交通",
    "accommodation": "经济型酒店"
  }
  Response (201): {
    "plan_id": "uuid",
    "status": "completed",
    "version": 1,
    ...TripPlan 完整数据
  }

GET /api/trip/plan/{plan_id}
  Response: { ...TripPlan + plan_id + status + version }

PUT /api/trip/plan/{plan_id}
  Request: {
    "plan_json": { ...修改后的 TripPlan },
    "change_summary": "删除了第2天的故宫景点，调整了酒店"
  }
  Response: {
    "plan_id": "uuid",
    "version": 2,
    "status": "editing",
    ...更新后的 TripPlan
  }

GET /api/trip/plan/{plan_id}/versions
  Response: {
    "versions": [
      {"version": 3, "change_summary": "调整预算", "created_at": "..."},
      {"version": 2, "change_summary": "删除故宫", "created_at": "..."},
      {"version": 1, "change_summary": "初始创建", "created_at": "..."}
    ]
  }

POST /api/trip/plan/{plan_id}/revert/{version}
  Response: { ...恢复到指定版本的 TripPlan }

DELETE /api/trip/plan/{plan_id}
  Response (204): (实际执行 archive，软删除)
```

### 对话

```
POST /api/conversation/{session_id}
  Request: {
    "message": "我想把第二天的行程调整一下，少去一个博物馆，多安排一个公园",
    "referenced_plan_id": "uuid"   // 可选，关联到特定计划
  }
  Response: {
    "message_id": "uuid",
    "role": "assistant",
    "content": "好的，我已经将第二天的大钟寺博物馆替换为玉渊潭公园...",
    "tool_calls": [                     // 如果 LLM 调用了工具
      {"tool": "itinerary_planner", "arguments": {...}}
    ],
    "updated_plan": { ... }            // 如果行程有更新
  }

GET /api/conversation/{session_id}
  Query: ?limit=50&before_id=uuid    // 分页游标
  Response: {
    "messages": [
      {"id": "uuid", "role": "user", "content": "...", "created_at": "..."},
      {"id": "uuid", "role": "assistant", "content": "...", "tool_calls": null, "created_at": "..."}
    ],
    "has_more": false
  }
```

### 用户偏好

```
GET /api/preferences?user_id=uuid
  Response: {
    "preferred_categories": ["历史文化", "美食"],
    "budget_profile": {"经济": 3, "中等": 7, "舒适": 2},
    "travel_style": "慢节奏",
    "favorite_cities": ["北京", "杭州"],
    ...
  }

PUT /api/preferences?user_id=uuid
  Request: { ...偏好字段 }
  Response: { ...更新后的偏好 }
```

---

## 4.2 后端依赖注入

```python
# backend/app/api/deps.py

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.orchestration.bootstrap import bootstrap_orchestration
from app.tools.bootstrap import bootstrap_tools
from app.services.amap_service import AmapService
from app.services.unsplash_service import UnsplashService
from app.config import get_settings

# 全局单例（应用启动时初始化）
_orchestrator = None
_tool_registry = None


def get_tool_registry():
    global _tool_registry
    if _tool_registry is None:
        settings = get_settings()
        amap = AmapService(settings.amap_api_key)
        unsplash = UnsplashService(settings.unsplash_access_key)
        _tool_registry = bootstrap_tools(amap, unsplash)
    return _tool_registry


def get_orchestrator():
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = bootstrap_orchestration()
    return _orchestrator
```

---

## 4.3 后端路由实现要点

### trip.py 关键逻辑

```python
# POST /api/trip/plan 核心流程
@router.post("/plan", response_model=TripPlanResponse)
async def create_trip_plan(
    request: TripPlanRequest,
    session_id: UUID = Body(...),
    db: AsyncSession = Depends(get_db),
    orchestrator = Depends(get_orchestrator),
):
    # 1. 创建/更新 user
    user = await get_or_create_user(db, session_id)

    # 2. 创建 trip_plan 记录，状态 = 'generating'
    plan = TripPlanModel(
        user_id=user.id,
        session_id=session_id,
        status='generating',
        city=request.city,
        ...
    )
    db.add(plan)
    await db.flush()

    # 3. 构建 context 并执行编排
    context = {"request": request, "plan_id": str(plan.id)}
    trace = await orchestrator.run(str(plan.id), context)

    # 4. 提取 PlannerAgent 输出
    trip_plan = context.get("trip_planner")
    plan.plan_json = trip_plan.model_dump()
    plan.status = 'completed'
    plan.budget_summary = trip_plan.budget.model_dump() if trip_plan.budget else None

    # 5. 写入版本历史
    version = TripPlanVersionModel(
        trip_plan_id=plan.id,
        version=1,
        plan_json=plan.plan_json,
        change_type='initial_create',
        change_summary='初始创建',
    )
    db.add(version)
    await db.flush()

    return {"plan_id": str(plan.id), "status": plan.status, "version": 1, **trip_plan.model_dump()}
```

### conversation.py 关键逻辑

```python
@router.post("/conversation/{session_id}")
async def send_message(
    session_id: UUID,
    body: ConversationRequest,
    db: AsyncSession = Depends(get_db),
):
    # 1. 保存用户消息
    user_msg = ConversationMessageModel(session_id=session_id, role='user', content=body.message)
    db.add(user_msg)

    # 2. 加载最近对话上下文作为 LLM 上下文
    history = await get_recent_messages(db, session_id, limit=20)
    context_messages = [{"role": m.role, "content": m.content} for m in history]

    # 3. 调用 LLM（可带工具）
    llm_service = get_llm_service()
    reply, tool_calls, usage = await llm_service.chat_with_tools(
        system_prompt=CONVERSATION_PROMPT,
        user_prompt=body.message,
        messages=context_messages,
        tools=get_tool_registry().get_openai_functions(),
    )

    # 4. 追溯 token 用量
    record_token_usage(db, usage, session_id=session_id)

    # 5. 如果 LLM 调用了工具，执行工具并再次调用 LLM 生成最终回复
    if tool_calls:
        # 执行工具...
        pass

    # 6. 保存 assistant 回复
    assistant_msg = ConversationMessageModel(
        session_id=session_id,
        role='assistant',
        content=reply,
        tool_calls_json=tool_calls if tool_calls else None,
    )
    db.add(assistant_msg)
    await db.flush()

    return {"message_id": str(assistant_msg.id), "role": "assistant", "content": reply, "tool_calls": tool_calls}
```

---

## 4.4 前端 Pinia Store 完整定义

### tripPlanStore.ts

```typescript
// frontend/src/stores/tripPlanStore.ts

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { TripPlan, TripPlanRequest, Budget } from '../types'
import { generateTripPlan, updateTripPlan, getTripPlan, getPlanVersions, revertPlanVersion } from '../services/api'

export type PlanStatus = 'idle' | 'generating' | 'completed' | 'editing' | 'archived'

export interface PlanVersion {
  version: number
  changeSummary: string
  createdAt: string
}

export const useTripPlanStore = defineStore('tripPlan', () => {
  // --- State ---
  const currentPlan = ref<TripPlan | null>(null)
  const planId = ref<string | null>(null)
  const planStatus = ref<PlanStatus>('idle')
  const originalPlan = ref<TripPlan | null>(null)   // 编辑前的快照（用于取消）
  const versions = ref<PlanVersion[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const progress = ref(0)
  const progressStatus = ref('')

  // --- Getters ---
  const isEditable = computed(() =>
    planStatus.value === 'completed' || planStatus.value === 'editing'
  )
  const hasUnsavedChanges = computed(() =>
    planStatus.value === 'editing' &&
    JSON.stringify(currentPlan.value) !== JSON.stringify(originalPlan.value)
  )
  const versionCount = computed(() => versions.value.length)

  // --- Actions ---
  async function createPlan(request: TripPlanRequest) {
    loading.value = true
    planStatus.value = 'generating'
    error.value = null
    progress.value = 0

    try {
      const interval = setInterval(() => {
        if (progress.value < 90) progress.value += 10
      }, 300)

      const result = await generateTripPlan(request)
      clearInterval(interval)

      currentPlan.value = result.plan
      planId.value = result.plan_id
      planStatus.value = 'completed'
      progress.value = 100
      versions.value = [{ version: 1, changeSummary: '初始创建', createdAt: new Date().toISOString() }]
    } catch (e) {
      error.value = e instanceof Error ? e.message : '生成计划失败'
      planStatus.value = 'idle'
    } finally {
      loading.value = false
    }
  }

  function startEdit() {
    if (!currentPlan.value) return
    originalPlan.value = JSON.parse(JSON.stringify(currentPlan.value))
    planStatus.value = 'editing'
  }

  async function saveEdit(changeSummary?: string) {
    if (!currentPlan.value || !planId.value) return
    try {
      const result = await updateTripPlan(planId.value, {
        plan_json: currentPlan.value,
        change_summary: changeSummary || '手动编辑',
      })
      currentPlan.value = result.plan
      planStatus.value = 'completed'
      originalPlan.value = null
      await loadVersions()
    } catch (e) {
      error.value = e instanceof Error ? e.message : '保存失败'
    }
  }

  function cancelEdit() {
    if (originalPlan.value) {
      currentPlan.value = JSON.parse(JSON.stringify(originalPlan.value))
    }
    originalPlan.value = null
    planStatus.value = 'completed'
  }

  async function loadVersions() {
    if (!planId.value) return
    try {
      const result = await getPlanVersions(planId.value)
      versions.value = result.versions
    } catch { /* silent */ }
  }

  async function revertToVersion(version: number) {
    if (!planId.value) return
    try {
      const result = await revertPlanVersion(planId.value, version)
      currentPlan.value = result.plan
      planStatus.value = 'completed'
      await loadVersions()
    } catch (e) {
      error.value = e instanceof Error ? e.message : '恢复版本失败'
    }
  }

  function moveAttraction(dayIndex: number, fromIndex: number, direction: 'up' | 'down') {
    if (!currentPlan.value) return
    const list = currentPlan.value.days[dayIndex].attractions
    const toIndex = direction === 'up' ? fromIndex - 1 : fromIndex + 1
    if (toIndex < 0 || toIndex >= list.length) return
    ;[list[fromIndex], list[toIndex]] = [list[toIndex], list[fromIndex]]
  }

  function deleteAttraction(dayIndex: number, attractionIndex: number) {
    if (!currentPlan.value) return
    currentPlan.value.days[dayIndex].attractions.splice(attractionIndex, 1)
  }

  function recalculateBudget() {
    if (!currentPlan.value) return
    const plan = currentPlan.value
    const totalAttractions = plan.days.reduce((s, d) => s + d.attractions.reduce((a, b) => a + (b.ticket_price || 0), 0), 0)
    const totalMeals = plan.days.reduce((s, d) => s + d.meals.reduce((a, b) => a + b.estimated_cost, 0), 0)
    const hotelCost = (plan.days.find(d => d.hotel)?.hotel?.estimated_cost || 0) * Math.max(plan.days.length - 1, 0)
    const budget: Budget = {
      total_attractions: totalAttractions,
      total_hotels: hotelCost,
      total_meals: totalMeals,
      total_transportation: plan.budget?.total_transportation || 0,
      total: totalAttractions + hotelCost + totalMeals + (plan.budget?.total_transportation || 0),
    }
    plan.budget = budget
  }

  return {
    currentPlan, planId, planStatus, originalPlan, versions, loading, error, progress, progressStatus,
    isEditable, hasUnsavedChanges, versionCount,
    createPlan, startEdit, saveEdit, cancelEdit, loadVersions, revertToVersion,
    moveAttraction, deleteAttraction, recalculateBudget,
  }
})
```

### sessionStore.ts

```typescript
// frontend/src/stores/sessionStore.ts

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { createSession, getSession, sendMessage, getConversation } from '../services/conversationApi'
import type { ConversationMessage } from '../types'

export const useSessionStore = defineStore('session', () => {
  const sessionId = ref<string>(localStorage.getItem('session_id') || '')
  const userId = ref<string | null>(null)
  const messages = ref<ConversationMessage[]>([])
  const isActive = ref(false)

  const messageCount = computed(() => messages.value.length)

  async function initSession() {
    if (sessionId.value) {
      try {
        const session = await getSession(sessionId.value)
        userId.value = session.user_id
        return
      } catch { /* session 可能过期，创建新的 */ }
    }
    const session = await createSession()
    sessionId.value = session.session_id
    userId.value = session.user_id
    localStorage.setItem('session_id', session.session_id)
  }

  async function loadHistory() {
    if (!sessionId.value) return
    const result = await getConversation(sessionId.value)
    messages.value = result.messages
  }

  async function send(content: string, planId?: string) {
    if (!sessionId.value) await initSession()
    messages.value.push({ id: '', role: 'user', content, created_at: new Date().toISOString() })
    const reply = await sendMessage(sessionId.value, { message: content, referenced_plan_id: planId })
    messages.value.push({
      id: reply.message_id,
      role: reply.role,
      content: reply.content,
      tool_calls: reply.tool_calls,
      created_at: new Date().toISOString(),
    })
    return reply
  }

  return { sessionId, userId, messages, isActive, messageCount, initSession, loadHistory, send }
})
```

### uiStore.ts

```typescript
// frontend/src/stores/uiStore.ts

import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useUiStore = defineStore('ui', () => {
  const editMode = ref(false)
  const activeSection = ref('overview')
  const exportingState = ref<'idle' | 'exporting-image' | 'exporting-pdf'>('idle')
  const loadingProgress = ref(0)
  const loadingStatus = ref('')

  function setSection(section: string) { activeSection.value = section }
  function startExport(type: 'image' | 'pdf') { exportingState.value = type === 'image' ? 'exporting-image' : 'exporting-pdf' }
  function finishExport() { exportingState.value = 'idle' }
  function resetLoading() { loadingProgress.value = 0; loadingStatus.value = '' }

  return { editMode, activeSection, exportingState, loadingProgress, loadingStatus, setSection, startExport, finishExport, resetLoading }
})
```

### preferencesStore.ts

```typescript
// frontend/src/stores/preferencesStore.ts

import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getPreferences, updatePreferences, saveItem, unsaveItem, getSavedItems } from '../services/api'
import type { SavedItem } from '../types'

export const usePreferencesStore = defineStore('preferences', () => {
  const preferredCategories = ref<string[]>([])
  const budgetProfile = ref<Record<string, number>>({})
  const travelStyle = ref('')
  const favoriteCities = ref<string[]>([])
  const savedItems = ref<SavedItem[]>([])
  const loaded = ref(false)

  async function load(userId: string) {
    if (loaded.value) return
    try {
      const prefs = await getPreferences(userId)
      preferredCategories.value = prefs.preferred_categories
      budgetProfile.value = prefs.budget_profile
      travelStyle.value = prefs.travel_style
      favoriteCities.value = prefs.favorite_cities
      const items = await getSavedItems(userId)
      savedItems.value = items
      loaded.value = true
    } catch { /* 静默失败，使用默认值 */ }
  }

  async function save(userId: string) {
    await updatePreferences(userId, {
      preferred_categories: preferredCategories.value,
      budget_profile: budgetProfile.value,
      travel_style: travelStyle.value,
      favorite_cities: favoriteCities.value,
    })
  }

  async function addSavedItem(userId: string, item: Omit<SavedItem, 'id' | 'created_at'>) {
    const result = await saveItem(userId, item)
    savedItems.value.unshift(result)
  }

  async function removeSavedItem(userId: string, itemId: string) {
    await unsaveItem(userId, itemId)
    savedItems.value = savedItems.value.filter(i => i.id !== itemId)
  }

  return { preferredCategories, budgetProfile, travelStyle, favoriteCities, savedItems, loaded, load, save, addSavedItem, removeSavedItem }
})
```

---

## 4.5 Composables（从现有 Result.vue 抽取）

```
composables/
├── useTripPlanner.ts      # 提交→轮询/回调→结果 流程
├── useExport.ts           # 导出图片/PDF 逻辑
└── useMap.ts              # 高德地图初始化逻辑
```

---

## 4.6 新增前端页面

- `History.vue` — 历史计划列表，支持查看/继续编辑/归档
- `Conversation.vue` — 多轮对话式行程调整

---

## 关键文件清单

| 文件 | 操作 |
|---|---|
| `backend/app/api/deps.py` | 新建：依赖注入（get_db, get_session, get_orchestrator） |
| `backend/app/api/routes/trip.py` | 重构：完整 CRUD + 状态机 + 版本管理 |
| `backend/app/api/routes/conversation.py` | 新建：多轮对话端点 |
| `backend/app/api/routes/sessions.py` | 新建：会话管理端点 |
| `backend/app/api/routes/preferences.py` | 新建：用户偏好端点 |
| `frontend/src/stores/tripPlanStore.ts` | 新建 |
| `frontend/src/stores/sessionStore.ts` | 新建 |
| `frontend/src/stores/uiStore.ts` | 新建 |
| `frontend/src/stores/preferencesStore.ts` | 新建 |
| `frontend/src/composables/useTripPlanner.ts` | 新建 |
| `frontend/src/composables/useExport.ts` | 新建（从 Result.vue 抽取） |
| `frontend/src/composables/useMap.ts` | 新建（从 Result.vue 抽取） |
| `frontend/src/views/Home.vue` | 重构：使用 tripPlanStore + sessionStore |
| `frontend/src/views/Result.vue` | 重构：使用 stores + composables |
| `frontend/src/views/History.vue` | 新建 |
| `frontend/src/views/Conversation.vue` | 新建 |
| `frontend/src/router/index.ts` | 修改：新增 /history, /conversation/:sessionId 路由 |
| `frontend/src/services/api.ts` | 扩展：新增 session, conversation, preferences 接口 |
| `frontend/src/services/conversationApi.ts` | 新建：多轮对话 API 封装 |

---

## 验证方式

1. 后端：启动服务后调用 `POST /api/sessions` → 获取 session_id → `POST /api/trip/plan` → 验证返回 plan_id + status=completed
2. 后端：`PUT /api/trip/plan/{id}` 编辑计划 → 验证版本号自增 → `GET /api/trip/plan/{id}/versions` 查看版本列表
3. 后端：`POST /api/conversation/{session_id}` 发送调整消息 → 验证 LLM 回复 + updated_plan
4. 前端：`npm run dev` → 创建计划 → 编辑景点（拖拽排序/删除）→ 保存 → 查看版本历史 → 恢复旧版本
5. 前端：历史页面查看过往计划列表 → 点击继续编辑
6. 前端：对话页面发送调整请求 → 验证行程实时更新
