import { QueryClient } from '@tanstack/vue-query'

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
