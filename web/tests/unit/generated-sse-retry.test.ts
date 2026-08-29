import { describe, expect, it, vi } from 'vitest'

import { createSseClient } from '../../../contracts/generated/typescript/control-api/core/serverSentEvents.gen'

function emptySSE(): Response {
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) { controller.close() },
  }), { status: 200 })
}

function eventThenError(): Response {
  let emitted = false
  return new Response(new ReadableStream<Uint8Array>({
    pull(controller) {
      if (!emitted) {
        emitted = true
        controller.enqueue(new TextEncoder().encode('data: {"value":1}\n\n'))
        return
      }
      controller.error(new Error('stream failed after connect'))
    },
  }), { status: 200 })
}

async function drain(stream: AsyncGenerator<unknown>): Promise<void> {
  for await (const value of stream) {
    // The generator owns retry handling; draining observes its terminal bound.
    void value
  }
}

describe('generated SSE retry guards', () => {
  it('reconnects after normal EOF and stops after the next consecutive failure', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(emptySSE())
      .mockRejectedValueOnce(new Error('connection failed'))
    const sleep = vi.fn(() => Promise.resolve())
    const errors: unknown[] = []
    const { stream } = createSseClient({
      method: 'GET',
      url: 'https://control.invalid/events',
      fetch,
      sseMaxRetryAttempts: 2,
      sseSleepFn: sleep,
      onSseError: (error) => errors.push(error),
    })

    await drain(stream)
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(sleep).toHaveBeenCalledTimes(1)
    expect(errors).toHaveLength(2)
  })

  it('resets the consecutive failure count after a successful connection', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockRejectedValueOnce(new Error('first failure'))
      .mockResolvedValueOnce(eventThenError())
      .mockRejectedValueOnce(new Error('second consecutive failure'))
    const sleep = vi.fn(() => Promise.resolve())
    const { stream } = createSseClient<{ value: number }>({
      method: 'GET',
      url: 'https://control.invalid/events',
      fetch,
      sseMaxRetryAttempts: 2,
      sseSleepFn: sleep,
    })

    await drain(stream)
    expect(fetch).toHaveBeenCalledTimes(3)
    expect(sleep).toHaveBeenCalledTimes(2)
  })
})
