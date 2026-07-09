import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const apiSource = readFileSync(resolve(__dirname, '../src/services/api.ts'), 'utf8')
const match = apiSource.match(/timeout:\s*(\d+)/)

if (!match) {
  throw new Error('axios timeout is not configured in src/services/api.ts')
}

const timeoutMs = Number(match[1])
const expectedTimeoutMs = 300000

if (timeoutMs !== expectedTimeoutMs) {
  throw new Error(`expected axios timeout ${expectedTimeoutMs}ms, got ${timeoutMs}ms`)
}

console.log(`api timeout is ${timeoutMs}ms`)
