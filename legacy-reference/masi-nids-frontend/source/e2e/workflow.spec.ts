import { expect, test } from "@playwright/test";

import { installMockApi } from "./mock-api";

function workflowSummary(revision: number) {
  const stateVersion = revision + 3;
  return {
    id: "wf-1",
    workflow_id: "wf-1",
    revision,
    revision_id: `revision-${revision}`,
    state_version: stateVersion,
    status: "awaiting_review",
    execution_status: "awaiting_review",
    review_status: "open",
    deployment_status: "none",
    desired_state: "active",
    template_name: "block_exact_flow",
    locale: "zh-CN",
    requested_template_params: { ttl_seconds: 300 },
    intent: { template_name: "block_exact_flow" },
    source_event_id: "event-1",
    next_action: "submit_review_decision",
    next_actions: ["approve", "edit", "reject", "cancel"],
    key_artifacts: [],
    nodes: [],
    review_packet: {
      id: `packet-${revision}`,
      packet_hash: `packet-hash-${revision}`,
      status: "open",
      payload: {
        p4info_hash: "p4info-hash",
        table_schema_hash: "schema-hash",
        risk_policy_version: 7,
      },
    },
    approval_request: {
      review_packet_id: `packet-${revision}`,
      review_packet_hash: `packet-hash-${revision}`,
      artifact_ids: [],
      plan_revision: revision,
      risk_policy_version: 7,
      p4info_hash: "p4info-hash",
      table_schema_hash: "schema-hash",
      expected_revision: revision,
      expected_state_version: stateVersion,
    },
    plan: {
      id: `plan-${revision}`,
      plan_hash: `plan-hash-${revision}`,
      table_name: "MyIngress.nids_acl",
      operation: "insert",
      match_fields: { src_ip: "10.0.0.1" },
      action_name: "MyIngress.drop",
      action_params: {},
      ttl_seconds: 300,
    },
    risk: { version: 7, risk_level: "medium", requires_human: true },
    report: null,
    deployment: null,
    review_summary: null,
  };
}

test("workflow deep link preserves a review draft after a stale revision conflict", async ({ page }) => {
  let latestRevision = 1;
  await installMockApi(page);
  await page.route("**/api/workflows/wf-1/summary", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(workflowSummary(latestRevision)),
    }),
  );
  await page.route("**/api/workflows/wf-1", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ...workflowSummary(latestRevision),
        artifacts: [],
        agent_steps: [],
        p4_rule_plans: [],
      }),
    }),
  );
  await page.route("**/api/workflows/wf-1/review", async (route) => {
    const payload = route.request().postDataJSON();
    expect(payload).toMatchObject({
      decision: "reject",
      comment: "证据方向需要复核",
      expected_revision: 1,
      expected_state_version: 4,
    });
    latestRevision = 2;
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: { code: "WORKFLOW_STATE_CONFLICT" } }),
    });
  });

  await page.goto("/workflows/wf-1?tab=raw");
  await expect(page).toHaveURL(/\/workflows\/wf-1\?tab=raw$/);
  await expect(page.getByRole("heading", { name: "wf-1" })).toBeVisible();
  await page.getByLabel("原因 / 备注").fill("证据方向需要复核");
  await page.getByRole("button", { name: "拒绝", exact: true }).click();
  await page.getByRole("button", { name: "确认提交" }).click();

  await expect(page.getByText(/草稿已保留。提交时 revision 为 1，服务端当前 revision 为 2/)).toBeVisible();
  await expect(page.getByLabel("原因 / 备注")).toHaveValue("证据方向需要复核");
  await expect(page.getByText("revision 2", { exact: true })).toBeVisible();
});
