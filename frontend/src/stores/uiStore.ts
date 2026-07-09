import { ref } from 'vue'
import { defineStore } from 'pinia'

export const useUiStore = defineStore('ui', () => {
  const editMode = ref(false)
  const activeSection = ref('overview')
  const exportingState = ref<'idle' | 'exporting-image' | 'exporting-pdf'>('idle')
  const loadingProgress = ref(0)
  const loadingStatus = ref('')

  function setSection(section: string) {
    activeSection.value = section
  }
  function startExport(type: 'image' | 'pdf') {
    exportingState.value = type === 'image' ? 'exporting-image' : 'exporting-pdf'
  }
  function finishExport() {
    exportingState.value = 'idle'
  }
  function resetLoading() {
    loadingProgress.value = 0
    loadingStatus.value = ''
  }

  return {
    editMode,
    activeSection,
    exportingState,
    loadingProgress,
    loadingStatus,
    setSection,
    startExport,
    finishExport,
    resetLoading
  }
})
