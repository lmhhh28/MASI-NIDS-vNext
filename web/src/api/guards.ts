import type { DashboardSchema, Session } from '@masi/control-api'
import { acceptProjectionContext } from './context'

export type ProjectionState =
  | 'current'
  | 'desired'
  | 'observed'
  | 'stale'
  | 'hold'
  | 'unknown'
  | 'reconciling'

export interface Projection {
  schema_version: 'masi-web-projection/v1'
  projection_type: ProjectionState
  resource_kind: string
  resource_id?: string
  cursor: string
  page_size: number
  total_count: number
  items: Array<Record<string, unknown>>
  generation: number
  session_scope: string
  authorized_scope: string
  sse_event: Record<string, unknown> | null
  actor_ref: string
  reason_code: string
  trace_id: string
}

export class ContractError extends Error {
  constructor(
    public readonly reasonCode: string,
    message: string,
  ) {
    super(message)
    this.name = 'ContractError'
  }
}

export function asRecord(value: unknown, reason = 'RESPONSE_NOT_OBJECT'): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new ContractError(reason, 'The server response is not a JSON object.')
  }
  return value as Record<string, unknown>
}

function asBoundedString(value: unknown, field: string, maximum: number, minimum = 1): string {
  if (typeof value !== 'string' || value.length < minimum || value.length > maximum) {
    throw new ContractError('RESPONSE_FIELD_INVALID', `${field} is missing or outside its bound.`)
  }
  return value
}

function asIdentity(value: unknown, field: string): string {
  const identity = asBoundedString(value, field, 128)
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(identity)) {
    throw new ContractError('RESPONSE_FIELD_INVALID', `${field} is not a canonical identity.`)
  }
  return identity
}

function asNonNegativeInteger(value: unknown, field: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new ContractError('RESPONSE_FIELD_INVALID', `${field} is not a non-negative integer.`)
  }
  return value as number
}

export function assertSession(value: unknown): Session {
  const record = asRecord(value)
  if (record.schema_version !== 'masi-web-projection/v1') {
    throw new ContractError('SESSION_MAJOR_UNSUPPORTED', 'The session contract major is unsupported.')
  }
  const expectedFields = new Set(['schema_version', 'actor_ref', 'csrf_token', 'expires_at', 'step_up'])
  if (Object.keys(record).some((field) => !expectedFields.has(field))) {
    throw new ContractError('SESSION_FIELD_UNKNOWN', 'The session response contains an unknown field.')
  }
  const actorRef = asIdentity(record.actor_ref, 'actor_ref')
  const csrfToken = asBoundedString(record.csrf_token, 'csrf_token', 256, 32)
  const expiresAt = asNonNegativeInteger(record.expires_at, 'expires_at')
  if (expiresAt < 1) {
    throw new ContractError('RESPONSE_FIELD_INVALID', 'expires_at must be positive.')
  }
  if (!['none', 'webauthn-fido2', 'passkey', 'hardware-key'].includes(String(record.step_up))) {
    throw new ContractError('SESSION_STEP_UP_UNSUPPORTED', 'The step-up state is unsupported.')
  }
  return {
    schema_version: 'masi-web-projection/v1',
    actor_ref: actorRef,
    csrf_token: csrfToken,
    expires_at: expiresAt,
    step_up: record.step_up as Session['step_up'],
  }
}

export function assertProjection(value: unknown, expectedKind: string): Projection {
  const record = asRecord(value)
  if (record.schema_version !== 'masi-web-projection/v1') {
    throw new ContractError('PROJECTION_MAJOR_UNSUPPORTED', 'The projection contract major is unsupported.')
  }
  if (record.resource_kind !== expectedKind) {
    throw new ContractError('PROJECTION_KIND_MISMATCH', 'The projection resource kind does not match the route.')
  }
  if (!['current', 'desired', 'observed', 'stale', 'hold', 'unknown', 'reconciling'].includes(String(record.projection_type))) {
    throw new ContractError('PROJECTION_STATE_UNSUPPORTED', 'The projection state is unsupported.')
  }
  const pageSize = asNonNegativeInteger(record.page_size, 'page_size')
  if (pageSize < 1 || pageSize > 200) {
    throw new ContractError('PROJECTION_PAGE_OVERSIZE', 'The projection page exceeds 200 rows.')
  }
  if (!Array.isArray(record.items) || record.items.length > 200) {
    throw new ContractError('PROJECTION_ITEMS_OVERSIZE', 'The projection contains too many rows.')
  }
  const items = record.items.map((item) => asRecord(item, 'PROJECTION_ITEM_INVALID'))
  const cursor = record.cursor === '' ? '' : asBoundedString(record.cursor, 'cursor', 512)
  const generation = asNonNegativeInteger(record.generation, 'generation')
  if (generation < 1) {
    throw new ContractError('PROJECTION_GENERATION_INVALID', 'Projection generation must be positive.')
  }
  const projection: Projection = {
    schema_version: 'masi-web-projection/v1',
    projection_type: record.projection_type as ProjectionState,
    resource_kind: expectedKind,
    ...(typeof record.resource_id === 'string' ? { resource_id: record.resource_id } : {}),
    cursor,
    page_size: pageSize,
    total_count: asNonNegativeInteger(record.total_count, 'total_count'),
    items,
    generation,
    session_scope: asBoundedString(record.session_scope, 'session_scope', 4096),
    authorized_scope: asBoundedString(record.authorized_scope, 'authorized_scope', 4096),
    sse_event: record.sse_event === null ? null : asRecord(record.sse_event),
    actor_ref: asBoundedString(record.actor_ref, 'actor_ref', 128),
    reason_code: asBoundedString(record.reason_code, 'reason_code', 64),
    trace_id: typeof record.trace_id === 'string' ? record.trace_id.slice(0, 128) : '',
  }
  acceptProjectionContext(
    projection.actor_ref,
    projection.session_scope,
    projection.authorized_scope,
    projection.generation,
  )
  return projection
}

export function assertDashboard(value: unknown): DashboardSchema {
  const record = asRecord(value)
  if (record.schema_version !== 'masi-web-dashboard/v1') {
    throw new ContractError('DASHBOARD_MAJOR_UNSUPPORTED', 'The dashboard contract major is unsupported.')
  }
  if (!['ready', 'partial', 'stale', 'hold'].includes(String(record.state))) {
    throw new ContractError('DASHBOARD_STATE_UNSUPPORTED', 'The dashboard state is unsupported.')
  }
  const boundedLists: Array<[unknown, string]> = [
    [record.recent_alerts, 'recent_alerts'],
    [record.active_operations, 'active_operations'],
    [record.target_health, 'target_health'],
  ]
  for (const [list, field] of boundedLists) {
    if (!Array.isArray(list) || list.length > 8) {
      throw new ContractError('DASHBOARD_SECTION_OVERSIZE', `${field} exceeds its eight-item bound.`)
    }
  }
  if (!Array.isArray(record.authorized_scopes) || record.authorized_scopes.length === 0 || record.authorized_scopes.length > 256) {
    throw new ContractError('DASHBOARD_SCOPE_INVALID', 'Dashboard scopes are missing or outside their bound.')
  }
  const authorizedScopes = record.authorized_scopes.map((scope) =>
    asBoundedString(scope, 'authorized_scopes[]', 256),
  )
  asRecord(record.counts, 'DASHBOARD_COUNTS_INVALID')
  asNonNegativeInteger(record.snapshot_unix_ms, 'snapshot_unix_ms')
  const generation = asNonNegativeInteger(record.generation, 'generation')
  if (generation < 1) {
    throw new ContractError('DASHBOARD_GENERATION_INVALID', 'Dashboard generation must be positive.')
  }
  const actorRef = asBoundedString(record.actor_ref, 'actor_ref', 128)
  acceptProjectionContext(actorRef, authorizedScopes.join(','), authorizedScopes.join(','), generation)
  return record as unknown as DashboardSchema
}

export function responseData(value: unknown): unknown {
  const result = asRecord(value, 'SDK_RESULT_INVALID')
  if (result.data !== undefined) return result.data
  const error = result.error === undefined ? undefined : asRecord(result.error, 'API_ERROR_INVALID')
  const reason = typeof error?.error === 'string' ? error.error : 'API_REQUEST_FAILED'
  throw new ContractError(reason, `The Control API request failed (${reason}).`)
}
