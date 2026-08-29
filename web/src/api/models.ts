import {
  advanceModelRolloutGroup,
  createModelRolloutGroup,
  type CreateModelRolloutGroupRequest,
  type Session,
} from '@masi/control-api'
import { controlClient, withRequestSlot } from './client'
import { responseData } from './guards'

function identity(prefix: string): string { return `${prefix}-${crypto.randomUUID()}` }
function headers(session: Session): Record<string, string> { return { 'X-CSRF-Token': session.csrf_token } }

export async function frozenIdentityDigest(ids: string[]): Promise<string> {
  const sorted = [...ids].sort()
  const bytes = new TextEncoder().encode(sorted.join(','))
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return `sha256:${[...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('')}`
}

export interface RolloutDraft {
  groupID: string
  orderedShards: string[]
  logicalPoolID: string
  targetGeneration: number
  targetRevisionID: string
  scope: string
  wireProfileDigest: string
  runtimeProfileDigest: string
  optimizationProfileDigest: string
  availabilityProfile: 'availability-single/v1' | 'availability-ha/v1'
  minReadyReplicas: number
  modelControlIncarnationID: string
}

export async function submitRollout(draft: RolloutDraft, session: Session): Promise<unknown> {
  const body: CreateModelRolloutGroupRequest = {
    group_id: draft.groupID,
    ordered_shards: draft.orderedShards,
    logical_pool_id: draft.logicalPoolID,
    target_generation: draft.targetGeneration,
    target_revision_id: draft.targetRevisionID,
    scope: draft.scope,
    target_set_digest: await frozenIdentityDigest(draft.orderedShards),
    reason_code: 'MODEL_ROLLOUT_APPROVED',
    trace_id: identity('web-trace'),
    idempotency_key: identity('web-model-rollout'),
    wire_profile: 'inference-central-grpc-batch/v1',
    wire_profile_digest: draft.wireProfileDigest,
    runtime_profile_digest: draft.runtimeProfileDigest,
    optimization_profile_digest: draft.optimizationProfileDigest,
    availability_profile: draft.availabilityProfile,
    min_ready_replicas: draft.minReadyReplicas,
    model_control_incarnation_id: draft.modelControlIncarnationID,
  }
  const result: unknown = await withRequestSlot((signal) => createModelRolloutGroup({ client: controlClient, body, headers: headers(session), signal }))
  return responseData(result)
}

export async function submitRolloutAdvance(groupID: string, scope: string, orderedShards: string[], session: Session): Promise<unknown> {
  const targetSetDigest = await frozenIdentityDigest(orderedShards)
  const result: unknown = await withRequestSlot((signal) => advanceModelRolloutGroup({
    client: controlClient,
    path: { groupID },
    body: {
      scope,
      target_set_digest: targetSetDigest,
      idempotency_key: identity('web-model-advance'),
    },
    headers: headers(session),
    signal,
  }))
  return responseData(result)
}
