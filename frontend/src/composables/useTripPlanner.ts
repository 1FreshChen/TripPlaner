import type { Router } from 'vue-router'
import type { TripPlanRequest } from '../types'
import { useSessionStore } from '../stores/sessionStore'
import { useTripPlanStore } from '../stores/tripPlanStore'

export function useTripPlanner(router: Router) {
  const sessionStore = useSessionStore()
  const tripPlanStore = useTripPlanStore()

  async function submit(request: TripPlanRequest) {
    const session = await sessionStore.initSession()
    const task = await tripPlanStore.createPlan({ ...request, session_id: session.session_id })
    await router.push({ name: 'task', params: { taskId: task.task_id } })
  }

  return { submit, sessionStore, tripPlanStore }
}
