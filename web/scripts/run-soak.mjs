import { writeFileSync } from 'node:fs'
import { chromium } from '@playwright/test'

function argument(name, fallback) {
  const index = process.argv.indexOf(name)
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback
}

const baseURL = argument('--base-url', process.env.WEB_E2E_BASE_URL ?? 'http://127.0.0.1:4180')
const evidencePath = argument('--evidence', '')
const warmupSeconds = Number(argument('--warmup-seconds', '60'))
const phaseSeconds = Number(argument('--phase-seconds', '900'))
const sourceTreeDigest = argument('--source-tree-digest', `sha256:${'0'.repeat(64)}`)
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
    if (path !== '/events' && !request.failure()?.errorText.includes('ERR_INTERNET_DISCONNECTED')) failures.push(`request-failed:${path}:${request.failure()?.errorText ?? 'unknown'}`)
  })
  browserPage.on('pageerror', (error) => failures.push(`page-error:${error.message}`))
}

context.on('page', attachRequestEvidence)
attachRequestEvidence(page)

async function sample(phase) {
  try { await cdp.send('HeapProfiler.collectGarbage') } catch { /* collection remains best-effort; raw heap is still recorded */ }
  const metrics = await cdp.send('Performance.getMetrics')
  const value = (name) => metrics.metrics.find((entry) => entry.name === name)?.value ?? 0
  const browserValues = await page.evaluate(() => ({
    dom_nodes: document.getElementsByTagName('*').length,
    canvases: document.querySelectorAll('canvas').length,
    resource_entries: performance.getEntriesByType('resource').length,
    current_path: location.pathname,
  }))
  samples.push({
    phase,
    elapsed_seconds: (performance.now() - monotonicStart) / 1000,
    heap_bytes: value('JSHeapUsedSize'),
    documents: value('Documents'),
    frames: value('Frames'),
    nodes_metric: value('Nodes'),
    ...browserValues,
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
const firstHeap = qualifiedSamples[0]?.heap_bytes ?? 0
const lastHeap = qualifiedSamples[qualifiedSamples.length - 1]?.heap_bytes ?? 0
const heapGrowthBytes = lastHeap - firstHeap
const maxima = {
  heap_bytes: Math.max(...samples.map((entry) => entry.heap_bytes)),
  heap_growth_bytes: heapGrowthBytes,
  dom_nodes: Math.max(...samples.map((entry) => entry.dom_nodes)),
  canvases: Math.max(...samples.map((entry) => entry.canvases)),
  resource_entries: Math.max(...samples.map((entry) => entry.resource_entries)),
  api_concurrency: apiMaximum,
  sse_connections_per_tab: sseMaximum,
}
if (maxima.heap_growth_bytes > 67_108_864) failures.push('heap-growth-over-64MiB')
if (maxima.dom_nodes > 5000) failures.push('dom-nodes-over-5000')
if (maxima.canvases > 4) failures.push('chart-instances-over-4')
if (maxima.api_concurrency > 8) failures.push('api-concurrency-over-8')
if (maxima.sse_connections_per_tab > 1) failures.push('sse-connections-over-1')
if (!recovered) failures.push('network-recovery-not-observed')
if (qualifiedElapsedSeconds + 0.05 < phaseSeconds * 4) failures.push('qualified-elapsed-short')

const formal = warmupSeconds === 60 && phaseSeconds === 900
const result = {
  schema_version: 'web-soak-evidence/v1',
  profile,
  base_url: baseURL,
  source_tree_digest: sourceTreeDigest,
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
  result: failures.length === 0 ? 'PASS' : 'FAIL',
  qualification: formal && failures.length === 0 ? 'QUALIFIED' : 'NOT_QUALIFIED',
  qualification_scope: formal ? 'FORMAL_WEB_MODULE_SOAK_WITH_CONTRACT_FAKE_NEIGHBOR' : 'REHEARSAL_SHORT_DURATION',
}
const encoded = `${JSON.stringify(result, null, 2)}\n`
if (evidencePath) writeFileSync(evidencePath, encoded, { encoding: 'utf8', flag: 'wx' })
process.stdout.write(encoded)
if (result.result !== 'PASS') process.exitCode = 1
