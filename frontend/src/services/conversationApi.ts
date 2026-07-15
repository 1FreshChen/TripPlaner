import { api } from './api'
import type { ConversationListResponse, ConversationReply, SessionDetailResponse, SessionInfo } from '../types'

export const createSession = async (): Promise<SessionInfo> => {
  const response = await api.post<SessionInfo>('/sessions')
  return response.data
}

export const getSession = async (sessionId: string): Promise<SessionDetailResponse> => {
  const response = await api.get<SessionDetailResponse>(`/sessions/${sessionId}`)
  return response.data
}

export const sendMessage = async (
  sessionId: string,
  body: { message: string; referenced_plan_id?: string; apply_to_plan?: boolean }
): Promise<ConversationReply> => {
  const response = await api.post<ConversationReply>(`/conversation/${sessionId}`, body)
  return response.data
}

export const getConversation = async (
  sessionId: string,
  limit = 50,
  beforeId?: string
): Promise<ConversationListResponse> => {
  const response = await api.get<ConversationListResponse>(`/conversation/${sessionId}`, {
    params: { limit, before_id: beforeId }
  })
  return response.data
}
