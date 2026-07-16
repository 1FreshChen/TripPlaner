<template>
  <main class="app-shell">
    <div class="workspace">
      <header class="topbar">
        <div class="brand">
          <MapPinned :size="30" />
          <h1>智能旅行助手</h1>
        </div>
        <a-space>
          <a-button @click="router.push({ name: 'history' })">历史</a-button>
          <a-button @click="router.push({ name: 'conversation' })">对话</a-button>
        </a-space>
      </header>

      <div class="planner-grid">
        <section class="panel">
          <h2>旅行需求</h2>
          <a-form layout="vertical" @submit.prevent="handleSubmit">
            <a-row :gutter="16">
              <a-col :xs="24" :md="12">
                <a-form-item label="目的地城市" required>
                  <a-input v-model:value="formData.city" placeholder="北京" size="large" />
                </a-form-item>
              </a-col>
              <a-col :xs="24" :md="12">
                <a-form-item label="旅行日期" required>
                  <a-range-picker v-model:value="dateRange" size="large" style="width: 100%" />
                </a-form-item>
              </a-col>
            </a-row>

            <a-form-item label="偏好">
              <a-select
                v-model:value="selectedPreferences"
                mode="multiple"
                size="large"
                :options="preferenceOptions"
                placeholder="历史文化"
              />
            </a-form-item>

            <a-row :gutter="16">
              <a-col :xs="24" :md="8">
                <a-form-item label="预算">
                  <a-segmented v-model:value="formData.budget" :options="budgetOptions" block />
                </a-form-item>
              </a-col>
              <a-col :xs="24" :md="8">
                <a-form-item label="交通">
                  <a-segmented v-model:value="formData.transportation" :options="transportationOptions" block />
                </a-form-item>
              </a-col>
              <a-col :xs="24" :md="8">
                <a-form-item label="住宿">
                  <a-select v-model:value="formData.accommodation" :options="accommodationOptions" size="large" />
                </a-form-item>
              </a-col>
            </a-row>

            <a-button type="primary" size="large" html-type="submit" :loading="loading">
              <template #icon><Sparkles :size="18" /></template>
              开始规划
            </a-button>

            <div v-if="loading" class="loading-box">
              <a-progress :percent="progress" />
              <a-typography-text type="secondary">{{ progressStatus }}</a-typography-text>
            </div>
          </a-form>
        </section>

        <aside class="panel">
          <h2>当前配置</h2>
          <div class="quick-facts">
            <div class="fact-row"><span>城市</span><strong>{{ formData.city || '未填写' }}</strong></div>
            <div class="fact-row"><span>天数</span><strong>{{ computedDays }} 天</strong></div>
            <div class="fact-row"><span>预算</span><strong>{{ formData.budget }}</strong></div>
            <div class="fact-row"><span>交通</span><strong>{{ formData.transportation }}</strong></div>
            <div class="fact-row"><span>住宿</span><strong>{{ formData.accommodation }}</strong></div>
          </div>
        </aside>
      </div>
    </div>
  </main>
</template>

<script setup lang="ts">
import dayjs, { Dayjs } from 'dayjs'
import { computed, ref } from 'vue'
import {
  Button as AButton,
  Col as ACol,
  Form as AForm,
  FormItem as AFormItem,
  Input as AInput,
  message,
  Progress as AProgress,
  RangePicker as ARangePicker,
  Row as ARow,
  Segmented as ASegmented,
  Select as ASelect,
  Space as ASpace,
  TypographyText as ATypographyText
} from 'ant-design-vue'
import { storeToRefs } from 'pinia'
import { MapPinned, Sparkles } from 'lucide-vue-next'
import { useRouter } from 'vue-router'
import { useTripPlanner } from '../composables/useTripPlanner'
import type { TripPlanRequest } from '../types'

const router = useRouter()
const { submit, tripPlanStore } = useTripPlanner(router)
const { loading, progress, progressStatus } = storeToRefs(tripPlanStore)
const today = dayjs().add(7, 'day')
const dateRange = ref<[Dayjs, Dayjs]>([today, today.add(2, 'day')])
const selectedPreferences = ref<string[]>(['历史文化'])

const formData = ref<TripPlanRequest>({
  city: '北京',
  start_date: today.format('YYYY-MM-DD'),
  end_date: today.add(2, 'day').format('YYYY-MM-DD'),
  days: 3,
  preferences: '历史文化',
  budget: '中等',
  transportation: '公共交通',
  accommodation: '经济型酒店'
})

const preferenceOptions = [
  { label: '历史文化', value: '历史文化' },
  { label: '自然风光', value: '自然风光' },
  { label: '美食', value: '美食' },
  { label: '城市漫步', value: '城市漫步' },
  { label: '亲子', value: '亲子' }
]

const budgetOptions = ['经济', '中等', '舒适', '高']
const transportationOptions = ['公共交通', '地铁', '打车', '自驾']
const accommodationOptions = [
  { label: '经济型酒店', value: '经济型酒店' },
  { label: '精品酒店', value: '精品酒店' },
  { label: '舒适型酒店', value: '舒适型酒店' },
  { label: '豪华酒店', value: '豪华酒店' }
]

const computedDays = computed(() => {
  if (!dateRange.value) return 1
  return dateRange.value[1].diff(dateRange.value[0], 'day') + 1
})

const syncRequestDates = () => {
  formData.value.start_date = dateRange.value[0].format('YYYY-MM-DD')
  formData.value.end_date = dateRange.value[1].format('YYYY-MM-DD')
  formData.value.days = computedDays.value
  formData.value.preferences = selectedPreferences.value.join(',') || '经典景点'
}

const handleSubmit = async () => {
  syncRequestDates()
  if (!formData.value.city.trim()) {
    message.warning('请填写目的地城市')
    return
  }
  if (formData.value.days < 1) {
    message.warning('旅行日期不正确')
    return
  }

  try {
    await submit(formData.value)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '生成计划失败')
  }
}
</script>
