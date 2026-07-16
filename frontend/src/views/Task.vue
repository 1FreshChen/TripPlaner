<template>
  <main class="app-shell">
    <div class="workspace task-workspace">
      <header class="topbar">
        <div class="brand">
          <LoaderCircle class="spin" :size="28" />
          <h1>正在生成旅行计划</h1>
        </div>
        <a-button @click="router.push({ name: 'home' })">返回首页</a-button>
      </header>

      <section class="panel task-panel">
        <template v-if="!hasTerminalFailure">
          <a-progress :percent="progress" :status="taskStatus === 'succeeded' ? 'success' : 'active'" />
          <h2>{{ phaseLabel }}</h2>
          <p class="status-message">{{ progressStatus || '正在恢复任务状态...' }}</p>

          <div class="timing-grid">
            <div>
              <Clock3 :size="18" />
              <span>总耗时</span>
              <strong>{{ formatDuration(taskElapsedMs) }}</strong>
            </div>
            <div>
              <Gauge :size="18" />
              <span>当前阶段</span>
              <strong>{{ formatDuration(taskPhaseElapsedMs) }}</strong>
            </div>
          </div>

          <a-timeline class="phase-list">
            <a-timeline-item v-for="item in completedPhases" :key="item.phase" color="green">
              {{ item.label }} · {{ formatDuration(item.elapsed) }}
            </a-timeline-item>
            <a-timeline-item v-if="taskStatus !== 'succeeded'" color="blue">
              {{ phaseLabel }} · 进行中
            </a-timeline-item>
          </a-timeline>

          <a-typography-text type="secondary">
            任务编号：{{ routeTaskId }}。刷新或稍后重新打开此地址，任务仍可恢复。
          </a-typography-text>
        </template>

        <a-result
          v-else
          :status="taskStatus === 'failed' ? 'error' : 'warning'"
          :title="terminalTitle"
          :sub-title="error || progressStatus"
        >
          <template #extra>
            <a-button type="primary" @click="router.push({ name: 'home' })">重新规划</a-button>
          </template>
        </a-result>
      </section>
    </div>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue'
import {
  Button as AButton,
  Progress as AProgress,
  Result as AResult,
  Timeline as ATimeline,
  TimelineItem as ATimelineItem,
  TypographyText as ATypographyText
} from 'ant-design-vue'
import { storeToRefs } from 'pinia'
import { Clock3, Gauge, LoaderCircle } from 'lucide-vue-next'
import { useRoute, useRouter } from 'vue-router'
import { useTripPlanStore } from '../stores/tripPlanStore'
import type { TaskPhase } from '../types'

const router = useRouter()
const route = useRoute()
const tripPlanStore = useTripPlanStore()
const {
  progress,
  progressStatus,
  error,
  taskStatus,
  planId,
  taskPhase,
  taskElapsedMs,
  taskPhaseElapsedMs,
  taskPhaseTimings
} = storeToRefs(tripPlanStore)
const routeTaskId = String(route.params.taskId || '')
let stopMonitoring: (() => void) | null = null

const phaseLabels: Record<TaskPhase, string> = {
  queued: '等待 Worker 接收任务',
  preparing: '准备行程请求',
  collecting_context: '加载偏好和会话上下文',
  llm_planning: '生成每日行程',
  meal_enrichment: '补充餐饮信息',
  validating: '校验行程质量',
  saving: '保存行程和初始版本',
  completed: '行程规划完成',
  failed: '行程生成失败'
}

const phaseLabel = computed(() => phaseLabels[taskPhase.value])
const hasTerminalFailure = computed(
  () => taskStatus.value === 'failed' || taskStatus.value === 'cancelled' || taskStatus.value === 'expired'
)
const terminalTitle = computed(() => {
  if (taskStatus.value === 'cancelled') return '任务已取消'
  if (taskStatus.value === 'expired') return '任务已过期'
  return '行程生成失败'
})
const phaseOrder: TaskPhase[] = [
  'queued',
  'preparing',
  'collecting_context',
  'llm_planning',
  'meal_enrichment',
  'validating',
  'saving',
  'completed',
  'failed'
]
const completedPhases = computed(() =>
  Object.entries(taskPhaseTimings.value)
    .map(([phase, elapsed]) => ({
      phase,
      label: phaseLabels[phase as TaskPhase] || phase,
      elapsed
    }))
    .sort((left, right) => phaseOrder.indexOf(left.phase as TaskPhase) - phaseOrder.indexOf(right.phase as TaskPhase))
)

const formatDuration = (milliseconds: number) => {
  if (milliseconds < 1000) return `${milliseconds} ms`
  return `${(milliseconds / 1000).toFixed(1)} 秒`
}

onMounted(() => {
  if (!routeTaskId) {
    void router.replace({ name: 'home' })
    return
  }
  stopMonitoring = tripPlanStore.monitorTask(routeTaskId, () => {
    void router.replace({ name: 'result', params: { planId: planId.value || undefined } })
  })
})

onBeforeUnmount(() => stopMonitoring?.())
</script>

<style scoped>
.task-workspace {
  max-width: 900px;
}

.task-panel {
  margin-top: 24px;
  padding: 32px;
}

.task-panel h2 {
  margin: 28px 0 8px;
}

.status-message {
  color: #667085;
  font-size: 16px;
}

.timing-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin: 28px 0;
}

.timing-grid > div {
  align-items: center;
  background: #f8fafc;
  border-radius: 12px;
  display: grid;
  gap: 6px;
  grid-template-columns: auto 1fr;
  padding: 16px;
}

.timing-grid strong {
  grid-column: 2;
}

.phase-list {
  margin: 8px 0 20px;
}

.spin {
  animation: spin 1.2s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 640px) {
  .timing-grid {
    grid-template-columns: 1fr;
  }
}
</style>
