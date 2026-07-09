import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import type { Budget, PlanStatus, PlanVersion, TripPlan, TripPlanRequest, TripPlanResponse } from '../types'
import { generateTripPlan, getPlanVersions, revertPlanVersion, updateTripPlan } from '../services/api'

export const useTripPlanStore = defineStore('tripPlan', () => {
  const currentPlan = ref<TripPlanResponse | null>(null)
  const planId = ref<string | null>(null)
  const planStatus = ref<PlanStatus>('idle')
  const originalPlan = ref<TripPlanResponse | null>(null)
  const versions = ref<PlanVersion[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const progress = ref(0)
  const progressStatus = ref('')

  const isEditable = computed(() => planStatus.value === 'completed' || planStatus.value === 'editing')
  const hasUnsavedChanges = computed(
    () => planStatus.value === 'editing' && JSON.stringify(currentPlan.value) !== JSON.stringify(originalPlan.value)
  )
  const versionCount = computed(() => versions.value.length)

  async function createPlan(request: TripPlanRequest) {
    loading.value = true
    planStatus.value = 'generating'
    error.value = null
    progress.value = 0
    progressStatus.value = '正在搜索景点...'
    const interval = window.setInterval(() => {
      if (progress.value < 90) {
        progress.value += 10
        if (progress.value <= 30) progressStatus.value = '正在搜索景点...'
        else if (progress.value <= 55) progressStatus.value = '正在查询天气...'
        else if (progress.value <= 75) progressStatus.value = '正在推荐酒店...'
        else progressStatus.value = '正在生成行程...'
      }
    }, 300)

    try {
      const result = await generateTripPlan(request)
      currentPlan.value = result
      planId.value = result.plan_id
      planStatus.value = result.status
      progress.value = 100
      progressStatus.value = '完成'
      versions.value = [{ version: result.version, change_summary: '初始创建', created_at: new Date().toISOString() }]
      return result
    } catch (e) {
      error.value = e instanceof Error ? e.message : '生成计划失败'
      planStatus.value = 'idle'
      throw e
    } finally {
      window.clearInterval(interval)
      loading.value = false
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
    currentPlan.value = result
    planStatus.value = result.status
    originalPlan.value = null
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

  async function revertToVersion(version: number) {
    if (!planId.value) return
    const result = await revertPlanVersion(planId.value, version)
    currentPlan.value = result
    planStatus.value = result.status
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
    isEditable,
    hasUnsavedChanges,
    versionCount,
    createPlan,
    startEdit,
    saveEdit,
    cancelEdit,
    loadVersions,
    revertToVersion,
    moveAttraction,
    deleteAttraction,
    recalculateBudget
  }
})
