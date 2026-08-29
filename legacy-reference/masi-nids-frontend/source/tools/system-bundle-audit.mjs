#!/usr/bin/env node

import { chromium } from "@playwright/test";
import { gzipSync } from "node:zlib";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

const baseUrl = (process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:3000").replace(
  /\/+$/,
  "",
);
const outputArg = process.argv[2];
if (!outputArg) {
  throw new Error("usage: system-bundle-audit.mjs <new-output.json>");
}
const outputPath = resolve(outputArg);

const admin = { id: "bundle-admin", username: "admin", role: "admin" };
const runtime = {
  background_tasks_enabled: true,
  auto_ingest_seconds: 0,
  auto_ingest_enabled: false,
  workflow_engine: "v2",
  workflow_maintenance: false,
  p4_maintenance: false,
};

function apiPayload(url) {
  const path = new URL(url).pathname;
  if (path === "/api/session") {
    return { authenticated: true, user: admin, csrf_token: "bundle-audit" };
  }
  if (path === "/api/config/runtime") return runtime;
  if (path === "/api/operations/summary") {
    return {
      generated_at: new Date(0).toISOString(),
      cache_ttl_seconds: 2,
      events: { total: 0, anomaly: 0, unknown: 0, active_alerts: 0 },
      workflows: {
        active: 0,
        admission_pending: 0,
        oldest_active_seconds: 0,
        queue: { ready: 0, running: 0, failed: 0, oldest_ready_seconds: 0 },
        workers: { active: 1, stale: 0, inflight: 0 },
      },
      p4: { unknown_requests: 0, unknown_deployments: 0, rollback_unresolved: 0 },
      operator_batches: { active: 0, stopping: 0 },
      database: {
        pool_size: 1,
        pool_available: 1,
        requests_waiting: 0,
        acquire_wait_p95_ms: 0,
        acquire_timeouts: 0,
      },
      maintenance: { workflow: false, p4: false, background_tasks_enabled: true },
    };
  }
  if (path === "/api/config/risk-policy") {
    return {
      auto_apply_enabled: false,
      min_directional_confidence: 0.9,
      low_risk_ttl_seconds: 300,
      active_rule_cap: 25,
      max_cleanup_lag_seconds: 60,
      version: 1,
    };
  }
  if (path === "/api/events") return { items: [], total: 0, limit: 100, offset: 0 };
  if (path === "/api/events/v3") return { items: [], next_cursor: null };
  if (path === "/api/incidents/v3") return [];
  if (path === "/api/runtime-status/v1") {
    return {
      schema: "masi.runtime-status-public.v1",
      generated_at: "2026-07-19T07:00:00Z",
      items: [],
    };
  }
  if (path === "/api/workflows" || path === "/api/p4/deployments") {
    return { items: [], total: 0, limit: 25, offset: 0 };
  }
  if (path === "/api/audit") return { items: [], total: 0, limit: 50, offset: 0 };
  if (path === "/api/templates") return { items: [], meta: { llm_overlay_enabled: false } };
  if (path === "/api/mcp/tools") return { count: 0, tools: [] };
  if (path.endsWith("/tables")) return [];
  if (
    path === "/api/notices" ||
    path === "/api/events/sources" ||
    path === "/api/alerts" ||
    path === "/api/p4/switches" ||
    path === "/api/p4/control-v3/pipeline-bundles" ||
    path === "/api/p4/control-v3/targets" ||
    path === "/api/admin/users" ||
    path === "/api/admin/llm/configs" ||
    path === "/api/operator-batches"
  ) {
    return path === "/api/notices" ? { items: [] } : [];
  }
  return {};
}

async function measure(browser, route, viewport) {
  const context = await browser.newContext({
    viewport: { width: viewport, height: viewport <= 375 ? 812 : 900 },
    locale: "zh-CN",
    colorScheme: "light",
  });
  const page = await context.newPage();
  await page.route("**/api/**", async (requestRoute) => {
    await requestRoute.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(apiPayload(requestRoute.request().url())),
    });
  });
  const scripts = new Map();
  page.on("response", (response) => {
    const url = response.url();
    if (!url.includes("/_next/static/") || !/\.js(?:\?|$)/.test(url)) return;
    if (!scripts.has(url)) {
      scripts.set(
        url,
        response
          .body()
          .then((body) => ({ rawBytes: body.length, gzipBytes: gzipSync(body).length }))
          .catch((error) => ({ error: String(error) })),
      );
    }
  });
  const started = performance.now();
  const response = await page.goto(`${baseUrl}${route}`, { waitUntil: "networkidle" });
  const loadMs = Math.round((performance.now() - started) * 1000) / 1000;
  const resolved = [];
  for (const [url, pending] of scripts) resolved.push({ url, ...(await pending) });
  await context.close();
  const successful = resolved.filter((item) => !item.error);
  const gzipBytes = successful.reduce((total, item) => total + item.gzipBytes, 0);
  return {
    route,
    viewport,
    httpStatus: response?.status() ?? null,
    loadMs,
    uniqueScriptCount: successful.length,
    rawBytes: successful.reduce((total, item) => total + item.rawBytes, 0),
    gzipBytes,
    gzipKiB: Math.round((gzipBytes / 1024) * 1000) / 1000,
    scripts: resolved,
  };
}

const routes = [
  { path: "/", budgetKiB: 300, class: "dashboard" },
  { path: "/sources", budgetKiB: 275, class: "crud" },
  { path: "/workflows", budgetKiB: 275, class: "crud" },
  { path: "/p4", budgetKiB: 275, class: "crud" },
  { path: "/detections", budgetKiB: 275, class: "crud" },
  { path: "/detections/incidents", budgetKiB: 275, class: "crud" },
  { path: "/reviews", budgetKiB: 275, class: "crud" },
  { path: "/mcp", budgetKiB: 275, class: "crud" },
  { path: "/audit", budgetKiB: 275, class: "crud" },
  { path: "/admin/users", budgetKiB: 275, class: "crud" },
  { path: "/admin/pipeline", budgetKiB: 275, class: "crud" },
  { path: "/admin/pipeline/bundles", budgetKiB: 275, class: "crud" },
];
const browser = await chromium.launch({ headless: true });
const chromiumVersion = browser.version();
const measurements = [];
try {
  for (const viewport of [375, 1440]) {
    for (const route of routes) {
      const result = await measure(browser, route.path, viewport);
      measurements.push({ ...route, ...result, passed: result.gzipKiB <= route.budgetKiB });
    }
  }
} finally {
  await browser.close();
}

const report = {
  schemaVersion: 1,
  baseUrl,
  apiMode: "browser-route-mock",
  compression: "node:zlib.gzipSync decoded response body; unique script URL per fresh context",
  browser: `Chromium ${chromiumVersion}`,
  node: process.version,
  measurements,
  passed: measurements.every(
    (item) => item.passed && item.httpStatus === 200 && item.uniqueScriptCount > 0,
  ),
};
mkdirSync(dirname(outputPath), { recursive: true });
writeFileSync(outputPath, `${JSON.stringify(report, null, 2)}\n`, { flag: "wx" });
console.log(
  JSON.stringify(
    {
      outputPath,
      passed: report.passed,
      browser: report.browser,
      measurements: measurements.map(
        ({ route, viewport, httpStatus, uniqueScriptCount, gzipKiB, budgetKiB, passed }) => ({
          route,
          viewport,
          httpStatus,
          uniqueScriptCount,
          gzipKiB,
          budgetKiB,
          passed,
        }),
      ),
    },
    null,
    2,
  ),
);
process.exitCode = report.passed ? 0 : 1;
