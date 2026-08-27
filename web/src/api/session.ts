import { getSession, type Session } from '@masi/control-api'
import { queryOptions, useQuery } from '@tanstack/vue-query'
import { controlClient, withRequestSlot } from './client'
import { assertSession, responseData } from './guards'

export const sessionQueryKey = ['session'] as const

export const sessionQueryOptions = queryOptions({
  queryKey: sessionQueryKey,
  queryFn: async (): Promise<Session> => {
    const result: unknown = await withRequestSlot(() => getSession({ client: controlClient }))
    return assertSession(responseData(result))
  },
  staleTime: 30_000,
  gcTime: 300_000,
  retry: false,
})

export function useSessionQuery() {
  return useQuery(sessionQueryOptions)
}
