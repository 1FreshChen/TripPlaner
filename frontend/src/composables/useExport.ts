import { nextTick, ref } from 'vue'

export function useExport() {
  const exporting = ref(false)

  async function exportAsImage(elementId: string, filename: string): Promise<void> {
    const element = document.getElementById(elementId)
    if (!element || exporting.value) return
    exporting.value = true
    try {
      await nextTick()
      const { default: html2canvas } = await import('html2canvas')
      const canvas = await html2canvas(element, { backgroundColor: '#ffffff', scale: 2, useCORS: true })
      const link = document.createElement('a')
      link.download = `${filename}.png`
      link.href = canvas.toDataURL('image/png')
      link.click()
    } finally {
      exporting.value = false
    }
  }

  async function exportAsPDF(elementId: string, filename: string): Promise<void> {
    const element = document.getElementById(elementId)
    if (!element || exporting.value) return
    exporting.value = true
    try {
      await nextTick()
      const [{ default: html2canvas }, { jsPDF }] = await Promise.all([
        import('html2canvas'),
        import('jspdf')
      ])
      const canvas = await html2canvas(element, { backgroundColor: '#ffffff', scale: 2, useCORS: true })
      const pdf = new jsPDF('p', 'mm', 'a4')
      const imgData = canvas.toDataURL('image/png')
      const imgWidth = 210
      const imgHeight = (canvas.height * imgWidth) / canvas.width
      pdf.addImage(imgData, 'PNG', 0, 0, imgWidth, imgHeight)
      pdf.save(`${filename}.pdf`)
    } finally {
      exporting.value = false
    }
  }

  return { exporting, exportAsImage, exportAsPDF }
}
