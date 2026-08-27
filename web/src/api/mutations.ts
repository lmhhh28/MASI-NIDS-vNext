import {
  createFirewallActivationProposal,
  createProposal,
  decideFirewallActivationProposal,
  getProposal,
  prepareFirewallActivation,
  recordDecision,
  type CreateProposalRequest,
  type EffectProposalDetail,
  type FirewallProposalRequest,
  type PrepareFirewallActivationRequest,
  type Session,
} from '@masi/control-api'
import { controlClient, withRequestSlot } from './client'
import { asRecord, ContractError, responseData } from './guards'

function requestIdentity(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`
}

function mutationHeaders(session: Session): Record<string, string> {
  return { 'X-CSRF-Token': session.csrf_token }
}

export interface ProposalDraft {
  effectKind: string
  targetIDs: string[]
  scope: string
  policyDigest: string
  riskLevel: 'R1' | 'R2'
  evidenceRefs: string[]
  note: string
  expiresAtUnixMS: number
}

export async function submitProposal(draft: ProposalDraft, session: Session): Promise<unknown> {
  const body: CreateProposalRequest = {
    effect_kind: draft.effectKind,
    target_ids: draft.targetIDs,
    scope: draft.scope,
    policy_digest: draft.policyDigest,
    risk_level: draft.riskLevel,
    evidence_refs: draft.evidenceRefs,
    note: draft.note,
    idempotency_key: requestIdentity('web-proposal'),
    trace_id: requestIdentity('web-trace'),
    expires_at_unix_ms: draft.expiresAtUnixMS,
  }
  const result: unknown = await withRequestSlot(() => createProposal({ client: controlClient, body, headers: mutationHeaders(session) }))
  return responseData(result)
}

export async function loadProposal(proposalID: string): Promise<EffectProposalDetail> {
  const result: unknown = await withRequestSlot(() => getProposal({ client: controlClient, path: { proposalID } }))
  const record = asRecord(responseData(result), 'PROPOSAL_DETAIL_INVALID')
  if (record.proposal_id !== proposalID || !Array.isArray(record.target_ids) || record.target_ids.length > 128 || !Array.isArray(record.evidence_refs) || record.evidence_refs.length > 128) {
    throw new ContractError('PROPOSAL_DETAIL_INVALID', 'The exact proposal context is malformed or mismatched.')
  }
  return record as unknown as EffectProposalDetail
}

export async function decideProposal(
  proposalID: string,
  decision: 'approve' | 'reject',
  reasonCode: string,
  session: Session,
  firewall: boolean,
): Promise<unknown> {
  const body = {
    decision,
    reason_code: reasonCode,
    idempotency_key: requestIdentity('web-decision'),
  }
  const result: unknown = firewall
    ? await withRequestSlot(() => decideFirewallActivationProposal({ client: controlClient, path: { proposalID }, body, headers: mutationHeaders(session) }))
    : await withRequestSlot(() => recordDecision({ client: controlClient, body: { ...body, proposal_id: proposalID }, headers: mutationHeaders(session) }))
  return responseData(result)
}

export interface FirewallActivationProposalDraft {
  revisionID: string
  targetSetDigest: string
  evidenceRefs: string[]
  expiresAtUnixMS: number
  note: string
}

export async function submitFirewallActivationProposal(
  draft: FirewallActivationProposalDraft,
  session: Session,
): Promise<unknown> {
  const body: FirewallProposalRequest = {
    proposal_id: requestIdentity('fw-proposal'),
    target_set_digest: draft.targetSetDigest,
    evidence_refs: draft.evidenceRefs,
    expires_at_unix_ms: draft.expiresAtUnixMS,
    note: draft.note,
    trace_id: requestIdentity('web-trace'),
    idempotency_key: requestIdentity('web-fw-proposal'),
  }
  const result: unknown = await withRequestSlot(() => createFirewallActivationProposal({
    client: controlClient,
    path: { revisionID: draft.revisionID },
    body,
    headers: mutationHeaders(session),
  }))
  return responseData(result)
}

export interface FirewallActivationDraft {
  targetID: string
  revisionID: string
  operationID: string
  scope: string
  targetSetDigest: string
}

export async function submitFirewallActivation(draft: FirewallActivationDraft, session: Session): Promise<unknown> {
  const body: PrepareFirewallActivationRequest = {
    target_id: draft.targetID,
    revision_id: draft.revisionID,
    operation_id: draft.operationID,
    scope: draft.scope,
    target_set_digest: draft.targetSetDigest,
    idempotency_key: requestIdentity('web-fw-activate'),
  }
  const result: unknown = await withRequestSlot(() => prepareFirewallActivation({
    client: controlClient,
    body,
    headers: mutationHeaders(session),
  }))
  return responseData(result)
}
