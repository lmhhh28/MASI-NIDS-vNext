import { expect, test } from '@playwright/test'
import axe from 'axe-core'

test('opens one bounded Overview snapshot and preserves a single SSE connection', async ({ page }) => {
  let streams = 0
  page.on('request', (request) => { if (new URL(request.url()).pathname === '/events') streams += 1 })
  await page.goto('/overview')
  await expect(page.getByRole('heading', { name: 'Operational evidence at a glance' })).toBeVisible()
  await expect(page.getByText('Signal → governance → device truth')).toBeVisible()
  await expect(page.getByText('DASHBOARD_ATTENTION')).toBeVisible()
  await page.getByRole('link', { name: 'Events', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Detection events' })).toBeVisible()
  await page.getByRole('link', { name: 'Overview', exact: true }).click()
  await expect.poll(() => streams).toBe(1)
})

test('shows the three rule evidence layers without treating counters as outcomes', async ({ page }) => {
  await page.goto('/governance/rule-effectiveness')
  await expect(page.getByRole('heading', { name: 'Rule effectiveness' })).toBeVisible()
  await expect(page.getByText('Exact installation readback')).toBeVisible()
  await expect(page.getByText('Dataplane match', { exact: true })).toBeVisible()
  await expect(page.getByText('Independent packet/action outcome')).toBeVisible()
  await expect(page.getByText('18')).toBeVisible()
  await expect(page.getByText('This result is written by the independent packet oracle')).toBeVisible()
})

test('runs exact-context maker-checker review without optimistic success', async ({ page }) => {
  await page.goto('/governance/approvals')
  await page.getByRole('button', { name: 'Review' }).click()
  const dialog = page.getByRole('dialog', { name: 'Review exact proposal context' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('proposal-r2-41', { exact: true })).toBeVisible()
  await expect(dialog.getByText('actor:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', { exact: true })).toBeVisible()
  await expect(dialog.getByText(/Approval never implies device success/)).toBeVisible()
  await dialog.getByRole('button', { name: 'Authorize exact proposal' }).click()
  await expect(page.getByText('No external effect is implied')).toBeVisible()
})

test('uses the fixed statistics renderer and exposes equivalent summary', async ({ page }) => {
  await page.goto('/plugins/statistics')
  await expect(page.getByRole('heading', { name: 'Plugin statistics' })).toBeVisible()
  await page.getByRole('button', { name: /fixture\.alert-rate/ }).click()
  await expect(page.getByRole('heading', { name: 'fixture.alert-rate' })).toBeVisible()
  await expect(page.locator('canvas')).toBeVisible()
  await page.getByText('Equivalent data summary').click()
  await expect(page.getByText('Series coverage summary')).toBeVisible()
  await expect(page.getByText('No plugin route, component, HTML')).toBeVisible()
})

test('keeps high-risk mutation controls blocked on narrow read-only viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/operations/targets')
  await expect(page.getByText('Target and fleet mutations require a viewport')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Register target' })).toBeDisabled()
})

test('serves every primary deep link from the immutable SPA artifact', async ({ page }) => {
  const routes = [
    ['/detection/incidents', 'Incident queue'], ['/evidence/captures', 'Bounded captures'],
    ['/governance/firewall-policies', 'Firewall policy revisions'], ['/operations/fleet', 'Fleet operations'],
    ['/operations/models', 'Model operations'], ['/plugins/catalog', 'Plugin catalog'], ['/analysis', 'Evidence analysis'],
  ] as const
  for (const [route, heading] of routes) {
    await page.goto(route)
    await expect(page.getByRole('heading', { name: heading })).toBeVisible()
  }
})

test('restores exact fact, tab, and artifact context from shareable URLs', async ({ page }) => {
  await page.goto('/detection/events?cursor=deep-link-page&fact=evt-2026-0001')
  await expect(page.getByRole('heading', { name: 'Detection events' })).toBeVisible()
  await expect(page.getByText('evt-2026-0001', { exact: true }).last()).toBeVisible()
  await page.reload()
  await expect(page.getByText('COMMITTED', { exact: true }).last()).toBeVisible()

  await page.goto('/operations/models?tab=model-rollouts&fact=rollout-10')
  await expect(page.getByRole('button', { name: 'Rollout groups' })).toHaveAttribute('aria-current', 'page')
  await expect(page.getByText('rollout-10', { exact: true }).last()).toBeVisible()

  await page.goto('/analysis?tab=analysis-artifacts&fact=analysis-artifact-8')
  await expect(page.getByRole('button', { name: 'Artifacts' })).toHaveAttribute('aria-current', 'page')
  await expect(page.getByText('analysis-artifact-8', { exact: true }).last()).toBeVisible()

  await page.goto('/plugins/statistics?artifact=stat-artifact-17&definition=fixture.alert-rate')
  await expect(page.getByRole('heading', { name: 'fixture.alert-rate' })).toBeVisible()
  await expect(page.locator('canvas')).toBeVisible()
})

test('submits target and fleet control facts without collapsing child outcomes', async ({ page }) => {
  const exactDigest = `sha256:${'a'.repeat(64)}`
  let registrationRequests = 0
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/targets') registrationRequests += 1
  })
  await page.goto('/operations/targets')
  await expect(page.getByText('Edge switch A')).toBeVisible()
  await expect(page.getByText('https://switch-a.example:9559')).toBeVisible()
  await page.getByRole('button', { name: 'Register target' }).click()
  const registration = page.getByRole('dialog', { name: 'Register stable target' })
  await registration.getByLabel('Display name').fill('Mismatched identity must hold')
  await registration.getByLabel('P4Runtime TLS endpoint').fill('https://mismatch.example:9559')
  await registration.getByLabel('Desired profile digest').fill(exactDigest)
  await registration.getByLabel('Credential reference').fill('credential:one')
  await registration.getByLabel('Authorization scope').fill('scope-e2e')
  await registration.getByLabel('TLS server name').fill('mismatch.example')
  await registration.getByLabel('TLS identity reference').fill('credential:two')
  await registration.getByLabel('Frozen target-set digest').fill(exactDigest)
  await registration.getByRole('button', { name: 'Submit controlled operation' }).click()
  await expect(registration.getByText('must exactly match the credential reference')).toBeVisible()
  expect(registrationRequests).toBe(0)
  await registration.getByRole('button', { name: 'Cancel' }).click()

  await page.getByRole('button', { name: 'Manage lifecycle' }).click()
  const lifecycle = page.getByRole('dialog', { name: 'Change target lifecycle' })
  await lifecycle.getByLabel('Authorization scope').fill('scope-e2e')
  await lifecycle.getByLabel('Frozen target-set digest').fill(exactDigest)
  await lifecycle.getByRole('button', { name: 'Submit controlled operation' }).click()
  await expect(page.getByText('Connection or primary state is not inferred')).toBeVisible()

  await page.goto('/operations/fleet')
  await page.getByRole('button', { name: 'Advance wave' }).click()
  const wave = page.getByRole('dialog', { name: 'Advance fleet wave gate' })
  await wave.getByLabel('Authorization scope').fill('scope-e2e')
  await wave.getByRole('button', { name: 'Submit controlled operation' }).click()
  await expect(page.getByText('per-target child outcomes remain authoritative')).toBeVisible()
})

test('keeps model and plugin lifecycle changes exact and server-confirmed', async ({ page }) => {
  const exactDigest = `sha256:${'b'.repeat(64)}`
  await page.goto('/operations/models')
  await page.getByRole('button', { name: 'Rollout groups' }).click()
  await page.getByRole('button', { name: 'Advance one shard' }).click()
  const rollout = page.getByRole('dialog', { name: 'Advance one rollout shard' })
  await rollout.getByLabel('Authorization scope').fill('scope-e2e')
  await rollout.getByRole('button', { name: 'Advance one shard' }).click()
  await expect(page.getByText('Mixed/current state remains per-shard')).toBeVisible()

  await page.goto('/plugins/catalog')
  await page.getByRole('button', { name: 'Manage' }).click()
  const plugin = page.getByRole('dialog', { name: 'Record qualification' })
  await plugin.getByLabel('Authorization scope').fill('scope-e2e')
  await plugin.getByLabel('Frozen target-set digest').fill(exactDigest)
  await plugin.getByRole('button', { name: 'Submit exact lifecycle fact' }).click()
  await expect(page.getByText('Runtime readiness remains separately observed')).toBeVisible()
})

test('reflows at 320 CSS pixels and honors keyboard and reduced-motion preferences', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.setViewportSize({ width: 320, height: 720 })
  await page.goto('/overview')
  await expect(page.getByRole('button', { name: 'Open navigation' })).toHaveAttribute('aria-expanded', 'false')
  await page.getByRole('button', { name: 'Open navigation' }).focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('button', { name: 'Close navigation' })).toHaveAttribute('aria-expanded', 'true')
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: 'Open navigation' })).toHaveAttribute('aria-expanded', 'false')
  const metrics = await page.evaluate(() => ({
    viewport: window.innerWidth,
    documentWidth: document.documentElement.scrollWidth,
    animationDurationSeconds: Number.parseFloat(getComputedStyle(document.body).animationDuration),
    transitionDurationSeconds: Number.parseFloat(getComputedStyle(document.body).transitionDuration),
  }))
  expect(metrics.documentWidth).toBeLessThanOrEqual(metrics.viewport)
  expect(metrics.animationDurationSeconds).toBeLessThanOrEqual(0.00001)
  expect(metrics.transitionDurationSeconds).toBeLessThanOrEqual(0.00001)
})

test('has no automated WCAG A or AA violations on every page and responsive variant', async ({ page }) => {
  test.setTimeout(120_000)
  const routes = [
    '/overview', '/detection/events', '/detection/incidents', '/evidence', '/evidence/captures',
    '/governance/response-rules', '/governance/firewall-policies', '/governance/approvals',
    '/governance/operations', '/governance/rule-effectiveness', '/analysis', '/plugins/catalog',
    '/plugins/statistics', '/plugins/masi.statistics.fixture/statistics', '/operations/targets',
    '/operations/fleet', '/operations/models', '/operations/audit', '/route-not-found',
  ]
  const variants = [
    { id: 'desktop-light', theme: 'light', width: 1440, height: 900 },
    { id: 'desktop-dark', theme: 'dark', width: 1440, height: 900 },
    { id: 'narrow-light', theme: 'light', width: 390, height: 844 },
  ] as const
  for (const variant of variants) {
    await page.setViewportSize({ width: variant.width, height: variant.height })
    await page.goto('/overview')
    await page.evaluate((value) => window.localStorage.setItem('masi.theme', value), variant.theme)
    for (const route of routes) {
      await page.goto(route)
      await page.locator('main').waitFor()
      await page.evaluate(axe.source)
      const violations = await page.evaluate(async () => {
        const runtime = (window as unknown as {
          axe: { run: (context: Document, options: unknown) => Promise<{ violations: Array<{ id: string; nodes: Array<{ target: unknown; failureSummary?: string }> }> }> }
        }).axe
        const result = await runtime.run(document, {
          runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'] },
        })
        return result.violations.map((violation) => ({
          id: violation.id,
          nodes: violation.nodes.map((node) => ({ target: node.target, failure: node.failureSummary ?? '' })),
        }))
      })
      expect(violations, `${variant.id} ${route} accessibility violations`).toEqual([])
    }
  }
})
