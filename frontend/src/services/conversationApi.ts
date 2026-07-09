import { api } from './api'
import type { ConversationMessage, ConversationReply, SessionDetailResponse, SessionInfo } from '../types'

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
  body: { message: string; referenced_plan_id?: string }
): Promise<ConversationReply> => {
  const response = await api.post<ConversationReply>(`/conversation/${sessionId}`, body)
  return response.data
}

export const getConversation = async (
  sessionId: string,
  limit = 50
): Promise<{ messages: ConversationMessage[]; has_more: boolean }> => {
  const response = await api.get<{ messages: ConversationMessage[]; has_more: boolean }>(
    `/conversation/${sessionId}`,
    { params: { limit } }
  )
  return response.data
}
