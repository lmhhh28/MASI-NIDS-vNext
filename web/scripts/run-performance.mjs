import { readFileSync, writeFileSync } from 'node:fs'
import { cpus, freemem, platform, release, totalmem } from 'node:os'
import { chromium } from '@playwright/test'
import {
  classifyPerformance,
  maximum,
  percentile,
  summarizeMeasurement,
  thresholdExceeded,
} from './performance-evidence.mjs'

function argument(name, fallback) {
  const index = process.argv.indexOf(name)
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback
}

function positiveInteger(name, fallback, maximumValue = 1_000) {
  const value = Number(argument(name, String(fallback)))
  if (!Number.isSafeInteger(value) || value < 1 || value > maximumValue) {
    throw new RangeError(`${name} must be an integer in 1..${maximumValue}`)
  }
  return value
}

const load = (url) => JSON.parse(readFileSync(url, 'utf8'))
const performanceProfile = load(new URL('../../contracts/profiles/v1/web-performance.json', import.meta.url))
const browserProfile = load(new URL('../../contracts/profiles/v1/web-browser.json', import.meta.url))
const expectedChromium = browserProfile.engines.find((entry) => entry.name === 'chromium')
if (!expectedChromium) throw new Error('web-browser/v1 has no Chromium identity')
const expectedBrowserVersion = expectedChromium.browser.match(/[0-9]+(?:\.[0-9]+)+$/)?.[0]
if (!expectedBrowserVersion) throw new Error('web-browser/v1 Chromium version is malformed')

const baseURL = argument('--base-url', process.env.WEB_E2E_BASE_URL ?? 'http://127.0.0.1:4180')
const evidencePath = argument('--evidence', '')
const sourceTreeDigest = argument('--source-tree-digest', '')
const imageDigest = argument('--image-digest', '')
const digestPattern = /^sha256:(?!0{64}$)[0-9a-f]{64}$/
const coldTrials = positiveInteger('--cold-trials', performanceProfile.measurement.cold_trials)
const warmTrials = positiveInteger('--warm-trials', performanceProfile.measurement.warm_trials)
const networks = performanceProfile.networks.map((network) => ({
  id: network.id,
  latency: network.latency_ms,
  downMbps: network.down_mbps,
  upMbps: network.up_mbps,
}))
const viewports = browserProfile.viewports
  .filter((viewport) => ['soc-desktop', 'soc-narrow-readonly'].includes(viewport.id))
  .map(({ id, width, height }) => ({ id, width, height }))
if (networks.length !== 2 || viewports.length !== 2) throw new Error('performance matrix profile is incomplete')

const thresholds = {
  lcp_p75_ms: performanceProfile.core_web_vitals_p75.lcp_ms_max,
  inp_p75_ms: performanceProfile.core_web_vitals_p75.inp_ms_max,
  cls_p75: performanceProfile.core_web_vitals_p75.cls_max,
  route_navigation_p95_ms: performanceProfile.runtime_bounds.route_navigation_p95_ms,
  dom_nodes: performanceProfile.runtime_bounds.dom_nodes,
  long_task_ms: performanceProfile.runtime_bounds.long_task_ms_max,
  api_concurrency: performanceProfile.runtime_bounds.http_concurrency,
  sse_connections: performanceProfile.runtime_bounds.sse_connections,
  query_cache_entries: performanceProfile.runtime_bounds.query_cache_entries,
  query_cache_bytes: performanceProfile.runtime_bounds.query_cache_bytes,
  chart_instances: performanceProfile.runtime_bounds.chart_instances,
}

const browser = await chromium.launch({ headless: true })
const browserVersion = browser.version()
const revisionMatch = chromium.executablePath().match(/chromium-(\d+)/)
const observedPlaywrightRevision = revisionMatch ? Number(revisionMatch[1]) : null
const samples = []
const failures = []

async function configureMetrics(context) {
  await context.addInitScript(() => {
    const supported = new Set(
      typeof PerformanceObserver === 'undefined'
        ? []
        : (PerformanceObserver.supportedEntryTypes ?? []),
    )
    const metric = (type, zeroIsMeasurement = false) => ({
      status: supported.has(type) ? (zeroIsMeasurement ? 'MEASURED' : 'NOT_MEASURED') : 'UNSUPPORTED',
      value: supported.has(type) && zeroIsMeasurement ? 0 : null,
      entry_count: 0,
    })
    window.__masiMetrics = {
      lcp: metric('largest-contentful-paint'),
      cls: metric('layout-shift', true),
      inp: metric('event'),
      long_task: metric('longtask', true),
      long_task_count: 0,
      observer_errors: [],
    }
    const observe = (name, type, callback, options) => {
      if (!supported.has(type)) return
      try {
        new PerformanceObserver(callback).observe(options)
      } catch (error) {
        window.__masiMetrics[name] = { status: 'ERROR', value: null, entry_count: 0 }
        if (window.__masiMetrics.observer_errors.length < 16) {
          window.__masiMetrics.observer_errors.push(`${name}:${error instanceof Error ? error.message : 'observer-error'}`)
        }
      }
    }
    observe('lcp', 'largest-contentful-paint', (list) => {
      const entries = list.getEntries()
      const last = entries.at(-1)
      if (!last || !Number.isFinite(last.startTime)) return
      window.__masiMetrics.lcp = {
        status: 'MEASURED', value: last.startTime,
        entry_count: window.__masiMetrics.lcp.entry_count + entries.length,
      }
    }, { type: 'largest-contentful-paint', buffered: true })
    observe('cls', 'layout-shift', (list) => {
      for (const entry of list.getEntries()) {
        if (entry.hadRecentInput || !Number.isFinite(entry.value)) continue
        window.__masiMetrics.cls.value += entry.value
        window.__masiMetrics.cls.entry_count += 1
      }
    }, { type: 'layout-shift', buffered: true })
    const interactions = new Map()
    observe('inp', 'event', (list) => {
      for (const entry of list.getEntries()) {
        if (!entry.interactionId || !Number.isFinite(entry.duration)) continue
        if (!interactions.has(entry.interactionId) && interactions.size >= 10_000) {
          window.__masiMetrics.inp = { status: 'ERROR', value: null, entry_count: interactions.size }
          window.__masiMetrics.observer_errors.push('inp:interaction-bound-exceeded')
          return
        }
        interactions.set(entry.interactionId, Math.max(interactions.get(entry.interactionId) ?? 0, entry.duration))
      }
      const descending = [...interactions.values()].sort((left, right) => right - left)
      if (descending.length > 0) {
        const index = Math.min(descending.length - 1, Math.floor(descending.length / 50))
        window.__masiMetrics.inp = { status: 'MEASURED', value: descending[index], entry_count: descending.length }
      }
    }, { type: 'event', buffered: true, durationThreshold: 16 })
    observe('long_task', 'longtask', (list) => {
      for (const entry of list.getEntries()) {
        if (!Number.isFinite(entry.duration)) continue
        window.__masiMetrics.long_task_count += 1
        window.__masiMetrics.long_task.value = Math.max(window.__masiMetrics.long_task.value, entry.duration)
        window.__masiMetrics.long_task.entry_count += 1
      }
    }, { type: 'longtask', buffered: true })
  })
}

async function runTrial(network, viewport, cache, trial, context) {
  const ownContext = context === undefined
  const activeContext = context ?? await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height }, locale: 'en-US', timezoneId: 'UTC',
  })
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

  try {
    const started = performance.now()
    await page.goto(`${baseURL}/overview`, { waitUntil: 'domcontentloaded' })
    await page.getByRole('heading', { name: 'Operational evidence at a glance' }).waitFor()
    const initialReadyMS = performance.now() - started
    const interactionStarted = performance.now()
    await Promise.all([
      page.waitForResponse((response) => new URL(response.url()).pathname === '/api/dashboard' && response.status() === 200),
      page.getByRole('button', { name: 'Refresh snapshot' }).click(),
    ])
    const refreshDriverRoundtripMS = performance.now() - interactionStarted
    const routeStarted = performance.now()
    await page.getByRole('link', { name: /Alerts · 24h/ }).click()
    await page.getByRole('heading', { name: 'Detection events' }).waitFor()
    const routeNavigationMS = performance.now() - routeStarted
    await page.waitForTimeout(250)
    await page.evaluate(() => new Promise((resolveFrame) => window.requestAnimationFrame(() => window.requestAnimationFrame(resolveFrame))))
    const vitals = await page.evaluate(() => ({
      ...window.__masiMetrics,
      dom_nodes: document.getElementsByTagName('*').length,
      resource_requests: performance.getEntriesByType('resource').length,
      runtime_readback: typeof window.__masiRuntimeReadback === 'function' ? window.__masiRuntimeReadback() : null,
    }))
    const cdpMetrics = await cdp.send('Performance.getMetrics')
    const cdpMetric = (name) => {
      const value = cdpMetrics.metrics.find((entry) => entry.name === name)?.value
      return typeof value === 'number' && Number.isFinite(value) ? value : null
    }
    const heapBytes = cdpMetric('JSHeapUsedSize')
    const eventListeners = cdpMetric('JSEventListeners')
    const runtime = vitals.runtime_readback
    samples.push({
      network: network.id,
      viewport: viewport.id,
      cache,
      trial,
      lcp_ms: vitals.lcp.status === 'MEASURED' ? vitals.lcp.value : null,
      lcp_status: vitals.lcp.status,
      lcp_entry_count: vitals.lcp.entry_count,
      inp_ms: vitals.inp.status === 'MEASURED' ? vitals.inp.value : null,
      inp_status: vitals.inp.status,
      inp_interaction_count: vitals.inp.entry_count,
      cls: vitals.cls.status === 'MEASURED' ? vitals.cls.value : null,
      cls_status: vitals.cls.status,
      cls_entry_count: vitals.cls.entry_count,
      initial_ready_ms: initialReadyMS,
      refresh_driver_roundtrip_ms: refreshDriverRoundtripMS,
      route_navigation_ms: routeNavigationMS,
      long_task_max_ms: vitals.long_task.status === 'MEASURED' ? vitals.long_task.value : null,
      long_task_status: vitals.long_task.status,
      long_task_count: vitals.long_task_count,
      heap_bytes: heapBytes,
      heap_status: heapBytes === null ? 'NOT_MEASURED' : 'MEASURED',
      event_listeners: eventListeners,
      event_listener_status: eventListeners === null ? 'NOT_MEASURED' : 'MEASURED',
      dom_nodes: vitals.dom_nodes,
      resource_requests: vitals.resource_requests,
      maximum_concurrent_api_requests: apiMaximum,
      sse_connections: sseConnections,
      query_cache_entries: runtime?.query_cache_entries ?? null,
      query_cache_bytes: runtime?.query_cache_bytes ?? null,
      chart_instances: runtime?.chart_instances ?? null,
      application_timers: runtime?.application_timers ?? null,
      runtime_readback_status: runtime === null ? 'NOT_MEASURED' : 'MEASURED',
      observer_errors: vitals.observer_errors,
    })
  } finally {
    await page.close()
    if (ownContext) await activeContext.close()
  }
}

try {
  for (const network of networks) {
    for (const viewport of viewports) {
      for (let trial = 1; trial <= coldTrials; trial += 1) {
        try {
          await runTrial(network, viewport, 'cold', trial)
        } catch (error) {
          failures.push(`${network.id}/${viewport.id}/cold/${trial}: ${error instanceof Error ? error.message : 'trial-failed'}`)
        }
      }
      const warmContext = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height }, locale: 'en-US', timezoneId: 'UTC',
      })
      await configureMetrics(warmContext)
      try {
        for (let trial = 1; trial <= warmTrials; trial += 1) {
          try {
            await runTrial(network, viewport, 'warm', trial, warmContext)
          } catch (error) {
            failures.push(`${network.id}/${viewport.id}/warm/${trial}: ${error instanceof Error ? error.message : 'trial-failed'}`)
          }
        }
      } finally {
        await warmContext.close()
      }
    }
  }
} finally {
  await browser.close()
}

const groups = []
for (const network of networks) for (const viewport of viewports) for (const cache of ['cold', 'warm']) {
  const values = samples.filter((sample) => sample.network === network.id && sample.viewport === viewport.id && sample.cache === cache)
  const expectedSamples = cache === 'cold' ? coldTrials : warmTrials
  groups.push({
    network: network.id,
    viewport: viewport.id,
    cache,
    trials: values.length,
    expected_trials: expectedSamples,
    measurements: {
      lcp: summarizeMeasurement(values.map((value) => value.lcp_ms), expectedSamples),
      inp: summarizeMeasurement(values.map((value) => value.inp_ms), expectedSamples),
      cls: summarizeMeasurement(values.map((value) => value.cls), expectedSamples),
      heap: summarizeMeasurement(values.map((value) => value.heap_bytes), expectedSamples),
      long_task: summarizeMeasurement(values.map((value) => value.long_task_max_ms), expectedSamples),
      event_listener: summarizeMeasurement(values.map((value) => value.event_listeners), expectedSamples),
      query_cache: summarizeMeasurement(values.map((value) => value.query_cache_entries), expectedSamples),
      chart_instance: summarizeMeasurement(values.map((value) => value.chart_instances), expectedSamples),
      application_timer: summarizeMeasurement(values.map((value) => value.application_timers), expectedSamples),
    },
    lcp_p75_ms: percentile(values.map((value) => value.lcp_ms), 0.75),
    inp_p75_ms: percentile(values.map((value) => value.inp_ms), 0.75),
    cls_p75: percentile(values.map((value) => value.cls), 0.75),
    initial_ready_p95_ms: percentile(values.map((value) => value.initial_ready_ms), 0.95),
    refresh_driver_roundtrip_p95_ms: percentile(values.map((value) => value.refresh_driver_roundtrip_ms), 0.95),
    route_navigation_p95_ms: percentile(values.map((value) => value.route_navigation_ms), 0.95),
    heap_max_bytes: maximum(values.map((value) => value.heap_bytes)),
    event_listeners_max: maximum(values.map((value) => value.event_listeners)),
    dom_max_nodes: maximum(values.map((value) => value.dom_nodes)),
    long_task_max_ms: maximum(values.map((value) => value.long_task_max_ms)),
    api_concurrency_max: maximum(values.map((value) => value.maximum_concurrent_api_requests)),
    sse_connections_max: maximum(values.map((value) => value.sse_connections)),
    query_cache_entries_max: maximum(values.map((value) => value.query_cache_entries)),
    query_cache_bytes_max: maximum(values.map((value) => value.query_cache_bytes)),
    chart_instances_max: maximum(values.map((value) => value.chart_instances)),
    application_timers_max: maximum(values.map((value) => value.application_timers)),
  })
}

const thresholdFailures = groups.flatMap((group) => [
  thresholdExceeded(group.lcp_p75_ms, thresholds.lcp_p75_ms) ? `${group.network}/${group.viewport}/${group.cache}: LCP` : '',
  thresholdExceeded(group.inp_p75_ms, thresholds.inp_p75_ms) ? `${group.network}/${group.viewport}/${group.cache}: INP` : '',
  thresholdExceeded(group.cls_p75, thresholds.cls_p75) ? `${group.network}/${group.viewport}/${group.cache}: CLS` : '',
  thresholdExceeded(group.route_navigation_p95_ms, thresholds.route_navigation_p95_ms) ? `${group.network}/${group.viewport}/${group.cache}: route navigation` : '',
  thresholdExceeded(group.dom_max_nodes, thresholds.dom_nodes) ? `${group.network}/${group.viewport}/${group.cache}: DOM` : '',
  thresholdExceeded(group.long_task_max_ms, thresholds.long_task_ms) ? `${group.network}/${group.viewport}/${group.cache}: long-task` : '',
  thresholdExceeded(group.api_concurrency_max, thresholds.api_concurrency) ? `${group.network}/${group.viewport}/${group.cache}: API concurrency` : '',
  thresholdExceeded(group.sse_connections_max, thresholds.sse_connections) ? `${group.network}/${group.viewport}/${group.cache}: SSE count` : '',
  thresholdExceeded(group.query_cache_entries_max, thresholds.query_cache_entries) ? `${group.network}/${group.viewport}/${group.cache}: query cache entries` : '',
  thresholdExceeded(group.query_cache_bytes_max, thresholds.query_cache_bytes) ? `${group.network}/${group.viewport}/${group.cache}: query cache bytes` : '',
  thresholdExceeded(group.chart_instances_max, thresholds.chart_instances) ? `${group.network}/${group.viewport}/${group.cache}: chart instances` : '',
].filter(Boolean))

const measurementHolds = []
if (browserVersion !== expectedBrowserVersion) measurementHolds.push('BROWSER_VERSION_MISMATCH')
if (observedPlaywrightRevision !== expectedChromium.playwright_revision) measurementHolds.push('PLAYWRIGHT_REVISION_MISMATCH')
if (!digestPattern.test(sourceTreeDigest)) measurementHolds.push('SOURCE_TREE_DIGEST_MISSING')
if (!digestPattern.test(imageDigest)) measurementHolds.push('IMAGE_DIGEST_MISSING')
if (cpus().length < performanceProfile.workstation.logical_cpu_min) measurementHolds.push('LOGICAL_CPU_BELOW_PROFILE')
if (totalmem() < performanceProfile.workstation.ram_bytes_min) measurementHolds.push('RAM_BELOW_PROFILE')
if (coldTrials !== performanceProfile.measurement.cold_trials) measurementHolds.push('COLD_TRIAL_COUNT_MISMATCH')
if (warmTrials !== performanceProfile.measurement.warm_trials) measurementHolds.push('WARM_TRIAL_COUNT_MISMATCH')
for (const group of groups) {
  for (const [metric, measurement] of Object.entries(group.measurements)) {
    if (measurement.status !== 'MEASURED') {
      measurementHolds.push(`${group.network}/${group.viewport}/${group.cache}:${metric}:${measurement.status}`)
    }
  }
}

const resultStatus = classifyPerformance(failures, thresholdFailures, measurementHolds)
const result = {
  schema_version: 'web-performance-evidence/v1',
  module_id: 'MOD-WEB-001',
  profile: 'web-performance/v1',
  measured_at: new Date().toISOString(),
  source_tree_digest: digestPattern.test(sourceTreeDigest) ? sourceTreeDigest : null,
  image_digest: digestPattern.test(imageDigest) ? imageDigest : null,
  base_url: baseURL,
  browser: {
    engine: 'chromium',
    expected_product_build: expectedChromium.browser,
    expected_version: expectedBrowserVersion,
    observed_version: browserVersion,
    expected_playwright_revision: expectedChromium.playwright_revision,
    observed_playwright_revision: observedPlaywrightRevision,
  },
  environment: {
    platform: platform(), release: release(), cpu_model: cpus()[0]?.model ?? 'unknown',
    logical_cpus: cpus().length, total_memory_bytes: totalmem(), free_memory_bytes_end: freemem(),
  },
  workload: { cold_trials: coldTrials, warm_trials: warmTrials, networks, viewports },
  thresholds,
  groups,
  samples,
  failures: [...failures, ...thresholdFailures],
  measurement_holds: [...new Set(measurementHolds)].sort(),
  level: 'MODULE',
  applicability: 'APPLICABLE',
  result: resultStatus,
  qualification: 'NOT_QUALIFIED',
  qualification_scope: 'LOCAL_PRODUCTION_WEB_WITH_CONTRACT_FAKE_NEIGHBOR',
}
const encoded = `${JSON.stringify(result, null, 2)}\n`
if (evidencePath) writeFileSync(evidencePath, encoded, { encoding: 'utf8', flag: 'wx' })
process.stdout.write(encoded)
if (result.result === 'FAIL') process.exitCode = 1
if (result.result === 'HOLD') process.exitCode = 2
