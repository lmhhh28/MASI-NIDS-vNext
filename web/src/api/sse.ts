import type { QueryClient } from '@tanstack/vue-query'
import { reactive } from 'vue'
import { asRecord, ContractError } from './guards'
import type { ResourceKey } from './resources'

type StreamMode = 'connecting' | 'live' | 'retrying' | 'polling' | 'stopped'

interface InvalidationEvent {
  event_type: 'invalidate' | 'heartbeat' | 'snapshot-refetch-required'
  resource_kind: string
  resource_id: string
  cursor: string
  sequence: number
  generation: number
  produced_at_unix_ms: number
  data_time_unix_ms: number
  payload_digest: string
  bytes: number
  emitted_at_unix_ms: number
}

const maximumEventBytes = 65_536
const maximumPendingEvents = 1_000
const maximumPendingBytes = 1_048_576
const pollingIntervalMS = 15_000

const resourceKeys: Partial<Record<string, ResourceKey[]>> = {
  event: ['events'],
  incident: ['incidents'],
  evidence: ['evidence', 'captures'],
  proposal: ['proposals'],
  decision: ['decisions'],
  intent: ['intents'],
  'firewall-revision': ['firewall-revisions', 'firewall-bindings'],
  target: ['targets'],
  'fleet-operation': ['fleet'],
  'rule-effectiveness': ['rule-effectiveness'],
  'model-revision': ['model-revisions'],
  'model-binding': ['model-bindings', 'model-rollouts', 'model-pools'],
  'plugin-binding': ['plugins', 'statistics-definitions'],
  'plugin-statistics-run': ['statistics-runs'],
  'plugin-statistics-current': ['statistics-current'],
}

export const streamState: {
  mode: StreamMode
  failureCount: number
  lastEventUnixMS: number
  lastCursor: string
  reasonCode: string
} = reactive({
  mode: 'stopped',
  failureCount: 0,
  lastEventUnixMS: 0,
  lastCursor: '',
  reasonCode: 'SSE_STOPPED',
})

function parseEvent(raw: string): InvalidationEvent {
  if (new TextEncoder().encode(raw).byteLength > maximumEventBytes) {
    throw new ContractError('SSE_EVENT_OVERSIZE', 'SSE event exceeds 64 KiB.')
  }
  const record = asRecord(JSON.parse(raw) as unknown, 'SSE_EVENT_INVALID')
  const eventType = record.event_type
  if (!['invalidate', 'heartbeat', 'snapshot-refetch-required'].includes(String(eventType))) {
    throw new ContractError('SSE_EVENT_TYPE_UNSUPPORTED', 'SSE event type is unsupported.')
  }
  const numericFields = ['sequence', 'generation', 'produced_at_unix_ms', 'data_time_unix_ms', 'bytes', 'emitted_at_unix_ms'] as const
  for (const field of numericFields) {
    if (!Number.isSafeInteger(record[field]) || (record[field] as number) < 0) {
      throw new ContractError('SSE_EVENT_INVALID', `SSE ${field} is invalid.`)
    }
  }
  if ((record.bytes as number) > maximumEventBytes) {
    throw new ContractError('SSE_EVENT_OVERSIZE', 'SSE declared bytes exceeds 64 KiB.')
  }
  return record as unknown as InvalidationEvent
}

class BoundedInvalidationStream {
  private source: EventSource | undefined
  private queryClient: QueryClient | undefined
  private reconnectTimer: number | undefined
  private pollingTimer: number | undefined
  private queue: InvalidationEvent[] = []
  private queueBytes = 0
  private draining = false
  private stopped = true

  start(queryClient: QueryClient): void {
    if (!this.stopped) return
    this.stopped = false
    this.queryClient = queryClient
    this.connect()
  }

  stop(): void {
    this.stopped = true
    this.source?.close()
    this.source = undefined
    if (this.reconnectTimer !== undefined) window.clearTimeout(this.reconnectTimer)
    if (this.pollingTimer !== undefined) window.clearInterval(this.pollingTimer)
    this.reconnectTimer = undefined
    this.pollingTimer = undefined
    this.queue = []
    this.queueBytes = 0
    streamState.mode = 'stopped'
    streamState.reasonCode = 'SSE_STOPPED'
  }

  private connect(): void {
    if (this.stopped || document.visibilityState === 'hidden') {
      this.scheduleReconnect()
      return
    }
    streamState.mode = 'connecting'
    streamState.reasonCode = 'SSE_CONNECTING'
    const source = new EventSource('/events', { withCredentials: true })
    this.source = source
    source.onopen = () => {
      streamState.mode = 'live'
      streamState.failureCount = 0
      streamState.reasonCode = 'SSE_LIVE'
    }
    const receive = (message: MessageEvent<string>) => this.enqueue(message.data)
    source.addEventListener('invalidate', receive)
    source.addEventListener('heartbeat', receive)
    source.addEventListener('snapshot-refetch-required', receive)
    source.onerror = () => {
      source.close()
      if (this.source === source) this.source = undefined
      streamState.failureCount += 1
      if (streamState.failureCount >= 10) {
        this.beginPolling()
      } else {
        this.scheduleReconnect()
      }
    }
  }

  private enqueue(raw: string): void {
    try {
      const event = parseEvent(raw)
      if (event.generation < 1 || event.sequence < 1) throw new ContractError('SSE_SEQUENCE_INVALID', 'SSE identity is invalid.')
      if (streamState.lastCursor !== '' && event.sequence <= this.lastSequence()) return
      this.queue.push(event)
      this.queueBytes += event.bytes
      if (this.queue.length > maximumPendingEvents || this.queueBytes > maximumPendingBytes) {
        this.queue = []
        this.queueBytes = 0
        this.invalidateEverything('SSE_PENDING_BOUND_EXCEEDED')
        return
      }
      if (!this.draining) {
        this.draining = true
        queueMicrotask(() => this.drain())
      }
    } catch {
      this.invalidateEverything('SSE_EVENT_REJECTED')
    }
  }

  private lastSequence(): number {
    const parts = streamState.lastCursor.split(':')
    const value = Number(parts.at(-1))
    return Number.isSafeInteger(value) ? value : 0
  }

  private drain(): void {
    const queue = this.queue
    this.queue = []
    this.queueBytes = 0
    this.draining = false
    for (const event of queue) {
      streamState.lastEventUnixMS = event.emitted_at_unix_ms
      streamState.lastCursor = event.cursor
      if (event.event_type === 'heartbeat') continue
      if (event.event_type === 'snapshot-refetch-required' || event.resource_kind === 'system-health') {
        this.invalidateEverything('SSE_REFETCH_REQUIRED')
        continue
      }
      void this.queryClient?.invalidateQueries({ queryKey: ['dashboard'] })
      for (const key of resourceKeys[event.resource_kind] ?? []) {
        void this.queryClient?.invalidateQueries({ queryKey: ['resource', key] })
      }
    }
  }

  private invalidateEverything(reasonCode: string): void {
    streamState.reasonCode = reasonCode
    void this.queryClient?.invalidateQueries()
  }

  private scheduleReconnect(): void {
    if (this.stopped) return
    streamState.mode = 'retrying'
    streamState.reasonCode = 'SSE_RETRYING'
    const ceilingSeconds = Math.min(30, Math.max(1, 2 ** streamState.failureCount))
    const delayMS = Math.max(1_000, Math.floor(ceilingSeconds * (0.5 + Math.random() * 0.5) * 1_000))
    this.reconnectTimer = window.setTimeout(() => this.connect(), delayMS)
  }

  private beginPolling(): void {
    streamState.mode = 'polling'
    streamState.reasonCode = 'SSE_POLLING_FALLBACK'
    if (this.pollingTimer !== undefined) return
    void this.queryClient?.invalidateQueries()
    this.pollingTimer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void this.queryClient?.invalidateQueries()
    }, pollingIntervalMS)
  }
}

export const invalidationStream = new BoundedInvalidationStream()
