import { nextTick, ref } from 'vue'
import html2canvas from 'html2canvas'
import jsPDF from 'jspdf'

export function useExport() {
  const exporting = ref(false)

  async function exportAsImage(elementId: string, filename: string) {
    const element = document.getElementById(elementId)
    if (!element) return
    exporting.value = true
    await nextTick()
    const canvas = await html2canvas(element, { backgroundColor: '#ffffff', scale: 2, useCORS: true })
    exporting.value = false
    const link = document.createElement('a')
    link.download = `${filename}.png`
    link.href = canvas.toDataURL('image/png')
    link.click()
  }

  async function exportAsPDF(elementId: string, filename: string) {
    const element = document.getElementById(elementId)
    if (!element) return
    exporting.value = true
    await nextTick()
    const canvas = await html2canvas(element, { backgroundColor: '#ffffff', scale: 2, useCORS: true })
    exporting.value = false
    const pdf = new jsPDF('p', 'mm', 'a4')
    const imgData = canvas.toDataURL('image/png')
    const imgWidth = 210
    const imgHeight = (canvas.height * imgWidth) / canvas.width
    pdf.addImage(imgData, 'PNG', 0, 0, imgWidth, imgHeight)
    pdf.save(`${filename}.pdf`)
  }

  return { exporting, exportAsImage, exportAsPDF }
}
