import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

import { installMockApi } from "./mock-api";

const bundleId = "a".repeat(64);
const eventId = "10000000-0000-4000-8000-000000000001";
const targetUuid = "20000000-0000-4000-8000-000000000002";

const eventV3 = {
  event_key_sha256: "b".repeat(64),
  event_id: eventId,
  event_kind: "ANOMALY_ENTER",
  incident_id: "30000000-0000-4000-8000-000000000003",
  source_run_id: "run-v3",
  segment_id: "segment-1",
  created_at: "2026-07-18T00:00:01Z",
  window_start_at: "2026-07-18T00:00:00Z",
  window_end_at: "2026-07-18T00:00:01Z",
  window_sequence: 7,
  target_uuid: targetUuid,
  runtime_generation: 3,
  protected_target_id: "protected-web",
  dst_ip: "10.0.0.2",
  ip_protocol: 6,
  dst_port: 443,
  pipeline_bundle_id: bundleId,
  bundle_variant_id: "bmv2-v1model",
  feature_contract_id: "feature-v3",
  model_release_id: "model-v3",
  model_role: "champion",
  decision: "anomaly",
  nearest_class: "syn_flood",
  compatibility: 0.98,
  reject_reason: "",
  completeness: 1,
  ingested_at: "2026-07-18T00:00:02Z",
  event: { schema_version: 4, decision: "anomaly" },
  workflow_admission: {
    eligible: true,
    policy_id: "masi.event-v4-workflow-admission.v1",
    template_name: "inspect_only",
    reason_code: null,
  },
};

const incidentV3 = {
  incident_id: eventV3.incident_id,
  target_uuid: targetUuid,
  runtime_generation: 3,
  protected_target_id: "protected-web",
  source_run_id: "run-v3",
  pipeline_bundle_id: bundleId,
  feature_contract_id: "feature-v3",
  model_release_id: "model-v3",
  model_role: "champion",
  status: "active",
  active_class: "syn_flood",
  opened_at: "2026-07-18T00:00:01Z",
  last_event_at: "2026-07-18T00:00:02Z",
  recovered_at: null,
  first_event_key_sha256: "b".repeat(64),
  last_event_key_sha256: "b".repeat(64),
  last_window_sequence: 7,
  event_count: 1,
  updated_at: "2026-07-18T00:00:02Z",
};

const pipelineBundle = {
  bundle_id: bundleId,
  schema_version: 2,
  bundle_name: "masi-ae-nids",
  bundle_version: "3.0.0",
  source_revision: "c".repeat(40),
  signing_key_id: "release-key-1",
  manifest: {},
  store_relative_path: `aa/${bundleId}`,
  status: "available",
  bundle_created_at: "2026-07-18T00:00:00Z",
  imported_by_user_id: "admin-1",
  imported_at: "2026-07-18T00:00:01Z",
  variants: [{
    bundle_id: bundleId,
    variant_id: "bmv2-v1model",
    adapter_kind: "bmv2_p4runtime",
    architecture: "v1model",
    activation_mode: "p4runtime_reconcile_and_commit",
    pipeline_cookie: "3",
    p4_source_sha256: "d".repeat(64),
    p4info_path: "build/p4info.txt",
    p4info_sha256: "e".repeat(64),
    p4info_size_bytes: 10,
    device_config_path: "build/pipeline.json",
    device_config_sha256: "f".repeat(64),
    device_config_size_bytes: 10,
    compiler: { name: "p4c", version: "1.2.3", oci_image_digest: `sha256:${"1".repeat(64)}` },
    capabilities: ["p4runtime_arbitration", "p4runtime_pipeline_update"],
    table_allowlists: { readable: ["Ingress.flow_table"], observation_write: [], mitigation_write: [] },
  }, {
    bundle_id: bundleId,
    variant_id: "bmv2-cold-boot",
    adapter_kind: "bmv2_p4runtime",
    architecture: "v1model",
    activation_mode: "immutable_cold_boot",
    pipeline_cookie: "4",
    p4_source_sha256: "d".repeat(64),
    p4info_path: "build/p4info.txt",
    p4info_sha256: "e".repeat(64),
    p4info_size_bytes: 10,
    device_config_path: "build/pipeline.json",
    device_config_sha256: "f".repeat(64),
    device_config_size_bytes: 10,
    compiler: { name: "p4c", version: "1.2.3", oci_image_digest: `sha256:${"1".repeat(64)}` },
    capabilities: ["immutable_pipeline"],
    table_allowlists: { readable: ["Ingress.flow_table"], observation_write: [], mitigation_write: [] },
  }],
};

const pipelineTarget = {
  target_uuid: targetUuid,
  target_identity_sha256: "2".repeat(64),
  registration: {
    schema_version: 3,
    target_uuid: targetUuid,
    connect_uri: "grpc://127.0.0.1:50051",
    device_id: "0",
    role: "",
    transport_security: { mode: "plaintext" },
  },
  connect_uri: "grpc://127.0.0.1:50051",
  device_id: "0",
  p4_role: "",
  transport_security_mode: "plaintext",
  enabled: true,
  desired_bundle_id: bundleId,
  desired_variant_id: "bmv2-v1model",
  active_bundle_id: bundleId,
  active_variant_id: "bmv2-v1model",
  active_pipeline_cookie: "3",
  runtime_generation: 3,
  generation_state: "changed",
  registration_version: 2,
  created_by_user_id: "admin-1",
  updated_by_user_id: "admin-1",
  created_at: "2026-07-18T00:00:00Z",
  updated_at: "2026-07-18T00:00:01Z",
};

const unknownActivation = {
  id: "40000000-0000-4000-8000-000000000004",
  target_uuid: targetUuid,
  bundle_id: bundleId,
  variant_id: "bmv2-v1model",
  activation_mode: "p4runtime_reconcile_and_commit",
  status: "outcome_unknown",
  expected_registration_version: 2,
  expected_runtime_generation: 3,
  idempotency_key: "ui-activation-history",
  request_sha256: "4".repeat(64),
  requested_by_user_id: "admin-1",
  observed_pipeline_cookie: null,
  observed_live_p4info_sha256: null,
  observed_agent_session_version: null,
  observation_ready: false,
  configured_digest_count: null,
  error_code: "P4_AGENT_ACTIVATION_OUTCOME_UNKNOWN",
  attempt_count: 1,
  next_retry_at: null,
  last_attempt_at: "2026-07-18T00:00:02Z",
  created_at: "2026-07-18T00:00:01Z",
  updated_at: "2026-07-18T00:00:02Z",
  terminal_at: null,
};

test("Event v3 list and detail remain accessible on a narrow viewport", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await installMockApi(page, {
    eventsV3: [eventV3],
    incidentsV3: [incidentV3],
    admitEventV4: async (route) => {
      expect(route.request().method()).toBe("POST");
      expect(route.request().headers()["idempotency-key"]).toMatch(/^[0-9a-f-]{36}$/);
      const body = route.request().postDataJSON();
      expect(body.requested_intent).toBeNull();
      expect(["zh-CN", "en-US"]).toContain(body.locale);
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          id: "workflow-event-v4-1",
          revision: 0,
          status: "admitted",
          template_name: "inspect_only",
          requested_intent: null,
          actor_user_id: "admin-1",
          source_event_id: eventId,
          source_event_schema_version: 4,
          source_event_hash: "9".repeat(64),
          event_admission_policy_id: "masi.event-v4-workflow-admission.v1",
          alert_group_id: null,
          review_packet_hash: null,
          approved_at: null,
          approval_user_id: null,
          created_at: "2026-07-18T00:00:03Z",
          updated_at: "2026-07-18T00:00:03Z",
          artifacts: [],
          agent_steps: [],
          p4_rule_plans: [],
        }),
      });
    },
  });
  await page.goto("/detections");
  await expect(page.getByRole("heading", { name: /检测事件与事故 v3|Detections and Incidents v3/ })).toBeVisible();
  await expect(page.getByText("syn_flood", { exact: false })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)).toBe(false);
  for (const control of [
    page.locator("#detections-v3-target-filter"),
    page.getByRole("button", { name: /应用筛选|Apply filter/ }),
    page.getByRole("link", { name: /事件事实|Event facts/ }),
    page.getByRole("link", { name: /事故投影|Incident projections/ }),
    page.locator("#event-v3-decision"),
    page.locator("#event-v3-role"),
    page.getByRole("link", { name: /查看事件详情|View event details/ }),
  ]) {
    const box = await control.boundingBox();
    expect(box?.height).toBeGreaterThanOrEqual(44);
  }
  let results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter((violation) => ["serious", "critical"].includes(violation.impact ?? ""))).toEqual([]);

  await page.getByRole("link", { name: /查看事件详情|View event details/ }).click();
  await expect(page).toHaveURL(new RegExp(`/detections/${eventId}$`));
  await expect(page.getByRole("heading", { name: /v3 事件事实|v3 event fact/ })).toBeVisible();
  const startWorkflow = page.getByRole("button", { name: /启动调查|Start investigation/ });
  expect((await startWorkflow.boundingBox())?.height).toBeGreaterThanOrEqual(44);
  await startWorkflow.click();
  const workflowLink = page.getByRole("link", { name: /打开工作流|Open workflow/ });
  await expect(workflowLink).toHaveAttribute("href", "/workflows/workflow-event-v4-1");
  results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter((violation) => ["serious", "critical"].includes(violation.impact ?? ""))).toEqual([]);

  await page.goto("/detections/incidents");
  await expect(page.getByText("syn_flood", { exact: false })).toBeVisible();
  expect((await page.locator("#incident-v3-status").boundingBox())?.height).toBeGreaterThanOrEqual(44);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)).toBe(false);
});

test("Detection v3 views keep navigation position and a valid target URL filter", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const eventRequests: string[] = [];
  const incidentRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/events/v3") eventRequests.push(url.search);
    if (url.pathname === "/api/incidents/v3") incidentRequests.push(url.search);
  });
  await installMockApi(page, { eventsV3: [eventV3], incidentsV3: [incidentV3] });

  await page.goto("/detections?target_uuid=not-a-uuid");
  const targetInput = page.locator("#detections-v3-target-filter");
  const applyFilter = page.getByRole("button", { name: /应用筛选|Apply filter/ });
  const views = page.getByRole("navigation", { name: /检测 v3 视图|Detection v3 views/ });
  const incidentsLink = page.getByRole("link", { name: /事故投影|Incident projections/ });

  await expect(targetInput).toHaveValue("not-a-uuid");
  await expect(targetInput).toHaveAttribute("aria-invalid", "true");
  await expect(
    page.getByRole("alert").filter({ hasText: /请输入规范的 UUID|Enter a canonical UUID/ }),
  ).toBeVisible();
  await expect(incidentsLink).toHaveAttribute("href", "/detections/incidents");
  await expect.poll(() => eventRequests.length).toBeGreaterThan(0);
  expect(eventRequests.every((search) => !new URLSearchParams(search).has("target_uuid"))).toBe(true);

  await targetInput.fill(targetUuid);
  await applyFilter.click();
  await expect(page).toHaveURL(new RegExp(`/detections\\?target_uuid=${targetUuid}$`));
  await expect.poll(() => eventRequests.some(
    (search) => new URLSearchParams(search).get("target_uuid") === targetUuid,
  )).toBe(true);

  const eventsNavBox = await views.boundingBox();
  if (!eventsNavBox) throw new Error("Detection views navigation is not visible on the events page");
  await incidentsLink.click();
  await expect(page).toHaveURL(new RegExp(`/detections/incidents\\?target_uuid=${targetUuid}$`));
  await expect(targetInput).toHaveValue(targetUuid);
  await expect.poll(() => incidentRequests.some(
    (search) => new URLSearchParams(search).get("target_uuid") === targetUuid,
  )).toBe(true);

  const incidentsNavBox = await views.boundingBox();
  if (!incidentsNavBox) throw new Error("Detection views navigation is not visible on the incidents page");
  expect(incidentsNavBox.y).toBe(eventsNavBox.y);

  await targetInput.fill("");
  await applyFilter.click();
  await expect(page).toHaveURL(/\/detections\/incidents$/);
  await expect.poll(() => {
    const latest = incidentRequests.at(-1) ?? "";
    return new URLSearchParams(latest).has("target_uuid");
  }).toBe(false);
});

test("Runtime operations stay HOLD, responsive, bilingual, and GET-only", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  const runtimeMethods: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/runtime-status/v1") runtimeMethods.push(request.method());
  });
  await installMockApi(page);
  await page.goto("/sources");
  await expect(page.getByRole("heading", { name: /v3 运行时运维状态|v3 runtime operations/ })).toBeVisible();
  await expect(page.getByText(/HOLD \/ NO-GO/)).toBeVisible();
  await expect(page.getByText("DATAPLANE_QUIESCENCE_UNPROVEN")).toBeVisible();
  await expect(page.getByText(/数据面 quiescence 不受支持|Dataplane quiescence is unsupported/)).toBeVisible();
  await expect(page.getByText("900 / 1000 B")).toBeVisible();
  await expect(page.getByText("release-v7")).toBeVisible();
  await expect(page.getByText("scope-exact-target")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)).toBe(false);

  const refresh = page.getByRole("button", { name: /刷新|Refresh/ }).first();
  expect((await refresh.boundingBox())?.height).toBeGreaterThanOrEqual(44);
  await refresh.focus();
  await expect(refresh).toBeFocused();
  await refresh.click();
  await page.getByRole("button", { name: /EN|英语|English/ }).click();
  await expect(page.getByText("Release qualification: HOLD / NO-GO")).toBeVisible();
  await expect(page.getByText("Operational health never proves release qualification.", { exact: false })).toBeVisible();
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter((violation) => ["serious", "critical"].includes(violation.impact ?? ""))).toEqual([]);
  expect(runtimeMethods.length).toBeGreaterThan(0);
  expect(new Set(runtimeMethods)).toEqual(new Set(["GET"]));
});

test("Stale and missing runtime roles fail closed as unknown HOLD", async ({ page }) => {
  const staleTelemetry = {
    role: "telemetry_v3",
    process_instance_id: "10000000-0000-4000-8000-000000000001",
    config_id: "a".repeat(64), sequence: "7", observed_at: "2026-07-19T07:00:00Z",
    received_at: "2026-07-19T07:00:00Z", expires_at: "2026-07-19T07:00:15Z",
    fresh: false, operational_status: "unknown", release_status: "hold",
    reason_codes: ["DATAPLANE_QUIESCENCE_UNPROVEN", "RUNTIME_STATUS_STALE"],
    metrics: {
      kind: "telemetry_v3", runtime_generation: "3", bank_age_seconds: 20,
      rotation_lateness_seconds: 1, pending_phase: null, output_bytes: "1",
      output_capacity_bytes: "2", output_horizon_seconds: "0", journal_bytes: null,
      journal_operations: null, quality: "unknown", next_rotation_ready: false,
      active_segment_sequence: null, active_segment_bytes: null, active_segment_rotations: null,
      oldest_unacknowledged_sequence: null, unacknowledged_segments: null,
      unacknowledged_bytes: null, ack_high_water_sequence: null, deletion_candidates: null,
      segment_handoff_phase: null,
    },
  };
  await installMockApi(page, {
    runtimeStatusV1: {
      schema: "masi.runtime-status-public.v1",
      generated_at: "2026-07-19T07:01:00Z",
      items: [staleTelemetry],
    },
  });
  await page.goto("/sources");
  await expect(page.getByText("RUNTIME_STATUS_STALE")).toBeVisible();
  await expect(page.getByText("RUNTIME_STATUS_ROLE_MISSING")).toBeVisible();
  await expect(page.getByText(/未知|Unknown/).first()).toBeVisible();
  await expect(page.getByText(/发布 HOLD|Release HOLD/).first()).toBeVisible();
});

test("Pipeline console permits a guarded hot activation for a verified generation", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await installMockApi(page, {
    pipelineBundlesV3: [pipelineBundle],
    pipelineTargetsV3: [{ ...pipelineTarget, generation_state: "verified" }],
    pipelineActivationsV3: [unknownActivation],
    activatePipeline: async (route) => {
      expect(route.request().method()).toBe("POST");
      expect(route.request().headers()["idempotency-key"]).toMatch(/^ui-activation-[0-9a-f-]+$/);
      expect(route.request().postDataJSON()).toEqual({
        schema_version: 1,
        bundle_id: bundleId,
        variant_id: "bmv2-v1model",
      });
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({ status: "pending" }),
      });
    },
  });
  await page.goto("/admin/pipeline");
  await expect(page.getByRole("heading", { name: /P4 Pipeline 控制 v3|P4 Pipeline Control v3/ })).toBeVisible();
  await expect(page.getByText(/generation 已验证|generation verified/).first()).toBeVisible();
  await page.getByRole("button", { name: /请求激活|Request activation/ }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByRole("combobox", { name: /Bundle variant/ })).toContainText("bmv2-v1model");
  await expect(page.getByText(/热激活需要已验证|Hot activation needs/)).toHaveCount(0);
  await expect(page.getByRole("checkbox")).not.toBeChecked();
  const submit = page.getByRole("button", { name: /提交激活请求|Submit activation request/ });
  await expect(submit).toBeDisabled();
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter((violation) => ["serious", "critical"].includes(violation.impact ?? ""))).toEqual([]);

  await page.getByRole("checkbox").check();
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(page.getByText(/激活请求已受理|Activation request accepted/)).toBeVisible();
  await page.getByRole("button", { name: /激活历史|Activation history/ }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByText(/远端激活结果未知|remote activation outcome is unknown/)).toBeVisible();
  await expect(page.getByText("P4_AGENT_ACTIVATION_OUTCOME_UNKNOWN")).toBeVisible();
  await page.getByRole("button", { name: /关闭|Close/ }).click();
  await page.getByRole("link", { name: /签名 bundles|Signed bundles/ }).click();
  await expect(page).toHaveURL(/\/admin\/pipeline\/bundles$/);
  await expect(page.getByRole("heading", { name: /签名不可变 bundles|Signed immutable bundles/ })).toBeVisible();
});

test("An unbound target disables hot activation but keeps immutable cold boot available", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const unboundTarget = {
    ...pipelineTarget,
    desired_bundle_id: null,
    desired_variant_id: null,
    active_bundle_id: null,
    active_variant_id: null,
    active_pipeline_cookie: null,
    runtime_generation: 0,
    generation_state: "unbound",
  };
  await installMockApi(page, {
    pipelineBundlesV3: [pipelineBundle],
    pipelineTargetsV3: [unboundTarget],
    pipelineActivationsV3: [{ ...unknownActivation, status: "restart_required", error_code: null }],
    activatePipeline: async (route) => {
      expect(route.request().postDataJSON()).toEqual({
        schema_version: 1,
        bundle_id: bundleId,
        variant_id: "bmv2-cold-boot",
      });
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({ status: "restart_required" }),
      });
    },
  });

  await page.goto("/admin/pipeline");
  await expect(page.getByText(/尚未绑定|not yet bound/).first()).toBeVisible();
  await page.getByRole("button", { name: /请求激活|Request activation/ }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByText(/热激活需要已验证|Hot activation needs/)).toBeVisible();

  const variantSelect = page.getByRole("combobox", { name: /Bundle variant/ });
  await expect(variantSelect).toContainText("bmv2-cold-boot");
  await variantSelect.click();
  await expect(page.getByRole("option", { name: /bmv2-v1model/ })).toBeDisabled();
  await expect(page.getByRole("option", { name: /bmv2-cold-boot/ })).toBeEnabled();
  await page.keyboard.press("Escape");

  const submit = page.getByRole("button", { name: /提交激活请求|Submit activation request/ });
  await expect(submit).toBeDisabled();
  await page.getByRole("checkbox").check();
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(page.getByText(/激活请求已受理|Activation request accepted/)).toBeVisible();
  await page.getByRole("button", { name: /激活历史|Activation history/ }).click();
  await expect(page.getByText(/使用批准的外部重启 runbook|approved external restart runbook/)).toBeVisible();
});
