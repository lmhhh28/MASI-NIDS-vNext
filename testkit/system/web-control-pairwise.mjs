import { writeFileSync } from 'node:fs'

const { chromium, firefox, webkit } = await import(new URL('../../web/node_modules/@playwright/test/index.mjs', import.meta.url))

function argument(name, fallback = '') {
  const index = process.argv.indexOf(name)
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback
}

function requireCondition(condition, message) {
  if (!condition) throw new Error(message)
}

const webBaseURL = argument('--web-base-url', 'http://127.0.0.1:4189')
const controlBaseURL = argument('--control-base-url', 'http://127.0.0.1:18080')
const evidencePath = argument('--evidence')
const selectedBrowser = argument('--browser', 'all')
const controlImageDigest = argument('--control-image-digest')
const webImageDigest = argument('--web-image-digest')
const startedAt = new Date().toISOString()
const targetSetDigest = 'sha256:b040b50e2f8f96bfcee9bdd70ca819d01ace68e4af8c5a28015c738eb7ac607e'
const exactDigest = `sha256:${'a'.repeat(64)}`
const resourcePaths = [
  '/api/dashboard', '/api/events', '/api/incidents', '/api/evidence', '/api/evidence/captures',
  '/api/effects/proposals', '/api/effects/decisions', '/api/effects/intents', '/api/targets',
  '/api/fleet/operations', '/api/firewall/revisions', '/api/firewall/bindings', '/api/rule-effectiveness',
  '/api/models/revisions', '/api/models/bindings', '/api/models/rollout-groups', '/api/models/pools',
  '/api/plugins', '/api/plugins/statistics/definitions', '/api/plugins/statistics/runs',
  '/api/plugins/statistics/current', '/api/plugins/statistics/schedules', '/api/analysis/tasks',
  '/api/analysis/artifacts', '/api/audit',
]

async function navigate(page, path, heading) {
  const target = `${webBaseURL}${path}`
  const errors = []
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try { await page.goto(target, { waitUntil: 'domcontentloaded', timeout: 15_000 }) } catch (error) { errors.push(error instanceof Error ? error.message : String(error)) }
    try {
      await page.getByRole('heading', { name: heading }).waitFor({ timeout: 5_000 })
      return attempt
    } catch (error) {
      errors.push(error instanceof Error ? error.message : String(error))
    }
  }
  throw new Error(`navigation failed after 3 attempts; url=${page.url()}; ${errors.join(' | ')}`)
}

async function loginCookie() {
  const response = await fetch(`${controlBaseURL}/oidc/test-login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Origin: webBaseURL, 'Sec-Fetch-Site': 'same-origin' },
    body: JSON.stringify({ issuer: 'https://idp.example', subject: 'operator-a' }),
  })
  requireCondition(response.status === 200, `test login status ${response.status}`)
  const setCookies = typeof response.headers.getSetCookie === 'function' ? response.headers.getSetCookie() : [response.headers.get('set-cookie')]
  const match = setCookies.filter(Boolean).join(',').match(/(?:^|[, ]+)masi_session=([^;]+)/)
  requireCondition(match, 'test login did not return the opaque session cookie')
  return match[1]
}

async function runBrowser(name, browserType) {
  const cookieValue = await loginCookie()
  const browser = await browserType.launch({ headless: true })
  let context
  try {
    context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'en-US', timezoneId: 'UTC' })
    await context.addCookies([{ name: 'masi_session', value: cookieValue, url: webBaseURL, httpOnly: true, sameSite: 'Strict' }])
    const page = await context.newPage()
    const pageErrors = []
    const requestFailures = []
    let sseConnections = 0
    page.on('pageerror', (error) => pageErrors.push(error.message))
    page.on('request', (request) => { if (new URL(request.url()).pathname === '/events') sseConnections += 1 })
    page.on('requestfailed', (request) => {
      const path = new URL(request.url()).pathname
      if (path !== '/events') requestFailures.push(`${path}:${request.failure()?.errorText ?? 'unknown'}`)
    })

    const initialNavigationAttempts = await navigate(page, '/overview', 'Operational evidence at a glance')
    const session = await page.evaluate(async () => ({ status: 0, body: await fetch('/api/session').then(async (response) => ({ status: response.status, value: await response.json() })) }))
    requireCondition(session.body.status === 200 && session.body.value.schema_version === 'masi-web-projection/v1', `${name}: authenticated session projection failed`)
    requireCondition(typeof session.body.value.csrf_token === 'string' && session.body.value.csrf_token.length >= 32, `${name}: CSRF token missing`)

    const projections = await page.evaluate(async (paths) => Promise.all(paths.map(async (path) => {
      const response = await fetch(path)
      const value = await response.json()
      return { path, status: response.status, schema: value.schema_version, projection: value.projection_type, total: value.total_count }
    })), resourcePaths)
    for (const projection of projections) {
      requireCondition(projection.status === 200, `${name}: ${projection.path} status ${projection.status}`)
      const expectedSchema = projection.path === '/api/dashboard' ? 'masi-web-dashboard/v1' : 'masi-web-projection/v1'
      requireCondition(projection.schema === expectedSchema, `${name}: ${projection.path} schema ${projection.schema}`)
    }
    const targetsBefore = await page.evaluate(async () => (await fetch('/api/targets')).json())
    const targetIDsBefore = new Set(targetsBefore.items.map((item) => item.target_id))

    const csrfDenied = await page.evaluate(async () => (await fetch('/api/targets', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ idempotency_key: 'missing-csrf' }),
    })).status)
    requireCondition(csrfDenied === 403, `${name}: mutation without CSRF returned ${csrfDenied}`)

    await page.getByRole('link', { name: 'Managed targets', exact: true }).first().click()
    await page.getByRole('heading', { name: 'Managed targets' }).waitFor()
    await page.getByRole('button', { name: 'Register target' }).click()
    const dialog = page.getByRole('dialog', { name: 'Register stable target' })
    const displayName = `P9 ${name} target`
    await dialog.getByLabel('Display name').fill(displayName)
    await dialog.getByLabel('P4Runtime TLS endpoint').fill(`https://${name}.p9.example:9559`)
    await dialog.getByLabel('Desired profile digest').fill(exactDigest)
    await dialog.getByLabel('Credential reference').fill(`credential:p9:${name}`)
    await dialog.getByLabel('Authorization scope').fill('scope-e2e')
    await dialog.getByLabel('TLS server name').fill(`${name}.p9.example`)
    await dialog.getByLabel('TLS identity reference').fill(`credential:p9:${name}`)
    await dialog.getByLabel('Frozen target-set digest').fill(targetSetDigest)
    await dialog.getByRole('button', { name: 'Submit controlled operation' }).click()
    await page.getByText('Connection or primary state is not inferred').waitFor()

    const targetProjection = await page.evaluate(async () => (await fetch('/api/targets')).json())
    const registered = targetProjection.items.find((item) => !targetIDsBefore.has(item.target_id))
    requireCondition(targetProjection.total_count === targetsBefore.total_count + 1, `${name}: target count did not advance exactly once`)
    requireCondition(registered?.display_name === displayName, `${name}: registered target absent from PostgreSQL projection`)
    requireCondition(registered?.p4runtime_endpoint === `https://${name}.p9.example:9559`, `${name}: endpoint projection drift`)
    requireCondition(registered?.profile_alignment === 'not-observed' && registered?.lease_state === 'unassigned', `${name}: candidate target inferred assignment/readback`)
    await page.getByRole('link', { name: 'Overview', exact: true }).click()
    await page.getByRole('heading', { name: 'Operational evidence at a glance' }).waitFor()
    requireCondition(sseConnections === 1, `${name}: expected one SSE connection, observed ${sseConnections}`)
    requireCondition(pageErrors.length === 0, `${name}: page errors: ${pageErrors.join('; ')}`)
    requireCondition(requestFailures.length === 0, `${name}: request failures: ${requestFailures.join('; ')}`)

    return {
      browser: name,
      initial_navigation_attempts: initialNavigationAttempts,
      projections_checked: projections.length,
      csrf_negative_status: csrfDenied,
      target_registration_observed: true,
      sse_connections_per_tab: sseConnections,
      page_errors: pageErrors,
      request_failures: requestFailures,
      result: 'PASS',
    }
  } finally {
    if (context) await context.close().catch(() => {})
    await browser.close().catch(() => {})
  }
}

const runs = []
const failures = []
const engines = [['chromium', chromium], ['firefox', firefox], ['webkit', webkit]].filter(([name]) => selectedBrowser === 'all' || selectedBrowser === name)
requireCondition(engines.length > 0, `unsupported browser ${selectedBrowser}`)
for (const [name, browserType] of engines) {
  try { runs.push(await runBrowser(name, browserType)) } catch (error) { failures.push(`${name}:${error instanceof Error ? error.message : String(error)}`) }
}
const result = {
  schema_version: 'web-control-pairwise-rehearsal/v1',
  boundary: 'P9_GO_CONTROL_POSTGRESQL_TO_PRODUCTION_WEB',
  started_at: startedAt,
  finished_at: new Date().toISOString(),
  participants: {
    postgresql: 'postgresql-18-test-container',
    control_core: controlImageDigest,
    web: webImageDigest,
    browsers: '@playwright/test@1.62.1 exact three-engine profile',
  },
  real_boundaries: ['HTTP generated-client projections', 'opaque session cookie', 'CSRF', 'SSE', 'PostgreSQL target fact'],
  runs,
  failures,
  result: failures.length === 0 && runs.length === engines.length ? 'PASS' : 'FAIL',
  qualification: 'NOT_QUALIFIED',
  qualification_scope: 'REAL_P9_REHEARSAL_WITH_TEST_PROFILE_PLAINTEXT_TEST_LOGIN; FORMAL_HTTPS_OIDC_PAIRWISE_NOT_CLAIMED',
}
if (evidencePath) writeFileSync(evidencePath, `${JSON.stringify(result, null, 2)}\n`, { encoding: 'utf8', flag: 'wx' })
process.stdout.write(`${JSON.stringify(result, null, 2)}\n`)
if (result.result !== 'PASS') process.exitCode = 1
