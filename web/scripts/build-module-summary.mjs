import { readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createHash } from 'node:crypto'
import Ajv2020 from 'ajv/dist/2020.js'
import addFormats from 'ajv-formats'

function argument(name) {
  const index = process.argv.indexOf(name)
  if (index < 0 || !process.argv[index + 1]) throw new Error(`missing ${name}`)
  return process.argv[index + 1]
}

const moduleRoot = resolve(import.meta.dirname, '..')
const repoRoot = resolve(moduleRoot, '..')
const runRoot = resolve(argument('--run-root'))
const runID = argument('--run-id')
const startedAt = argument('--started-at')
const sourceTreeDigest = argument('--source-tree-digest')
const workingTreeDirty = argument('--working-tree-dirty') === 'true'
const workingTreeStatusDigest = argument('--working-tree-status-digest')

const load = (path) => JSON.parse(readFileSync(path, 'utf8'))
const sha256 = (path) => `sha256:${createHash('sha256').update(readFileSync(path)).digest('hex')}`
const commandDirectory = resolve(runRoot, 'commands')
const commands = readdirSync(commandDirectory)
  .filter((name) => name.endsWith('.json'))
  .sort()
  .map((name) => load(resolve(commandDirectory, name)))
  .map(({ command_id, result, qualification, duration_ms, log_digest }) => ({ command_id, result, qualification, duration_ms, log_digest }))

const parseLastObject = (path, schemaVersion) => {
  const lines = readFileSync(path, 'utf8').trim().split('\n').reverse()
  for (const line of lines) {
    try {
      const value = JSON.parse(line)
      if (value.schema_version === schemaVersion) return value
    } catch { /* command logs include non-JSON tool output */ }
  }
  throw new Error(`missing ${schemaVersion} in ${path}`)
}

const bundle = parseLastObject(resolve(runRoot, 'logs', 'bundle.log'), 'web-bundle-budget/v1')
const browser = load(resolve(runRoot, 'browser.json'))
const oci = load(resolve(runRoot, 'oci.json'))
const performanceEvidence = load(resolve(runRoot, 'performance.json'))
const soak = load(resolve(runRoot, 'soak.json'))
const findingsPath = resolve(moduleRoot, 'module-findings.json')
const findings = load(findingsPath)
const openP0 = findings.findings.filter((entry) => entry.status === 'OPEN' && entry.severity === 'P0').length
const commandFailures = commands.filter((entry) => entry.result !== 'PASS')
const browserFailures = (browser.stats?.unexpected ?? 0) + (browser.stats?.flaky ?? 0)
const browserTests = browser.stats?.expected ?? 0
const formalSoak = soak.warmup_seconds === 60 && soak.phase_seconds === 900 && soak.qualified_elapsed_seconds >= 3600 && soak.qualification === 'QUALIFIED'
const performanceMaximum = (field) => Math.max(...performanceEvidence.groups.map((entry) => entry[field]))
const operationalComplete = commandFailures.length === 0
  && browserTests >= 18 && browserFailures === 0
  && bundle.result === 'PASS' && oci.result === 'PASS'
  && oci.source_tree_digest === sourceTreeDigest
  && performanceEvidence.result === 'PASS'
  && soak.result === 'PASS' && formalSoak && soak.errors.length === 0
  && openP0 === 0

const summary = {
  schema_version: 'web-spa-module-gate-summary/v1',
  module_id: 'MOD-WEB-001',
  run_id: runID,
  started_at: startedAt,
  finished_at: new Date().toISOString(),
  source_tree_digest: sourceTreeDigest,
  working_tree_dirty: workingTreeDirty,
  working_tree_status_digest: workingTreeStatusDigest,
  profiles: { spa: 'web-spa/v1', browser: 'web-browser/v1', performance: 'web-performance/v1' },
  commands,
  browser_matrix: {
    driver: '@playwright/test@1.62.1',
    chromium: '151.0.7922.34@1234',
    firefox: '153.0@1538',
    webkit: '26.5@2336',
    tests: browserTests,
    failures: browserFailures,
  },
  bundle: {
    initial_javascript_gzip_bytes: bundle.initial_javascript_gzip_bytes,
    initial_css_gzip_bytes: bundle.initial_css_gzip_bytes,
    maximum_route_gzip_bytes: bundle.maximum_route_gzip_bytes,
    result: bundle.result,
  },
  performance: {
    lcp_p75_ms: performanceMaximum('lcp_p75_ms'),
    inp_p75_ms: performanceMaximum('inp_p75_ms'),
    cls_p75: performanceMaximum('cls_p75'),
    route_navigation_p95_ms: performanceMaximum('route_navigation_p95_ms'),
    api_concurrency: performanceMaximum('api_concurrency_max'),
    sse_connections_per_tab: performanceMaximum('sse_connections_max'),
    result: performanceEvidence.result,
  },
  oci: {
    image_digest: oci.image_digest,
    user: oci.user,
    health: oci.health,
    deep_link: oci.deep_link,
    same_origin_proxy: oci.same_origin_proxy,
    security_headers: oci.security_headers,
    result: oci.result,
  },
  soak: {
    warmup_seconds: soak.warmup_seconds,
    qualified_elapsed_seconds: soak.qualified_elapsed_seconds,
    phases: soak.phases.map((entry) => entry.phase),
    errors: soak.errors.length,
    result: soak.result,
  },
  findings: { open_p0: openP0, registry_digest: sha256(findingsPath) },
  level: 'MODULE',
  applicability: 'APPLICABLE',
  result: operationalComplete ? 'HOLD' : (commandFailures.length > 0 ? 'FAIL' : 'HOLD'),
  qualification: 'NOT_QUALIFIED',
  qualification_scope: operationalComplete
    ? 'DEC-044_OPERATIONAL_COMPLETE; FORMAL_GO_WEB_PAIRWISE_SYSTEM_PROTECTED_BASELINE_NOT_QUALIFIED'
    : 'WEB_OPERATIONAL_GATES_INCOMPLETE',
  overall_module_complete: operationalComplete,
}

const schema = load(resolve(repoRoot, 'contracts/evidence/web-module/v1/schema.json'))
const ajv = new Ajv2020({ allErrors: true, strict: false })
addFormats(ajv)
const validate = ajv.compile(schema)
if (!validate(summary)) throw new Error(ajv.errorsText(validate.errors))
if (summary.overall_module_complete && (summary.result === 'FAIL' || summary.result === 'NOT_RUN')) throw new Error('complete Web module has invalid result')

const output = resolve(runRoot, 'gate-summary.json')
writeFileSync(output, `${JSON.stringify(summary, null, 2)}\n`, { encoding: 'utf8', flag: 'wx' })
process.stdout.write(`${JSON.stringify({ output, result: summary.result, overall_module_complete: summary.overall_module_complete })}\n`)
if (!summary.overall_module_complete) process.exitCode = summary.result === 'FAIL' ? 1 : 2
