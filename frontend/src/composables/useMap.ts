import { computed, onBeforeUnmount, ref, type Ref } from 'vue'
import AMapLoader from '@amap/amap-jsapi-loader'
import type { Attraction, TripPlan } from '../types'

interface MapInstance {
  add(marker: unknown): void
  destroy(): void
  setFitView(): void
}

export function useMap(tripPlan: Ref<TripPlan | null>) {
  const mapReady = ref(false)
  let mapInstance: MapInstance | null = null
  let initializationSequence = 0

  const allAttractions = computed<Attraction[]>(() => {
    if (!tripPlan.value) return []
    return tripPlan.value.days.flatMap(day => day.attractions)
  })

  function destroyMap(): void {
    initializationSequence += 1
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
      allAttractions.value.forEach((attraction, index) => {
        const marker = new AMap.Marker({
          position: [attraction.location.longitude, attraction.location.latitude],
          title: attraction.name,
          label: {
            content: `${index + 1}`,
            direction: 'top'
          }
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
