import { expect, test } from "@playwright/test";

import { installMockApi } from "./mock-api";

const switchSession = {
  id: "s1",
  name: "switch-one",
  grpc_addr: "127.0.0.1:50051",
  device_id: 0,
  p4info_path: "p4/ae_nids.p4",
  p4info_hash: "p4-hash",
  pipeline_owner: "backend_p4_manager",
  read_state: "connected",
  writes_enabled: true,
};

test("a persisted P4 unknown outcome resolves only with its original key", async ({ page }) => {
  let resolveCalls = 0;
  let allowApplied = false;
  await installMockApi(page, {
    switches: [switchSession],
    resolveP4: async (route) => {
      expect(route.request().headers()["idempotency-key"]).toBe("original-p4-key");
      resolveCalls += 1;
      const applied = allowApplied;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          request_id: "request-1",
          status: applied ? "applied" : "unknown",
          switch_id: "s1",
          table_name: "MyIngress.nids_acl",
          operation: "insert",
          attempt_count: 1,
          max_attempts: 5,
          created_at: "2026-07-14T00:00:00Z",
          updated_at: "2026-07-14T00:00:00Z",
          deployment: applied
            ? { id: "deployment-1", status: "applied", operation: "insert", created_at: "2026-07-14T00:00:00Z", updated_at: "2026-07-14T00:00:00Z" }
            : null,
        }),
      });
    },
  });
  await page.addInitScript(() => {
    localStorage.setItem(
      "nids-operation-registry-v1",
      JSON.stringify({
        state: {
          operations: {
            "original-p4-key": {
              key: "original-p4-key",
              kind: "p4.apply",
              target: "s1:MyIngress.nids_acl",
              payloadFingerprint: "fingerprint",
              status: "outcome_unknown",
              operationStatus: "unknown",
              createdAt: "2026-07-14T00:00:00Z",
              updatedAt: "2026-07-14T00:00:00Z",
            },
          },
        },
        version: 1,
      }),
    );
  });

  await page.goto("/p4/s1");
  await expect(page.getByText("操作结果未决，请勿生成新幂等键重试")).toBeVisible();
  await expect(page.getByText("original-p4-key")).toBeVisible();
  allowApplied = true;
  await page.getByRole("button", { name: "使用原键查询" }).click();
  await expect(page.getByText("操作结果已确认")).toBeVisible();
  expect(resolveCalls).toBeGreaterThanOrEqual(2);
});

test("runtime configuration failure disables destructive P4 controls", async ({ page }) => {
  await installMockApi(page, { runtimeUnavailable: true, switches: [] });
  await page.goto("/p4");
  await expect(page.getByRole("button", { name: "注册交换机" })).toBeDisabled();
  await expect(page.getByText("运行时配置状态未知，高风险操作已禁用")).toBeVisible({ timeout: 10_000 });
});

test("an open P4 dialog keeps its draft and cannot submit after runtime becomes unknown", async ({ page }) => {
  let runtimeUnavailable = false;
  let registerCalls = 0;
  await installMockApi(page, { switches: [] });
  await page.route("**/api/config/runtime", (route) => route.fulfill({
    status: runtimeUnavailable ? 503 : 200,
    contentType: "application/json",
    body: JSON.stringify(runtimeUnavailable
      ? { detail: { code: "RUNTIME_UNAVAILABLE" } }
      : {
          background_tasks_enabled: true,
          auto_ingest_seconds: 0,
          auto_ingest_enabled: false,
          workflow_engine: "v2",
          workflow_maintenance: false,
          p4_maintenance: false,
        }),
  }));
  await page.route("**/api/p4/switches", async (route) => {
    if (route.request().method() === "POST") registerCalls += 1;
    await route.fulfill({ contentType: "application/json", body: "[]" });
  });

  await page.goto("/p4");
  await page.getByRole("button", { name: "注册交换机" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("名称").fill("draft-switch");
  runtimeUnavailable = true;

  await expect(page.getByText("运行时配置状态未知，高风险操作已禁用").first())
    .toBeVisible({ timeout: 15_000 });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel("名称")).toHaveValue("draft-switch");
  await expect(dialog.getByRole("button", { name: "注册", exact: true })).toBeDisabled();
  expect(registerCalls).toBe(0);
});

test("MCP manifest exposes 15 tools and redirects write tools to managed flows", async ({ page }) => {
  const names = [
    "get_recent_nids_events",
    "get_nids_event",
    "get_workflow_status",
    "list_p4_switches",
    "list_p4_tables",
    "get_p4_table_entries",
    "start_nids_analysis_workflow",
    "validate_p4_rule",
    "submit_human_review_decision",
    "cancel_nids_analysis_workflow",
    "deploy_approved_workflow",
    "retry_workflow_node",
    "retry_workflow_deployment",
    "apply_p4_rule",
    "rollback_p4_rule",
  ];
  const workflowTools = new Set([
    "start_nids_analysis_workflow",
    "submit_human_review_decision",
    "cancel_nids_analysis_workflow",
    "deploy_approved_workflow",
    "retry_workflow_node",
    "retry_workflow_deployment",
  ]);
  const tools = names.map((name) => ({
    name,
    description: `${name} description`,
    role_required: name.startsWith("apply_") || name.startsWith("rollback_") || name.startsWith("retry_") ? "admin" : "analyze",
    side_effect: name === "validate_p4_rule"
      ? "validate"
      : name === "apply_p4_rule" || name === "rollback_p4_rule"
        ? "p4_write"
        : workflowTools.has(name)
          ? "workflow_command"
          : "read",
    input_schema: { type: "object", properties: {} },
    output_schema_hint: "object",
  }));
  await installMockApi(page, { manifest: { count: 15, tools } });
  await page.goto("/mcp");
  await expect(page.getByText(/实时清单 · 15/)).toBeVisible();

  await page.getByText("apply_p4_rule", { exact: true }).click();
  await expect(page.getByRole("link", { name: "前往 P4 流程" })).toHaveAttribute("href", "/p4");
  await expect(page.getByText("该工具具有副作用，只能前往专用受控流程执行。")).toBeVisible();
});
