import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { createSession, getConversation, getSession, sendMessage } from '../services/conversationApi'
import type { ConversationMessage } from '../types'

export const useSessionStore = defineStore('session', () => {
  const sessionId = ref<string>(localStorage.getItem('session_id') || '')
  const userId = ref<string | null>(null)
  const messages = ref<ConversationMessage[]>([])
  const isActive = ref(false)

  const messageCount = computed(() => messages.value.length)

  async function initSession() {
    if (sessionId.value) {
      try {
        const session = await getSession(sessionId.value)
        userId.value = session.user_id
        isActive.value = true
        return session
      } catch {
        sessionId.value = ''
        localStorage.removeItem('session_id')
      }
    }
    const session = await createSession()
    sessionId.value = session.session_id
    userId.value = session.user_id
    isActive.value = true
    localStorage.setItem('session_id', session.session_id)
    return session
  }

  async function loadHistory() {
    if (!sessionId.value) return
    const result = await getConversation(sessionId.value)
    messages.value = result.messages
  }

  async function send(content: string, planId?: string) {
    if (!sessionId.value) await initSession()
    messages.value.push({ id: '', role: 'user', content, created_at: new Date().toISOString() })
    const reply = await sendMessage(sessionId.value, { message: content, referenced_plan_id: planId })
    messages.value.push({
      id: reply.message_id,
      role: reply.role,
      content: reply.content,
      tool_calls: reply.tool_calls,
      created_at: new Date().toISOString()
    })
    return reply
  }

  return { sessionId, userId, messages, isActive, messageCount, initSession, loadHistory, send }
})
