import { QueryClient } from '@tanstack/vue-query'
import { beforeEach, describe, expect, it } from 'vitest'
import { assertDashboard, assertProjection, assertSession, ContractError, responseData } from '@/api/guards'
import { bindSessionContext, clearSessionContext } from '@/api/context'
import { assertResourceDetailValue } from '@/api/resources'

function projection() {
  return {
    schema_version: 'masi-web-projection/v1', projection_type: 'current', resource_kind: 'event',
    cursor: '', page_size: 50, total_count: 1, items: [{ event_id: 'evt-1' }], generation: 1,
    session_scope: 'scope-a', authorized_scope: 'scope-a', sse_event: null,
    actor_ref: 'actor:abc', reason_code: 'OK', trace_id: '',
  }
}

function dashboard() {
  return {
    schema_version: 'masi-web-dashboard/v1', snapshot_id: 'dashboard-1', snapshot_unix_ms: 1,
    generation: 1, state: 'ready', actor_ref: 'actor:abc', authorized_scopes: ['scope-a'], counts: {},
    recent_alerts: [], active_operations: [], target_health: [], reason_code: 'DASHBOARD_READY',
  }
}

describe('public response guards', () => {
  beforeEach(() => {
    clearSessionContext()
    bindSessionContext({
      schema_version: 'masi-web-projection/v1',
      actor_ref: 'actor:abc',
      csrf_token: 'c'.repeat(32),
      expires_at: 4_102_444_800,
      step_up: 'none',
    }, new QueryClient())
  })

  it('accepts the exact projection major and preserves empty first-page cursor', () => {
    expect(assertProjection(projection(), 'event').cursor).toBe('')
  })

  it('enforces the exact session identity, CSRF, expiry, and closed shape contract', () => {
    const valid = {
      schema_version: 'masi-web-projection/v1',
      actor_ref: 'actor:abc',
      csrf_token: 'c'.repeat(32),
      expires_at: 4_102_444_800,
      step_up: 'none',
    }
    expect(assertSession(valid)).toEqual(valid)
    expect(() => assertSession({ ...valid, actor_ref: 'actor with spaces' })).toThrow('canonical identity')
    expect(() => assertSession({ ...valid, csrf_token: 'short' })).toThrow('outside its bound')
    expect(() => assertSession({ ...valid, expires_at: 0 })).toThrow('must be positive')
    expect(() => assertSession({ ...valid, unexpected: true })).toThrow('unknown field')
  })

  it('fences a rotated CSRF token without exposing it in the reactive session identity', () => {
    clearSessionContext()
    const client = new QueryClient()
    const session = {
      schema_version: 'masi-web-projection/v1' as const,
      actor_ref: 'actor:abc',
      csrf_token: 'a'.repeat(32),
      expires_at: 4_102_444_800,
      step_up: 'none' as const,
    }
    expect(bindSessionContext(session, client)).toBe(true)
    expect(bindSessionContext(session, client)).toBe(false)
    expect(bindSessionContext({ ...session, csrf_token: 'b'.repeat(32) }, client)).toBe(true)
  })

  it('rejects kind confusion and oversized pages', () => {
    expect(() => assertProjection(projection(), 'incident')).toThrowError(ContractError)
    expect(() => assertProjection({ ...projection(), page_size: 201 }, 'event')).toThrow('exceeds 200')
  })

  it('rejects unknown dashboard major and section overflow', () => {
    expect(() => assertDashboard({ ...dashboard(), schema_version: 'masi-web-dashboard/v2' })).toThrow('unsupported')
    expect(() => assertDashboard({ ...dashboard(), recent_alerts: Array.from({ length: 9 }, () => ({})) })).toThrow('eight-item')
  })

  it('allows resource-local generations while fencing actor and authorization drift', () => {
    expect(assertProjection({ ...projection(), generation: 11 }, 'event').generation).toBe(11)
    expect(assertDashboard({ ...dashboard(), generation: 42 }).generation).toBe(42)
    expect(() => assertProjection({ ...projection(), actor_ref: 'actor:other' }, 'event')).toThrow('PROJECTION_ACTOR_MISMATCH')
    expect(assertProjection({ ...projection(), generation: 12 }, 'event').generation).toBe(12)
    expect(() => assertProjection({ ...projection(), authorized_scope: 'scope-b' }, 'event')).toThrow('PROJECTION_CONTEXT_MISMATCH')
  })

  it('turns generated-client field errors into observable contract failures', () => {
    expect(() => responseData({ error: { error: 'SCOPE_DENIED' } })).toThrow('SCOPE_DENIED')
    try {
      responseData(undefined)
      throw new Error('expected missing generated-client result to fail closed')
    } catch (error) {
      expect(error).toMatchObject({ reasonCode: 'SDK_RESULT_INVALID' })
    }
  })

  it('rejects a detail projection whose sole item belongs to another identity', () => {
    const detail = { ...projection(), resource_id: 'evt-1' }
    expect(assertResourceDetailValue('events', 'evt-1', detail).event_id).toBe('evt-1')
    try {
      assertResourceDetailValue('events', 'evt-1', { ...detail, items: [{ event_id: 'evt-2' }] })
      throw new Error('expected detail identity mismatch')
    } catch (error) {
      expect(error).toMatchObject({ reasonCode: 'RESOURCE_DETAIL_IDENTITY_MISMATCH' })
    }
  })
})
