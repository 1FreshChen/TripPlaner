<template>
  <main class="result-shell">
    <div class="workspace">
      <header class="topbar">
        <div class="brand">
          <MapPinned :size="30" />
          <h1>{{ tripPlan?.city || '旅行计划' }}</h1>
        </div>
        <a-space wrap>
          <a-button @click="router.push('/')">
            <template #icon><ArrowLeft :size="16" /></template>
            返回
          </a-button>
          <a-button v-if="!editMode && tripPlan" type="primary" @click="startEdit">
            <template #icon><Edit3 :size="16" /></template>
            编辑行程
          </a-button>
          <a-button v-if="editMode" type="primary" @click="saveChanges">
            <template #icon><Save :size="16" /></template>
            保存
          </a-button>
          <a-button v-if="editMode" @click="cancelEdit">
            <template #icon><X :size="16" /></template>
            取消
          </a-button>
          <a-button v-if="tripPlan" @click="router.push({ name: 'conversation' })">
            对话调整
          </a-button>
          <a-dropdown>
            <a-button>
              <template #icon><Download :size="16" /></template>
              导出行程
            </a-button>
            <template #overlay>
              <a-menu>
                <a-menu-item key="image" @click="exportAsImage">
                  <FileImage :size="15" /> 导出图片
                </a-menu-item>
                <a-menu-item key="pdf" @click="exportAsPDF">
                  <FileText :size="15" /> 导出 PDF
                </a-menu-item>
              </a-menu>
            </template>
          </a-dropdown>
        </a-space>
      </header>

      <div v-if="!tripPlan" class="empty-state">
        <a-empty description="暂无行程数据" />
      </div>

      <div v-else class="result-layout">
        <aside class="side-nav">
          <a-menu :selected-keys="[activeSection]" @click="scrollToSection">
            <a-menu-item key="overview">行程概览</a-menu-item>
            <a-menu-item key="budget">预算明细</a-menu-item>
            <a-menu-item key="map">地图</a-menu-item>
            <a-menu-item key="itinerary">每日行程</a-menu-item>
            <a-menu-item key="weather">天气</a-menu-item>
          </a-menu>
        </aside>

        <div id="trip-plan-content" class="content-stack" :class="{ exporting }">
          <section id="overview" class="section-band">
            <h2 class="section-title">行程概览</h2>
            <div class="overview-grid">
              <div class="metric"><span>目的地</span><strong>{{ tripPlan.city }}</strong></div>
              <div class="metric"><span>开始日期</span><strong>{{ tripPlan.start_date }}</strong></div>
              <div class="metric"><span>结束日期</span><strong>{{ tripPlan.end_date }}</strong></div>
              <div class="metric"><span>天数</span><strong>{{ tripPlan.days.length }} 天</strong></div>
            </div>
            <a-divider />
            <a-typography-paragraph>{{ tripPlan.overall_suggestions }}</a-typography-paragraph>
          </section>

          <section id="budget" class="section-band">
            <h2 class="section-title">预算明细</h2>
            <div v-if="tripPlan.budget" class="budget-grid">
              <div class="metric"><span>景点门票</span><strong>¥{{ tripPlan.budget.total_attractions }}</strong></div>
              <div class="metric"><span>酒店住宿</span><strong>¥{{ tripPlan.budget.total_hotels }}</strong></div>
              <div class="metric"><span>餐饮费用</span><strong>¥{{ tripPlan.budget.total_meals }}</strong></div>
              <div class="metric"><span>交通费用</span><strong>¥{{ tripPlan.budget.total_transportation }}</strong></div>
              <div class="metric"><span>总计</span><strong>¥{{ tripPlan.budget.total }}</strong></div>
            </div>
          </section>

          <section id="map" class="section-band map-section">
            <h2 class="section-title">地图</h2>
            <div class="map-panel">
              <div v-show="mapReady" id="amap-container"></div>
              <div v-if="!mapReady" class="fallback-map">
                <div v-for="(attraction, index) in allAttractions" :key="`${attraction.name}-${index}`" class="map-pin">
                  <strong>{{ index + 1 }}. {{ attraction.name }}</strong>
                  <span>{{ attraction.location.longitude.toFixed(4) }}, {{ attraction.location.latitude.toFixed(4) }}</span>
                </div>
              </div>
            </div>
          </section>

          <section id="itinerary" class="section-band">
            <h2 class="section-title">每日行程</h2>
            <div class="day-list">
              <article v-for="(day, dayIndex) in tripPlan.days" :key="day.date" class="day-card">
                <div class="day-head">
                  <div>
                    <h3>第 {{ day.day_index + 1 }} 天 · {{ day.date }}</h3>
                    <a-typography-text type="secondary">{{ day.description }}</a-typography-text>
                  </div>
                  <a-tag color="blue">{{ day.transportation }}</a-tag>
                </div>

                <a-descriptions size="small" :column="1" bordered>
                  <a-descriptions-item label="住宿">
                    {{ day.hotel?.name || day.accommodation }}
                  </a-descriptions-item>
                </a-descriptions>

                <a-divider orientation="left">景点</a-divider>
                <ul class="attraction-list">
                  <li
                    v-for="(attraction, attractionIndex) in day.attractions"
                    :key="`${day.date}-${attraction.name}-${attractionIndex}`"
                    class="attraction-item"
                  >
                    <div>
                      <div class="item-title">
                        <span>{{ attraction.name }}</span>
                        <a-tag>{{ attraction.category || '景点' }}</a-tag>
                        <a-tag color="green">¥{{ attraction.ticket_price || 0 }}</a-tag>
                      </div>
                      <div class="item-meta">{{ attraction.address }} · {{ attraction.visit_duration }} 分钟</div>
                    </div>
                    <div v-if="editMode" class="inline-actions">
                      <a-tooltip title="上移">
                        <a-button
                          size="small"
                          :disabled="attractionIndex === 0"
                          @click="moveAttraction(dayIndex, attractionIndex, 'up')"
                        >
                          <ArrowUp :size="14" />
                        </a-button>
                      </a-tooltip>
                      <a-tooltip title="下移">
                        <a-button
                          size="small"
                          :disabled="attractionIndex === day.attractions.length - 1"
                          @click="moveAttraction(dayIndex, attractionIndex, 'down')"
                        >
                          <ArrowDown :size="14" />
                        </a-button>
                      </a-tooltip>
                      <a-tooltip title="删除">
                        <a-button size="small" danger @click="deleteAttraction(dayIndex, attractionIndex)">
                          <Trash2 :size="14" />
                        </a-button>
                      </a-tooltip>
                    </div>
                  </li>
                </ul>

                <a-divider orientation="left">餐饮</a-divider>
                <ul class="meal-list">
                  <li v-for="meal in day.meals" :key="`${day.date}-${meal.type}`" class="meal-item">
                    <div class="meal-info">
                      <div class="item-title">
                        {{ meal.name }}
                        <a-rate
                          v-if="meal.rating"
                          :value="meal.rating"
                          :count="5"
                          disabled
                          :style="{ fontSize: '14px', marginLeft: '8px' }"
                          allow-half
                        />
                        <span v-if="meal.rating" class="rating-text">{{ meal.rating }}</span>
                      </div>
                      <div class="item-meta">
                        <span v-if="meal.address">{{ meal.address }}</span>
                        <span v-if="meal.shop_hours" class="meta-tag">🕐 {{ meal.shop_hours }}</span>
                        <span v-if="meal.comment_num" class="meta-tag">💬 {{ meal.comment_num }}条评论</span>
                      </div>
                      <div v-if="meal.description" class="item-desc">{{ meal.description }}</div>
                    </div>
                    <div class="meal-cost">
                      <a-tag v-if="meal.price_per_person" color="blue">人均 ¥{{ meal.price_per_person }}</a-tag>
                      <a-tag color="orange">预算 ¥{{ meal.estimated_cost }}</a-tag>
                    </div>
                  </li>
                </ul>
              </article>
            </div>
          </section>

          <section id="weather" class="section-band">
            <h2 class="section-title">天气</h2>
            <div class="weather-grid">
              <div v-for="weather in tripPlan.weather_info" :key="weather.date" class="weather-cell">
                <strong>{{ weather.date }}</strong>
                <div>白天 {{ weather.day_weather }} · {{ weather.day_temp }}℃</div>
                <div>夜间 {{ weather.night_weather }} · {{ weather.night_temp }}℃</div>
                <div>{{ weather.wind_direction }}风 {{ weather.wind_power }}</div>
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>
  </main>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import { storeToRefs } from 'pinia'
import {
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  Download,
  Edit3,
  FileImage,
  FileText,
  MapPinned,
  Save,
  Trash2,
  X
} from 'lucide-vue-next'
import { useRouter } from 'vue-router'
import { useExport } from '../composables/useExport'
import { useMap } from '../composables/useMap'
import { useTripPlanStore } from '../stores/tripPlanStore'

const router = useRouter()
const tripPlanStore = useTripPlanStore()
const { currentPlan: tripPlan } = storeToRefs(tripPlanStore)
const editMode = computed(() => tripPlanStore.planStatus === 'editing')
const activeSection = computed(() => 'overview')
const { exporting, exportAsImage: exportImage, exportAsPDF: exportPDF } = useExport()
const { allAttractions, mapReady, initMap } = useMap(tripPlan)

const startEdit = () => {
  tripPlanStore.startEdit()
}

const saveChanges = async () => {
  if (!tripPlan.value) return
  tripPlanStore.recalculateBudget()
  await tripPlanStore.saveEdit('手动编辑')
  message.success('修改已保存')
  await nextTick()
  initMap()
}

const cancelEdit = () => {
  tripPlanStore.cancelEdit()
}

const moveAttraction = (dayIndex: number, attractionIndex: number, direction: 'up' | 'down') => {
  tripPlanStore.moveAttraction(dayIndex, attractionIndex, direction)
}

const deleteAttraction = (dayIndex: number, attractionIndex: number) => {
  tripPlanStore.deleteAttraction(dayIndex, attractionIndex)
}

const scrollToSection = ({ key }: { key: string }) => {
  document.getElementById(key)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

const exportAsImage = async () => {
  if (!tripPlan.value) return
  await exportImage('trip-plan-content', `${tripPlan.value.city}旅行计划`)
}

const exportAsPDF = async () => {
  if (!tripPlan.value) return
  await exportPDF('trip-plan-content', `${tripPlan.value.city}旅行计划`)
}

onMounted(() => {
  initMap()
})
</script>
