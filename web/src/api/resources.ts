import {
  getDashboard,
  listAnalysisArtifacts,
  listAnalysisTasks,
  listAuditFacts,
  listBoundedCaptures,
  listDecisions,
  listEffectIntents,
  listEvents,
  listEvidence,
  listFirewallBindings,
  listFirewallRevisions,
  listFleetOperations,
  listIncidents,
  listModelBindings,
  listModelPools,
  listModelRevisions,
  listModelRolloutGroups,
  listPlugins,
  listProposals,
  listRuleEffectiveness,
  listStatisticsCurrent,
  listStatisticsDefinitions,
  listStatisticsRuns,
  listStatisticsSchedules,
  listTargets,
  type DashboardSchema,
} from '@masi/control-api'
import { controlClient, withRequestSlot } from './client'
import { assertDashboard, assertProjection, responseData, type Projection } from './guards'

export type ResourceKey =
  | 'events'
  | 'incidents'
  | 'evidence'
  | 'captures'
  | 'proposals'
  | 'decisions'
  | 'intents'
  | 'targets'
  | 'fleet'
  | 'firewall-revisions'
  | 'firewall-bindings'
  | 'rule-effectiveness'
  | 'model-revisions'
  | 'model-bindings'
  | 'model-rollouts'
  | 'model-pools'
  | 'plugins'
  | 'statistics-definitions'
  | 'statistics-runs'
  | 'statistics-current'
  | 'statistics-schedules'
  | 'analysis-tasks'
  | 'analysis-artifacts'
  | 'audit'

export interface ResourceColumn {
  field: string
  label: string
  kind?: 'text' | 'identity' | 'status' | 'time' | 'number' | 'digest'
}

export interface ResourceDefinition {
  key: ResourceKey
  contractKind: string
  title: string
  eyebrow: string
  description: string
  identityField: string
  columns: ResourceColumn[]
}

export const resourceDefinitions: Record<ResourceKey, ResourceDefinition> = {
  events: { key: 'events', contractKind: 'event', title: 'Detection events', eyebrow: 'Detection', description: 'Canonical committed inference outcomes, with quality and execution state kept distinct.', identityField: 'event_id', columns: [{field:'event_id',label:'Event',kind:'identity'},{field:'event_time_unix_ms',label:'Observed',kind:'time'},{field:'decision',label:'Decision',kind:'status'},{field:'quality',label:'Quality',kind:'status'},{field:'predicted_label',label:'Label',kind:'number'},{field:'shard_id',label:'Shard',kind:'identity'}] },
  incidents: { key: 'incidents', contractKind: 'incident', title: 'Incident queue', eyebrow: 'Detection', description: 'Scope-filtered cases awaiting triage; event facts remain canonical.', identityField: 'incident_id', columns: [{field:'incident_id',label:'Incident',kind:'identity'},{field:'severity',label:'Severity',kind:'status'},{field:'status',label:'State',kind:'status'},{field:'created_at_unix_ms',label:'Created',kind:'time'}] },
  evidence: { key: 'evidence', contractKind: 'evidence', title: 'Evidence register', eyebrow: 'Evidence', description: 'Immutable references to observation, audit, bounded-capture, and plugin artifacts.', identityField: 'evidence_id', columns: [{field:'evidence_id',label:'Evidence',kind:'identity'},{field:'kind',label:'Kind',kind:'status'},{field:'source',label:'Source'},{field:'reference_digest',label:'Digest',kind:'digest'},{field:'created_at_unix_ms',label:'Created',kind:'time'}] },
  captures: { key: 'captures', contractKind: 'bounded-capture', title: 'Bounded captures', eyebrow: 'Evidence', description: 'Explicitly governed capture requests with hard duration, byte, and sample limits.', identityField: 'capture_id', columns: [{field:'capture_id',label:'Capture',kind:'identity'},{field:'target_id',label:'Target',kind:'identity'},{field:'state',label:'State',kind:'status'},{field:'duration_ms',label:'Duration ms',kind:'number'},{field:'observed_samples',label:'Samples',kind:'number'},{field:'gap',label:'Gap',kind:'status'}] },
  proposals: { key: 'proposals', contractKind: 'proposal', title: 'Response proposals', eyebrow: 'Effects & governance', description: 'Non-executable proposals. Only an authorized effect intent can enter the dispatcher queue.', identityField: 'proposal_id', columns: [{field:'proposal_id',label:'Proposal',kind:'identity'},{field:'risk_level',label:'Risk',kind:'status'},{field:'effect_kind',label:'Effect'},{field:'reason_code',label:'Reason',kind:'status'},{field:'created_at_unix_ms',label:'Created',kind:'time'}] },
  decisions: { key: 'decisions', contractKind: 'decision', title: 'Authorization decisions', eyebrow: 'Effects & governance', description: 'Append-only maker-checker outcomes bound to exact proposal digests.', identityField: 'decision_id', columns: [{field:'decision_id',label:'Decision',kind:'identity'},{field:'proposal_id',label:'Proposal',kind:'identity'},{field:'risk_level',label:'Risk',kind:'status'},{field:'decision',label:'Outcome',kind:'status'},{field:'expires_at_unix_ms',label:'Expires',kind:'time'}] },
  intents: { key: 'intents', contractKind: 'intent', title: 'Effect operations', eyebrow: 'Effects & governance', description: 'The only claimable durable queue, shown by intent, target, deadline, and claim state.', identityField: 'effect_intent_id', columns: [{field:'effect_intent_id',label:'Intent',kind:'identity'},{field:'operation_id',label:'Operation',kind:'identity'},{field:'target_id',label:'Target',kind:'identity'},{field:'claim_state',label:'Claim',kind:'status'},{field:'deadline_unix_ms',label:'Deadline',kind:'time'},{field:'reason_code',label:'Reason',kind:'status'}] },
  targets: { key: 'targets', contractKind: 'target', title: 'Managed targets', eyebrow: 'Operations & audit', description: 'Stable identity, endpoint, assignment, profile alignment and observation freshness remain distinct facts.', identityField: 'target_id', columns: [{field:'display_name',label:'Target'},{field:'p4runtime_endpoint',label:'Endpoint'},{field:'lifecycle',label:'Lifecycle',kind:'status'},{field:'assignment_generation',label:'Assignment',kind:'number'},{field:'lease_state',label:'Lease',kind:'status'},{field:'freshness',label:'Readback',kind:'status'},{field:'profile_alignment',label:'Profile',kind:'status'},{field:'application_generation',label:'Application',kind:'number'},{field:'p4info_digest',label:'P4Info',kind:'digest'}] },
  fleet: { key: 'fleet', contractKind: 'fleet-operation', title: 'Fleet operations', eyebrow: 'Operations & audit', description: 'Static waves with per-target child outcomes; mixed state is never collapsed to green.', identityField: 'fleet_operation_id', columns: [{field:'fleet_operation_id',label:'Fleet operation',kind:'identity'},{field:'aggregate_status',label:'Aggregate',kind:'status'},{field:'wave_count',label:'Waves',kind:'number'},{field:'target_set_digest',label:'Target set',kind:'digest'},{field:'created_at_unix_ms',label:'Created',kind:'time'}] },
  'firewall-revisions': { key: 'firewall-revisions', contractKind: 'firewall-revision', title: 'Firewall policy revisions', eyebrow: 'Effects & governance', description: 'Immutable normalized baseline revisions for the qualified IPv4 stateless profile.', identityField: 'revision_id', columns: [{field:'revision_id',label:'Revision',kind:'identity'},{field:'target_id',label:'Target',kind:'identity'},{field:'default_action',label:'Default',kind:'status'},{field:'revision_digest',label:'Digest',kind:'digest'},{field:'created_at_unix_ms',label:'Created',kind:'time'}] },
  'firewall-bindings': { key: 'firewall-bindings', contractKind: 'firewall-binding', title: 'Firewall bindings', eyebrow: 'Effects & governance', description: 'Current and previous revision facts with exact bank and selector state.', identityField: 'target_id', columns: [{field:'target_id',label:'Target',kind:'identity'},{field:'current_revision_id',label:'Current',kind:'identity'},{field:'previous_revision_id',label:'Previous',kind:'identity'},{field:'active_bank',label:'Bank',kind:'number'},{field:'selector_state',label:'Selector',kind:'status'},{field:'binding_version',label:'Version',kind:'number'}] },
  'rule-effectiveness': { key: 'rule-effectiveness', contractKind: 'rule-effectiveness', title: 'Rule effectiveness', eyebrow: 'Effects & governance', description: 'Installation, dataplane match, eligible traffic, and independent outcome remain separate facts.', identityField: 'epoch_id', columns: [{field:'rule_id',label:'Rule',kind:'identity'},{field:'quality_status',label:'Quality',kind:'status'},{field:'rate',label:'Rate',kind:'number'},{field:'coverage',label:'Coverage',kind:'number'},{field:'outcome_status',label:'Outcome',kind:'status'}] },
  'model-revisions': { key: 'model-revisions', contractKind: 'model-revision', title: 'Model revisions', eyebrow: 'Operations & audit', description: 'Immutable model bundles and qualification state; a revision is not a plugin.', identityField: 'model_revision_id', columns: [{field:'model_revision_id',label:'Revision',kind:'identity'},{field:'qualification_status',label:'Qualification',kind:'status'},{field:'reader_runtime_profile',label:'Runtime'},{field:'model_revision_digest',label:'Digest',kind:'digest'}] },
  'model-bindings': { key: 'model-bindings', contractKind: 'model-binding', title: 'Model shard bindings', eyebrow: 'Operations & audit', description: 'Exact current binding and route epoch for every canonical Edge shard.', identityField: 'shard_id', columns: [{field:'shard_id',label:'Shard',kind:'identity'},{field:'logical_pool_id',label:'Pool',kind:'identity'},{field:'current_revision_id',label:'Revision',kind:'identity'},{field:'current_generation',label:'Generation',kind:'number'},{field:'route_epoch',label:'Route epoch',kind:'number'},{field:'resume_state',label:'Resume',kind:'status'}] },
  'model-rollouts': { key: 'model-rollouts', contractKind: 'model-rollout-group', title: 'Model rollout groups', eyebrow: 'Operations & audit', description: 'Ordered per-shard rollout, recovery, and rollback progress without Ready-as-current shortcuts.', identityField: 'group_id', columns: [{field:'group_id',label:'Group',kind:'identity'},{field:'logical_pool_id',label:'Pool',kind:'identity'},{field:'target_revision_id',label:'Target revision',kind:'identity'},{field:'target_generation',label:'Generation',kind:'number'},{field:'status',label:'State',kind:'status'},{field:'next_index',label:'Next shard',kind:'number'}] },
  'model-pools': { key: 'model-pools', contractKind: 'model-pool', title: 'Inference pools', eyebrow: 'Operations & audit', description: 'Selected runtime and availability profile, separate from observed worker readiness.', identityField: 'logical_pool_id', columns: [{field:'logical_pool_id',label:'Pool',kind:'identity'},{field:'current_generation',label:'Generation',kind:'number'},{field:'availability_profile',label:'Availability',kind:'status'},{field:'runtime_profile',label:'Runtime',kind:'status'}] },
  plugins: { key: 'plugins', contractKind: 'plugin', title: 'Plugin catalog', eyebrow: 'Plugins', description: 'Signed exact revisions and closed kinds; runtime activation remains separately governed.', identityField: 'plugin_id', columns: [{field:'plugin_id',label:'Plugin',kind:'identity'},{field:'kind',label:'Kind',kind:'status'},{field:'manifest_id',label:'Manifest',kind:'identity'},{field:'manifest_revision',label:'Revision',kind:'number'},{field:'runtime_profile',label:'Runtime',kind:'status'}] },
  'statistics-definitions': { key: 'statistics-definitions', contractKind: 'plugin-statistics-definition', title: 'Statistics definitions', eyebrow: 'Plugins', description: 'Immutable host-owned projection definitions with a closed display union.', identityField: 'definition_id', columns: [{field:'definition_id',label:'Definition',kind:'identity'},{field:'plugin_id',label:'Plugin',kind:'identity'},{field:'producer_kind',label:'Producer',kind:'status'},{field:'display_hint',label:'Display',kind:'status'},{field:'binding_generation',label:'Generation',kind:'number'},{field:'revoked',label:'Revoked',kind:'status'}] },
  'statistics-runs': { key: 'statistics-runs', contractKind: 'plugin-statistics-run', title: 'Statistics runs', eyebrow: 'Plugins', description: 'The single durable run ledger, including idempotent terminal artifact identity.', identityField: 'run_id', columns: [{field:'run_id',label:'Run',kind:'identity'},{field:'definition_id',label:'Definition',kind:'identity'},{field:'status',label:'State',kind:'status'},{field:'binding_generation',label:'Generation',kind:'number'},{field:'started_at_unix_ms',label:'Started',kind:'time'},{field:'artifact_id',label:'Artifact',kind:'identity'}] },
  'statistics-current': { key: 'statistics-current', contractKind: 'plugin-statistics-current', title: 'Current plugin statistics', eyebrow: 'Plugins', description: 'Validated current artifacts; stale and revoked output remains explicitly marked.', identityField: 'definition_id', columns: [{field:'definition_id',label:'Definition',kind:'identity'},{field:'quality',label:'Quality',kind:'status'},{field:'binding_generation',label:'Generation',kind:'number'},{field:'run_id',label:'Run',kind:'identity'},{field:'artifact_id',label:'Artifact',kind:'identity'},{field:'updated_at_unix_ms',label:'Updated',kind:'time'}] },
  'statistics-schedules': { key: 'statistics-schedules', contractKind: 'plugin-statistics-schedule', title: 'Statistics schedules', eyebrow: 'Plugins', description: 'Immutable append-only schedule revisions owned and dispatched by Go Control.', identityField: 'schedule_id', columns: [{field:'schedule_id',label:'Schedule',kind:'identity'},{field:'schedule_revision',label:'Revision',kind:'number'},{field:'definition_id',label:'Definition',kind:'identity'},{field:'interval_seconds',label:'Interval sec',kind:'number'},{field:'disabled',label:'Disabled',kind:'status'},{field:'reason_code',label:'Reason',kind:'status'}] },
  'analysis-tasks': { key: 'analysis-tasks', contractKind: 'analysis-task', title: 'Analysis tasks', eyebrow: 'Analysis', description: 'Bounded, non-executable Analysis Agent tasks across the direct typed A2A boundary.', identityField: 'task_id', columns: [{field:'task_id',label:'Task',kind:'identity'},{field:'plugin_id',label:'Plugin',kind:'identity'},{field:'status',label:'State',kind:'status'},{field:'poll_count',label:'Polls',kind:'number'},{field:'deadline_unix_ms',label:'Deadline',kind:'time'},{field:'updated_at_unix_ms',label:'Updated',kind:'time'}] },
  'analysis-artifacts': { key: 'analysis-artifacts', contractKind: 'analysis-artifact', title: 'Analysis artifacts', eyebrow: 'Analysis', description: 'Non-executable evidence and recommendations produced through the governed Analysis Agent binding.', identityField: 'artifact_id', columns: [{field:'artifact_id',label:'Artifact',kind:'identity'},{field:'task_id',label:'Task',kind:'identity'},{field:'plugin_id',label:'Plugin',kind:'identity'},{field:'analysis_outcome',label:'Outcome',kind:'status'},{field:'non_executable',label:'Non-executable',kind:'status'},{field:'deployment_eligible',label:'Deployable',kind:'status'},{field:'created_at_unix_ms',label:'Created',kind:'time'}] },
  audit: { key: 'audit', contractKind: 'audit', title: 'Audit trail', eyebrow: 'Operations & audit', description: 'Scope-filtered append-only plugin lifecycle, restricted MCP, and evidence audit facts.', identityField: 'audit_id', columns: [{field:'audit_id',label:'Audit fact',kind:'identity'},{field:'category',label:'Category',kind:'status'},{field:'subject_id',label:'Subject',kind:'identity'},{field:'action',label:'Action'},{field:'outcome',label:'Outcome',kind:'status'},{field:'reason_code',label:'Reason',kind:'status'},{field:'created_at_unix_ms',label:'Recorded',kind:'time'}] },
}

interface ListQuery {
  cursor?: string
  page_size: number
}

async function invokeList(key: ResourceKey, query: ListQuery): Promise<unknown> {
  const options = { client: controlClient, query }
  switch (key) {
    case 'events': return listEvents(options)
    case 'incidents': return listIncidents(options)
    case 'evidence': return listEvidence(options)
    case 'captures': return listBoundedCaptures(options)
    case 'proposals': return listProposals(options)
    case 'decisions': return listDecisions(options)
    case 'intents': return listEffectIntents(options)
    case 'targets': return listTargets(options)
    case 'fleet': return listFleetOperations(options)
    case 'firewall-revisions': return listFirewallRevisions(options)
    case 'firewall-bindings': return listFirewallBindings(options)
    case 'rule-effectiveness': return listRuleEffectiveness(options)
    case 'model-revisions': return listModelRevisions(options)
    case 'model-bindings': return listModelBindings(options)
    case 'model-rollouts': return listModelRolloutGroups(options)
    case 'model-pools': return listModelPools(options)
    case 'plugins': return listPlugins(options)
    case 'statistics-definitions': return listStatisticsDefinitions(options)
    case 'statistics-runs': return listStatisticsRuns(options)
    case 'statistics-current': return listStatisticsCurrent(options)
    case 'statistics-schedules': return listStatisticsSchedules(options)
    case 'analysis-tasks': return listAnalysisTasks(options)
    case 'analysis-artifacts': return listAnalysisArtifacts(options)
    case 'audit': return listAuditFacts(options)
  }
}

export async function fetchResource(key: ResourceKey, cursor = '', pageSize = 50): Promise<Projection> {
  const definition = resourceDefinitions[key]
  const query: ListQuery = cursor === '' ? { page_size: pageSize } : { page_size: pageSize, cursor }
  const result = await withRequestSlot(() => invokeList(key, query))
  return assertProjection(responseData(result), definition.contractKind)
}

export async function fetchDashboard(): Promise<DashboardSchema> {
  const result: unknown = await withRequestSlot(() => getDashboard({ client: controlClient }))
  return assertDashboard(responseData(result))
}
