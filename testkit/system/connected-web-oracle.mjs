import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'

const { chromium, firefox, webkit } = await import(
  new URL('../../web/node_modules/@playwright/test/index.mjs', import.meta.url)
)

function argument(name, fallback = '') {
  const index = process.argv.indexOf(name)
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback
}

function requireCondition(condition, message) {
  if (!condition) throw new Error(message)
}

function fileDigest(path) {
  return `sha256:${createHash('sha256').update(readFileSync(path)).digest('hex')}`
}

const browserName = argument('--browser', 'chromium')
const webBaseURL = argument('--web-base-url')
const controlBaseURL = argument('--control-base-url')
const eventID = argument('--event-id')
const decision = argument('--decision')
const incidentID = argument('--incident-id')
const statisticsDefinitionID = argument('--statistics-definition-id')
const statisticsArtifactID = argument('--statistics-artifact-id')
const analysisTaskID = argument('--analysis-task-id')
const analysisArtifactID = argument('--analysis-artifact-id')
const screenshotPath = argument('--screenshot')
const evidencePath = argument('--evidence')
const engines = { chromium, firefox, webkit }
const browserType = engines[browserName]
requireCondition(browserType, `unsupported browser ${browserName}`)
for (const [name, value] of Object.entries({ webBaseURL, controlBaseURL, eventID, decision, incidentID, screenshotPath, evidencePath })) {
  requireCondition(value, `missing ${name}`)
}
const sidePlaneArguments = { statisticsDefinitionID, statisticsArtifactID, analysisTaskID, analysisArtifactID }
const withSidePlanes = Object.values(sidePlaneArguments).some(Boolean)
if (withSidePlanes) {
  for (const [name, value] of Object.entries(sidePlaneArguments)) requireCondition(value, `missing ${name}`)
}

const startedAt = new Date().toISOString()
const login = await fetch(`${controlBaseURL}/oidc/test-login`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', Origin: webBaseURL, 'Sec-Fetch-Site': 'same-origin' },
  body: JSON.stringify({ issuer: 'https://idp.example', subject: 'operator-a' }),
  signal: AbortSignal.timeout(15_000),
})
requireCondition(login.status === 200, `test login status ${login.status}`)
const cookies = typeof login.headers.getSetCookie === 'function'
  ? login.headers.getSetCookie()
  : [login.headers.get('set-cookie')]
const cookieMatch = cookies.filter(Boolean).join(',').match(/(?:^|[, ]+)masi_session=([^;]+)/)
requireCondition(cookieMatch, 'test login did not return masi_session')

const browser = await browserType.launch({ headless: true, timeout: 30_000 })
let context
try {
  context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: 'en-US',
    timezoneId: 'UTC',
  })
  await context.addCookies([
    { name: 'masi_session', value: cookieMatch[1], url: webBaseURL, httpOnly: true, sameSite: 'Strict' },
  ])
  const page = await context.newPage()
  page.setDefaultTimeout(15_000)
  page.setDefaultNavigationTimeout(30_000)
  const consoleErrors = []
  const pageErrors = []
  const requestFailures = []
  const responseErrors = []
  let sseConnections = 0
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  page.on('pageerror', (error) => pageErrors.push(error.message))
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/events') sseConnections += 1
  })
  page.on('requestfailed', (request) => {
    const path = new URL(request.url()).pathname
    if (path !== '/events') requestFailures.push(`${path}:${request.failure()?.errorText ?? 'unknown'}`)
  })
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname
    if (response.status() >= 400 && path !== '/events') responseErrors.push(`${path}:${response.status()}`)
  })

  await page.goto(`${webBaseURL}/detection/events`, { waitUntil: 'domcontentloaded', timeout: 30_000 })
  await page.getByRole('heading', { name: 'Detection events' }).waitFor({ timeout: 15_000 })
  const session = await page.evaluate(async () => {
    const response = await fetch('/api/session')
    return { status: response.status, value: await response.json() }
  })
  requireCondition(
    session.status === 200 && session.value.schema_version === 'masi-web-projection/v1'
      && typeof session.value.csrf_token === 'string' && session.value.csrf_token.length >= 32,
    `${browserName}: authenticated session projection failed`,
  )
  await page.getByText(eventID, { exact: true }).first().waitFor({ timeout: 15_000 })
  await page.getByText(decision, { exact: true }).first().waitFor()
  await page.getByRole('button', { name: `Inspect ${eventID}` }).click()
  const eventDrawer = page.locator('.el-drawer:visible')
  await eventDrawer.getByText(eventID, { exact: true }).first().waitFor()
  await eventDrawer.getByText(decision, { exact: true }).first().waitFor()
  await page.keyboard.press('Escape')
  await eventDrawer.waitFor({ state: 'hidden', timeout: 5_000 })

  await page.getByRole('link', { name: 'Incidents', exact: true }).click()
  await page.getByRole('heading', { name: 'Incident queue' }).waitFor({ timeout: 15_000 })
  await page.getByText(incidentID, { exact: true }).first().waitFor({ timeout: 15_000 })
  await page.getByRole('button', { name: `Inspect ${incidentID}` }).click()
  const incidentDrawer = page.locator('.el-drawer:visible')
  await incidentDrawer.getByText(incidentID, { exact: true }).first().waitFor()

  let sidePlaneObservation = null
  if (withSidePlanes) {
    sidePlaneObservation = await page.evaluate(async ({ statisticsDefinitionID, statisticsArtifactID, analysisTaskID, analysisArtifactID }) => {
      async function read(path) {
        const controller = new AbortController()
        const timeout = setTimeout(() => controller.abort(), 15_000)
        try {
          const response = await fetch(path, { signal: controller.signal })
          return { status: response.status, value: await response.json() }
        } finally {
          clearTimeout(timeout)
        }
      }
      return {
        statisticsCurrent: await read('/api/plugins/statistics/current?page_size=50'),
        statisticsArtifact: await read(`/api/plugins/statistics/artifacts/${encodeURIComponent(statisticsArtifactID)}`),
        analysisTasks: await read('/api/analysis/tasks?page_size=50'),
        analysisArtifacts: await read('/api/analysis/artifacts?page_size=50'),
        analysisArtifact: await read(`/api/analysis/artifacts/${encodeURIComponent(analysisArtifactID)}`),
        expected: { statisticsDefinitionID, statisticsArtifactID, analysisTaskID, analysisArtifactID },
      }
    }, sidePlaneArguments)
    const currentItem = sidePlaneObservation.statisticsCurrent.value.items?.find(
      (item) => item.definition_id === statisticsDefinitionID && item.artifact_id === statisticsArtifactID,
    )
    requireCondition(sidePlaneObservation.statisticsCurrent.status === 200 && currentItem, `${browserName}: statistics current projection missing`)
    const statisticsArtifact = sidePlaneObservation.statisticsArtifact.value
    requireCondition(
      sidePlaneObservation.statisticsArtifact.status === 200
        && statisticsArtifact.record_id === statisticsArtifactID
        && statisticsArtifact.definition_id === statisticsDefinitionID
        && statisticsArtifact.status === 'succeeded'
        && statisticsArtifact.quality === 'valid'
        && statisticsArtifact.metrics?.some((metric) => metric.metric_id === 'row-count' && metric.value === 2),
      `${browserName}: exact statistics artifact failed validation`,
    )
    const analysisTask = sidePlaneObservation.analysisTasks.value.items?.find((item) => item.task_id === analysisTaskID)
    const analysisArtifactListItem = sidePlaneObservation.analysisArtifacts.value.items?.find(
      (item) => item.artifact_id === analysisArtifactID && item.task_id === analysisTaskID,
    )
    const analysisArtifact = sidePlaneObservation.analysisArtifact.value
    requireCondition(
      sidePlaneObservation.analysisTasks.status === 200
        && ['succeeded', 'limited'].includes(analysisTask?.status),
      `${browserName}: Analysis task projection missing or non-terminal`,
    )
    requireCondition(
      sidePlaneObservation.analysisArtifacts.status === 200
        && analysisArtifactListItem?.non_executable === true
        && analysisArtifactListItem?.deployment_eligible === false
        && sidePlaneObservation.analysisArtifact.status === 200
        && analysisArtifact.artifact_id === analysisArtifactID
        && analysisArtifact.non_executable === true
        && analysisArtifact.deployment_eligible === false,
      `${browserName}: Analysis artifact executable boundary drifted`,
    )

    if (await incidentDrawer.isVisible().catch(() => false)) {
      await page.keyboard.press('Escape')
      await incidentDrawer.waitFor({ state: 'hidden', timeout: 5_000 })
    }
    await page.getByRole('link', { name: 'Statistics', exact: true }).click()
    await page.getByRole('heading', { name: 'Plugin statistics' }).waitFor({ timeout: 15_000 })
    await page.getByText(statisticsDefinitionID, { exact: true }).first().waitFor({ timeout: 15_000 })
    await page.locator('button').filter({ hasText: statisticsDefinitionID }).first().click()
    await page.getByRole('heading', { name: statisticsDefinitionID, exact: true }).waitFor({ timeout: 15_000 })
    const metricRegion = page.getByRole('region', { name: 'Artifact metrics' })
    await metricRegion.getByText('row-count', { exact: true }).waitFor()
    await metricRegion.getByText('2', { exact: true }).waitFor()

    await page.getByRole('link', { name: 'Agent tasks', exact: true }).click()
    await page.getByRole('heading', { name: 'Evidence analysis' }).waitFor({ timeout: 15_000 })
    await page.getByText(analysisTaskID, { exact: true }).first().waitFor({ timeout: 15_000 })
    await page.getByRole('button', { name: 'Artifacts', exact: true }).click()
    await page.getByText(analysisArtifactID, { exact: true }).first().waitFor({ timeout: 15_000 })
    await page.getByRole('button', { name: `Inspect ${analysisArtifactID}` }).click()
    const drawer = page.locator('.el-drawer:visible')
    await drawer.getByText(analysisArtifactID, { exact: true }).first().waitFor()
    const nonExecutableFact = drawer.locator('.fact').filter({ hasText: 'Non executable' })
    const deploymentEligibleFact = drawer.locator('.fact').filter({ hasText: 'Deployment eligible' })
    await nonExecutableFact.getByText('Yes', { exact: true }).waitFor()
    await deploymentEligibleFact.getByText('No', { exact: true }).waitFor()
  }
  await page.screenshot({ path: screenshotPath, fullPage: true })

  requireCondition(sseConnections === 1, `${browserName}: expected one SSE connection, got ${sseConnections}`)
  requireCondition(consoleErrors.length === 0, `${browserName}: console errors ${consoleErrors.join('; ')}`)
  requireCondition(pageErrors.length === 0, `${browserName}: page errors ${pageErrors.join('; ')}`)
  requireCondition(requestFailures.length === 0, `${browserName}: request failures ${requestFailures.join('; ')}`)
  requireCondition(responseErrors.length === 0, `${browserName}: response errors ${responseErrors.join('; ')}`)

  const result = {
    schema_version: 'connected-web-browser-observation/v1',
    browser: browserName,
    event_id: eventID,
    decision,
    incident_id: incidentID,
    session_schema: session.value.schema_version,
    csrf_present: true,
    sse_connections: sseConnections,
    console_errors: consoleErrors,
    page_errors: pageErrors,
    request_failures: requestFailures,
    response_errors: responseErrors,
    screenshot_digest: fileDigest(screenshotPath),
    started_at: startedAt,
    finished_at: new Date().toISOString(),
    result: 'PASS',
  }
  if (withSidePlanes) {
    Object.assign(result, {
      statistics_definition_id: statisticsDefinitionID,
      statistics_artifact_id: statisticsArtifactID,
      statistics_metric_value: 2,
      analysis_task_id: analysisTaskID,
      analysis_artifact_id: analysisArtifactID,
      analysis_outcome: sidePlaneObservation.analysisArtifact.value.analysis_outcome,
      non_executable: sidePlaneObservation.analysisArtifact.value.non_executable,
      deployment_eligible: sidePlaneObservation.analysisArtifact.value.deployment_eligible,
    })
  }
  writeFileSync(evidencePath, `${JSON.stringify(result, null, 2)}\n`, { encoding: 'utf8', flag: 'wx' })
  process.stdout.write(`${JSON.stringify(result)}\n`)
} finally {
  if (context) await context.close().catch(() => {})
  await browser.close().catch(() => {})
}
