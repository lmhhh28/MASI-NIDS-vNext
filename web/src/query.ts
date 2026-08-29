import { QueryClient } from '@tanstack/vue-query'

const maximumQueryCacheEntries = 128
const maximumQueryCacheBytes = 16_777_216
const encoder = new TextEncoder()

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      gcTime: 300_000,
      retry(failureCount, error) {
        const reason = error instanceof Error ? error.message : ''
        if (/UNAUTHENTICATED|SCOPE_DENIED|UNSUPPORTED|MISMATCH|OVERSIZE/.test(reason)) return false
        return failureCount < 2
      },
      refetchOnWindowFocus: true,
      refetchOnReconnect: true,
    },
    mutations: { retry: false },
  },
})

function estimatedQueryBytes(value: unknown): number {
  try {
    return encoder.encode(JSON.stringify(value)).byteLength
  } catch {
    return maximumQueryCacheBytes + 1
  }
}

export function queryCacheMetrics(): { query_cache_entries: number; query_cache_bytes: number } {
  const queries = queryClient.getQueryCache().getAll()
  return {
    query_cache_entries: queries.length,
    query_cache_bytes: queries.reduce((total, query) => total + estimatedQueryBytes({
      key: query.queryKey, data: query.state.data, error: query.state.error,
    }), 0),
  }
}

let enforcementScheduled = false
export function enforceQueryCacheBounds(): void {
  const cache = queryClient.getQueryCache()
  const entries = cache.getAll().map((query) => ({
    query,
    bytes: estimatedQueryBytes({ key: query.queryKey, data: query.state.data, error: query.state.error }),
  }))
  let totalBytes = entries.reduce((total, entry) => total + entry.bytes, 0)
  const oldestFirst = (left: typeof entries[number], right: typeof entries[number]) =>
    left.query.state.dataUpdatedAt - right.query.state.dataUpdatedAt
  const inactive = entries.filter((entry) => entry.query.getObserversCount() === 0).sort(oldestFirst)
  const active = entries.filter((entry) => entry.query.getObserversCount() > 0).sort(oldestFirst)
  const evictionCandidates = [...inactive, ...active]
  while ((cache.getAll().length > maximumQueryCacheEntries || totalBytes > maximumQueryCacheBytes)
    && evictionCandidates.length > 0) {
    const oldest = evictionCandidates.shift()
    if (!oldest) break
    cache.remove(oldest.query)
    totalBytes -= oldest.bytes
  }
}

queryClient.getQueryCache().subscribe(() => {
  if (enforcementScheduled) return
  enforcementScheduled = true
  queueMicrotask(() => {
    enforcementScheduled = false
    enforceQueryCacheBounds()
  })
})
