import { ref } from 'vue'
import { defineStore } from 'pinia'
import { getPreferences, getSavedItems, saveItem, unsaveItem, updatePreferences } from '../services/api'
import type { SavedItem } from '../types'

export const usePreferencesStore = defineStore('preferences', () => {
  const preferredCategories = ref<string[]>([])
  const budgetProfile = ref<Record<string, number>>({})
  const travelStyle = ref('')
  const favoriteCities = ref<string[]>([])
  const savedItems = ref<SavedItem[]>([])
  const loaded = ref(false)

  async function load(sessionId: string) {
    if (loaded.value) return
    const prefs = await getPreferences(sessionId)
    preferredCategories.value = prefs.preferred_categories
    budgetProfile.value = prefs.budget_profile
    travelStyle.value = prefs.travel_style
    favoriteCities.value = prefs.favorite_cities
    savedItems.value = await getSavedItems(sessionId)
    loaded.value = true
  }

  async function save(sessionId: string) {
    await updatePreferences(sessionId, {
      preferred_categories: preferredCategories.value,
      budget_profile: budgetProfile.value,
      travel_style: travelStyle.value,
      favorite_cities: favoriteCities.value
    })
  }

  async function addSavedItem(sessionId: string, item: Omit<SavedItem, 'id' | 'created_at'>) {
    const result = await saveItem(sessionId, item)
    savedItems.value.unshift(result)
  }

  async function removeSavedItem(sessionId: string, itemId: string) {
    await unsaveItem(sessionId, itemId)
    savedItems.value = savedItems.value.filter(item => item.id !== itemId)
  }

  return {
    preferredCategories,
    budgetProfile,
    travelStyle,
    favoriteCities,
    savedItems,
    loaded,
    load,
    save,
    addSavedItem,
    removeSavedItem
  }
})
