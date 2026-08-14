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
import { getStoredValue } from '../utils/storage'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api'

const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 300000,
  headers: {
    'Content-Type': 'application/json'
  }
})

api.interceptors.request.use(config => {
  const sessionId = getStoredValue('session_id')
  if (sessionId) config.headers.set('X-Session-ID', sessionId)
  return config
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

type EventStreamListener = EventListenerOrEventListenerObject

class FetchEventStream {
  onopen: ((event: Event) => void) | null = null
  onerror: ((event: Event) => void) | null = null

  private readonly controller = new AbortController()
  private readonly listeners = new Map<string, Set<EventStreamListener>>()

  constructor(url: string, sessionId: string) {
    void this.connect(url, sessionId)
  }

  addEventListener(type: string, listener: EventStreamListener): void {
    const listeners = this.listeners.get(type) ?? new Set<EventStreamListener>()
    listeners.add(listener)
    this.listeners.set(type, listeners)
  }

  close(): void {
    this.controller.abort()
  }

  private dispatch(type: string, data: string): void {
    const event = new MessageEvent(type, { data })
    for (const listener of this.listeners.get(type) ?? []) {
      if (typeof listener === 'function') listener(event)
      else listener.handleEvent(event)
    }
  }

  private async connect(url: string, sessionId: string): Promise<void> {
    try {
      const response = await fetch(url, {
        headers: {
          Accept: 'text/event-stream',
          'X-Session-ID': sessionId
        },
        signal: this.controller.signal
      })
      if (!response.ok || !response.body) {
        throw new ApiError(`进度连接失败 (${response.status})`, response.status)
      }

      this.onopen?.(new Event('open'))
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (!this.controller.signal.aborted) {
        const { done, value } = await reader.read()
        buffer += decoder.decode(value, { stream: !done })
        const blocks = buffer.split(/\r?\n\r?\n/)
        buffer = blocks.pop() ?? ''
        for (const block of blocks) this.parseBlock(block)
        if (done) break
      }

      if (!this.controller.signal.aborted) this.onerror?.(new Event('error'))
    } catch (error) {
      if (this.controller.signal.aborted) return
      console.warn('Task event stream failed; polling will be used', error)
      this.onerror?.(new Event('error'))
    }
  }

  private parseBlock(block: string): void {
    if (!block || block.startsWith(':')) return
    let eventType = 'message'
    const data: string[] = []
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith('event:')) eventType = line.slice(6).trim()
      else if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
    }
    if (data.length) this.dispatch(eventType, data.join('\n'))
  }
}

export const createTripPlanTaskEventSource = (taskId: string): FetchEventStream => {
  const sessionId = getStoredValue('session_id')
  if (!sessionId) throw new ApiError('会话不存在，请刷新页面后重试', 401)
  return new FetchEventStream(`${API_BASE_URL}/trip/tasks/${encodeURIComponent(taskId)}/events`, sessionId)
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
