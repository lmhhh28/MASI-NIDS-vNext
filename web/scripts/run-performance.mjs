import { writeFileSync } from 'node:fs'
import { cpus, freemem, platform, release, totalmem } from 'node:os'
import { chromium } from '@playwright/test'

function argument(name, fallback) {
  const index = process.argv.indexOf(name)
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback
}

const baseURL = argument('--base-url', process.env.WEB_E2E_BASE_URL ?? 'http://127.0.0.1:4180')
const evidencePath = argument('--evidence', '')
const coldTrials = Number(argument('--cold-trials', '5'))
const warmTrials = Number(argument('--warm-trials', '10'))
const networks = [
  { id: 'controlled-lan', latency: 20, downMbps: 100, upMbps: 100 },
  { id: 'controlled-weak', latency: 100, downMbps: 10, upMbps: 5 },
]
const viewports = [
  { id: 'soc-desktop', width: 1440, height: 900 },
  { id: 'soc-narrow-readonly', width: 390, height: 844 },
]

function percentile(values, ratio) {
  const sorted = [...values].sort((a, b) => a - b)
  return sorted[Math.max(0, Math.ceil(sorted.length * ratio) - 1)] ?? 0
}

const browser = await chromium.launch({ headless: true })
const samples = []
const failures = []

async function configureMetrics(context) {
  await context.addInitScript(() => {
    window.__masiMetrics = { lcp: 0, cls: 0, inp: 0, longTasks: 0, longTaskMax: 0 }
    try {
      new PerformanceObserver((list) => {
        const entries = list.getEntries()
        const last = entries[entries.length - 1]
        if (last) window.__masiMetrics.lcp = last.startTime
      }).observe({ type: 'largest-contentful-paint', buffered: true })
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) if (!entry.hadRecentInput) window.__masiMetrics.cls += entry.value
      }).observe({ type: 'layout-shift', buffered: true })
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) if (entry.interactionId) window.__masiMetrics.inp = Math.max(window.__masiMetrics.inp, entry.duration)
      }).observe({ type: 'event', buffered: true, durationThreshold: 16 })
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          window.__masiMetrics.longTasks += 1
          window.__masiMetrics.longTaskMax = Math.max(window.__masiMetrics.longTaskMax, entry.duration)
        }
      }).observe({ type: 'longtask', buffered: true })
    } catch { /* unsupported observers remain explicit zero and driver latency is retained */ }
  })
}

async function runTrial(network, viewport, cache, trial, context) {
  const ownContext = !context
  const activeContext = context ?? await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, locale: 'en-US', timezoneId: 'UTC' })
  if (ownContext) await configureMetrics(activeContext)
  const page = await activeContext.newPage()
  const cdp = await activeContext.newCDPSession(page)
  await cdp.send('Network.enable')
  await cdp.send('Network.emulateNetworkConditions', {
    offline: false,
    latency: network.latency,
    downloadThroughput: network.downMbps * 1024 * 1024 / 8,
    uploadThroughput: network.upMbps * 1024 * 1024 / 8,
    connectionType: 'ethernet',
  })
  await cdp.send('Performance.enable')
  let apiActive = 0
  let apiMaximum = 0
  let sseConnections = 0
  let driverInteractionMS
  const pendingAPI = new Set()
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname
    if (path.startsWith('/api/')) {
      pendingAPI.add(request)
      apiActive += 1
      apiMaximum = Math.max(apiMaximum, apiActive)
    }
    if (path === '/events') sseConnections += 1
  })
  const finish = (request) => {
    if (pendingAPI.delete(request)) apiActive -= 1
  }
  page.on('requestfinished', finish)
  page.on('requestfailed', finish)
  page.on('pageerror', (error) => failures.push(`${network.id}/${viewport.id}/${cache}/${trial}: ${error.message}`))
  const started = performance.now()
  await page.goto(`${baseURL}/overview`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('heading', { name: 'Operational evidence at a glance' }).waitFor()
  const interactionStarted = performance.now()
  await Promise.all([
    page.waitForResponse((response) => new URL(response.url()).pathname === '/api/dashboard' && response.status() === 200),
    page.getByRole('button', { name: 'Refresh snapshot' }).click(),
  ])
  driverInteractionMS = performance.now() - interactionStarted
  const routeStarted = performance.now()
  await page.getByRole('link', { name: /Alerts · 24h/ }).click()
  await page.getByRole('heading', { name: 'Detection events' }).waitFor()
  const routeNavigationMS = performance.now() - routeStarted
  await page.waitForTimeout(250)
  const initialReadyMS = routeStarted - started
  const vitals = await page.evaluate(() => ({ ...window.__masiMetrics, domNodes: document.getElementsByTagName('*').length, resources: performance.getEntriesByType('resource').length }))
  const metrics = await cdp.send('Performance.getMetrics')
  const metric = (name) => metrics.metrics.find((entry) => entry.name === name)?.value ?? 0
  samples.push({
    network: network.id, viewport: viewport.id, cache, trial,
    lcp_ms: vitals.lcp,
    inp_ms: vitals.inp || driverInteractionMS,
    inp_source: vitals.inp ? 'PerformanceEventTiming' : 'driver-roundtrip-fallback',
    cls: vitals.cls,
    initial_ready_ms: initialReadyMS,
    route_navigation_ms: routeNavigationMS,
    long_tasks: vitals.longTasks,
    long_task_max_ms: vitals.longTaskMax,
    heap_bytes: metric('JSHeapUsedSize'),
    dom_nodes: vitals.domNodes,
    resource_requests: vitals.resources,
    maximum_concurrent_api_requests: apiMaximum,
    sse_connections: sseConnections,
  })
  await page.close()
  if (ownContext) await activeContext.close()
}

for (const network of networks) {
  for (const viewport of viewports) {
    for (let trial = 1; trial <= coldTrials; trial += 1) await runTrial(network, viewport, 'cold', trial)
    const warmContext = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, locale: 'en-US', timezoneId: 'UTC' })
    await configureMetrics(warmContext)
    for (let trial = 1; trial <= warmTrials; trial += 1) await runTrial(network, viewport, 'warm', trial, warmContext)
    await warmContext.close()
  }
}
await browser.close()

const groups = []
for (const network of networks) for (const viewport of viewports) for (const cache of ['cold', 'warm']) {
  const values = samples.filter((sample) => sample.network === network.id && sample.viewport === viewport.id && sample.cache === cache)
  groups.push({
    network: network.id, viewport: viewport.id, cache, trials: values.length,
    lcp_p75_ms: percentile(values.map((value) => value.lcp_ms), 0.75),
    inp_p75_ms: percentile(values.map((value) => value.inp_ms), 0.75),
    cls_p75: percentile(values.map((value) => value.cls), 0.75),
    initial_ready_p95_ms: percentile(values.map((value) => value.initial_ready_ms), 0.95),
    route_navigation_p95_ms: percentile(values.map((value) => value.route_navigation_ms), 0.95),
    heap_max_bytes: Math.max(...values.map((value) => value.heap_bytes)),
    dom_max_nodes: Math.max(...values.map((value) => value.dom_nodes)),
    long_task_max_ms: Math.max(...values.map((value) => value.long_task_max_ms)),
    api_concurrency_max: Math.max(...values.map((value) => value.maximum_concurrent_api_requests)),
    sse_connections_max: Math.max(...values.map((value) => value.sse_connections)),
  })
}
const thresholdFailures = groups.flatMap((group) => [
  group.lcp_p75_ms > 2500 ? `${group.network}/${group.viewport}/${group.cache}: LCP` : '',
  group.inp_p75_ms > 200 ? `${group.network}/${group.viewport}/${group.cache}: INP` : '',
  group.cls_p75 > 0.1 ? `${group.network}/${group.viewport}/${group.cache}: CLS` : '',
  group.route_navigation_p95_ms > 500 ? `${group.network}/${group.viewport}/${group.cache}: route navigation` : '',
  group.dom_max_nodes > 5000 ? `${group.network}/${group.viewport}/${group.cache}: DOM` : '',
  group.long_task_max_ms > 200 ? `${group.network}/${group.viewport}/${group.cache}: long-task` : '',
  group.api_concurrency_max > 8 ? `${group.network}/${group.viewport}/${group.cache}: API concurrency` : '',
  group.sse_connections_max > 1 ? `${group.network}/${group.viewport}/${group.cache}: SSE count` : '',
].filter(Boolean))
const result = {
  schema_version: 'web-performance-evidence/v1',
  profile: 'web-performance/v1',
  base_url: baseURL,
  browser: 'chromium-151.0.7922.34@playwright-1234',
  environment: { platform: platform(), release: release(), cpu_model: cpus()[0]?.model ?? 'unknown', logical_cpus: cpus().length, total_memory_bytes: totalmem(), free_memory_bytes_end: freemem() },
  workload: { cold_trials: coldTrials, warm_trials: warmTrials, networks, viewports },
  thresholds: { lcp_p75_ms: 2500, inp_p75_ms: 200, cls_p75: 0.1, route_navigation_p95_ms: 500, dom_nodes: 5000, long_task_ms: 200, api_concurrency: 8, sse_connections: 1 },
  groups,
  samples,
  failures: [...failures, ...thresholdFailures],
  result: failures.length === 0 && thresholdFailures.length === 0 ? 'PASS' : 'FAIL',
  qualification: 'NOT_QUALIFIED',
  qualification_scope: 'LOCAL_PRODUCTION_WEB_WITH_CONTRACT_FAKE_NEIGHBOR',
}
const encoded = `${JSON.stringify(result, null, 2)}\n`
if (evidencePath) writeFileSync(evidencePath, encoded, { encoding: 'utf8', flag: 'wx' })
process.stdout.write(encoded)
if (result.result !== 'PASS') process.exitCode = 1
