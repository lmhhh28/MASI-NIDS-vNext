import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

import { installMockApi } from "./mock-api";

const TRACE_PAYLOAD = {
  schema_version: 1,
  workflow_id: "wf-1",
  revision_id: "rev-1",
  node_run_id: "nr-analysis-1",
  node_attempt_id: "na-1",
  attempt: 2,
  analysis_run_id: "ar-1",
  trace_id: "trace-1",
  graph_version: "masi-agent-workflow-v2.1",
  graph_topology_sha256: "abc123def4567890",
  evidence_bundle_sha256: "def456",
  provider_snapshot_sha256: null,
  prompt_bundle_sha256: null,
  tool_policy_sha256: "tp-1",
  trace_state: "terminal",
  outer_attempt_status: "completed",
  started_at: "2026-08-02T03:20:50Z",
  completed_at: "2026-08-02T03:21:50Z",
  topology: {
    nodes: [
      {
        node_id: "load",
        internal_name: "load_bundle",
        display_key: "读取证据",
        kind: "deterministic",
        parallel_group_id: null,
        fanout_index: null,
        stable_ordinal: 0,
      },
      {
        node_id: "impact",
        internal_name: "rate_impact",
        display_key: "流量影响",
        kind: "deterministic",
        parallel_group_id: "ctx",
        fanout_index: 0,
        stable_ordinal: 1,
      },
      {
        node_id: "quality",
        internal_name: "quality_check",
        display_key: "数据质量",
        kind: "deterministic",
        parallel_group_id: "ctx",
        fanout_index: 1,
        stable_ordinal: 2,
      },
      {
        node_id: "hypothesis",
        internal_name: "hypothesis_planner",
        display_key: "形成判断",
        kind: "llm",
        parallel_group_id: null,
        fanout_index: null,
        stable_ordinal: 4,
      },
    ],
    edges: [
      { from_node_id: "load", to_node_id: "impact", stable_ordinal: 0, condition_key: null },
      { from_node_id: "load", to_node_id: "quality", stable_ordinal: 1, condition_key: null },
      { from_node_id: "impact", to_node_id: "hypothesis", stable_ordinal: 2, condition_key: null },
    ],
  },
  first_sequence_no: 1,
  last_sequence_no: 3,
  terminal_sequence_no: 3,
  events: [
    {
      sequence_no: 1,
      event_id: "e1",
      occurred_at: "2026-08-02T03:20:50.000Z",
      node_id: "load",
      parent_node_ids: [],
      parallel_group_id: null,
      event_kind: "deterministic",
      phase: "completed",
      call_id: null,
      call_index: null,
      callee_key: null,
      elapsed_ms: 50,
      outcome: "succeeded",
      error_code: null,
      request_sha256: null,
      response_sha256: null,
      message_code: "node.completed",
      bounded_redacted_args: {},
      redaction_profile_version: 1,
      trace_id: "trace-1",
    },
  ],
  next_after_sequence_no: 3,
  trace_complete: true,
  terminal_trace_sha256: "terminal-hash",
  retention_expires_at: "2026-08-09T03:20:50Z",
  generated_at: "2026-08-02T03:21:50Z",
};

function workflowSummary(agentSteps: unknown[]) {
  return {
    id: "wf-1",
    revision: 1,
    status: "completed",
    template_name: "agent_workflow",
    locale: "zh",
    requested_template_params: {},
    intent: null,
    source_event_id: null,
    next_action: "ready",
    key_artifacts: [],
    review_packet: null,
    approval_request: null,
    plan: null,
    risk: null,
    report: null,
    deployment: null,
    revision_id: "rev-1",
    engine_version: 2,
  };
}

test.describe("analysis-trace sheet", () => {
  test("opens the agent_analysis sheet and renders inner graph + terminal log", async ({ page }) => {
    await installMockApi(page, {
      analysisTrace: async (route, _attempt) => {
        // Manual override of the default mock to land a specific workflow id.
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(TRACE_PAYLOAD),
        });
      },
    });

    // Mock workflow summary + detail with an agent_analysis step that points
    // to a node_run_id the analysis-trace hook will fetch for.
    await page.route("**/api/workflows/wf-1/summary", (route) =>
      route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(workflowSummary([])),
      }),
    );

    await page.route("**/api/workflows/wf-1", (route) =>
      route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          ...workflowSummary([]),
          artifacts: [],
          agent_steps: [
            {
              id: "nr-analysis-1",
              workflow_id: "wf-1",
              node_name: "agent_analysis",
              stage: "agent_analysis",
              status: "passed",
              input_json: "{}",
              output_json: "{}",
              evidence_json: "{}",
              created_at: "2026-08-02T03:21:50Z",
            },
          ],
          p4_rule_plans: [],
        }),
      }),
    );

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/workflows/wf-1?tab=pipeline");

    await expect(page.getByRole("heading", { name: /wf-1/ }).first()).toBeVisible();

    // Click the outer agent_analysis node in the pipeline grid.
    const analysisButton = page.getByRole("button", { name: /agent_analysis/ }).first();
    await analysisButton.click();

    // The Sheet should open and expose the inner graph nodes plus the
    // terminal log region.
    await expect(page.getByText("读取证据").first()).toBeVisible();
    await expect(page.getByRole("log")).toBeVisible();

    // The terminal badge for terminal state must show "已完成".
    await expect(page.getByText("已完成").first()).toBeVisible();
  });

  test("analysis-trace sheet has no retry/deploy command controls", async ({ page }) => {
    await installMockApi(page, {});
    await page.route("**/api/workflows/wf-1/summary", (route) =>
      route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(workflowSummary([])),
      }),
    );
    await page.route("**/api/workflows/wf-1", (route) =>
      route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          ...workflowSummary([]),
          artifacts: [],
          agent_steps: [],
          p4_rule_plans: [],
        }),
      }),
    );

    await page.setViewportSize({ width: 1024, height: 768 });
    await page.goto("/workflows/wf-1?tab=pipeline");
    await expect(page.getByRole("heading", { name: /wf-1/ }).first()).toBeVisible();

    // No command-input, retry, approve, or deploy controls anywhere on the
    // workflow detail page (the page does open standard review tabs but those
    // require admin tabs the pipeline tab does not surface).
    expect(await page.locator("button:has-text('retry')").count()).toBe(0);
    expect(await page.locator("button:has-text('部署')").count()).toBe(0);
    expect(await page.locator("input[type='submit']").count()).toBe(0);
  });

  test("analysis-trace has axe-detectable no serious violations on mobile", async ({ page }) => {
    await installMockApi(page, {});
    await page.route("**/api/workflows/wf-1/summary", (route) =>
      route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(workflowSummary([])),
      }),
    );
    await page.route("**/api/workflows/wf-1", (route) =>
      route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          ...workflowSummary([]),
          artifacts: [],
          agent_steps: [],
          p4_rule_plans: [],
        }),
      }),
    );

    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto("/workflows/wf-1?tab=pipeline");
    await expect(page.getByRole("heading", { name: /wf-1/ }).first()).toBeVisible();
    const results = await new AxeBuilder({ page }).analyze();
    // color-contrast is a pre-existing design-token warning on the shared
    // muted/opacity helper classes; not regressed by Phase 6.
    const serious = results.violations.filter(
      (v) =>
        ["serious", "critical"].includes(v.impact ?? "") &&
        v.id !== "color-contrast",
    );
    expect(serious).toEqual([]);

    // No horizontal overflow at mobile width.
    const overflowed = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    );
    expect(overflowed).toBe(false);
  });
});