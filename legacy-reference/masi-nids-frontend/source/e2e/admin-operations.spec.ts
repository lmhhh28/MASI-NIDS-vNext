import { expect, test } from "@playwright/test";

import { installMockApi } from "./mock-api";

const riskPolicy = (version: number, activeRuleCap: number) => ({
  id: "default",
  version,
  auto_apply_enabled: false,
  min_directional_confidence: 0.9,
  low_risk_ttl_seconds: 300,
  active_rule_cap: activeRuleCap,
  max_cleanup_lag_seconds: 60,
  updated_at: "2026-07-14T00:00:00Z",
});

test("risk policy conflict preserves the draft and never overwrites the newer version", async ({ page }) => {
  let conflicted = false;
  await installMockApi(page);
  await page.route("**/api/admin/risk-policy", async (route) => {
    if (route.request().method() === "GET") {
      const policy = conflicted ? riskPolicy(2, 30) : riskPolicy(1, 25);
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(policy) });
      return;
    }
    expect(route.request().headers()["if-match"]).toBe("1");
    expect(route.request().postDataJSON()).toMatchObject({
      active_rule_cap: 20,
      change_reason: "降低测试环境规则上限",
    });
    conflicted = true;
    await route.fulfill({
      status: 412,
      contentType: "application/json",
      body: JSON.stringify({ detail: { code: "RISK_POLICY_VERSION_CONFLICT" } }),
    });
  });

  await page.goto("/admin/risk");
  await page.getByLabel("活跃规则上限").fill("20");
  await page.getByLabel("变更原因").fill("降低测试环境规则上限");
  await page.getByRole("button", { name: "保存策略" }).click();
  await page.getByRole("button", { name: "保存", exact: true }).click();

  await expect(page.getByText(/草稿基于 v1，服务端为 v2/)).toBeVisible();
  await expect(page.getByLabel("活跃规则上限")).toHaveValue("20");
  await expect(page.getByText("策略设置（v2）")).toBeVisible();
});

test("demo cleanup requires a read-only preview and exact run-id confirmation", async ({ page }) => {
  let cleanupCalls = 0;
  let operationPolls = 0;
  await installMockApi(page);
  await page.route("**/api/admin/demo-traffic/status", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        enabled: true,
        available: true,
        running: false,
        default_train_run_id: "demo-train",
        default_online_run_id: "online-demo",
        active_operation_id: null,
        active_online_run_id: null,
        runtime_state: "ready",
        started_at: null,
        finished_at: null,
        online_health: {},
        detail: null,
      }),
    }),
  );
  await page.route("**/api/admin/demo-traffic/cleanup/preview**", (route) => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().postDataJSON()).toEqual({
      online_run_id: "online-demo",
      reset_backend_state: true,
    });
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        preview_id: "preview-demo-cleanup",
        online_run_id: "online-demo",
        reset_backend_state: true,
        backend_event_schema: "event_v2",
        global_event_count_before: 9,
        scope_hash: "scope-hash-demo-cleanup",
        expires_at: "2026-07-14T00:10:00Z",
        scoped_ids: { workflows: ["wf-preview"] },
        row_counts: { workflows: 1, events: 2 },
        paths: [{ path: "runs/online-demo", exists: true }],
        active_run_state: { active: false, incomplete: false },
      }),
    });
  });
  await page.route("**/api/admin/demo-traffic/operations/operation-demo-cleanup", async (route) => {
    operationPolls += 1;
    const succeeded = operationPolls >= 2;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        operation_id: "operation-demo-cleanup",
        kind: "cleanup",
        status: succeeded ? "succeeded" : "running",
        phase: succeeded ? "complete" : "database",
        online_run_id: "online-demo",
        attempt_count: 1,
        result: succeeded ? {
          cleaned: true,
          backend_event_schema: "event_v2",
          deleted_events: 2,
          deleted_alerts: 1,
          deleted_workflows: 1,
          deleted_source_runs: 1,
          remaining_global_events: 7,
        } : {},
        error_code: null,
        created_at: "2026-07-14T00:00:00Z",
        updated_at: succeeded ? "2026-07-14T00:00:02Z" : "2026-07-14T00:00:01Z",
        finished_at: succeeded ? "2026-07-14T00:00:02Z" : null,
      }),
    });
  });
  await page.route("**/api/admin/demo-traffic/cleanup", async (route) => {
    cleanupCalls += 1;
    expect(route.request().postDataJSON()).toEqual({
      preview_id: "preview-demo-cleanup",
      scope_hash: "scope-hash-demo-cleanup",
    });
    expect(route.request().headers()["idempotency-key"]).toBeTruthy();
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        operation_id: "operation-demo-cleanup",
        kind: "cleanup",
        status: "queued",
        phase: "admitted",
        online_run_id: "online-demo",
        attempt_count: 0,
        result: {},
        error_code: null,
        created_at: "2026-07-14T00:00:00Z",
        updated_at: "2026-07-14T00:00:00Z",
        finished_at: null,
      }),
    });
  });

  await page.goto("/admin/demo");
  await page.getByRole("textbox", { name: "在线运行 ID" }).fill("online-demo");
  await page.getByRole("button", { name: "清理", exact: true }).first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "清理范围预览" })).toBeVisible();
  await expect(dialog.getByText(/当前运行的 2 条 Event v2.*全局共有 9 条/)).toBeVisible();
  await expect(dialog.getByText(/不会删除 Event v3 数据/)).toBeVisible();
  await dialog.getByText("范围 ID").click();
  await expect(dialog.getByText("wf-preview")).toBeVisible();
  const confirm = dialog.getByRole("button", { name: "清理", exact: true });
  await expect(confirm).toBeDisabled();
  expect(cleanupCalls).toBe(0);
  await dialog.getByLabel(/输入 online run ID/).fill("online-demo");
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect.poll(() => cleanupCalls).toBe(1);
  await expect(page.getByText("任务已提交")).toBeVisible();
  await expect(page.getByText("running", { exact: true })).toBeVisible();
  await expect(page.getByText("演示数据已清理")).toBeVisible({ timeout: 8_000 });
  await expect(page.getByText("全局剩余 Event v2")).toBeVisible();
  await expect(page.getByText("7", { exact: true })).toBeVisible();
});

test("demo stop resolves a lost admission response by the same idempotency key", async ({ page }) => {
  let stopCalls = 0;
  let resolveCalls = 0;
  let submittedKey = "";
  await installMockApi(page);
  await page.route("**/api/admin/demo-traffic/status", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        enabled: true,
        available: true,
        running: true,
        runtime_state: "replaying",
        default_train_run_id: "demo-train",
        default_online_run_id: "online-demo",
        active_operation_id: "operation-demo-run",
        active_online_run_id: "online-demo",
        started_at: "2026-07-14T00:00:00Z",
        finished_at: null,
        online_health: {},
        detail: null,
      }),
    }),
  );
  await page.route("**/api/admin/demo-traffic/stop", async (route) => {
    stopCalls += 1;
    submittedKey = route.request().headers()["idempotency-key"] ?? "";
    expect(submittedKey).toBeTruthy();
    await route.abort("timedout");
  });
  await page.route("**/api/admin/demo-traffic/operations/resolve", async (route) => {
    resolveCalls += 1;
    expect(route.request().headers()["idempotency-key"]).toBe(submittedKey);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        operation_id: "operation-demo-stop",
        kind: "stop",
        status: "running",
        phase: "runtime",
        online_run_id: "online-demo",
        attempt_count: 1,
        result: {},
        error_code: null,
        created_at: "2026-07-14T00:00:00Z",
        updated_at: "2026-07-14T00:00:01Z",
        finished_at: null,
      }),
    });
  });
  await page.route("**/api/admin/demo-traffic/operations/operation-demo-stop", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        operation_id: "operation-demo-stop",
        kind: "stop",
        status: "succeeded",
        phase: "complete",
        online_run_id: "online-demo",
        attempt_count: 1,
        result: { stopped: true },
        error_code: null,
        created_at: "2026-07-14T00:00:00Z",
        updated_at: "2026-07-14T00:00:02Z",
        finished_at: "2026-07-14T00:00:02Z",
      }),
    }),
  );

  await page.goto("/admin/demo");
  await page.getByRole("button", { name: "停止", exact: true }).click();
  await expect.poll(() => resolveCalls).toBeGreaterThan(0);
  expect(stopCalls).toBe(1);
  await expect(page.getByText("演示流量停止已完成")).toBeVisible({ timeout: 8_000 });
});
