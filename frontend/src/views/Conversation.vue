<template>
  <main class="app-shell">
    <div class="workspace">
      <header class="topbar">
        <div class="brand">
          <h1>对话调整</h1>
        </div>
        <a-button @click="router.push('/')">返回</a-button>
      </header>
      <section class="panel conversation-panel">
        <a-alert v-if="pageError" type="error" show-icon :message="pageError" />
        <div class="plan-context">
          <div class="plan-context-copy">
            <strong>当前调整行程</strong>
            <span v-if="currentPlan">
              {{ currentPlan.city }} · {{ currentPlan.start_date }} 至 {{ currentPlan.end_date }} · 版本
              {{ currentPlan.version }}
            </span>
            <span v-else>请选择一份行程后再发送调整要求</span>
          </div>
          <a-space wrap>
            <a-select
              v-if="planOptions.length"
              v-model:value="selectedPlanId"
              :options="planOptions"
              style="width: 320px"
              @change="handlePlanChange"
            />
            <a-button v-if="currentPlan" @click="handleViewPlan">查看更新后行程</a-button>
          </a-space>
        </div>
        <a-alert
          v-if="!initializing && !availablePlans.length"
          type="info"
          show-icon
          message="当前会话还没有可调整的行程，请先生成一份旅行计划。"
        />
        <a-spin :spinning="initializing || historyLoading">
          <div v-if="hasMore" class="history-actions">
            <a-button :loading="historyLoading" @click="handleLoadMore">加载更早消息</a-button>
          </div>
          <a-empty v-if="!messages.length" description="暂无历史消息，可以从下方输入调整需求" />
          <div v-else class="message-list">
            <div v-for="item in messages" :key="item.id" class="message-row">
              <div class="message-meta">
                <strong>{{ item.role === 'user' ? '我' : '助手' }}</strong>
                <a-tag v-if="item._planUpdated" color="success">行程已更新</a-tag>
                <a-tag v-else-if="item._planUpdateFailed" color="warning">更新未保存</a-tag>
              </div>
              <span class="message-content">{{ item.content }}</span>
            </div>
          </div>
        </a-spin>
        <a-input-search
          v-model:value="draft"
          enter-button="发送"
          :loading="sending"
          :disabled="sending || initializing || !currentPlan"
          placeholder="例如：第二天少去一个博物馆，多安排公园"
          @search="handleSend"
        />
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
  InputSearch as AInputSearch,
  message,
  Select as ASelect,
  Space as ASpace,
  Spin as ASpin,
  Tag as ATag
} from 'ant-design-vue'
import { storeToRefs } from 'pinia'
import { useRoute, useRouter } from 'vue-router'
import { getSession } from '../services/conversationApi'
import { useSessionStore } from '../stores/sessionStore'
import { useTripPlanStore } from '../stores/tripPlanStore'
import type { SessionTripPlanSummary } from '../types'

const router = useRouter()
const route = useRoute()
const sessionStore = useSessionStore()
const tripPlanStore = useTripPlanStore()
const { hasMore, historyLoading, messages, sending } = storeToRefs(sessionStore)
const { currentPlan } = storeToRefs(tripPlanStore)
const draft = ref('')
const initializing = ref(true)
const pageError = ref('')
const availablePlans = ref<SessionTripPlanSummary[]>([])
const selectedPlanId = ref<string>()
const routePlanId = computed(() => {
  const value = route.query.planId
  if (Array.isArray(value)) return value[0] || null
  return typeof value === 'string' && value ? value : null
})
const planOptions = computed(() =>
  availablePlans.value.map(plan => ({
    label: `${plan.city} · ${plan.start_date} 至 ${plan.end_date} · 版本 ${plan.version}`,
    value: plan.id
  }))
)

const activatePlan = async (targetPlanId: string, updateRoute = true) => {
  if (!currentPlan.value || tripPlanStore.planId !== targetPlanId) {
    await tripPlanStore.loadPlan(targetPlanId)
  }
  selectedPlanId.value = targetPlanId
  if (updateRoute && routePlanId.value !== targetPlanId) {
    await router.replace({ query: { ...route.query, planId: targetPlanId } })
  }
}

const initializePlanContext = async () => {
  const session = await sessionStore.initSession()
  const detail = await getSession(session.session_id)
  availablePlans.value = detail.trip_plans
  const validIds = new Set(detail.trip_plans.map(plan => plan.id))
  const targetPlanId = [routePlanId.value, tripPlanStore.planId, detail.trip_plans[0]?.id].find(
    candidate => candidate && validIds.has(candidate)
  )
  if (targetPlanId) {
    await activatePlan(targetPlanId)
  } else {
    tripPlanStore.clearPlan()
  }
}

const handlePlanChange = async (targetPlanId: unknown) => {
  if (typeof targetPlanId !== 'string') return
  pageError.value = ''
  const previousPlanId = tripPlanStore.planId || undefined
  try {
    await activatePlan(targetPlanId)
  } catch (error) {
    selectedPlanId.value = previousPlanId
    pageError.value = error instanceof Error ? error.message : '加载行程失败'
    message.error(pageError.value)
  }
}

const handleViewPlan = async () => {
  if (!tripPlanStore.planId) return
  await router.push({ name: 'result', params: { planId: tripPlanStore.planId } })
}

const handleSend = async () => {
  const content = draft.value.trim()
  if (!content) return
  if (!tripPlanStore.planId || !currentPlan.value) {
    pageError.value = '请先选择一份需要调整的行程'
    message.warning(pageError.value)
    return
  }
  pageError.value = ''
  draft.value = ''
  try {
    const reply = await sessionStore.send(content, tripPlanStore.planId)
    if (reply.updated_plan) {
      const updatedPlan = reply.updated_plan
      availablePlans.value = availablePlans.value.map(plan =>
        plan.id === updatedPlan.plan_id
          ? { ...plan, status: updatedPlan.status, version: updatedPlan.version }
          : plan
      )
      selectedPlanId.value = updatedPlan.plan_id
      await router.replace({ query: { ...route.query, planId: updatedPlan.plan_id } })
      message.success(`行程已更新并保存为版本 ${updatedPlan.version}`)
    } else if (reply.plan_update_failed) {
      message.warning('行程修改未保存')
    } else {
      message.info('本轮未识别到需要写入行程的变化')
    }
  } catch (error) {
    if (!draft.value) draft.value = content
    pageError.value = error instanceof Error ? error.message : '发送失败'
    message.error(pageError.value)
  }
}

const handleLoadMore = async () => {
  pageError.value = ''
  try {
    await sessionStore.loadMoreHistory()
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '加载更多消息失败'
  }
}

onMounted(async () => {
  pageError.value = ''
  try {
    await initializePlanContext()
    await sessionStore.loadHistory()
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '初始化会话失败'
    message.error(pageError.value)
  } finally {
    initializing.value = false
  }
})
</script>

<style scoped>
.history-actions {
  display: flex;
  justify-content: center;
  margin-bottom: 12px;
}

.plan-context {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin: 12px 0 16px;
  padding: 12px 16px;
  border: 1px solid #d9e6f2;
  border-radius: 10px;
  background: #f7fbff;
}

.plan-context-copy {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.plan-context-copy span {
  color: #64748b;
}

@media (max-width: 720px) {
  .plan-context {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
