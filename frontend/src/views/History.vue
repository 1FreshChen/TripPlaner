<template>
  <main class="app-shell">
    <div class="workspace">
      <header class="topbar">
        <div class="brand">
          <HistoryIcon :size="30" />
          <h1>历史计划</h1>
        </div>
        <a-space wrap>
          <a-button @click="refresh">
            <template #icon><RefreshCw :size="16" /></template>
            刷新
          </a-button>
          <a-button @click="router.push('/')">
            <template #icon><ArrowLeft :size="16" /></template>
            返回
          </a-button>
        </a-space>
      </header>

      <section class="panel history-panel">
        <div class="history-summary">
          <div>
            <span>当前会话</span>
            <strong>{{ shortSessionId }}</strong>
          </div>
          <div>
            <span>历史计划</span>
            <strong>{{ plans.length }}</strong>
          </div>
          <div>
            <span>对话消息</span>
            <strong>{{ conversationCount }}</strong>
          </div>
        </div>

        <a-alert v-if="error" class="history-alert" type="error" show-icon :message="error" />

        <a-spin :spinning="loading">
          <a-empty v-if="!plans.length" description="创建计划后，历史记录会在这里显示" />
          <div v-else class="plan-list">
            <article v-for="plan in plans" :key="plan.id" class="plan-card">
              <div class="plan-main">
                <div class="plan-title">
                  <MapPinned :size="20" />
                  <h2>{{ plan.city }}</h2>
                  <a-tag :color="statusColor(plan.status)">{{ statusText(plan.status) }}</a-tag>
                </div>
                <div class="plan-meta">
                  <span>{{ plan.start_date }} 至 {{ plan.end_date }}</span>
                  <span>{{ planDays(plan) }} 天</span>
                  <span>v{{ plan.version }}</span>
                  <span>更新于 {{ formatDate(plan.updated_at || plan.created_at) }}</span>
                </div>
              </div>

              <a-space wrap>
                <a-tooltip title="查看计划">
                  <a-button type="primary" @click="openPlan(plan.id)">
                    <template #icon><Eye :size="16" /></template>
                    查看
                  </a-button>
                </a-tooltip>
                <a-tooltip title="继续编辑">
                  <a-button @click="openPlan(plan.id, true)">
                    <template #icon><Edit3 :size="16" /></template>
                    编辑
                  </a-button>
                </a-tooltip>
                <a-tooltip title="对话调整">
                  <a-button @click="continueConversation(plan.id)">
                    <template #icon><MessageCircle :size="16" /></template>
                    对话
                  </a-button>
                </a-tooltip>
                <a-tooltip title="归档计划">
                  <a-button danger @click="confirmArchive(plan.id, plan.city)">
                    <template #icon><Archive :size="16" /></template>
                    归档
                  </a-button>
                </a-tooltip>
              </a-space>
            </article>
          </div>
        </a-spin>
      </section>
    </div>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  Alert as AAlert,
  Button as AButton,
  Empty as AEmpty,
  message,
  Modal,
  Space as ASpace,
  Spin as ASpin,
  Tag as ATag,
  Tooltip as ATooltip
} from 'ant-design-vue'
import { Archive, ArrowLeft, Edit3, Eye, History as HistoryIcon, MapPinned, MessageCircle, RefreshCw } from 'lucide-vue-next'
import { useRouter } from 'vue-router'
import { archiveTripPlan } from '../services/api'
import { getSession } from '../services/conversationApi'
import { useSessionStore } from '../stores/sessionStore'
import { useTripPlanStore } from '../stores/tripPlanStore'
import type { PlanStatus, SessionTripPlanSummary } from '../types'

const router = useRouter()
const sessionStore = useSessionStore()
const tripPlanStore = useTripPlanStore()
const plans = ref<SessionTripPlanSummary[]>([])
const conversationCount = ref(0)
const loading = ref(false)
const error = ref('')

const shortSessionId = computed(() => {
  if (!sessionStore.sessionId) return '未初始化'
  return `${sessionStore.sessionId.slice(0, 8)}...${sessionStore.sessionId.slice(-6)}`
})

const refresh = async () => {
  loading.value = true
  error.value = ''
  try {
    const session = await sessionStore.initSession()
    const detail = await getSession(session.session_id)
    plans.value = detail.trip_plans
    conversationCount.value = detail.conversation_count
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载历史记录失败'
  } finally {
    loading.value = false
  }
}

const openPlan = async (
  planId: string,
  edit = false,
  destination: 'result' | 'conversation' = 'result'
) => {
  loading.value = true
  try {
    await tripPlanStore.loadPlan(planId)
    if (edit) {
      tripPlanStore.startEdit()
    }
    if (destination === 'result') {
      await router.push({ name: 'result', params: { planId } })
    } else {
      await router.push({ name: 'conversation', query: { planId } })
    }
  } catch (err) {
    message.error(err instanceof Error ? err.message : '打开计划失败')
  } finally {
    loading.value = false
  }
}

const continueConversation = async (planId: string) => {
  await openPlan(planId, false, 'conversation')
}

const confirmArchive = (planId: string, city: string) => {
  Modal.confirm({
    title: `归档 ${city} 行程？`,
    content: '归档后它不会继续出现在历史计划列表中。',
    okText: '归档',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      await archiveTripPlan(planId)
      if (tripPlanStore.planId === planId) {
        tripPlanStore.clearPlan()
      }
      message.success('计划已归档')
      await refresh()
    }
  })
}

const statusText = (status: PlanStatus) => {
  const map: Record<PlanStatus, string> = {
    idle: '空闲',
    draft: '草稿',
    generating: '生成中',
    completed: '已完成',
    editing: '编辑中',
    archived: '已归档'
  }
  return map[status] || status
}

const statusColor = (status: PlanStatus) => {
  const map: Record<PlanStatus, string> = {
    idle: 'default',
    draft: 'default',
    generating: 'processing',
    completed: 'green',
    editing: 'blue',
    archived: 'default'
  }
  return map[status] || 'default'
}

const planDays = (plan: SessionTripPlanSummary) => {
  if (plan.days_count) return plan.days_count
  const start = new Date(plan.start_date).getTime()
  const end = new Date(plan.end_date).getTime()
  if (Number.isNaN(start) || Number.isNaN(end)) return 1
  return Math.max(1, Math.round((end - start) / 86_400_000) + 1)
}

const formatDate = (value: string) => {
  if (!value) return '-'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

onMounted(refresh)
</script>

<style scoped>
.history-panel {
  display: grid;
  gap: 18px;
}

.history-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.history-summary > div {
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fbfcfe;
}

.history-summary span {
  display: block;
  color: var(--muted);
  font-size: 13px;
}

.history-summary strong {
  display: block;
  margin-top: 6px;
  overflow-wrap: anywhere;
  color: var(--ink);
  font-size: 20px;
}

.history-alert {
  margin-bottom: 2px;
}

.plan-list {
  display: grid;
  gap: 12px;
}

.plan-card {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 16px;
  align-items: center;
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #ffffff;
}

.plan-main {
  min-width: 0;
}

.plan-title {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.plan-title h2 {
  margin: 0;
  overflow: hidden;
  color: var(--ink);
  font-size: 22px;
  line-height: 1.25;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.plan-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 16px;
  margin-top: 8px;
  color: var(--muted);
  font-size: 13px;
}

@media (max-width: 760px) {
  .history-summary,
  .plan-card {
    grid-template-columns: 1fr;
  }
}
</style>
