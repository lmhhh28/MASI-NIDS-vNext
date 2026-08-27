import { createClient, type Client } from '@masi/control-api/client'

const maximumConcurrentRequests = 8
let inFlight = 0
const waiters: Array<() => void> = []

export const controlClient: Client = createClient({
  baseUrl: typeof window === 'undefined' ? 'http://127.0.0.1' : window.location.origin,
  credentials: 'same-origin',
  headers: {
    Accept: 'application/json',
  },
  responseStyle: 'fields',
  throwOnError: false,
})

async function acquireRequestSlot(): Promise<void> {
  if (inFlight < maximumConcurrentRequests) {
    inFlight += 1
    return
  }
  await new Promise<void>((resolve) => waiters.push(resolve))
  inFlight += 1
}

function releaseRequestSlot(): void {
  inFlight -= 1
  waiters.shift()?.()
}

export async function withRequestSlot<T>(operation: () => Promise<T>): Promise<T> {
  await acquireRequestSlot()
  try {
    return await operation()
  } finally {
    releaseRequestSlot()
  }
}

export function currentRequestCount(): number {
  return inFlight
}
