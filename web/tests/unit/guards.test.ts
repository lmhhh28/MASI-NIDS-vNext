import { describe, expect, it } from 'vitest'
import { assertDashboard, assertProjection, ContractError } from '@/api/guards'

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
  it('accepts the exact projection major and preserves empty first-page cursor', () => {
    expect(assertProjection(projection(), 'event').cursor).toBe('')
  })

  it('rejects kind confusion and oversized pages', () => {
    expect(() => assertProjection(projection(), 'incident')).toThrowError(ContractError)
    expect(() => assertProjection({ ...projection(), page_size: 201 }, 'event')).toThrow('exceeds 200')
  })

  it('rejects unknown dashboard major and section overflow', () => {
    expect(() => assertDashboard({ ...dashboard(), schema_version: 'masi-web-dashboard/v2' })).toThrow('unsupported')
    expect(() => assertDashboard({ ...dashboard(), recent_alerts: Array.from({ length: 9 }, () => ({})) })).toThrow('eight-item')
  })
})
