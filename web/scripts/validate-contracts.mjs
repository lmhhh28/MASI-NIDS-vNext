import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import Ajv2020 from 'ajv/dist/2020.js'
import addFormats from 'ajv-formats'

const repo = resolve('..')
const load = (path) => JSON.parse(readFileSync(resolve(repo, path), 'utf8'))
const ajv = new Ajv2020({ allErrors: true, strict: false })
addFormats(ajv)

const schemas = [
  'contracts/web/v1/schema.json',
  'contracts/web/v1/dashboard.schema.json',
  'contracts/supply-chain/v1/schema.json',
  'contracts/evidence/module-findings/v1/schema.json',
  'contracts/evidence/command/v1/schema.json',
  'contracts/evidence/traceability/v1/schema.json',
  'contracts/evidence/web-module/v1/schema.json',
]
for (const path of schemas) {
  const schema = load(path)
  if (!ajv.validateSchema(schema)) throw new Error(`${path}: invalid schema: ${ajv.errorsText()}`)
  ajv.addSchema(schema, path)
}

function assertValid(path, value, label) {
  const validator = ajv.getSchema(path)
  if (!validator || !validator(value)) throw new Error(`${label}: ${ajv.errorsText(validator?.errors)}`)
}

function assertRejected(path, value, label) {
  const validator = ajv.getSchema(path)
  if (!validator || validator(value)) throw new Error(`${label}: invalid vector was accepted`)
}

const projection = {
  schema_version: 'masi-web-projection/v1', projection_type: 'current', resource_kind: 'analysis-artifact',
  cursor: '', page_size: 50, total_count: 0, items: [], generation: 1,
  session_scope: 'scope-a,scope-b', authorized_scope: 'scope-a,scope-b', sse_event: null,
  actor_ref: 'actor:abc', reason_code: 'OK', trace_id: '',
}
assertValid('contracts/web/v1/schema.json', projection, 'projection-positive-list-envelope')
assertRejected('contracts/web/v1/schema.json', { ...projection, schema_version: 'masi-web-projection/v2' }, 'projection-unknown-major')
assertRejected('contracts/web/v1/schema.json', { ...projection, resource_kind: 'remote-plugin-route' }, 'projection-unknown-kind')

const dashboard = {
  schema_version: 'masi-web-dashboard/v1', snapshot_id: 'dashboard-1', snapshot_unix_ms: 1, generation: 1,
  state: 'ready', actor_ref: 'actor:abc', authorized_scopes: ['scope-a'],
  counts: { events_24h: 0, alerts_24h: 0, degraded_events_24h: 0, open_incidents: 0, targets_total: 0, targets_active: 0, targets_attention: 0, pending_approvals: 0, active_effects: 0, unknown_effects: 0, model_shards_ready: 0, model_shards_unavailable: 0, plugins_active: 0, analysis_attention: 0 },
  recent_alerts: [], active_operations: [], target_health: [], reason_code: 'DASHBOARD_READY',
}
assertValid('contracts/web/v1/dashboard.schema.json', dashboard, 'dashboard-positive')
assertRejected('contracts/web/v1/dashboard.schema.json', { ...dashboard, target_health: Array.from({ length: 9 }, () => ({})) }, 'dashboard-section-overflow')

assertValid('contracts/supply-chain/v1/schema.json', load('contracts/supply-chain/v1/web-components.json'), 'web-supply-registry')
assertValid('contracts/evidence/module-findings/v1/schema.json', load('web/module-findings.json'), 'web-findings')

const packageJSON = load('web/package.json')
for (const [profilePath, profileID] of [
  ['contracts/profiles/v1/web-spa.json', 'web-spa/v1'],
  ['contracts/profiles/v1/web-browser.json', 'web-browser/v1'],
  ['contracts/profiles/v1/web-performance.json', 'web-performance/v1'],
]) {
  const profile = load(profilePath)
  if (profile.schema_version !== 'masi-profile/v1' || profile.profile_id !== profileID || profile.profile_version !== '1.0.0') throw new Error(`${profilePath}: identity drift`)
}
const spa = load('contracts/profiles/v1/web-spa.json')
const expected = { vue: '3.5.41', vue_router: '5.2.0', pinia: '4.0.3', element_plus: '2.14.5', echarts: '6.1.0', tanstack_vue_query: '5.102.5', tanstack_vue_virtual: '3.13.36', vueuse: '14.4.0' }
const packageNames = { vue: 'vue', vue_router: 'vue-router', pinia: 'pinia', element_plus: 'element-plus', echarts: 'echarts', tanstack_vue_query: '@tanstack/vue-query', tanstack_vue_virtual: '@tanstack/vue-virtual', vueuse: '@vueuse/core' }
for (const [profileName, version] of Object.entries(expected)) {
  if (spa.dependencies[profileName] !== version || packageJSON.dependencies[packageNames[profileName]] !== version) throw new Error(`web-spa dependency drift: ${profileName}`)
}

const openapi = readFileSync(resolve(repo, 'contracts/openapi/v1/openapi.yaml'), 'utf8')
for (const fragment of ['/api/dashboard:', '/api/analysis/artifacts:', '/api/audit:', 'operationId: getDashboard', 'EffectProposalDetail:']) {
  if (!openapi.includes(fragment)) throw new Error(`OpenAPI missing ${fragment}`)
}
const sdk = readFileSync(resolve(repo, 'contracts/generated/typescript/control-api/sdk.gen.ts'), 'utf8')
for (const operation of ['getDashboard', 'listAuditFacts', 'listAnalysisArtifacts', 'listRuleEffectiveness', 'getStatisticsArtifact']) {
  if (!sdk.includes(`export const ${operation}`)) throw new Error(`generated SDK missing ${operation}`)
}

process.stdout.write(`${JSON.stringify({ schema_version: 'web-contract-validation/v1', schemas: schemas.length, positive_vectors: 4, negative_vectors: 5, generated_operations: 5, result: 'PASS', qualification: 'NOT_QUALIFIED', qualification_scope: 'CONTRACT_AND_GENERATED_CLIENT_GATE' })}\n`)
