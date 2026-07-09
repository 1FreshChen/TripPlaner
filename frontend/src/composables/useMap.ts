import { computed, ref, type Ref } from 'vue'
import AMapLoader from '@amap/amap-jsapi-loader'
import type { Attraction, TripPlan } from '../types'

export function useMap(tripPlan: Ref<TripPlan | null>) {
  const mapReady = ref(false)
  let mapInstance: any = null

  const allAttractions = computed<Attraction[]>(() => {
    if (!tripPlan.value) return []
    return tripPlan.value.days.flatMap(day => day.attractions)
  })

  async function initMap() {
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
      const first = allAttractions.value[0].location
      mapInstance = new AMap.Map('amap-container', {
        zoom: 12,
        center: [first.longitude, first.latitude]
      })
      allAttractions.value.forEach((attraction, index) => {
        const marker = new AMap.Marker({
          position: [attraction.location.longitude, attraction.location.latitude],
          title: attraction.name,
          label: {
            content: `${index + 1}`,
            direction: 'top'
          }
        })
        mapInstance.add(marker)
      })
      mapInstance.setFitView()
      mapReady.value = true
    } catch {
      mapReady.value = false
    }
  }

  return { allAttractions, mapReady, initMap }
}
