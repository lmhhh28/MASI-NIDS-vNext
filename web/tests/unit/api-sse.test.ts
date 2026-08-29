import { afterEach, describe, expect, it, vi } from 'vitest'

import { assessCursorProgress, BoundedInvalidationStream, streamState } from '@/api/sse'
import { queryClient } from '@/query'

class FakeEventSource {
  static instances: FakeEventSource[] = []
  onopen: ((event: Event) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  private readonly listeners = new Map<string, (event: MessageEvent<string>) => void>()

  constructor() { FakeEventSource.instances.push(this) }
  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    this.listeners.set(type, listener as (event: MessageEvent<string>) => void)
  }
  emit(type: string, data: string): void {
    this.listeners.get(type)?.({ data } as MessageEvent<string>)
  }
  close(): void {}
}

function encodedEvent(sequence: number, emittedAt: number, eventType = 'invalidate'): string {
  const event = {
    event_type: eventType,
    resource_kind: eventType === 'snapshot-refetch-required' ? 'system-health' : 'target',
    resource_id: eventType === 'snapshot-refetch-required' ? 'boot' : 'target-1',
    cursor: `sse:1:${sequence}`,
    sequence,
    generation: 1,
    produced_at_unix_ms: emittedAt,
    data_time_unix_ms: emittedAt,
    payload_digest: `sha256:${'a'.repeat(64)}`,
    bytes: 1,
    emitted_at_unix_ms: emittedAt,
  }
  for (let index = 0; index < 3; index += 1) {
    event.bytes = new TextEncoder().encode(JSON.stringify(event)).byteLength
  }
  return JSON.stringify(event)
}

afterEach(() => {
  queryClient.clear()
  FakeEventSource.instances = []
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('SSE cursor progression', () => {
  it('requires sequence one at the first event of every generation', () => {
    expect(assessCursorProgress(0, 0, true, { generation: 1, sequence: 1 })).toEqual({ accept: true })
    expect(assessCursorProgress(0, 0, true, { generation: 1, sequence: 2 })).toEqual({
      accept: false,
      reasonCode: 'SSE_CURSOR_GAP',
    })
    expect(assessCursorProgress(1, 7, false, { generation: 2, sequence: 999 })).toEqual({
      accept: false,
      reasonCode: 'SSE_CURSOR_GAP',
    })
    expect(assessCursorProgress(1, 7, false, { generation: 2, sequence: 1 })).toEqual({ accept: true })
  })

  it('rejects same-generation gaps and cross-generation regression', () => {
    expect(assessCursorProgress(3, 4, false, { generation: 3, sequence: 6 })).toEqual({
      accept: false,
      reasonCode: 'SSE_CURSOR_GAP',
    })
    expect(assessCursorProgress(3, 4, false, { generation: 2, sequence: 1 })).toEqual({
      accept: false,
      reasonCode: 'SSE_GENERATION_REGRESSION',
    })
    expect(assessCursorProgress(3, 4, false, { generation: 3, sequence: 4 })).toEqual({ accept: false })
  })

  it('accepts the new sequence domain after same-generation reconnect', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
    vi.spyOn(Math, 'random').mockReturnValue(0)
    vi.stubGlobal('document', { visibilityState: 'visible' })
    vi.stubGlobal('window', {
      setTimeout: globalThis.setTimeout,
      clearTimeout: globalThis.clearTimeout,
      setInterval: globalThis.setInterval,
      clearInterval: globalThis.clearInterval,
    })
    vi.stubGlobal('EventSource', FakeEventSource)

    const stream = new BoundedInvalidationStream()
    stream.start(queryClient)
    const first = FakeEventSource.instances[0]
    expect(first).toBeDefined()
    first?.onopen?.({} as Event)
    first?.emit('snapshot-refetch-required', encodedEvent(1, 11, 'snapshot-refetch-required'))
    first?.emit('invalidate', encodedEvent(2, 12))
    await Promise.resolve()
    expect(streamState.lastEventUnixMS).toBe(12)

    first?.onerror?.({} as Event)
    await vi.advanceTimersByTimeAsync(1_000)
    const second = FakeEventSource.instances[1]
    expect(second).toBeDefined()
    second?.onopen?.({} as Event)
    second?.emit('snapshot-refetch-required', encodedEvent(1, 21, 'snapshot-refetch-required'))
    second?.emit('invalidate', encodedEvent(2, 22))
    await Promise.resolve()
    expect(streamState.lastEventUnixMS).toBe(22)
    stream.stop()
  })
})
