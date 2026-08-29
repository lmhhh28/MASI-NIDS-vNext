import type { Page, Route } from "@playwright/test";

export const adminUser = { id: "admin-1", username: "admin", role: "admin" };

const runtime = {
  background_tasks_enabled: true,
  auto_ingest_seconds: 0,
  auto_ingest_enabled: false,
  workflow_engine: "v2",
  workflow_maintenance: false,
  p4_maintenance: false,
};

const runtimeStatusV1 = {
  schema: "masi.runtime-status-public.v1",
  generated_at: "2026-07-19T07:00:00Z",
  items: [
    {
      role: "telemetry_v3",
      process_instance_id: "10000000-0000-4000-8000-000000000001",
      config_id: "a".repeat(64),
      sequence: "7",
      observed_at: "2026-07-19T07:00:00Z",
      received_at: "2026-07-19T07:00:00Z",
      expires_at: "2026-07-19T07:00:15Z",
      fresh: true,
      operational_status: "running",
      release_status: "hold",
      reason_codes: ["DATAPLANE_QUIESCENCE_UNPROVEN"],
      metrics: {
        kind: "telemetry_v3", runtime_generation: "3", bank_age_seconds: 0.4,
        rotation_lateness_seconds: 0.01, pending_phase: null, output_bytes: "2048",
        output_capacity_bytes: "8192", output_horizon_seconds: "120", journal_bytes: null,
        journal_operations: null, quality: "degraded", next_rotation_ready: true,
        active_segment_sequence: "9", active_segment_bytes: "2048", active_segment_rotations: "64",
        oldest_unacknowledged_sequence: "7", unacknowledged_segments: "3",
        unacknowledged_bytes: "4096", ack_high_water_sequence: "6", deletion_candidates: "1",
        segment_handoff_phase: "active",
      },
    },
    {
      role: "inference_v3",
      process_instance_id: "20000000-0000-4000-8000-000000000002",
      config_id: "b".repeat(64), sequence: "8", observed_at: "2026-07-19T07:00:00Z",
      received_at: "2026-07-19T07:00:00Z", expires_at: "2026-07-19T07:00:15Z",
      fresh: true, operational_status: "running", release_status: "hold", reason_codes: [],
      metrics: {
        kind: "inference_v3", source_lag_records: "8", wal_bytes: "900",
        wal_capacity_bytes: "1000", queue_depth: "3", queue_capacity: "32", group_size: null,
        fsync_rate_hz: null, event_retry_count: "2", checkpoint_sequence: "44",
        model_release_id: "release-v7", model_scope_id: "scope-exact-target", model_gate: "hold",
      },
    },
    {
      role: "p4_agent",
      process_instance_id: "30000000-0000-4000-8000-000000000003",
      config_id: "c".repeat(64), sequence: "9", observed_at: "2026-07-19T07:00:00Z",
      received_at: "2026-07-19T07:00:00Z", expires_at: "2026-07-19T07:00:15Z",
      fresh: true, operational_status: "running", release_status: "hold", reason_codes: [],
      metrics: {
        kind: "p4_agent", primary: true, session_state: "primary", generation: "3",
        operation_state: "idle", journal_bytes: "500", journal_operations: "4",
        bulk_queue_depth: "0", control_waiters: "0", p4_rpc_entity: null, p4_rpc_latency_ms: null,
      },
    },
  ],
};

const operations = {
  generated_at: "2026-07-14T00:00:00Z",
  cache_ttl_seconds: 2,
  events: { total: 0, anomaly: 0, unknown: 0, active_alerts: 0, latest_ingested_at: null },
  workflows: {
    active: 0,
    admission_pending: 0,
    oldest_active_seconds: 0,
    queue: { ready: 0, running: 0, failed: 0, oldest_ready_seconds: 0 },
    workers: { active: 1, stale: 0, inflight: 0 },
  },
  p4: { unknown_requests: 0, unknown_deployments: 0, rollback_unresolved: 0 },
  operator_batches: { active: 0, stopping: 0 },
  background_jobs: {
    scope: "durable_cluster",
    running: 0,
    failing: 0,
    items: [],
  },
  database: {
    scope: "process_instance",
    pool_size: 1,
    pool_available: 1,
    requests_waiting: 0,
    acquire_wait_p95_ms: 0,
    acquire_timeouts: 0,
  },
  maintenance: { workflow: false, p4: false, background_tasks_enabled: true },
};

export interface MockApiOptions {
  authenticated?: boolean;
  runtimeUnavailable?: boolean;
  switches?: unknown[];
  manifest?: unknown;
  eventsV3?: unknown[];
  incidentsV3?: unknown[];
  pipelineBundlesV3?: unknown[];
  pipelineTargetsV3?: unknown[];
  pipelineActivationsV3?: unknown[];
  runtimeStatusV1?: unknown;
  resolveP4?: (route: Route) => Promise<void>;
  activatePipeline?: (route: Route) => Promise<void>;
  admitEventV4?: (route: Route) => Promise<void>;
  liveV3?: unknown;
  trendsV3?: unknown;
  demoRunProjection?: unknown;
  analysisTrace?: (route: Route, attempt: string) => Promise<void>;
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

export async function installMockApi(page: Page, options: MockApiOptions = {}) {
  const authenticated = options.authenticated ?? true;
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/session") {
      return json(route, {
        authenticated,
        user: authenticated ? adminUser : null,
        csrf_token: "csrf-e2e",
      });
    }
    if (path === "/api/session/refresh") {
      return json(route, { authenticated: true, user: adminUser, csrf_token: "csrf-e2e" });
    }
    if (path === "/api/session/logout") return json(route, { status: "ok", revoked: true });
    if (path === "/api/notices") return json(route, { items: [] });
    if (path === "/api/config/runtime") {
      return options.runtimeUnavailable
        ? json(route, { detail: { code: "RUNTIME_UNAVAILABLE" } }, 503)
        : json(route, runtime);
    }
    if (path === "/api/config/risk-policy") {
      return json(route, {
        auto_apply_enabled: false,
        min_directional_confidence: 0.9,
        low_risk_ttl_seconds: 300,
        active_rule_cap: 25,
        max_cleanup_lag_seconds: 60,
        version: 1,
      });
    }
    if (path === "/api/operations/summary") return json(route, operations);
    if (path === "/api/runtime-status/v1") return json(route, options.runtimeStatusV1 ?? runtimeStatusV1);
    if (path === "/api/events/sources") return json(route, []);
    if (path === "/api/events/stats") return json(route, {});
    if (path === "/api/events/v3") {
      return json(route, { items: options.eventsV3 ?? [], next_cursor: null });
    }
    if (path.startsWith("/api/events/v3/")) {
      const id = path.split("/").at(-1);
      const event = (options.eventsV3 ?? []).find(
        (item) => typeof item === "object" && item !== null && (item as { event_id?: unknown }).event_id === id,
      );
      return event ? json(route, event) : json(route, { detail: { code: "EVENT_V3_NOT_FOUND" } }, 404);
    }
    if (path === "/api/incidents/v3") return json(route, options.incidentsV3 ?? []);
    if (path === "/api/analytics/v3/live") {
      return json(
        route,
        options.liveV3 ?? {
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
          samples: [],
          event_markers: [],
          current_incident: null,
        },
      );
    }
    if (path === "/api/analytics/v3/trends") {
      return json(
        route,
        options.trendsV3 ?? {
          schema: "masi.trends-v3.v1",
          generated_at: "2026-08-02T03:20:50Z",
          bucket: "5m",
          model_role: "champion",
          target_uuid: null,
          from: "2026-08-02T02:00:00Z",
          to: "2026-08-02T03:00:00Z",
          latest_event_observed_at: null,
          buckets: [],
        },
      );
    }
    if (path.startsWith("/api/demo/runs/") && path.endsWith("/projection")) {
      return json(
        route,
        options.demoRunProjection ?? {
          schema: "masi.demo-run-projection.v1",
          operation_id: "test-op",
          projection: {
            demo_operation_id: "test-op",
            source_run_id: "test-run",
            producer_status: "succeeded",
            overall_progress: "5/5",
            overall_complete: true,
          },
        },
      );
    }
    if (path === "/api/events") {
      return url.searchParams.has("include_total")
        ? json(route, { items: [], total: 0, limit: 100, offset: 0 })
        : json(route, []);
    }
    if (path.includes("/analysis-trace") && path.includes("/workflows/")) {
      const attempt = url.searchParams.get("attempt") ?? "current";
      if (options.analysisTrace) return options.analysisTrace(route, attempt);
      return json(
        route,
        {
          schema_version: 1,
          workflow_id: "wf-1",
          revision_id: "rev-1",
          node_run_id: "nr-1",
          node_attempt_id: "na-1",
          attempt: 2,
          analysis_run_id: "ar-1",
          trace_id: "trace-1",
          graph_version: "masi-agent-workflow-v2.1",
          graph_topology_sha256: "abc123def456",
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
              { node_id: "load", internal_name: "load_bundle", display_key: "读取证据", kind: "deterministic", parallel_group_id: null, fanout_index: null, stable_ordinal: 0 },
              { node_id: "hypothesis", internal_name: "hypothesis_planner", display_key: "形成判断", kind: "llm", parallel_group_id: null, fanout_index: null, stable_ordinal: 1 },
            ],
            edges: [
              { from_node_id: "load", to_node_id: "hypothesis", stable_ordinal: 0, condition_key: null },
            ],
          },
          first_sequence_no: 1,
          last_sequence_no: 2,
          terminal_sequence_no: 2,
          events: [
            { sequence_no: 1, event_id: "e1", occurred_at: "2026-08-02T03:20:50.000Z", node_id: "load", parent_node_ids: [], parallel_group_id: null, event_kind: "deterministic", phase: "completed", call_id: null, call_index: null, callee_key: null, elapsed_ms: 50, outcome: "succeeded", error_code: null, request_sha256: null, response_sha256: null, message_code: "node.completed", bounded_redacted_args: {}, redaction_profile_version: 1, trace_id: "trace-1" },
          ],
          next_after_sequence_no: 2,
          trace_complete: true,
          terminal_trace_sha256: "terminal-hash",
          retention_expires_at: "2026-08-09T03:20:50Z",
          generated_at: "2026-08-02T03:21:50Z",
        },
      );
    }
    if (path === "/api/alerts") return json(route, []);
    if (path === "/api/templates") return json(route, { items: [], meta: { llm_overlay_enabled: false } });
    if (path === "/api/workflows") return json(route, { items: [], total: 0, limit: 20, offset: 0 });
    if (path.startsWith("/api/workflows/event-v4/") && options.admitEventV4) {
      return options.admitEventV4(route);
    }
    if (path === "/api/p4/switches") return json(route, options.switches ?? []);
    if (path === "/api/p4/control-v3/pipeline-bundles") return json(route, options.pipelineBundlesV3 ?? []);
    if (path === "/api/p4/control-v3/targets") return json(route, options.pipelineTargetsV3 ?? []);
    if (path.endsWith("/activations")) {
      if (request.method() === "GET") return json(route, options.pipelineActivationsV3 ?? []);
      if (options.activatePipeline) return options.activatePipeline(route);
    }
    if (path.endsWith("/tables")) return json(route, []);
    if (path === "/api/p4/deployments") return json(route, { items: [], total: 0, limit: 25, offset: 0 });
    if (path === "/api/p4/deployment-requests/resolve" && options.resolveP4) {
      return options.resolveP4(route);
    }
    if (path === "/api/mcp/tools") {
      return json(route, options.manifest ?? { count: 0, tools: [] });
    }
    return json(route, {});
  });
}
