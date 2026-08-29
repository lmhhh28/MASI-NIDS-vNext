import { QueryObserver } from '@tanstack/vue-query'
import { afterEach, describe, expect, it } from 'vitest'
import {
  currentPendingRequestCount,
  currentRequestCount,
  boundedFetch,
  withRequestSlot,
} from '@/api/client'
import { parseEvent } from '@/api/sse'
import { enforceQueryCacheBounds, queryClient } from '@/query'

afterEach(() => queryClient.clear())

describe('bounded browser resources', () => {
  it('admits at most eight HTTP operations and queues the remainder', async () => {
    let release: (() => void) | undefined
    const blocker = new Promise<void>((resolve) => { release = resolve })
    let active = 0
    let maximum = 0
    const operations = Array.from({ length: 12 }, (_, index) => withRequestSlot(async (signal) => {
      expect(signal.aborted).toBe(false)
      active += 1
      maximum = Math.max(maximum, active)
      await blocker
      active -= 1
      return index
    }))
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(currentRequestCount()).toBe(8)
    expect(currentPendingRequestCount()).toBe(4)
    release?.()
    await expect(Promise.all(operations)).resolves.toHaveLength(12)
    expect(maximum).toBe(8)
    expect(currentRequestCount()).toBe(0)
    expect(currentPendingRequestCount()).toBe(0)
  })

  it('evicts oldest inactive queries at the entry and byte bounds', () => {
    for (let index = 0; index < 140; index += 1) queryClient.setQueryData(['bounded', index], index)
    enforceQueryCacheBounds()
    expect(queryClient.getQueryCache().getAll().length).toBeLessThanOrEqual(128)

    queryClient.clear()
    queryClient.setQueryData(['large', 1], 'a'.repeat(9_000_000))
    queryClient.setQueryData(['large', 2], 'b'.repeat(9_000_000))
    enforceQueryCacheBounds()
    expect(queryClient.getQueryCache().getAll().length).toBe(1)
  })

  it('enforces cache bounds even when every oversized query is observed', () => {
    const observers = Array.from({ length: 130 }, (_, index) => new QueryObserver(queryClient, {
      queryKey: ['active-bounded', index],
      queryFn: () => Promise.resolve(index),
      initialData: index,
    }))
    const unsubscribes = observers.map((observer) => observer.subscribe(() => undefined))
    try {
      enforceQueryCacheBounds()
      expect(queryClient.getQueryCache().getAll().length).toBeLessThanOrEqual(128)
    } finally {
      for (const unsubscribe of unsubscribes) unsubscribe()
    }
  })

  it('rejects an SSE event whose declared bytes understate its encoded size', () => {
    const event = {
      event_type: 'invalidate',
      resource_kind: 'event',
      resource_id: 'event-1',
      cursor: 'sse:1:1',
      sequence: 1,
      generation: 1,
      produced_at_unix_ms: 1,
      data_time_unix_ms: 1,
      payload_digest: `sha256:${'a'.repeat(64)}`,
      bytes: 1,
      emitted_at_unix_ms: 1,
    }
    for (let index = 0; index < 3; index += 1) {
      event.bytes = new TextEncoder().encode(JSON.stringify(event)).byteLength
    }
    expect(parseEvent(JSON.stringify(event))).toMatchObject({ bytes: event.bytes })
    event.bytes = 1
    try {
      parseEvent(JSON.stringify(event))
      throw new Error('expected mismatched SSE bytes to be rejected')
    } catch (error) {
      expect(error).toMatchObject({ reasonCode: 'SSE_EVENT_BYTES_MISMATCH' })
    }
  })

  it('does not buffer a keep-alive SSE response to EOF', async () => {
    const response = new Response(new ReadableStream<Uint8Array>({
      pull() { return undefined },
    }), { headers: { 'Content-Type': 'text/event-stream; charset=utf-8' } })
    const request = () => Promise.resolve(response)
    await expect(boundedFetch('https://control.invalid/events', undefined, request)).resolves.toBe(response)
    await response.body?.cancel()
  })

  it('rejects an all-zero SSE payload digest', () => {
    const event = {
      event_type: 'heartbeat', resource_kind: 'system-health', resource_id: 'heartbeat',
      cursor: 'sse:1:1', sequence: 1, generation: 1, produced_at_unix_ms: 1,
      data_time_unix_ms: 1, payload_digest: `sha256:${'0'.repeat(64)}`, bytes: 1,
      emitted_at_unix_ms: 1,
    }
    for (let index = 0; index < 3; index += 1) {
      event.bytes = new TextEncoder().encode(JSON.stringify(event)).byteLength
    }
    try {
      parseEvent(JSON.stringify(event))
      throw new Error('expected all-zero digest to be rejected')
    } catch (error) {
      expect(error).toMatchObject({ reasonCode: 'SSE_EVENT_INVALID' })
    }
  })
})
