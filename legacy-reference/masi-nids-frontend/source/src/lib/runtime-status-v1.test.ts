import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { parseRuntimeStatusProjection } from "@/lib/runtime-status-v1";

const observed = "2026-07-19T07:00:00Z";
const base = {
  process_instance_id: "10000000-0000-4000-8000-000000000001",
  config_id: "a".repeat(64),
  sequence: "1",
  observed_at: observed,
  received_at: observed,
  expires_at: "2026-07-19T07:00:15Z",
  fresh: true,
  operational_status: "running",
  release_status: "hold",
  reason_codes: [] as string[],
};

const telemetry = {
  ...base,
  role: "telemetry_v3",
  reason_codes: ["DATAPLANE_QUIESCENCE_UNPROVEN"],
  metrics: {
    kind: "telemetry_v3",
    runtime_generation: "18446744073709551615",
    bank_age_seconds: 0.25,
    rotation_lateness_seconds: 0.01,
    pending_phase: null,
    output_bytes: "1024",
    output_capacity_bytes: "4096",
    output_horizon_seconds: "60",
    journal_bytes: null,
    journal_operations: null,
    quality: "degraded",
    next_rotation_ready: true,
    active_segment_sequence: "9",
    active_segment_bytes: "1024",
    active_segment_rotations: "64",
    oldest_unacknowledged_sequence: "7",
    unacknowledged_segments: "3",
    unacknowledged_bytes: "2048",
    ack_high_water_sequence: "6",
    deletion_candidates: "1",
    segment_handoff_phase: "active",
  },
};
const inference = {
  ...base,
  process_instance_id: "20000000-0000-4000-8000-000000000002",
  config_id: "b".repeat(64),
  role: "inference_v3",
  metrics: {
    kind: "inference_v3",
    source_lag_records: "8",
    wal_bytes: "900",
    wal_capacity_bytes: "1000",
    queue_depth: "3",
    queue_capacity: "32",
    group_size: null,
    fsync_rate_hz: null,
    event_retry_count: "2",
    checkpoint_sequence: "44",
    model_release_id: "release-v7",
    model_scope_id: "scope-exact-target",
    model_gate: "hold",
  },
};
const agent = {
  ...base,
  process_instance_id: "30000000-0000-4000-8000-000000000003",
  config_id: "c".repeat(64),
  role: "p4_agent",
  metrics: {
    kind: "p4_agent",
    primary: true,
    session_state: "primary",
    generation: "11",
    operation_state: "idle",
    journal_bytes: "500",
    journal_operations: "4",
    bulk_queue_depth: "0",
    control_waiters: "0",
    p4_rpc_entity: null,
    p4_rpc_latency_ms: null,
  },
};

function payload(items: unknown[]) {
  return { schema: "masi.runtime-status-public.v1", generated_at: observed, items };
}

describe("runtime status v1 console projection", () => {
  it("keeps fresh operational roles separate from release HOLD", () => {
    const result = parseRuntimeStatusProjection(payload([telemetry, inference, agent]));
    expect(result.valid).toBe(true);
    expect(result.roles.p4_agent.state).toBe("operational");
    expect(result.roles.p4_agent.releaseStatus).toBe("hold");
    expect(result.qualificationStatus).toBe("hold");
    expect(result.qualificationReason).toBe("RELEASE_QUALIFICATION_NOT_ATTESTED");
    expect(result.roles.telemetry_v3.metrics?.kind).toBe("telemetry_v3");
  });

  it("projects stale and missing roles as unknown HOLD without fallback", () => {
    const stale = {
      ...telemetry,
      fresh: false,
      operational_status: "unknown",
      release_status: "hold",
      reason_codes: ["DATAPLANE_QUIESCENCE_UNPROVEN", "RUNTIME_STATUS_STALE"],
    };
    const result = parseRuntimeStatusProjection(payload([stale]));
    expect(result.roles.telemetry_v3).toMatchObject({ state: "unknown", freshness: "stale", releaseStatus: "hold" });
    expect(result.roles.inference_v3).toMatchObject({ state: "unknown", freshness: "missing", releaseStatus: "hold" });
    expect(result.roles.inference_v3.reasonCodes).toEqual(["RUNTIME_STATUS_ROLE_MISSING"]);
    expect(result.overallState).toBe("unknown");
  });

  it("surfaces restart, config drift and generation-stale reasons without authorizing actions", () => {
    const restartAgent = {
      ...agent,
      operational_status: "hold",
      release_status: "hold",
      reason_codes: ["P4_AGENT_RESTART_REQUIRED"],
    };
    const result = parseRuntimeStatusProjection(payload([telemetry, inference, restartAgent]));
    expect(result.roles.p4_agent.state).toBe("restart_required");
    expect(result.roles.p4_agent.reasonCodes).toContain("P4_AGENT_RESTART_REQUIRED");
    expect(result.qualificationStatus).toBe("hold");
  });

  it("preserves segment ACK, WAL capacity, model gate and unsupported quiescence facts", () => {
    const result = parseRuntimeStatusProjection(payload([telemetry, inference, agent]));
    const telemetryMetrics = result.roles.telemetry_v3.metrics;
    const inferenceMetrics = result.roles.inference_v3.metrics;
    expect(telemetryMetrics?.kind === "telemetry_v3" && telemetryMetrics.unacknowledged_segments).toBe("3");
    expect(telemetryMetrics?.kind === "telemetry_v3" && telemetryMetrics.ack_high_water_sequence).toBe("6");
    expect(inferenceMetrics?.kind === "inference_v3" && inferenceMetrics.wal_bytes).toBe("900");
    expect(inferenceMetrics?.kind === "inference_v3" && inferenceMetrics.model_gate).toBe("hold");
    expect(result.roles.telemetry_v3.reasonCodes).toContain("DATAPLANE_QUIESCENCE_UNPROVEN");
  });

  it("fails the complete projection closed on malformed, duplicate, or rounded uint64 data", () => {
    const malformed = { ...telemetry, metrics: { ...telemetry.metrics, runtime_generation: 9 } };
    const duplicate = payload([telemetry, { ...telemetry }]);
    const naiveTime = payload([{ ...telemetry, observed_at: "2026-07-19T07:00:00" }]);
    for (const candidate of [payload([malformed]), duplicate, naiveTime, { ...payload([]), secret: "forbidden" }]) {
      const result = parseRuntimeStatusProjection(candidate);
      expect(result.valid).toBe(false);
      expect(result.overallState).toBe("unknown");
      expect(result.qualificationStatus).toBe("hold");
      expect(result.roles.telemetry_v3.reasonCodes).toEqual(["RUNTIME_STATUS_PROJECTION_INVALID"]);
    }
  });

  it("uses GET-only polling so focus and reconnect cannot trigger a mutation", () => {
    const hookPath = `${process.cwd()}/src/hooks/use-runtime-status-v1.ts`;
    const source = readFileSync(hookPath, "utf8");
    expect(source).toContain('.get("/runtime-status/v1"');
    expect(source).toContain("refetchOnWindowFocus: true");
    expect(source).not.toMatch(/\.post\(|\.put\(|\.patch\(|\.delete\(/);
  });
});
