import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import type {
  Budget,
  PlanStatus,
  PlanVersion,
  TaskPhase,
  TaskStatus,
  TripPlan,
  TripPlanRequest,
  TripPlanResponse,
  TripPlanTaskStatusResponse
} from '../types'
import {
  createTripPlanTaskEventSource,
  generateTripPlan,
  getPlanVersions,
  getTripPlan,
  getTripPlanTask,
  getTripPlanTaskResult,
  revertPlanVersion,
  updateTripPlan
} from '../services/api'
import { getStoredValue, removeStoredValue, setStoredValue } from '../utils/storage'

const TASK_POLL_INTERVAL_MS = 1500
const MAX_TASK_POLL_FAILURES = 10
const UNSUCCESSFUL_TASK_STATUSES: ReadonlySet<TaskStatus> = new Set(['failed', 'cancelled', 'expired'])

export const useTripPlanStore = defineStore('tripPlan', () => {
  const currentPlan = ref<TripPlanResponse | null>(null)
  const planId = ref<string | null>(getStoredValue('activePlanId'))
  const planStatus = ref<PlanStatus>('idle')
  const originalPlan = ref<TripPlanResponse | null>(null)
  const versions = ref<PlanVersion[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const progress = ref(0)
  const progressStatus = ref('')
  const taskId = ref<string | null>(null)
  const taskStatus = ref<TaskStatus | null>(null)
  const taskPhase = ref<TaskPhase>('queued')
  const taskElapsedMs = ref(0)
  const taskPhaseElapsedMs = ref(0)
  const taskPhaseTimings = ref<Record<string, number>>({})
  let taskEventSource: EventSource | null = null
  let pollingTimer: number | null = null
  let monitoringGeneration = 0

  const isEditable = computed(() => planStatus.value === 'completed' || planStatus.value === 'editing')
  const hasUnsavedChanges = computed(
    () =>
      planStatus.value === 'editing' &&
      originalPlan.value !== null &&
      JSON.stringify(currentPlan.value) !== JSON.stringify(originalPlan.value)
  )
  const versionCount = computed(() => versions.value.length)

  async function createPlan(request: TripPlanRequest) {
    loading.value = true
    planStatus.value = 'generating'
    error.value = null
    progress.value = 0
    progressStatus.value = '正在提交任务...'

    try {
      const task = await generateTripPlan(request)
      taskId.value = task.task_id
      taskStatus.value = task.status
      taskPhase.value = 'queued'
      progressStatus.value = '任务已进入队列'
      setStoredValue('activeTripTaskId', task.task_id)
      return task
    } catch (e) {
      error.value = e instanceof Error ? e.message : '生成计划失败'
      planStatus.value = 'idle'
      throw e
    } finally {
      loading.value = false
    }
  }

  function applyTaskStatus(status: TripPlanTaskStatusResponse) {
    taskId.value = status.task_id
    taskStatus.value = status.status
    taskPhase.value = status.phase
    progress.value = status.progress
    progressStatus.value = status.message
    taskElapsedMs.value = status.elapsed_ms
    taskPhaseElapsedMs.value = status.phase_elapsed_ms
    taskPhaseTimings.value = status.phase_timings
    if (UNSUCCESSFUL_TASK_STATUSES.has(status.status)) {
      error.value = status.error_message || status.message
      planStatus.value = 'idle'
    } else {
      error.value = null
      if (status.status !== 'succeeded') planStatus.value = 'generating'
    }
  }

  function applyPlan(plan: TripPlanResponse): void {
    currentPlan.value = plan
    planId.value = plan.plan_id
    setStoredValue('activePlanId', plan.plan_id)
    planStatus.value = plan.status
    originalPlan.value = null
  }

  function clearPlan(): void {
    currentPlan.value = null
    planId.value = null
    removeStoredValue('activePlanId')
    planStatus.value = 'idle'
    originalPlan.value = null
    versions.value = []
  }

  async function loadPlan(targetPlanId: string): Promise<TripPlanResponse> {
    loading.value = true
    error.value = null
    try {
      const plan = await getTripPlan(targetPlanId)
      applyPlan(plan)
      return plan
    } catch (e) {
      error.value = e instanceof Error ? e.message : '加载计划失败'
      throw e
    } finally {
      loading.value = false
    }
  }

  async function loadTaskResult(
    activeTaskId: string,
    shouldApply: () => boolean = () => true
  ): Promise<TripPlanResponse | null> {
    const result = await getTripPlanTaskResult(activeTaskId)
    if (!('plan_id' in result) || !shouldApply()) return null
    applyPlan(result)
    progress.value = 100
    progressStatus.value = '完成'
    versions.value = [{ version: result.version, change_summary: '初始创建', created_at: new Date().toISOString() }]
    removeStoredValue('activeTripTaskId')
    return result
  }

  function stopTaskMonitoring() {
    monitoringGeneration += 1
    taskEventSource?.close()
    taskEventSource = null
    if (pollingTimer !== null) {
      window.clearTimeout(pollingTimer)
      pollingTimer = null
    }
  }

  function monitorTask(activeTaskId: string, onCompleted: () => void): () => void {
    stopTaskMonitoring()
    const currentGeneration = monitoringGeneration
    error.value = null
    taskId.value = activeTaskId
    setStoredValue('activeTripTaskId', activeTaskId)
    let completionHandled = false
    let consecutivePollingFailures = 0
    let pollingEnabled = false

    const isCurrentMonitor = () => currentGeneration === monitoringGeneration

    const handleStatus = async (status: TripPlanTaskStatusResponse) => {
      if (!isCurrentMonitor()) return

      applyTaskStatus(status)
      if (status.status === 'succeeded' && !completionHandled) {
        completionHandled = true
        try {
          const result = await loadTaskResult(activeTaskId, isCurrentMonitor)
          if (result) {
            stopTaskMonitoring()
            onCompleted()
          } else {
            completionHandled = false
          }
        } catch (e) {
          completionHandled = false
          throw e
        }
      } else if (status.status === 'failed' || status.status === 'cancelled' || status.status === 'expired') {
        removeStoredValue('activeTripTaskId')
        stopTaskMonitoring()
      }
    }

    const refresh = async (): Promise<void> => {
      try {
        await handleStatus(await getTripPlanTask(activeTaskId))
        consecutivePollingFailures = 0
      } catch (e) {
        if (!isCurrentMonitor()) return
        consecutivePollingFailures += 1
        const failureMessage = e instanceof Error ? e.message : '查询任务状态失败'
        error.value =
          consecutivePollingFailures >= MAX_TASK_POLL_FAILURES
            ? `${failureMessage}，已停止自动重试`
            : failureMessage
        if (consecutivePollingFailures >= MAX_TASK_POLL_FAILURES) {
          stopTaskMonitoring()
        }
      }
    }

    function schedulePolling(): void {
      if (!pollingEnabled || !isCurrentMonitor() || pollingTimer !== null) return
      pollingTimer = window.setTimeout(() => void runPoll(), TASK_POLL_INTERVAL_MS)
    }

    async function runPoll(): Promise<void> {
      pollingTimer = null
      if (!pollingEnabled || !isCurrentMonitor()) return
      await refresh()
      schedulePolling()
    }

    const startPolling = (): void => {
      if (!isCurrentMonitor()) return
      pollingEnabled = true
      schedulePolling()
    }

    void refresh()
    const eventSource = createTripPlanTaskEventSource(activeTaskId)
    taskEventSource = eventSource
    const handleEvent = (event: MessageEvent<string>) => {
      if (!isCurrentMonitor()) return
      try {
        void handleStatus(JSON.parse(event.data) as TripPlanTaskStatusResponse).catch(e => {
          if (!isCurrentMonitor()) return
          error.value = e instanceof Error ? e.message : '读取任务结果失败'
          startPolling()
        })
      } catch (e) {
        error.value = e instanceof Error ? e.message : '解析任务进度失败'
        startPolling()
      }
    }
    eventSource.addEventListener('progress', handleEvent as EventListener)
    eventSource.addEventListener('completed', handleEvent as EventListener)
    eventSource.addEventListener('failed', handleEvent as EventListener)
    eventSource.addEventListener('timeout', (() => {
      if (!isCurrentMonitor()) return
      eventSource.close()
      if (taskEventSource === eventSource) taskEventSource = null
      startPolling()
    }) as EventListener)
    eventSource.onopen = () => {
      if (!isCurrentMonitor()) return
      pollingEnabled = false
      consecutivePollingFailures = 0
      if (pollingTimer !== null) {
        window.clearTimeout(pollingTimer)
        pollingTimer = null
      }
    }
    eventSource.onerror = startPolling
    return () => {
      if (isCurrentMonitor()) stopTaskMonitoring()
    }
  }

  function startEdit() {
    if (!currentPlan.value) return
    originalPlan.value = JSON.parse(JSON.stringify(currentPlan.value))
    planStatus.value = 'editing'
  }

  async function saveEdit(changeSummary = '手动编辑') {
    if (!currentPlan.value || !planId.value) return
    const planJson: TripPlan = currentPlan.value
    const result = await updateTripPlan(planId.value, { plan_json: planJson, change_summary: changeSummary })
    applyPlan(result)
    await loadVersions()
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
    const result = await getPlanVersions(planId.value)
    versions.value = result.versions
  }

  function applyExternalUpdate(updatedPlan: TripPlanResponse) {
    applyPlan(updatedPlan)
    versions.value = [
      {
        version: updatedPlan.version,
        change_summary: '对话调整',
        created_at: new Date().toISOString()
      },
      ...versions.value.filter(item => item.version !== updatedPlan.version)
    ]
  }

  async function revertToVersion(version: number) {
    if (!planId.value) return
    const result = await revertPlanVersion(planId.value, version)
    applyPlan(result)
    await loadVersions()
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
    const totalAttractions = plan.days.reduce(
      (sum, day) => sum + day.attractions.reduce((daySum, item) => daySum + (item.ticket_price || 0), 0),
      0
    )
    const totalMeals = plan.days.reduce(
      (sum, day) => sum + day.meals.reduce((daySum, meal) => daySum + meal.estimated_cost, 0),
      0
    )
    const firstHotel = plan.days.find(day => day.hotel)?.hotel
    const totalHotels = (firstHotel?.estimated_cost || 0) * Math.max(plan.days.length - 1, 0)
    const budget: Budget = {
      total_attractions: totalAttractions,
      total_hotels: totalHotels,
      total_meals: totalMeals,
      total_transportation: plan.budget?.total_transportation || 0,
      total: totalAttractions + totalHotels + totalMeals + (plan.budget?.total_transportation || 0)
    }
    plan.budget = budget
  }

  return {
    currentPlan,
    planId,
    planStatus,
    originalPlan,
    versions,
    loading,
    error,
    progress,
    progressStatus,
    taskId,
    taskStatus,
    taskPhase,
    taskElapsedMs,
    taskPhaseElapsedMs,
    taskPhaseTimings,
    isEditable,
    hasUnsavedChanges,
    versionCount,
    createPlan,
    loadPlan,
    clearPlan,
    monitorTask,
    stopTaskMonitoring,
    startEdit,
    saveEdit,
    cancelEdit,
    loadVersions,
    applyExternalUpdate,
    revertToVersion,
    moveAttraction,
    deleteAttraction,
    recalculateBudget
  }
})
