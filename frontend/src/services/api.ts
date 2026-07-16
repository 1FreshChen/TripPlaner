import axios from 'axios'
import type {
  PlanVersion,
  SavedItem,
  TripPlanTaskCreatedResponse,
  TripPlanTaskPendingResult,
  TripPlanTaskStatusResponse,
  TripPlanResponse,
  TripPlanRequest,
  TripPlanUpdateRequest,
  UserPreferences
} from '../types'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api'

const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 300000,
  headers: {
    'Content-Type': 'application/json'
  }
})

export class ApiError extends Error {
  readonly status?: number
  readonly code?: string
  readonly details?: unknown

  constructor(message: string, status?: number, code?: string, details?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }
}

api.interceptors.response.use(
  response => response,
  (error: unknown) => {
    if (axios.isAxiosError(error)) {
      const detail = (error.response?.data as { detail?: unknown } | undefined)?.detail
      const message = detail ?? error.message ?? '请求失败'
      return Promise.reject(
        new ApiError(
          typeof message === 'string' ? message : JSON.stringify(message),
          error.response?.status,
          error.code,
          error.response?.data
        )
      )
    }
    return Promise.reject(error instanceof Error ? error : new ApiError('请求失败'))
  }
)

export const generateTripPlan = async (request: TripPlanRequest): Promise<TripPlanTaskCreatedResponse> => {
  const response = await api.post<TripPlanTaskCreatedResponse>('/trip/plan', request)
  return response.data
}

export const getTripPlanTask = async (taskId: string): Promise<TripPlanTaskStatusResponse> => {
  const response = await api.get<TripPlanTaskStatusResponse>(`/trip/tasks/${taskId}`)
  return response.data
}

export const getTripPlanTaskResult = async (
  taskId: string
): Promise<TripPlanResponse | TripPlanTaskPendingResult> => {
  const response = await api.get<TripPlanResponse | TripPlanTaskPendingResult>(`/trip/tasks/${taskId}/result`)
  return response.data
}

export const createTripPlanTaskEventSource = (taskId: string): EventSource => {
  return new EventSource(`${API_BASE_URL}/trip/tasks/${encodeURIComponent(taskId)}/events`)
}

export const getTripPlan = async (planId: string): Promise<TripPlanResponse> => {
  const response = await api.get<TripPlanResponse>(`/trip/plan/${planId}`)
  return response.data
}

export const updateTripPlan = async (
  planId: string,
  request: TripPlanUpdateRequest
): Promise<TripPlanResponse> => {
  const response = await api.put<TripPlanResponse>(`/trip/plan/${planId}`, request)
  return response.data
}

export const getPlanVersions = async (planId: string): Promise<{ versions: PlanVersion[] }> => {
  const response = await api.get<{ versions: PlanVersion[] }>(`/trip/plan/${planId}/versions`)
  return response.data
}

export const revertPlanVersion = async (planId: string, version: number): Promise<TripPlanResponse> => {
  const response = await api.post<TripPlanResponse>(`/trip/plan/${planId}/revert/${version}`)
  return response.data
}

export const archiveTripPlan = async (planId: string): Promise<void> => {
  await api.delete(`/trip/plan/${planId}`)
}

export const getPreferences = async (sessionId: string): Promise<UserPreferences> => {
  const response = await api.get<UserPreferences>('/preferences', { params: { session_id: sessionId } })
  return response.data
}

export const updatePreferences = async (
  sessionId: string,
  preferences: UserPreferences
): Promise<UserPreferences> => {
  const response = await api.put<UserPreferences>('/preferences', preferences, { params: { session_id: sessionId } })
  return response.data
}

export const saveItem = async (
  sessionId: string,
  item: Omit<SavedItem, 'id' | 'created_at'>
): Promise<SavedItem> => {
  const response = await api.post<SavedItem>('/saved-items', item, { params: { session_id: sessionId } })
  return response.data
}

export const unsaveItem = async (sessionId: string, itemId: string): Promise<void> => {
  await api.delete(`/saved-items/${encodeURIComponent(itemId)}`, { params: { session_id: sessionId } })
}

export const getSavedItems = async (sessionId: string): Promise<SavedItem[]> => {
  const response = await api.get<SavedItem[]>('/saved-items', { params: { session_id: sessionId } })
  return response.data
}

export { api }
