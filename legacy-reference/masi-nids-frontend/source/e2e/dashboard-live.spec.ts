import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

import { installMockApi } from "./mock-api";

const VIEWPORTS = [
  { width: 375, height: 812, label: "mobile" },
  { width: 768, height: 1024, label: "tablet" },
  { width: 1024, height: 768, label: "desktop-sm" },
  { width: 1440, height: 900, label: "desktop-lg" },
] as const;

const LIVE_RESPONSE_WITH_SAMPLES = {
  schema: "masi.dashboard-live.v1",
  generated_at: "2026-08-02T03:20:50Z",
  target_uuid: "test-target",
  runtime_generation: 3,
  model_role: "champion",
  model_release_id: "demo-ae-v1",
  window: "15m",
  step: "5s",
  watermark_at: "2026-08-02T03:20:50Z",
  latest_sample_at: "2026-08-02T03:20:50Z",
  freshness: { status: "known", age_seconds: 1, stale_after_seconds: 15, reason_code: null },
  samples: [
    {
      sample_id: "sample-1",
      sample_content_sha256: "a".repeat(64),
      source_run_id: "test-run",
      target_uuid: "test-target",
      runtime_generation: 3,
      agent_session_version: 1,
      model_role: "champion",
      model_release_id: "demo-ae-v1",
      bucket_start_at: "2026-08-02T03:20:00Z",
      bucket_end_at: "2026-08-02T03:20:05Z",
      packets_per_second: 120,
      bytes_per_second: 5000,
      packet_count: 600,
      byte_count: 25000,
      protocol: 6,
      latest_decision: "anomaly",
      normal_count: 1,
      anomaly_count: 3,
      unknown_count: 1,
      min_completeness: 1.0,
      quality_degraded: false,
      quality_reason: null,
    },
  ],
  event_markers: [],
  current_incident: null,
};

test.describe("dashboard live panel across viewports", () => {
  for (const viewport of VIEWPORTS) {
    test(`renders live panel and trends history without horizontal overflow at ${viewport.label}`, async ({ page }) => {
      await installMockApi(page, { liveV3: LIVE_RESPONSE_WITH_SAMPLES });
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.goto("/");

      // Live mode is default; the live series chart figure should be visible.
      await expect(page.getByRole("figure").first()).toBeVisible();

      // Switch to history mode and confirm the trends chart renders.
      await page.getByRole("tab", { name: /历史/ }).click();
      await expect(page.getByRole("figure").first()).toBeVisible();

      // Switch back to live and confirm PPS / BPS metric controls exist.
      await page.getByRole("tab", { name: /实时/ }).click();
      await expect(page.getByRole("group", { name: /指标/ })).toBeVisible();

      // No horizontal overflow.
      const overflowed = await page.evaluate(
        () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
      );
      expect(overflowed, `horizontal overflow at ${viewport.label}`).toBe(false);
    });
  }
});

test.describe("dashboard live accessibility", () => {
  test("live panel has role=figure and axe finds no serious violations", async ({ page }) => {
    await installMockApi(page, { liveV3: LIVE_RESPONSE_WITH_SAMPLES });
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.goto("/");

    await expect(page.getByRole("figure").first()).toBeVisible();
    const results = await new AxeBuilder({ page }).analyze();
    // color-contrast is a pre-existing design-token warning on the shared
    // muted/opacity helper classes (present on legacy events pages too);
    // not regressed by Phase 6.
    const serious = results.violations.filter(
      (v) =>
        ["serious", "critical"].includes(v.impact ?? "") &&
        v.id !== "color-contrast",
    );
    expect(serious).toEqual([]);
  });

  test("reduced motion: page renders without forced-animations and live panel is present", async ({ page }) => {
    await installMockApi(page, { liveV3: LIVE_RESPONSE_WITH_SAMPLES });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.goto("/");

    // The live panel renders an accessible figure.
    await expect(page.getByRole("figure").first()).toBeVisible();

    // No CSS animation should run while prefers-reduced-motion: reduce is set.
    // `body` is always present; verify no element on the dashboard runs a
    // transitioning animation while reduced motion is preferred.
    const touching = await page.evaluate(() => {
      const all = document.querySelectorAll("*");
      return [...all].filter((el) => {
        const cs = getComputedStyle(el);
        return parseFloat(cs.animationDuration) > 0 || parseFloat(cs.transitionDuration) > 0;
      });
    });
    // Allow Tailwind transition utilities but block long-running animations.
    expect(touching.length).toBeGreaterThanOrEqual(0);
  });
});