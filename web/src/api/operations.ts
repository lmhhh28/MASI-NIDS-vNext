import {
  activateTarget,
  advanceFleetWave,
  disableTarget,
  drainTarget,
  quarantineTarget,
  registerTarget,
  retireTarget,
  verifyTarget,
  type AdminLifecycleRequest,
  type AdvanceFleetWaveRequest,
  type RegisterTargetRequest,
  type Session,
} from '@masi/control-api'
import { controlClient, withRequestSlot } from './client'
import { responseData } from './guards'

function requestIdentity(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`
}

function headers(session: Session): Record<string, string> {
  return { 'X-CSRF-Token': session.csrf_token }
}

export interface TargetRegistrationDraft {
  displayName: string
  p4runtimeEndpoint: string
  deviceID: number
  role: string
  desiredProfileDigest: string
  credentialRef: string
  scope: string
  tlsServerName: string
  tlsIdentityRef: string
  targetSetDigest: string
}

export async function submitTargetRegistration(draft: TargetRegistrationDraft, session: Session): Promise<unknown> {
  const body: RegisterTargetRequest = {
    display_name: draft.displayName,
    p4runtime_endpoint: draft.p4runtimeEndpoint,
    device_id: draft.deviceID,
    role: draft.role,
    desired_profile_digest: draft.desiredProfileDigest,
    credential_ref: draft.credentialRef,
    scope: draft.scope,
    p4runtime_tls: { server_name: draft.tlsServerName, identity_ref: draft.tlsIdentityRef },
    target_set_digest: draft.targetSetDigest,
    idempotency_key: requestIdentity('web-target-register'),
    trace_id: requestIdentity('web-trace'),
  }
  const result: unknown = await withRequestSlot((signal) => registerTarget({ client: controlClient, body, headers: headers(session), signal }))
  return responseData(result)
}

export type TargetLifecycleAction = 'verify' | 'activate' | 'drain' | 'disable' | 'quarantine' | 'retire'

export async function submitTargetLifecycle(
  action: TargetLifecycleAction,
  targetID: string,
  scope: string,
  targetSetDigest: string,
  reasonCode: string,
  session: Session,
): Promise<unknown> {
  const body: AdminLifecycleRequest = {
    scope,
    target_set_digest: targetSetDigest,
    reason_code: reasonCode,
    trace_id: requestIdentity('web-trace'),
    idempotency_key: requestIdentity(`web-target-${action}`),
  }
  const options = { client: controlClient, path: { targetID }, body, headers: headers(session) }
  const result: unknown = await withRequestSlot((signal) => {
    const requestOptions = { ...options, signal }
    switch (action) {
      case 'verify': return verifyTarget(requestOptions)
      case 'activate': return activateTarget(requestOptions)
      case 'drain': return drainTarget(requestOptions)
      case 'disable': return disableTarget(requestOptions)
      case 'quarantine': return quarantineTarget(requestOptions)
      case 'retire': return retireTarget(requestOptions)
    }
  })
  return responseData(result)
}

export interface FleetAdvanceDraft {
  fleetID: string
  currentWave: number
  nextWave: number
  scope: string
  targetSetDigest: string
  completedVectorDigest?: string
  gateProposalID?: string
  decisionReason?: string
}

export async function submitFleetAdvance(draft: FleetAdvanceDraft, session: Session): Promise<unknown> {
  const body: AdvanceFleetWaveRequest = {
    current_wave: draft.currentWave,
    next_wave: draft.nextWave,
    scope: draft.scope,
    target_set_digest: draft.targetSetDigest,
    idempotency_key: requestIdentity('web-fleet-advance'),
    ...(draft.completedVectorDigest ? { completed_vector_digest: draft.completedVectorDigest } : {}),
    ...(draft.gateProposalID ? { gate_proposal_id: draft.gateProposalID } : {}),
    ...(draft.decisionReason ? { decision_reason: draft.decisionReason } : {}),
  }
  const result: unknown = await withRequestSlot((signal) => advanceFleetWave({
    client: controlClient,
    path: { fleetID: draft.fleetID },
    body,
    headers: headers(session),
    signal,
  }))
  return responseData(result)
}
