import { readFileSync, writeFileSync } from 'node:fs'
import { cpus, totalmem } from 'node:os'
import { chromium } from '@playwright/test'
import { classifyPerformance, maximum, thresholdExceeded } from './performance-evidence.mjs'

function argument(name, fallback) {
  const index = process.argv.indexOf(name)
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback
}

const baseURL = argument('--base-url', process.env.WEB_E2E_BASE_URL ?? 'http://127.0.0.1:4180')
const evidencePath = argument('--evidence', '')
const performanceProfile = JSON.parse(readFileSync(new URL('../../contracts/profiles/v1/web-performance.json', import.meta.url), 'utf8'))
const browserProfile = JSON.parse(readFileSync(new URL('../../contracts/profiles/v1/web-browser.json', import.meta.url), 'utf8'))
const expectedChromium = browserProfile.engines.find((entry) => entry.name === 'chromium')
const expectedBrowserVersion = expectedChromium?.browser.match(/[0-9]+(?:\.[0-9]+)+$/)?.[0]
if (!expectedChromium || !expectedBrowserVersion) throw new Error('web-browser/v1 Chromium identity is malformed')
function boundedSeconds(name, fallback, maximumValue) {
  const value = Number(argument(name, String(fallback)))
  if (!Number.isSafeInteger(value) || value < 0 || value > maximumValue) throw new RangeError(`${name} is outside its bound`)
  return value
}
const warmupSeconds = boundedSeconds('--warmup-seconds', 60, 3600)
const phaseSeconds = boundedSeconds('--phase-seconds', 900, 7200)
const sourceTreeDigest = argument('--source-tree-digest', '')
const imageDigest = argument('--image-digest', '')
const digestPattern = /^sha256:(?!0{64}$)[0-9a-f]{64}$/
const profile = 'qualification-soak-3600s/v1'
const routeSets = {
  steady: [
    { link: 'Overview', heading: 'Operational evidence at a glance' },
    { link: 'Events', heading: 'Detection events' },
    { link: 'Managed targets', heading: 'Managed targets' },
  ],
  peak: [
    { link: 'Statistics', heading: 'Plugin statistics' },
    { link: 'Rule effectiveness', heading: 'Rule effectiveness' },
    { link: 'Models', heading: 'Model operations' },
    { link: 'Overview', heading: 'Operational evidence at a glance' },
  ],
  saturation: [
    { link: 'Events', heading: 'Detection events' },
    { link: 'Firewall policies', heading: 'Firewall policy revisions' },
    { link: 'Fleet operations', heading: 'Fleet operations' },
    { link: 'Catalog', heading: 'Plugin catalog' },
  ],
  recovery: [
    { link: 'Overview', heading: 'Operational evidence at a glance' },
    { link: 'Statistics', heading: 'Plugin statistics' },
    { link: 'Audit trail', heading: 'Audit trail' },
  ],
}

const startedAt = new Date().toISOString()
const monotonicStart = performance.now()
const browser = await chromium.launch({ headless: true, args: ['--js-flags=--expose-gc'] })
const browserVersion = browser.version()
const revisionMatch = chromium.executablePath().match(/chromium-(\d+)/)
const observedPlaywrightRevision = revisionMatch ? Number(revisionMatch[1]) : null
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'en-US', timezoneId: 'UTC' })
const page = await context.newPage()
const cdp = await context.newCDPSession(page)
await cdp.send('Performance.enable')
const failures = []
const samples = []
const phaseEvidence = []
let apiMaximum = 0
let sseMaximum = 0
const apiByPage = new Map()
const sseByPage = new Map()
const activeAPI = new Map()
const activeSSE = new Map()

function requestStart(request) {
  const path = new URL(request.url()).pathname
  const owner = request.frame().page()
  if (path.startsWith('/api/')) {
    activeAPI.set(request, owner)
    apiByPage.set(owner, (apiByPage.get(owner) ?? 0) + 1)
    apiMaximum = Math.max(apiMaximum, apiByPage.get(owner))
  }
  if (path === '/events') {
    for (const [priorRequest, priorOwner] of activeSSE) {
      if (priorOwner === owner) activeSSE.delete(priorRequest)
    }
    activeSSE.set(request, owner)
    sseByPage.set(owner, 1)
    sseMaximum = Math.max(sseMaximum, sseByPage.get(owner))
  }
}

function requestEnd(request) {
  const apiOwner = activeAPI.get(request)
  if (apiOwner) {
    apiByPage.set(apiOwner, Math.max(0, (apiByPage.get(apiOwner) ?? 1) - 1))
    activeAPI.delete(request)
  }
  const sseOwner = activeSSE.get(request)
  if (sseOwner) {
    sseByPage.set(sseOwner, Math.max(0, (sseByPage.get(sseOwner) ?? 1) - 1))
    activeSSE.delete(request)
  }
}

function attachRequestEvidence(browserPage) {
  browserPage.on('request', requestStart)
  browserPage.on('requestfinished', requestEnd)
  browserPage.on('requestfailed', (request) => {
    requestEnd(request)
    const path = new URL(request.url()).pathname
    const errorText = request.failure()?.errorText ?? 'unknown'
    const expectedAbort = errorText === 'net::ERR_ABORTED'
    if (path !== '/events' && !expectedAbort && !errorText.includes('ERR_INTERNET_DISCONNECTED')) {
      failures.push(`request-failed:${path}:${errorText}`)
    }
  })
  browserPage.on('pageerror', (error) => failures.push(`page-error:${error.message}`))
}

context.on('page', attachRequestEvidence)
attachRequestEvidence(page)

async function sample(phase) {
  try { await cdp.send('HeapProfiler.collectGarbage') } catch { /* collection remains best-effort; raw heap is still recorded */ }
  const metrics = await cdp.send('Performance.getMetrics')
  const value = (name) => {
    const measured = metrics.metrics.find((entry) => entry.name === name)?.value
    return typeof measured === 'number' && Number.isFinite(measured) ? measured : null
  }
  const browserValues = await page.evaluate(() => ({
    dom_nodes: document.getElementsByTagName('*').length,
    canvases: document.querySelectorAll('canvas').length,
    resource_entries: performance.getEntriesByType('resource').length,
    current_path: location.pathname,
    runtime_readback: typeof window.__masiRuntimeReadback === 'function' ? window.__masiRuntimeReadback() : null,
  }))
  const runtime = browserValues.runtime_readback
  samples.push({
    phase,
    elapsed_seconds: (performance.now() - monotonicStart) / 1000,
    heap_bytes: value('JSHeapUsedSize'),
    documents: value('Documents'),
    frames: value('Frames'),
    nodes_metric: value('Nodes'),
    event_listeners: value('JSEventListeners'),
    dom_nodes: browserValues.dom_nodes,
    canvases: browserValues.canvases,
    resource_entries: browserValues.resource_entries,
    current_path: browserValues.current_path,
    query_cache_entries: runtime?.query_cache_entries ?? null,
    query_cache_bytes: runtime?.query_cache_bytes ?? null,
    chart_instances: runtime?.chart_instances ?? null,
    application_timers: runtime?.application_timers ?? null,
    runtime_readback_status: runtime === null ? 'NOT_MEASURED' : 'MEASURED',
    api_active: apiByPage.get(page) ?? 0,
    sse_active: sseByPage.get(page) ?? 0,
  })
}

async function navigate(route) {
  const link = page.getByRole('link', { name: route.link, exact: true }).first()
  await link.click()
  await page.getByRole('heading', { name: route.heading }).waitFor()
  if (route.link === 'Statistics') {
    const artifact = page.getByRole('button', { name: /fixture\.alert-rate/ })
    if (await artifact.count()) {
      await artifact.click()
      await page.getByRole('heading', { name: 'fixture.alert-rate' }).waitFor()
    }
  }
}

await page.goto(`${baseURL}/overview`, { waitUntil: 'domcontentloaded' })
await page.getByRole('heading', { name: 'Operational evidence at a glance' }).waitFor()
const warmupStart = performance.now()
while ((performance.now() - warmupStart) / 1000 < warmupSeconds) {
  await navigate(routeSets.steady[samples.length % routeSets.steady.length])
  await sample('warmup')
  const remaining = warmupSeconds * 1000 - (performance.now() - warmupStart)
  if (remaining > 0) await page.waitForTimeout(Math.min(5000, remaining))
}

let recovered = false
for (const [phase, routes] of Object.entries(routeSets)) {
  const phaseStart = performance.now()
  let iterations = 0
  const extraPages = []
  if (phase === 'saturation') {
    for (const path of ['/overview', '/plugins/statistics', '/governance/rule-effectiveness']) {
      const extra = await context.newPage()
      await extra.goto(`${baseURL}${path}`, { waitUntil: 'domcontentloaded' })
      extraPages.push(extra)
    }
  }
  if (phase === 'recovery') {
    await context.setOffline(true)
    await page.waitForTimeout(2000)
    await context.setOffline(false)
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.locator('h1').waitFor()
    recovered = true
  }
  while ((performance.now() - phaseStart) / 1000 < phaseSeconds) {
    await navigate(routes[iterations % routes.length])
    iterations += 1
    await sample(phase)
    const remaining = phaseSeconds * 1000 - (performance.now() - phaseStart)
    if (remaining > 0) await page.waitForTimeout(Math.min(5000, remaining))
  }
  for (const extra of extraPages) await extra.close()
  const phaseElapsedSeconds = (performance.now() - phaseStart) / 1000
  phaseEvidence.push({ phase, elapsed_seconds: phaseElapsedSeconds, iterations, samples: samples.filter((entry) => entry.phase === phase).length })
}

await sample('final')
await context.close()
await browser.close()
const qualifiedElapsedSeconds = phaseEvidence.reduce((total, phase) => total + phase.elapsed_seconds, 0)
const qualifiedSamples = samples.filter((sampleValue) => !['warmup', 'final'].includes(sampleValue.phase))
function growth(field) {
  const first = qualifiedSamples[0]?.[field]
  const last = qualifiedSamples.at(-1)?.[field]
  return typeof first === 'number' && Number.isFinite(first) && typeof last === 'number' && Number.isFinite(last)
    ? last - first
    : null
}
const maxima = {
  heap_bytes: maximum(samples.map((entry) => entry.heap_bytes)),
  heap_growth_bytes: growth('heap_bytes'),
  dom_nodes: maximum(samples.map((entry) => entry.dom_nodes)),
  canvases: maximum(samples.map((entry) => entry.canvases)),
  resource_entries: maximum(samples.map((entry) => entry.resource_entries)),
  event_listeners: maximum(samples.map((entry) => entry.event_listeners)),
  event_listener_growth: growth('event_listeners'),
  documents: maximum(samples.map((entry) => entry.documents)),
  document_growth: growth('documents'),
  frames: maximum(samples.map((entry) => entry.frames)),
  frame_growth: growth('frames'),
  query_cache_entries: maximum(samples.map((entry) => entry.query_cache_entries)),
  query_cache_bytes: maximum(samples.map((entry) => entry.query_cache_bytes)),
  chart_instances: maximum(samples.map((entry) => entry.chart_instances)),
  application_timers: maximum(samples.map((entry) => entry.application_timers)),
  application_timer_growth: growth('application_timers'),
  api_concurrency: apiMaximum,
  sse_connections_per_tab: sseMaximum,
}
if (thresholdExceeded(maxima.heap_growth_bytes, performanceProfile.runtime_bounds.heap_growth_bytes)) failures.push('heap-growth-over-bound')
if (thresholdExceeded(maxima.dom_nodes, performanceProfile.runtime_bounds.dom_nodes)) failures.push('dom-nodes-over-bound')
if (thresholdExceeded(maxima.canvases, performanceProfile.runtime_bounds.chart_instances)) failures.push('canvas-instances-over-bound')
if (thresholdExceeded(maxima.chart_instances, performanceProfile.runtime_bounds.chart_instances)) failures.push('chart-instances-over-bound')
if (thresholdExceeded(maxima.query_cache_entries, performanceProfile.runtime_bounds.query_cache_entries)) failures.push('query-cache-entries-over-bound')
if (thresholdExceeded(maxima.query_cache_bytes, performanceProfile.runtime_bounds.query_cache_bytes)) failures.push('query-cache-bytes-over-bound')
if (thresholdExceeded(maxima.resource_entries, performanceProfile.runtime_bounds.resource_timing_entries)) failures.push('resource-timing-entries-over-bound')
if (thresholdExceeded(maxima.event_listener_growth, performanceProfile.runtime_bounds.event_listener_growth)) failures.push('event-listener-growth-over-bound')
if (thresholdExceeded(maxima.application_timer_growth, performanceProfile.runtime_bounds.application_timer_growth)) failures.push('application-timer-growth-over-bound')
if (maxima.api_concurrency > performanceProfile.runtime_bounds.http_concurrency) failures.push('api-concurrency-over-bound')
if (maxima.sse_connections_per_tab > performanceProfile.runtime_bounds.sse_connections) failures.push('sse-connections-over-bound')
if (!recovered) failures.push('network-recovery-not-observed')
if (qualifiedElapsedSeconds + 0.05 < phaseSeconds * 4) failures.push('qualified-elapsed-short')

const formal = warmupSeconds === 60 && phaseSeconds === 900
const measurementHolds = []
if (!digestPattern.test(sourceTreeDigest)) measurementHolds.push('SOURCE_TREE_DIGEST_MISSING')
if (!digestPattern.test(imageDigest)) measurementHolds.push('IMAGE_DIGEST_MISSING')
if (browserVersion !== expectedBrowserVersion) measurementHolds.push('BROWSER_VERSION_MISMATCH')
if (observedPlaywrightRevision !== expectedChromium.playwright_revision) measurementHolds.push('PLAYWRIGHT_REVISION_MISMATCH')
if (cpus().length < performanceProfile.workstation.logical_cpu_min) measurementHolds.push('LOGICAL_CPU_BELOW_PROFILE')
if (totalmem() < performanceProfile.workstation.ram_bytes_min) measurementHolds.push('RAM_BELOW_PROFILE')
if (qualifiedSamples.length === 0) measurementHolds.push('QUALIFIED_SAMPLES_MISSING')
for (const field of ['heap_bytes', 'documents', 'frames', 'nodes_metric', 'event_listeners', 'query_cache_entries', 'query_cache_bytes', 'chart_instances', 'application_timers']) {
  if (samples.some((entry) => typeof entry[field] !== 'number' || !Number.isFinite(entry[field]))) {
    measurementHolds.push(`${field.toUpperCase()}_NOT_MEASURED`)
  }
}
const resultStatus = classifyPerformance(failures, [], measurementHolds)
const result = {
  schema_version: 'web-soak-evidence/v1',
  module_id: 'MOD-WEB-001',
  profile,
  base_url: baseURL,
  source_tree_digest: digestPattern.test(sourceTreeDigest) ? sourceTreeDigest : null,
  image_digest: digestPattern.test(imageDigest) ? imageDigest : null,
  browser: {
    engine: 'chromium', expected_product_build: expectedChromium.browser,
    expected_version: expectedBrowserVersion, observed_version: browserVersion,
    expected_playwright_revision: expectedChromium.playwright_revision,
    observed_playwright_revision: observedPlaywrightRevision,
  },
  started_at: startedAt,
  finished_at: new Date().toISOString(),
  warmup_seconds: warmupSeconds,
  warmup_excluded: true,
  phase_seconds: phaseSeconds,
  phases: phaseEvidence,
  qualified_elapsed_seconds: qualifiedElapsedSeconds,
  samples,
  maxima,
  recovery: { network_offline_online: recovered },
  errors: failures,
  measurement_holds: [...new Set(measurementHolds)].sort(),
  level: 'MODULE',
  applicability: 'APPLICABLE',
  result: resultStatus,
  qualification: formal && resultStatus === 'PASS' ? 'QUALIFIED' : 'NOT_QUALIFIED',
  qualification_scope: formal ? 'FORMAL_WEB_MODULE_SOAK_WITH_CONTRACT_FAKE_NEIGHBOR' : 'REHEARSAL_SHORT_DURATION',
}
const encoded = `${JSON.stringify(result, null, 2)}\n`
if (evidencePath) writeFileSync(evidencePath, encoded, { encoding: 'utf8', flag: 'wx' })
process.stdout.write(encoded)
if (result.result === 'FAIL') process.exitCode = 1
if (result.result === 'HOLD') process.exitCode = 2
