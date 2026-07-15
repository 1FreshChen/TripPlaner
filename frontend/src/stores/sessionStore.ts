import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { createSession, getConversation, getSession, sendMessage } from '../services/conversationApi'
import { ApiError } from '../services/api'
import type { ConversationMessage, ConversationReply, SessionInfo } from '../types'
import { getStoredValue, removeStoredValue, setStoredValue } from '../utils/storage'
import { useTripPlanStore } from './tripPlanStore'

export const useSessionStore = defineStore('session', () => {
  const sessionId = ref<string>(getStoredValue('session_id') || '')
  const userId = ref<string | null>(null)
  const messages = ref<ConversationMessage[]>([])
  const isActive = ref(false)
  const sending = ref(false)
  const historyLoading = ref(false)
  const hasMore = ref(false)

  const messageCount = computed(() => messages.value.length)

  function mapMessage(item: ConversationMessage): ConversationMessage {
    return {
      ...item,
      _planUpdated: Boolean(item.plan_updated),
      _planUpdateFailed: Boolean(item.plan_update_failed)
    }
  }

  async function initSession(): Promise<SessionInfo> {
    if (sessionId.value) {
      try {
        const session = await getSession(sessionId.value)
        userId.value = session.user_id
        isActive.value = true
        return session
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 404) throw error
        console.warn('Stored session no longer exists; creating a new session', error)
        sessionId.value = ''
        userId.value = null
        isActive.value = false
        removeStoredValue('session_id')
      }
    }
    const session = await createSession()
    sessionId.value = session.session_id
    userId.value = session.user_id
    isActive.value = true
    setStoredValue('session_id', session.session_id)
    return session
  }

  async function loadHistory(): Promise<void> {
    if (!sessionId.value) {
      messages.value = []
      hasMore.value = false
      return
    }
    historyLoading.value = true
    try {
      const result = await getConversation(sessionId.value)
      messages.value = result.messages.map(mapMessage)
      hasMore.value = result.has_more
    } finally {
      historyLoading.value = false
    }
  }

  async function loadMoreHistory(): Promise<void> {
    const beforeId = messages.value[0]?.id
    if (!sessionId.value || !beforeId || !hasMore.value || historyLoading.value) return
    historyLoading.value = true
    try {
      const result = await getConversation(sessionId.value, 50, beforeId)
      const existingIds = new Set(messages.value.map(item => item.id))
      const olderMessages = result.messages.map(mapMessage).filter(item => !existingIds.has(item.id))
      messages.value = [...olderMessages, ...messages.value]
      hasMore.value = result.has_more
    } finally {
      historyLoading.value = false
    }
  }

  async function send(content: string, planId?: string): Promise<ConversationReply> {
    if (sending.value) throw new Error('消息正在发送，请稍候')
    sending.value = true
    let optimisticId = ''
    try {
      if (!sessionId.value) await initSession()
      optimisticId = `pending-${crypto.randomUUID()}`
      messages.value.push({
        id: optimisticId,
        role: 'user',
        content,
        created_at: new Date().toISOString()
      })
      const reply = await sendMessage(sessionId.value, {
        message: content,
        referenced_plan_id: planId,
        apply_to_plan: Boolean(planId)
      })
      if (reply.updated_plan) {
        useTripPlanStore().applyExternalUpdate(reply.updated_plan)
      }
      messages.value.push({
        id: reply.message_id,
        role: reply.role,
        content: reply.content,
        tool_calls: reply.tool_calls,
        created_at: new Date().toISOString(),
        _planUpdated: Boolean(reply.updated_plan),
        _planUpdateFailed: Boolean(reply.plan_update_failed)
      })
      return reply
    } catch (error) {
      if (optimisticId) {
        messages.value = messages.value.filter(item => item.id !== optimisticId)
      }
      throw error
    } finally {
      sending.value = false
    }
  }

  return {
    sessionId,
    userId,
    messages,
    isActive,
    sending,
    historyLoading,
    hasMore,
    messageCount,
    initSession,
    loadHistory,
    loadMoreHistory,
    send
  }
})
