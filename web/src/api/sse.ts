import type { QueryClient } from '@tanstack/vue-query'
import { reactive } from 'vue'
import { asRecord, ContractError } from './guards'
import type { ResourceKey } from './resources'
import { acceptSSEGeneration, invalidateSessionQueryGroup } from './context'
import { adjustApplicationTimers } from '@/runtime-metrics'

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
const identityPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/
const digestPattern = /^sha256:(?!0{64}$)[0-9a-f]{64}$/
const cursorPattern = /^sse:([1-9][0-9]*):([1-9][0-9]*)$/

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
  'bounded-capture': ['captures', 'evidence'],
  'firewall-binding': ['firewall-bindings'],
  'model-rollout-group': ['model-rollouts'],
  'model-pool': ['model-pools'],
  plugin: ['plugins'],
  'plugin-statistics-definition': ['statistics-definitions'],
  'plugin-statistics-schedule': ['statistics-schedules'],
  'analysis-task': ['analysis-tasks'],
  'analysis-artifact': ['analysis-artifacts'],
  audit: ['audit'],
}
const resourceKinds = new Set([...Object.keys(resourceKeys), 'system-health'])

function cursorIdentity(cursor: string): { generation: number; sequence: number } | null {
  const match = cursor.match(cursorPattern)
  if (!match) return null
  const generation = Number(match[1])
  const sequence = Number(match[2])
  return Number.isSafeInteger(generation) && Number.isSafeInteger(sequence) ? { generation, sequence } : null
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

export function parseEvent(raw: string): InvalidationEvent {
  const actualBytes = new TextEncoder().encode(raw).byteLength
  if (actualBytes > maximumEventBytes) {
    throw new ContractError('SSE_EVENT_OVERSIZE', 'SSE event exceeds 64 KiB.')
  }
  const record = asRecord(JSON.parse(raw) as unknown, 'SSE_EVENT_INVALID')
  const eventType = record.event_type
  if (!['invalidate', 'heartbeat', 'snapshot-refetch-required'].includes(String(eventType))) {
    throw new ContractError('SSE_EVENT_TYPE_UNSUPPORTED', 'SSE event type is unsupported.')
  }
  const numericFields = ['sequence', 'generation', 'produced_at_unix_ms', 'data_time_unix_ms', 'bytes', 'emitted_at_unix_ms'] as const
  for (const field of numericFields) {
    if (!Number.isSafeInteger(record[field]) || (record[field] as number) < 1) {
      throw new ContractError('SSE_EVENT_INVALID', `SSE ${field} is invalid.`)
    }
  }
  if ((record.bytes as number) > maximumEventBytes) {
    throw new ContractError('SSE_EVENT_OVERSIZE', 'SSE declared bytes exceeds 64 KiB.')
  }
  if (record.bytes !== actualBytes) {
    throw new ContractError('SSE_EVENT_BYTES_MISMATCH', 'SSE declared bytes does not match the encoded event.')
  }
  if (typeof record.resource_kind !== 'string' || !resourceKinds.has(record.resource_kind)
      || typeof record.resource_id !== 'string' || !identityPattern.test(record.resource_id)
      || typeof record.payload_digest !== 'string' || !digestPattern.test(record.payload_digest)) {
    throw new ContractError('SSE_EVENT_INVALID', 'SSE resource or digest identity is invalid.')
  }
  if (typeof record.cursor !== 'string') throw new ContractError('SSE_CURSOR_INVALID', 'SSE cursor is missing.')
  const cursor = cursorIdentity(record.cursor)
  if (!cursor || cursor.generation !== record.generation || cursor.sequence !== record.sequence) {
    throw new ContractError('SSE_CURSOR_INVALID', 'SSE cursor does not match generation and sequence.')
  }
  return record as unknown as InvalidationEvent
}

export interface CursorProgress {
  accept: boolean
  reasonCode?: 'SSE_GENERATION_REGRESSION' | 'SSE_CURSOR_REGRESSION' | 'SSE_CURSOR_GAP'
}

export function assessCursorProgress(
  acceptedGeneration: number,
  acceptedSequence: number,
  freshConnection: boolean,
  event: Pick<InvalidationEvent, 'generation' | 'sequence'>,
): CursorProgress {
  if (acceptedGeneration === 0) {
    return event.sequence === 1 ? { accept: true } : { accept: false, reasonCode: 'SSE_CURSOR_GAP' }
  }
  if (event.generation < acceptedGeneration) {
    return { accept: false, reasonCode: 'SSE_GENERATION_REGRESSION' }
  }
  if (event.generation > acceptedGeneration) {
    return event.sequence === 1 ? { accept: true } : { accept: false, reasonCode: 'SSE_CURSOR_GAP' }
  }
  if (event.sequence <= acceptedSequence) {
    return freshConnection
      ? { accept: false, reasonCode: 'SSE_CURSOR_REGRESSION' }
      : { accept: false }
  }
  return event.sequence === acceptedSequence + 1
    ? { accept: true }
    : { accept: false, reasonCode: 'SSE_CURSOR_GAP' }
}

export class BoundedInvalidationStream {
  private source: EventSource | undefined
  private queryClient: QueryClient | undefined
  private reconnectTimer: number | undefined
  private pollingTimer: number | undefined
  private queue: InvalidationEvent[] = []
  private queueBytes = 0
  private draining = false
  private stopped = true
  private acceptedGeneration = 0
  private acceptedSequence = 0
  private freshConnection = true

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
    if (this.reconnectTimer !== undefined) {
      window.clearTimeout(this.reconnectTimer)
      adjustApplicationTimers(-1)
    }
    if (this.pollingTimer !== undefined) {
      window.clearInterval(this.pollingTimer)
      adjustApplicationTimers(-1)
    }
    this.reconnectTimer = undefined
    this.pollingTimer = undefined
    this.queue = []
    this.queueBytes = 0
    this.acceptedGeneration = 0
    this.acceptedSequence = 0
    this.freshConnection = true
    streamState.mode = 'stopped'
    streamState.failureCount = 0
    streamState.lastEventUnixMS = 0
    streamState.lastCursor = ''
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
      this.freshConnection = true
      // Each Go Hub subscription starts a new sequence domain at 1 and sends a
      // snapshot-refetch marker. Retaining the prior connection's cursor would
      // silently discard every event until that old sequence was reached.
      this.acceptedGeneration = 0
      this.acceptedSequence = 0
      streamState.mode = 'live'
      streamState.failureCount = 0
      streamState.reasonCode = 'SSE_LIVE'
    }
    const receive = (message: MessageEvent<string>) => this.enqueue(message.data)
    source.addEventListener('invalidate', receive)
    source.addEventListener('heartbeat', receive)
    source.addEventListener('snapshot-refetch-required', receive)
    source.onerror = () => {
      if (this.source !== source) return
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
      const progress = assessCursorProgress(
        this.acceptedGeneration,
        this.acceptedSequence,
        this.freshConnection,
        event,
      )
      if (!progress.accept) {
        if (progress.reasonCode) this.invalidateEverything(progress.reasonCode)
        return
      }
      this.freshConnection = false
      this.acceptedGeneration = event.generation
      this.acceptedSequence = event.sequence
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

  private drain(): void {
    const queue = this.queue
    this.queue = []
    this.queueBytes = 0
    this.draining = false
    for (const event of queue) {
      const generationChanged = acceptSSEGeneration(event.generation)
      streamState.lastEventUnixMS = event.emitted_at_unix_ms
      streamState.lastCursor = event.cursor
      if (generationChanged) {
        this.invalidateEverything('SSE_GENERATION_CHANGED')
        continue
      }
      if (event.event_type === 'heartbeat') continue
      if (event.event_type === 'snapshot-refetch-required' || event.resource_kind === 'system-health') {
        this.invalidateEverything('SSE_REFETCH_REQUIRED')
        continue
      }
      if (this.queryClient) void invalidateSessionQueryGroup(this.queryClient, 'dashboard')
      for (const key of resourceKeys[event.resource_kind] ?? []) {
        if (this.queryClient) void invalidateSessionQueryGroup(this.queryClient, 'resource', key)
      }
    }
  }

  private invalidateEverything(reasonCode: string): void {
    streamState.reasonCode = reasonCode
    if (this.queryClient) void invalidateSessionQueryGroup(this.queryClient)
  }

  private scheduleReconnect(): void {
    if (this.stopped) return
    streamState.mode = 'retrying'
    streamState.reasonCode = 'SSE_RETRYING'
    const ceilingSeconds = Math.min(30, Math.max(1, 2 ** streamState.failureCount))
    const delayMS = Math.max(1_000, Math.floor(ceilingSeconds * (0.5 + Math.random() * 0.5) * 1_000))
    if (this.reconnectTimer !== undefined) {
      window.clearTimeout(this.reconnectTimer)
      adjustApplicationTimers(-1)
    }
    adjustApplicationTimers(1)
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = undefined
      adjustApplicationTimers(-1)
      this.connect()
    }, delayMS)
  }

  private beginPolling(): void {
    streamState.mode = 'polling'
    streamState.reasonCode = 'SSE_POLLING_FALLBACK'
    if (this.pollingTimer !== undefined) return
    if (this.reconnectTimer !== undefined) {
      window.clearTimeout(this.reconnectTimer)
      this.reconnectTimer = undefined
      adjustApplicationTimers(-1)
    }
    if (this.queryClient) void invalidateSessionQueryGroup(this.queryClient)
    adjustApplicationTimers(1)
    this.pollingTimer = window.setInterval(() => {
      if (document.visibilityState === 'visible' && this.queryClient) {
        void invalidateSessionQueryGroup(this.queryClient)
      }
    }, pollingIntervalMS)
  }
}

export const invalidationStream = new BoundedInvalidationStream()
