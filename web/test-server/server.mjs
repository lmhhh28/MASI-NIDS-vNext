import { createHash } from 'node:crypto'
import { createReadStream, existsSync, statSync } from 'node:fs'
import { extname, join, normalize, resolve } from 'node:path'
import { createServer } from 'node:http'

const root = resolve(process.argv[2] ?? 'dist')
const port = Number(process.argv[3] ?? 4173)
const host = process.argv[4] ?? '127.0.0.1'
const now = () => Date.now()
const digest = (value) => `sha256:${createHash('sha256').update(value).digest('hex')}`
const d = digest('fixture')
const mutations = new Map()

const securityHeaders = {
  'Content-Security-Policy': "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'",
  'Cross-Origin-Opener-Policy': 'same-origin',
  'Cross-Origin-Resource-Policy': 'same-origin',
  'Referrer-Policy': 'same-origin',
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'DENY',
}

const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml' }

function json(response, status, body, headers = {}) {
  response.writeHead(status, { ...securityHeaders, 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...headers })
  response.end(JSON.stringify(body))
}

function projection(kind, items) {
  return {
    schema_version: 'masi-web-projection/v1', projection_type: 'current', resource_kind: kind,
    cursor: '', page_size: 50, total_count: items.length, items, generation: 7,
    session_scope: 'scope-e2e', authorized_scope: 'scope-e2e', sse_event: null,
    actor_ref: 'actor:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    reason_code: 'OK', trace_id: 'fixture-trace',
  }
}

const fixtures = {
  event: [{ event_id: 'evt-2026-0001', shard_id: 'shard-a', model_control_incarnation_id: 'inc-7', route_epoch: 9, commit_status: 'committed', quality: 'valid', reason_code: 'COMMITTED', decision: 'alert', predicted_label: 4, out_of_distribution: false, abstain: false, execution_status: 'ok', event_time_unix_ms: now() - 12_000 }],
  incident: [{ incident_id: 'inc-401', severity: 'high', status: 'investigating', created_at_unix_ms: now() - 60_000 }],
  evidence: [{ evidence_id: 'evidence-88', kind: 'observation', reference_digest: d, source: 'rule-observation', created_at_unix_ms: now() - 50_000 }],
  'bounded-capture': [{ capture_id: 'capture-9', target_id: 'target-edge-a', capture_digest: d, duration_ms: 2000, sample_limit: 32, byte_limit: 65536, state: 'completed', observed_samples: 12, gap: false }],
  proposal: [{ proposal_id: 'proposal-r2-41', risk_level: 'R2', effect_kind: 'firewall-overlay', reason_code: 'PROPOSED', created_at_unix_ms: now() - 30_000 }],
  decision: [{ decision_id: 'decision-12', proposal_id: 'proposal-old', proposal_digest: d, risk_level: 'R1', decision: 'approve', decision_digest: d, expires_at_unix_ms: now() + 300_000, reason_code: 'APPROVED' }],
  intent: [{ effect_intent_id: 'intent-19', operation_id: 'operation-19', target_id: 'target-edge-a', claim_state: 'executing', deadline_unix_ms: now() + 120_000, reason_code: 'EXECUTING' }],
  target: [{ target_id: 'target-edge-a', display_name: 'Edge switch A', p4runtime_endpoint: 'https://switch-a.example:9559', lifecycle: 'active', device_id: 1, role: 'p4runtime-primary', desired_profile_digest: d, assignment_generation: 7, lease_expires_at_unix_ms: now() + 60_000, lease_state: 'assigned', application_generation: 11, observed_profile_digest: d, p4info_digest: d, freshness: 'fresh', profile_alignment: 'exact', p4_connected: true, primary_actor: true }],
  'fleet-operation': [{ fleet_operation_id: 'fleet-7', target_set_digest: d, wave_count: 2, aggregate_status: 'partial', created_at_unix_ms: now() - 80_000, child_intents: [{ target_id: 'target-edge-a', intent_id: 'intent-a', wave_index: 0, status: 'applied', reason_code: 'APPLIED', gate_open: true }, { target_id: 'target-edge-b', intent_id: 'intent-b', wave_index: 1, status: 'hold', reason_code: 'TARGET_UNAVAILABLE', gate_open: false }] }],
  'firewall-revision': [{ revision_id: 'fw-revision-17', revision_digest: d, target_id: 'target-edge-a', default_action: 'permit-and-continue', scope: 'scope-e2e', created_at_unix_ms: now() - 100_000 }],
  'firewall-binding': [{ target_id: 'target-edge-a', current_revision_id: 'fw-revision-16', previous_revision_id: 'fw-revision-15', active_bank: 1, selector_state: 'exact', binding_version: 16 }],
  'rule-effectiveness': [{ epoch_id: 'epoch-71', rule_id: 'overlay-rule-8', target_id: 'target-edge-a', quality_status: 'valid', rate: 0.5, coverage: 1, outcome_status: 'observed', installation: { status: 'exact', observation_epoch: 7, reset_epoch: 2, readback: { status: 'applied', expected_entries: 1, observed_entries: 1, mismatched_entries: 0, active_bank: 1 } }, dataplane: { formula: 'direct_delta/eligible_delta', direct_packets: 18, direct_bytes: 1296, eligible_packets: 36, packet_match_ratio: 0.5, quality: 'valid', quality_reasons: ['NONE'], coverage: 1, read_completed_at_unix_ms: now() - 8_000 }, outcome: { status: 'observed', expected: 'dropped', actual: 'dropped' } }],
  'model-revision': [{ model_revision_id: 'model-rev-9', model_revision_digest: d, qualification_status: 'qualified', reader_runtime_profile: 'model-runtime-central-cpu/v1' }],
  'model-binding': [{ shard_id: 'shard-a', logical_pool_id: 'pool-central', current_generation: 9, current_binding_generation: 9, current_revision_id: 'model-rev-9', previous_generation: 8, previous_revision_id: 'model-rev-8', route_epoch: 12, resume_state: 'current', loaded: true, ready: true }],
  'model-rollout-group': [{ group_id: 'rollout-10', logical_pool_id: 'pool-central', target_generation: 10, target_revision_id: 'model-rev-10', ordered_shards: ['shard-a', 'shard-b'], status: 'rolling_mixed', next_index: 1, shards: [{ shard_id: 'shard-a', status: 'applied' }, { shard_id: 'shard-b', status: 'planned' }] }],
  'model-pool': [{ logical_pool_id: 'pool-central', current_generation: 9, availability_profile: 'availability-single/v1', runtime_profile: 'model-runtime-central-cpu/v1' }],
  plugin: [{ plugin_id: 'masi.statistics.fixture', manifest_id: 'manifest-fixture', manifest_revision: 3, manifest_digest: d, kind: 'pure-transform', runtime_profile: 'wasm-component/v1' }],
  'plugin-statistics-definition': [{ definition_id: 'fixture.alert-rate', definition_digest: d, plugin_id: 'masi.statistics.fixture', binding_generation: 3, producer_kind: 'pure-transform', display_hint: 'timeseries', revoked: false }],
  'plugin-statistics-run': [{ run_id: 'statrun-17', definition_id: 'fixture.alert-rate', status: 'succeeded', binding_generation: 3, started_at_unix_ms: now() - 20_000, finished_at_unix_ms: now() - 19_500, artifact_id: 'stat-artifact-17' }],
  'plugin-statistics-current': [{ definition_id: 'fixture.alert-rate', binding_generation: 3, artifact_id: 'stat-artifact-17', run_id: 'statrun-17', quality: 'valid', updated_at_unix_ms: now() - 19_500 }],
  'plugin-statistics-schedule': [{ schedule_id: 'stats-schedule-4', schedule_revision: 2, definition_id: 'fixture.alert-rate', definition_digest: d, interval_seconds: 300, disabled: false, data_class: 'internal', target_set_digest: d, reason_code: 'ACTIVE' }],
  'analysis-task': [{ task_id: 'analysis-task-8', plugin_id: 'masi.analysis.langgraph', binding_generation: 4, status: 'succeeded', poll_count: 2, deadline_unix_ms: now() + 20_000, remote_task_id: 'remote-8', updated_at_unix_ms: now() - 10_000 }],
  'analysis-artifact': [{ artifact_id: 'analysis-artifact-8', task_id: 'analysis-task-8', plugin_id: 'masi.analysis.langgraph', binding_generation: 4, artifact_digest: d, media_type: 'application/json', analysis_outcome: 'succeeded', non_executable: true, deployment_eligible: false, created_at_unix_ms: now() - 9_000 }],
  audit: [{ audit_id: 'audit-17', category: 'plugin-lifecycle', subject_id: 'masi.statistics.fixture', action: 'activate', outcome: 'recorded', reason_code: 'ACTIVE', actor_ref: 'actor:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', trace_id: 'trace-audit-17', created_at_unix_ms: now() - 7_000 }],
}

const paths = {
  '/api/events': 'event', '/api/incidents': 'incident', '/api/evidence': 'evidence',
  '/api/evidence/captures': 'bounded-capture', '/api/effects/proposals': 'proposal',
  '/api/effects/decisions': 'decision', '/api/effects/intents': 'intent', '/api/targets': 'target',
  '/api/fleet/operations': 'fleet-operation', '/api/firewall/revisions': 'firewall-revision',
  '/api/firewall/bindings': 'firewall-binding', '/api/rule-effectiveness': 'rule-effectiveness',
  '/api/models/revisions': 'model-revision', '/api/models/bindings': 'model-binding',
  '/api/models/rollout-groups': 'model-rollout-group', '/api/models/pools': 'model-pool',
  '/api/plugins': 'plugin', '/api/plugins/statistics/definitions': 'plugin-statistics-definition',
  '/api/plugins/statistics/runs': 'plugin-statistics-run', '/api/plugins/statistics/current': 'plugin-statistics-current',
  '/api/plugins/statistics/schedules': 'plugin-statistics-schedule', '/api/analysis/tasks': 'analysis-task',
  '/api/analysis/artifacts': 'analysis-artifact',
  '/api/audit': 'audit',
}

const statisticsArtifact = {
  schema_version: 'masi-plugin-statistics/v1', record_type: 'artifact', record_id: 'stat-artifact-17',
  artifact_digest: d, run_id: 'statrun-17', definition_id: 'fixture.alert-rate', definition_digest: d,
  status: 'succeeded', quality: 'valid',
  metrics: [{ metric_id: 'alert-rate', metric_kind: 'gauge', temporality: 'delta', value: 0.125, unit: 'ratio' }],
  series: [{ series_id: 'alerts', labels: { class: 'alert' }, point_count: 4, points: [{ timestamp_unix_ms: now() - 180_000, value: 0.08 }, { timestamp_unix_ms: now() - 120_000, value: 0.11 }, { timestamp_unix_ms: now() - 60_000, value: 0.09 }, { timestamp_unix_ms: now(), value: 0.125 }] }],
  tables: [{ table_id: 'alert-summary', columns: ['class', 'rate'], row_count: 1, rows: [['alert', 0.125]] }],
  truncation: { truncated_rows: 0, truncated_series: 0, reason_code: 'NONE' },
  provenance: { plugin_id: 'masi.statistics.fixture', plugin_revision: 'manifest-fixture:3', computed_at_unix_ms: now(), definition_id: 'fixture.alert-rate', definition_digest: d, run_id: 'statrun-17', binding_generation: 3 },
  bytes: 1024, actor_ref: 'masi.statistics.fixture', reason_code: 'SUCCEEDED', trace_id: 'trace-stat-17',
}

async function readBody(request) {
  const chunks = []
  let bytes = 0
  for await (const chunk of request) {
    bytes += chunk.length
    if (bytes > 4 * 1024 * 1024) throw new Error('BODY_OVERSIZE')
    chunks.push(chunk)
  }
  return chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : {}
}

function serveStatic(request, response) {
  const pathname = new URL(request.url, 'http://localhost').pathname
  const relative = normalize(pathname).replace(/^([/\\])+/, '')
  let file = join(root, relative || 'index.html')
  if (!file.startsWith(root) || !existsSync(file) || !statSync(file).isFile()) file = join(root, 'index.html')
  const immutable = file.includes(`${join(root, 'assets')}`)
  response.writeHead(200, { ...securityHeaders, 'Content-Type': mime[extname(file)] ?? 'application/octet-stream', 'Cache-Control': immutable ? 'public, max-age=31536000, immutable' : 'no-store' })
  createReadStream(file).pipe(response)
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url, `http://${request.headers.host}`)
  if (request.method === 'GET' && url.pathname === '/api/session') return json(response, 200, { schema_version: 'masi-web-projection/v1', actor_ref: 'actor:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', csrf_token: 'fixture-csrf-token-00000000000000000000000000000000', expires_at: Math.floor(Date.now() / 1000) + 3600, step_up: 'webauthn-fido2' })
  if (request.method === 'GET' && url.pathname === '/api/dashboard') return json(response, 200, { schema_version: 'masi-web-dashboard/v1', snapshot_id: `dashboard-${now()}`, snapshot_unix_ms: now(), generation: 7, state: 'partial', actor_ref: 'actor:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', authorized_scopes: ['scope-e2e'], counts: { events_24h: 2481, alerts_24h: 37, degraded_events_24h: 2, open_incidents: 4, targets_total: 3, targets_active: 2, targets_attention: 1, pending_approvals: 2, active_effects: 1, unknown_effects: 0, model_shards_ready: 2, model_shards_unavailable: 0, plugins_active: 2, analysis_attention: 0 }, recent_alerts: fixtures.event, active_operations: [{ effect_intent_id: 'intent-19', operation_id: 'operation-19', target_id: 'target-edge-a', effect_kind: 'firewall-overlay', risk_level: 'R2', claim_state: 'executing', deadline_unix_ms: now() + 120_000, reason_code: 'EXECUTING' }], target_health: [{ target_id: 'target-edge-a', display_name: 'Edge switch A', lifecycle: 'active', assignment_generation: 7, lease_expires_at_unix_ms: now() + 60_000 }, { target_id: 'target-edge-b', display_name: 'Edge switch B', lifecycle: 'quarantined', assignment_generation: null, lease_expires_at_unix_ms: null }], reason_code: 'DASHBOARD_ATTENTION' })
  if (request.method === 'GET' && paths[url.pathname]) return json(response, 200, projection(paths[url.pathname], fixtures[paths[url.pathname]] ?? []))
  if (request.method === 'GET' && url.pathname === '/api/effects/proposals/proposal-r2-41') return json(response, 200, { proposal_id: 'proposal-r2-41', proposal_digest: d, scope: 'scope-e2e', risk_level: 'R2', effect_kind: 'firewall-overlay', target_set_digest: d, target_ids: ['target-edge-a'], policy_digest: d, evidence_refs: ['evidence-88'], expires_at_unix_ms: now() + 300_000, note: 'Block the observed source for the bounded response window.', created_at_unix_ms: now() - 30_000, actor_ref: 'actor:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', reason_code: 'PROPOSED', governance_status: 'pending', superseded_by_proposal_id: null, superseded_at_unix_ms: null, supersede_reason_code: null })
  if (request.method === 'GET' && url.pathname === '/api/plugins/statistics/artifacts/stat-artifact-17') return json(response, 200, statisticsArtifact)
  if (request.method === 'GET' && url.pathname === '/events') {
    response.writeHead(200, { ...securityHeaders, 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', Connection: 'keep-alive', 'X-Accel-Buffering': 'no' })
    let sequence = 1
    const emit = (type) => {
      const event = { event_type: type, resource_kind: 'system-health', resource_id: type === 'heartbeat' ? 'heartbeat' : 'boot', cursor: `sse:7:${sequence}`, sequence, generation: 7, produced_at_unix_ms: now(), data_time_unix_ms: now(), payload_digest: d, bytes: 256, emitted_at_unix_ms: now() }
      sequence += 1
      response.write(`event: ${type}\ndata: ${JSON.stringify(event)}\n\n`)
    }
    emit('snapshot-refetch-required')
    const timer = setInterval(() => emit('heartbeat'), 15_000)
    request.on('close', () => clearInterval(timer))
    return
  }
  if (request.method === 'POST' && url.pathname.startsWith('/api/')) {
    if (request.headers['x-csrf-token'] !== 'fixture-csrf-token-00000000000000000000000000000000') return json(response, 403, { error: 'CSRF_REJECTED' })
    try {
      const body = await readBody(request)
      if (typeof body.idempotency_key !== 'string' || !body.idempotency_key) return json(response, 400, { error: 'IDEMPOTENCY_KEY_REQUIRED' })
      const bodyDigest = digest(JSON.stringify(body))
      const prior = mutations.get(body.idempotency_key)
      if (prior && prior !== bodyDigest) return json(response, 409, { error: 'IDEMPOTENCY_CONFLICT' })
      mutations.set(body.idempotency_key, bodyDigest)
      return json(response, url.pathname.includes('/advance') ? 200 : 201, { status: 'accepted', operation_id: `fixture-${body.idempotency_key}`, reason_code: 'ACCEPTED' })
    } catch { return json(response, 400, { error: 'BODY_MALFORMED' }) }
  }
  if (request.method === 'GET' && url.pathname === '/healthz') return json(response, 200, { state: 'ready' })
  return serveStatic(request, response)
})

server.listen(port, host, () => process.stdout.write(`web-contract-server http://${host}:${port} root=${root}\n`))
