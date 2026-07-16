function reportStorageError(operation: string, key: string, error: unknown): void {
  console.warn(`localStorage ${operation} failed for "${key}"`, error)
}

export function getStoredValue(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch (error) {
    reportStorageError('read', key, error)
    return null
  }
}

export function setStoredValue(key: string, value: string): boolean {
  try {
    window.localStorage.setItem(key, value)
    return true
  } catch (error) {
    reportStorageError('write', key, error)
    return false
  }
}

export function removeStoredValue(key: string): boolean {
  try {
    window.localStorage.removeItem(key)
    return true
  } catch (error) {
    reportStorageError('remove', key, error)
    return false
  }
}
