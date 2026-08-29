import type { QueryClient, QueryKey } from '@tanstack/vue-query'
import type { Session } from '@masi/control-api'
import { computed, reactive, toValue, type ComputedRef, type MaybeRefOrGetter } from 'vue'

const sessionBoundPrefix = 'session-bound'
let activeQueryClient: QueryClient | undefined
let activeCSRFToken = ''

export const sessionContextState = reactive({
  sessionIdentity: '',
  actorRef: '',
  authorizationIdentity: '',
  sseGeneration: 0,
  cacheVersion: 0,
})

export class SessionContextError extends Error {
  constructor(public readonly reasonCode: string) {
    super(reasonCode)
    this.name = 'SessionContextError'
  }
}

function isSessionBound(key: QueryKey): boolean {
  return key[0] === sessionBoundPrefix
}

function removeSessionQueries(): void {
  activeQueryClient?.removeQueries({ predicate: (query) => isSessionBound(query.queryKey) })
}

export function bindSessionContext(session: Session, queryClient: QueryClient): boolean {
  activeQueryClient = queryClient
  const identity = `${session.actor_ref}\0${session.expires_at}\0${session.step_up}`
  if (identity === sessionContextState.sessionIdentity && session.csrf_token === activeCSRFToken) return false
  removeSessionQueries()
  activeCSRFToken = session.csrf_token
  sessionContextState.sessionIdentity = identity
  sessionContextState.actorRef = session.actor_ref
  sessionContextState.authorizationIdentity = ''
  sessionContextState.sseGeneration = 0
  sessionContextState.cacheVersion += 1
  return true
}

export function clearSessionContext(): void {
  removeSessionQueries()
  activeCSRFToken = ''
  sessionContextState.sessionIdentity = ''
  sessionContextState.actorRef = ''
  sessionContextState.authorizationIdentity = ''
  sessionContextState.sseGeneration = 0
  sessionContextState.cacheVersion += 1
}

export function sessionBoundKey(
  ...parts: Array<MaybeRefOrGetter<unknown>>
): ComputedRef<readonly unknown[]> {
  return computed(() => [
    sessionBoundPrefix,
    sessionContextState.sessionIdentity,
    sessionContextState.cacheVersion,
    ...parts.map((part) => toValue(part)),
  ] as const)
}

export function acceptProjectionContext(
  actorRef: string,
  sessionScope: string,
  authorizedScope: string,
  generation: number,
): void {
  if (!sessionContextState.sessionIdentity || actorRef !== sessionContextState.actorRef) {
    sessionContextState.authorizationIdentity = ''
    sessionContextState.cacheVersion += 1
    removeSessionQueries()
    void activeQueryClient?.invalidateQueries({ queryKey: ['session'], exact: true })
    throw new SessionContextError('PROJECTION_ACTOR_MISMATCH')
  }
  if (!Number.isSafeInteger(generation) || generation < 1) {
    throw new SessionContextError('PROJECTION_GENERATION_INVALID')
  }
  const identity = `${actorRef}\0${sessionScope}\0${authorizedScope}`
  if (sessionContextState.authorizationIdentity === '') {
    sessionContextState.authorizationIdentity = identity
    return
  }
  if (identity !== sessionContextState.authorizationIdentity) {
    sessionContextState.authorizationIdentity = ''
    sessionContextState.cacheVersion += 1
    removeSessionQueries()
    void activeQueryClient?.invalidateQueries({ queryKey: ['session'], exact: true })
    throw new SessionContextError('PROJECTION_CONTEXT_MISMATCH')
  }
}

export function acceptSSEGeneration(generation: number): boolean {
  if (!Number.isSafeInteger(generation) || generation < 1) return false
  if (sessionContextState.sseGeneration === 0) {
    sessionContextState.sseGeneration = generation
    return false
  }
  if (generation !== sessionContextState.sseGeneration) {
    sessionContextState.sseGeneration = generation
    sessionContextState.cacheVersion += 1
    removeSessionQueries()
    return true
  }
  return false
}

export function invalidateSessionQueryGroup(
  queryClient: QueryClient,
  group?: string,
  identity?: string,
): Promise<void> {
  return queryClient.invalidateQueries({
    predicate: (query) => {
      const key = query.queryKey
      if (!isSessionBound(key)) return false
      if (group !== undefined && key[3] !== group) return false
      return identity === undefined || key[4] === identity
    },
  })
}
