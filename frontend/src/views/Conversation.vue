<template>
  <main class="app-shell">
    <div class="workspace">
      <header class="topbar">
        <div class="brand">
          <h1>对话调整</h1>
        </div>
        <a-button @click="router.push('/')">返回</a-button>
      </header>
      <section class="panel conversation-panel">
        <div class="message-list">
          <div v-for="(item, index) in messages" :key="`${item.id}-${index}`" class="message-row">
            <div class="message-meta">
              <strong>{{ item.role === 'user' ? '我' : '助手' }}</strong>
              <a-tag v-if="item._planUpdated" color="success">行程已更新</a-tag>
            </div>
            <span class="message-content">{{ item.content }}</span>
          </div>
        </div>
        <a-input-search
          v-model:value="draft"
          enter-button="发送"
          placeholder="例如：第二天少去一个博物馆，多安排公园"
          @search="handleSend"
        />
      </section>
    </div>
  </main>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import { useSessionStore } from '../stores/sessionStore'
import { useTripPlanStore } from '../stores/tripPlanStore'

const router = useRouter()
const sessionStore = useSessionStore()
const tripPlanStore = useTripPlanStore()
const { messages } = storeToRefs(sessionStore)
const draft = ref('')

const handleSend = async () => {
  const content = draft.value.trim()
  if (!content) return
  draft.value = ''
  try {
    const reply = await sessionStore.send(content, tripPlanStore.planId || undefined)
    if (reply.updated_plan) {
      message.success('行程已更新')
    }
  } catch (error) {
    message.error(error instanceof Error ? error.message : '发送失败')
  }
}

onMounted(async () => {
  await sessionStore.initSession()
  await sessionStore.loadHistory()
})
</script>
