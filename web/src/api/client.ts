import { createClient, type Client } from '@masi/control-api/client'

const maximumConcurrentRequests = 8
const maximumPendingRequests = 128
const admissionTimeoutMS = 5_000
const requestTimeoutMS = 15_000
const maximumResponseBytes = 4_194_304
const nativeRequest = globalThis.fetch.bind(globalThis)
let inFlight = 0
interface Waiter {
  resolve: () => void
  reject: (error: RequestBoundaryError) => void
  timer: ReturnType<typeof setTimeout>
}
const waiters: Waiter[] = []

export class RequestBoundaryError extends Error {
  constructor(public readonly reasonCode: string) {
    super(reasonCode)
    this.name = 'RequestBoundaryError'
  }
}

export async function boundedFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
  request: typeof fetch = nativeRequest,
): Promise<Response> {
  const response = await request(input, init)
  const declaredLength = response.headers.get('Content-Length')
  if (declaredLength !== null) {
    const bytes = Number(declaredLength)
    if (Number.isFinite(bytes) && bytes > maximumResponseBytes) {
      await response.body?.cancel()
      throw new RequestBoundaryError('RESPONSE_BODY_OVERSIZE')
    }
  }
  if (response.headers.get('Content-Type')?.split(';', 1)[0]?.trim().toLowerCase() === 'text/event-stream') {
    // SSE is already bounded event-by-event by its parser. Buffering a
    // keep-alive response to EOF here would deadlock generated streaming clients.
    return response
  }
  if (response.body === null) return response
  const reader = response.body.getReader()
  const chunks: Uint8Array[] = []
  let totalBytes = 0
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      totalBytes += value.byteLength
      if (totalBytes > maximumResponseBytes) {
        await reader.cancel()
        throw new RequestBoundaryError('RESPONSE_BODY_OVERSIZE')
      }
      chunks.push(value)
    }
  } finally {
    reader.releaseLock()
  }
  const body = totalBytes === 0 ? null : new Uint8Array(totalBytes)
  if (body !== null) {
    let offset = 0
    for (const chunk of chunks) {
      body.set(chunk, offset)
      offset += chunk.byteLength
    }
  }
  return new Response(body, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  })
}

export const controlClient: Client = createClient({
  baseUrl: typeof window === 'undefined' ? 'http://127.0.0.1' : window.location.origin,
  credentials: 'same-origin',
  headers: {
    Accept: 'application/json',
  },
  responseStyle: 'fields',
  throwOnError: false,
  fetch: boundedFetch,
})

async function acquireRequestSlot(): Promise<void> {
  if (inFlight < maximumConcurrentRequests) {
    inFlight += 1
    return
  }
  if (waiters.length >= maximumPendingRequests) {
    throw new RequestBoundaryError('REQUEST_QUEUE_FULL')
  }
  await new Promise<void>((resolve, reject) => {
    const waiter: Waiter = {
      resolve,
      reject,
      timer: setTimeout(() => {
        const index = waiters.indexOf(waiter)
        if (index >= 0) waiters.splice(index, 1)
        reject(new RequestBoundaryError('REQUEST_ADMISSION_TIMEOUT'))
      }, admissionTimeoutMS),
    }
    waiters.push(waiter)
  })
  inFlight += 1
}

function releaseRequestSlot(): void {
  inFlight = Math.max(0, inFlight - 1)
  const waiter = waiters.shift()
  if (waiter) {
    clearTimeout(waiter.timer)
    waiter.resolve()
  }
}

export async function withRequestSlot<T>(operation: (signal: AbortSignal) => Promise<T>): Promise<T> {
  await acquireRequestSlot()
  const controller = new AbortController()
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort(new RequestBoundaryError('REQUEST_TIMEOUT'))
  }, requestTimeoutMS)
  try {
    return await operation(controller.signal)
  } catch (error) {
    if (timedOut) throw new RequestBoundaryError('REQUEST_TIMEOUT')
    throw error
  } finally {
    clearTimeout(timer)
    releaseRequestSlot()
  }
}

export function currentRequestCount(): number {
  return inFlight
}

export function currentPendingRequestCount(): number {
  return waiters.length
}
