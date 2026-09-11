import { computed, onBeforeUnmount, ref, type Ref } from 'vue'
import AMapLoader from '@amap/amap-jsapi-loader'
import type { Attraction, TripPlan } from '../types'

interface MapInstance {
  add(marker: unknown): void
  destroy(): void
  setFitView(): void
}

interface MarkerInstance {
  on(event: 'click', handler: () => void): void
}

interface InfoWindowInstance {
  close(): void
  open(map: MapInstance, position: [number, number]): void
  setContent(content: HTMLElement): void
}

function safeImageUrl(value?: string): string | null {
  if (!value) return null
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.toString() : null
  } catch {
    return null
  }
}

function createInfoCard(attraction: Attraction): HTMLElement {
  const card = document.createElement('div')
  card.className = 'map-info-card'
  const imageUrl = safeImageUrl(attraction.image_url)
  if (imageUrl) {
    const image = document.createElement('img')
    image.className = 'map-info-image'
    image.src = imageUrl
    image.alt = `${attraction.name}景点图片`
    image.referrerPolicy = 'no-referrer'
    card.appendChild(image)
  } else {
    const placeholder = document.createElement('div')
    placeholder.className = 'map-info-image map-info-placeholder'
    placeholder.textContent = '暂无图片'
    card.appendChild(placeholder)
  }

  const body = document.createElement('div')
  body.className = 'map-info-body'
  const title = document.createElement('strong')
  title.textContent = attraction.name
  body.appendChild(title)
  const address = document.createElement('div')
  address.className = 'map-info-address'
  address.textContent = attraction.address || '暂无地址'
  body.appendChild(address)
  const provenance = document.createElement('div')
  provenance.className = 'map-info-provenance'
  const sourceLabels: Record<string, string> = {
    amap: '高德 POI',
    amap_mcp: '高德 MCP',
    amap_http: '高德 HTTP',
    llm: '模型生成',
    mock: '模拟数据'
  }
  const coordinateLabel = attraction.coordinate_verified ? '坐标已核验' : '坐标未核验'
  const imageLabel = attraction.image_source ? ` · 图片：${attraction.image_source}` : ''
  provenance.textContent = `${sourceLabels[attraction.data_source || ''] || attraction.data_source || '来源未知'} · ${coordinateLabel}${imageLabel}`
  body.appendChild(provenance)
  card.appendChild(body)
  return card
}

export function useMap(tripPlan: Ref<TripPlan | null>) {
  const mapReady = ref(false)
  let mapInstance: MapInstance | null = null
  let infoWindow: InfoWindowInstance | null = null
  let initializationSequence = 0

  const allAttractions = computed<Attraction[]>(() => {
    if (!tripPlan.value) return []
    return tripPlan.value.days.flatMap(day => day.attractions)
  })

  function destroyMap(): void {
    initializationSequence += 1
    infoWindow?.close()
    infoWindow = null
    mapInstance?.destroy()
    mapInstance = null
    mapReady.value = false
  }

  async function initMap(): Promise<void> {
    destroyMap()
    const currentSequence = initializationSequence
    if (!tripPlan.value || !allAttractions.value.length) return
    const amapKey = import.meta.env.VITE_AMAP_JS_KEY
    const securityCode = import.meta.env.VITE_AMAP_SECURITY_CODE
    if (!amapKey) {
      mapReady.value = false
      return
    }
    if (securityCode) {
      window._AMapSecurityConfig = { securityJsCode: securityCode }
    }
    try {
      const AMap = await AMapLoader.load({ key: amapKey, version: '2.0' })
      if (currentSequence !== initializationSequence) return
      const first = allAttractions.value[0].location
      const map = new AMap.Map('amap-container', {
        zoom: 12,
        center: [first.longitude, first.latitude]
      }) as MapInstance
      mapInstance = map
      infoWindow = new AMap.InfoWindow({
        isCustom: true,
        offset: new AMap.Pixel(0, -34)
      }) as InfoWindowInstance
      allAttractions.value.forEach((attraction, index) => {
        const position: [number, number] = [
          attraction.location.longitude,
          attraction.location.latitude
        ]
        const marker = new AMap.Marker({
          position,
          title: attraction.name,
          label: {
            content: `${index + 1}`,
            direction: 'top'
          }
        }) as MarkerInstance
        marker.on('click', () => {
          if (!infoWindow || mapInstance !== map) return
          infoWindow.setContent(createInfoCard(attraction))
          infoWindow.open(map, position)
        })
        map.add(marker)
      })
      map.setFitView()
      mapReady.value = true
    } catch {
      if (currentSequence === initializationSequence) {
        mapInstance?.destroy()
        mapInstance = null
        mapReady.value = false
      }
    }
  }

  onBeforeUnmount(destroyMap)

  return { allAttractions, mapReady, initMap }
}
