import {
  activatePluginBinding,
  drainPluginBinding,
  qualifyPluginManifest,
  revokePluginBinding,
  rollbackPluginBinding,
  type ActivatePluginBindingRequest,
  type PluginLifecycleRequest,
  type QualifyPluginRequest,
  type Session,
} from '@masi/control-api'
import { controlClient, withRequestSlot } from './client'
import { responseData } from './guards'

function identity(prefix: string): string { return `${prefix}-${crypto.randomUUID()}` }
function headers(session: Session): Record<string, string> { return { 'X-CSRF-Token': session.csrf_token } }

export interface PluginQualificationDraft {
  pluginID: string
  manifestID: string
  manifestRevision: number
  status: 'qualified' | 'unqualified' | 'hold'
  scope: string
  targetSetDigest: string
}

export async function submitPluginQualification(draft: PluginQualificationDraft, session: Session): Promise<unknown> {
  const body: QualifyPluginRequest = {
    manifest_id: draft.manifestID,
    manifest_revision: draft.manifestRevision,
    qualification_status: draft.status,
    scope: draft.scope,
    target_set_digest: draft.targetSetDigest,
    trace_id: identity('web-trace'),
    idempotency_key: identity('web-plugin-qualification'),
  }
  const result: unknown = await withRequestSlot((signal) => qualifyPluginManifest({
    client: controlClient, path: { pluginID: draft.pluginID }, body, headers: headers(session), signal,
  }))
  return responseData(result)
}

export interface PluginActivationDraft {
  pluginID: string
  bindingGeneration: number
  manifestID: string
  manifestRevision: number
  manifestDigest: string
  configDigest: string
  capabilityDigest: string
  resourceProfileDigest: string
  scope: string
  targetSetDigest: string
}

export async function submitPluginActivation(draft: PluginActivationDraft, session: Session): Promise<unknown> {
  const body: ActivatePluginBindingRequest = {
    binding: {
      plugin_id: draft.pluginID,
      binding_generation: draft.bindingGeneration,
      manifest_id: draft.manifestID,
      manifest_revision: draft.manifestRevision,
      manifest_digest: draft.manifestDigest,
      config_digest: draft.configDigest,
      capability_digest: draft.capabilityDigest,
      resource_profile_digest: draft.resourceProfileDigest,
      activation_state: 'active',
      qualification_status: 'qualified',
      scope: draft.scope,
    },
    target_set_digest: draft.targetSetDigest,
    trace_id: identity('web-trace'),
    idempotency_key: identity('web-plugin-activate'),
  }
  const result: unknown = await withRequestSlot((signal) => activatePluginBinding({
    client: controlClient, path: { pluginID: draft.pluginID }, body, headers: headers(session), signal,
  }))
  return responseData(result)
}

export type PluginLifecycleAction = 'drain' | 'revoke' | 'rollback'

export async function submitPluginLifecycle(
  action: PluginLifecycleAction,
  pluginID: string,
  scope: string,
  targetSetDigest: string,
  previousBindingGeneration: number | undefined,
  session: Session,
): Promise<unknown> {
  const body: PluginLifecycleRequest = {
    scope,
    target_set_digest: targetSetDigest,
    trace_id: identity('web-trace'),
    idempotency_key: identity(`web-plugin-${action}`),
    ...(previousBindingGeneration === undefined ? {} : { previous_binding_generation: previousBindingGeneration }),
  }
  const options = { client: controlClient, path: { pluginID }, body, headers: headers(session) }
  const result: unknown = await withRequestSlot((signal) => {
    const requestOptions = { ...options, signal }
    if (action === 'drain') return drainPluginBinding(requestOptions)
    if (action === 'revoke') return revokePluginBinding(requestOptions)
    return rollbackPluginBinding(requestOptions)
  })
  return responseData(result)
}
