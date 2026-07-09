import axios from 'axios'
import type {
  PlanVersion,
  SavedItem,
  TripPlanResponse,
  TripPlanRequest,
  TripPlanUpdateRequest,
  UserPreferences
} from '../types'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api',
  timeout: 300000,
  headers: {
    'Content-Type': 'application/json'
  }
})

api.interceptors.response.use(
  response => response,
  error => {
    const message = error.response?.data?.detail || error.message || '请求失败'
    return Promise.reject(new Error(typeof message === 'string' ? message : JSON.stringify(message)))
  }
)

export const generateTripPlan = async (request: TripPlanRequest): Promise<TripPlanResponse> => {
  const response = await api.post<TripPlanResponse>('/trip/plan', request)
  return response.data
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

export const getPreferences = async (userId: string): Promise<UserPreferences> => {
  const response = await api.get<UserPreferences>('/preferences', { params: { user_id: userId } })
  return response.data
}

export const updatePreferences = async (
  userId: string,
  preferences: UserPreferences
): Promise<UserPreferences> => {
  const response = await api.put<UserPreferences>('/preferences', preferences, { params: { user_id: userId } })
  return response.data
}

export const saveItem = async (_userId: string, item: Omit<SavedItem, 'id' | 'created_at'>): Promise<SavedItem> => {
  return { ...item, id: crypto.randomUUID(), created_at: new Date().toISOString() }
}

export const unsaveItem = async (_userId: string, _itemId: string): Promise<void> => {}

export const getSavedItems = async (_userId: string): Promise<SavedItem[]> => []

export { api }
