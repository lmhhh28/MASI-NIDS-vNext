import type {
  InferenceRuntimeMetrics,
  P4AgentRuntimeMetrics,
  RuntimeOperationalStatus,
  RuntimeReleaseStatus,
  RuntimeStatusMetrics,
  RuntimeStatusPublicItem,
  RuntimeStatusRole,
  TelemetryRuntimeMetrics,
} from "@/types/api";

export const RUNTIME_STATUS_ROLES = ["telemetry_v3", "inference_v3", "p4_agent"] as const;
export type RuntimeConsoleState =
  | "operational"
  | "hold"
  | "degraded"
  | "unknown"
  | "restart_required"
  | "failed";
export type RuntimeFreshness = "fresh" | "stale" | "missing" | "invalid";

export interface RuntimeRoleProjection {
  role: RuntimeStatusRole;
  state: RuntimeConsoleState;
  freshness: RuntimeFreshness;
  releaseStatus: RuntimeReleaseStatus;
  reasonCodes: string[];
  observedAt: string | null;
  receivedAt: string | null;
  expiresAt: string | null;
  processInstanceId: string | null;
  configId: string | null;
  sequence: string | null;
  metrics: RuntimeStatusMetrics | null;
}

export interface RuntimeConsoleProjection {
  schema: "masi.runtime-console.v1";
  sourceSchema: "masi.runtime-status-public.v1" | null;
  generatedAt: string | null;
  valid: boolean;
  overallState: RuntimeConsoleState;
  qualificationStatus: "hold";
  qualificationReason: "RELEASE_QUALIFICATION_NOT_ATTESTED";
  roles: Record<RuntimeStatusRole, RuntimeRoleProjection>;
}

const UINT64_MAX = "18446744073709551615";
const POSITIVE_INT64_MAX = "9223372036854775807";
const COUNT = /^(?:0|[1-9][0-9]{0,19})$/;
const POSITIVE_COUNT = /^[1-9][0-9]{0,18}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const REASON = /^[A-Z][A-Z0-9_]{0,63}$/;
const OPERATIONAL = new Set<RuntimeOperationalStatus>([
  "starting", "running", "hold", "degraded", "failed", "stopped", "unknown",
]);
const RELEASE = new Set<RuntimeReleaseStatus>(["pass", "hold", "unknown"]);

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function exactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const canonical = [...expected].sort();
  return actual.length === canonical.length && actual.every((key, index) => key === canonical[index]);
}

function validDate(value: unknown): value is string {
  return typeof value === "string"
    && value.length <= 40
    && /(?:Z|[+-][0-9]{2}:[0-9]{2})$/.test(value)
    && Number.isFinite(Date.parse(value));
}

function validCount(value: unknown, positive = false): value is string {
  if (typeof value !== "string" || !(positive ? POSITIVE_COUNT : COUNT).test(value)) return false;
  const maximum = positive ? POSITIVE_INT64_MAX : UINT64_MAX;
  return value.length < maximum.length || (value.length === maximum.length && value <= maximum);
}

function nullableCount(value: unknown): value is string | null {
  return value === null || validCount(value);
}

function validBoundedNumber(value: unknown, max: number): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= max;
}

function validSafeId(value: unknown): value is string {
  return typeof value === "string" && SAFE_ID.test(value);
}

const TELEMETRY_KEYS = [
  "kind", "runtime_generation", "bank_age_seconds", "rotation_lateness_seconds",
  "pending_phase", "output_bytes", "output_capacity_bytes", "output_horizon_seconds",
  "journal_bytes", "journal_operations", "quality", "next_rotation_ready",
  "active_segment_sequence", "active_segment_bytes", "active_segment_rotations",
  "oldest_unacknowledged_sequence", "unacknowledged_segments", "unacknowledged_bytes",
  "ack_high_water_sequence", "deletion_candidates", "segment_handoff_phase",
] as const;
const INFERENCE_KEYS = [
  "kind", "source_lag_records", "wal_bytes", "wal_capacity_bytes", "queue_depth",
  "queue_capacity", "group_size", "fsync_rate_hz", "event_retry_count",
  "checkpoint_sequence", "model_release_id", "model_scope_id", "model_gate",
] as const;
const AGENT_KEYS = [
  "kind", "primary", "session_state", "generation", "operation_state", "journal_bytes",
  "journal_operations", "bulk_queue_depth", "control_waiters", "p4_rpc_entity",
  "p4_rpc_latency_ms",
] as const;

function telemetryMetrics(value: Record<string, unknown>): TelemetryRuntimeMetrics | null {
  if (!exactKeys(value, TELEMETRY_KEYS)
    || value.kind !== "telemetry_v3"
    || !validCount(value.runtime_generation)
    || !validBoundedNumber(value.bank_age_seconds, 86_400)
    || !validBoundedNumber(value.rotation_lateness_seconds, 86_400)
    || !(value.pending_phase === null || validSafeId(value.pending_phase))
    || !validCount(value.output_bytes)
    || !validCount(value.output_capacity_bytes)
    || !validCount(value.output_horizon_seconds)
    || !nullableCount(value.journal_bytes)
    || !nullableCount(value.journal_operations)
    || !["healthy", "degraded", "unknown"].includes(String(value.quality))
    || typeof value.next_rotation_ready !== "boolean"
    || !nullableCount(value.active_segment_sequence)
    || !nullableCount(value.active_segment_bytes)
    || !nullableCount(value.active_segment_rotations)
    || !nullableCount(value.oldest_unacknowledged_sequence)
    || !nullableCount(value.unacknowledged_segments)
    || !nullableCount(value.unacknowledged_bytes)
    || !nullableCount(value.ack_high_water_sequence)
    || !nullableCount(value.deletion_candidates)
    || !(value.segment_handoff_phase === null || validSafeId(value.segment_handoff_phase))) return null;
  return value as unknown as TelemetryRuntimeMetrics;
}

function inferenceMetrics(value: Record<string, unknown>): InferenceRuntimeMetrics | null {
  if (!exactKeys(value, INFERENCE_KEYS)
    || value.kind !== "inference_v3"
    || !nullableCount(value.source_lag_records)
    || !validCount(value.wal_bytes)
    || !validCount(value.wal_capacity_bytes)
    || !validCount(value.queue_depth)
    || !validCount(value.queue_capacity)
    || !nullableCount(value.group_size)
    || !(value.fsync_rate_hz === null || validBoundedNumber(value.fsync_rate_hz, 1_000_000))
    || !nullableCount(value.event_retry_count)
    || !validCount(value.checkpoint_sequence)
    || !validSafeId(value.model_release_id)
    || !validSafeId(value.model_scope_id)
    || !["pass", "hold", "unknown"].includes(String(value.model_gate))) return null;
  return value as unknown as InferenceRuntimeMetrics;
}

function agentMetrics(value: Record<string, unknown>): P4AgentRuntimeMetrics | null {
  if (!exactKeys(value, AGENT_KEYS)
    || value.kind !== "p4_agent"
    || typeof value.primary !== "boolean"
    || !["no_session", "primary", "standby", "degraded"].includes(String(value.session_state))
    || !validCount(value.generation)
    || !["idle", "active", "unknown", "degraded"].includes(String(value.operation_state))
    || !validCount(value.journal_bytes)
    || !validCount(value.journal_operations)
    || !validCount(value.bulk_queue_depth)
    || !validCount(value.control_waiters)
    || !(value.p4_rpc_entity === null || validSafeId(value.p4_rpc_entity))
    || !(value.p4_rpc_latency_ms === null || validBoundedNumber(value.p4_rpc_latency_ms, 300_000))) return null;
  return value as unknown as P4AgentRuntimeMetrics;
}

function parseMetrics(value: unknown, role: RuntimeStatusRole): RuntimeStatusMetrics | null {
  const candidate = record(value);
  if (!candidate || candidate.kind !== role) return null;
  if (role === "telemetry_v3") return telemetryMetrics(candidate);
  if (role === "inference_v3") return inferenceMetrics(candidate);
  return agentMetrics(candidate);
}

const ITEM_KEYS = [
  "role", "process_instance_id", "config_id", "sequence", "observed_at", "received_at",
  "expires_at", "fresh", "operational_status", "release_status", "reason_codes", "metrics",
] as const;

function parseItem(value: unknown): RuntimeStatusPublicItem | null {
  const item = record(value);
  if (!item || !exactKeys(item, ITEM_KEYS)
    || !RUNTIME_STATUS_ROLES.includes(item.role as RuntimeStatusRole)
    || typeof item.process_instance_id !== "string" || !UUID_V4.test(item.process_instance_id)
    || typeof item.config_id !== "string" || !SHA256.test(item.config_id)
    || !validCount(item.sequence, true)
    || !validDate(item.observed_at) || !validDate(item.received_at) || !validDate(item.expires_at)
    || typeof item.fresh !== "boolean"
    || typeof item.operational_status !== "string" || !OPERATIONAL.has(item.operational_status as RuntimeOperationalStatus)
    || typeof item.release_status !== "string" || !RELEASE.has(item.release_status as RuntimeReleaseStatus)
    || !Array.isArray(item.reason_codes) || item.reason_codes.length > 16
    || item.reason_codes.some((code) => typeof code !== "string" || !REASON.test(code))
    || item.reason_codes.some((code, index, reasons) => index > 0 && reasons[index - 1] >= code)) return null;
  const role = item.role as RuntimeStatusRole;
  const metrics = parseMetrics(item.metrics, role);
  if (!metrics) return null;
  if (!item.fresh && (
    item.operational_status !== "unknown"
    || item.release_status !== "hold"
    || !item.reason_codes.includes("RUNTIME_STATUS_STALE")
  )) return null;
  return { ...(item as unknown as RuntimeStatusPublicItem), metrics };
}

function emptyRole(
  role: RuntimeStatusRole,
  freshness: RuntimeFreshness,
  reason: string,
): RuntimeRoleProjection {
  return {
    role,
    state: "unknown",
    freshness,
    releaseStatus: "hold",
    reasonCodes: [reason],
    observedAt: null,
    receivedAt: null,
    expiresAt: null,
    processInstanceId: null,
    configId: null,
    sequence: null,
    metrics: null,
  };
}

function consoleState(item: RuntimeStatusPublicItem): RuntimeConsoleState {
  if (!item.fresh) return "unknown";
  if (item.reason_codes.some((code) => code.includes("RESTART_REQUIRED"))) return "restart_required";
  if (item.operational_status === "running") return "operational";
  if (item.operational_status === "degraded") return "degraded";
  if (item.operational_status === "failed") return "failed";
  if (item.operational_status === "unknown") return "unknown";
  return "hold";
}

function itemProjection(item: RuntimeStatusPublicItem): RuntimeRoleProjection {
  const state = consoleState(item);
  return {
    role: item.role,
    state,
    freshness: item.fresh ? "fresh" : "stale",
    releaseStatus: item.fresh && state === "operational" ? item.release_status : "hold",
    reasonCodes: item.reason_codes,
    observedAt: item.observed_at,
    receivedAt: item.received_at,
    expiresAt: item.expires_at,
    processInstanceId: item.process_instance_id,
    configId: item.config_id,
    sequence: item.sequence,
    metrics: item.metrics,
  };
}

function overallState(roles: Record<RuntimeStatusRole, RuntimeRoleProjection>): RuntimeConsoleState {
  const states = RUNTIME_STATUS_ROLES.map((role) => roles[role].state);
  if (states.includes("failed")) return "failed";
  if (states.includes("unknown")) return "unknown";
  if (states.includes("restart_required")) return "restart_required";
  if (states.includes("degraded")) return "degraded";
  if (states.includes("hold")) return "hold";
  return "operational";
}

function baseProjection(
  valid: boolean,
  freshness: RuntimeFreshness,
  reason: string,
): RuntimeConsoleProjection {
  const roles = Object.fromEntries(
    RUNTIME_STATUS_ROLES.map((role) => [role, emptyRole(role, freshness, reason)]),
  ) as Record<RuntimeStatusRole, RuntimeRoleProjection>;
  return {
    schema: "masi.runtime-console.v1",
    sourceSchema: null,
    generatedAt: null,
    valid,
    overallState: "unknown",
    qualificationStatus: "hold",
    qualificationReason: "RELEASE_QUALIFICATION_NOT_ATTESTED",
    roles,
  };
}

export function unavailableRuntimeProjection(reason = "RUNTIME_STATUS_UNAVAILABLE"): RuntimeConsoleProjection {
  return baseProjection(false, "missing", reason);
}

export function parseRuntimeStatusProjection(value: unknown): RuntimeConsoleProjection {
  const payload = record(value);
  if (!payload
    || !exactKeys(payload, ["schema", "generated_at", "items"])
    || payload.schema !== "masi.runtime-status-public.v1"
    || !validDate(payload.generated_at)
    || !Array.isArray(payload.items)
    || payload.items.length > RUNTIME_STATUS_ROLES.length) {
    return baseProjection(false, "invalid", "RUNTIME_STATUS_PROJECTION_INVALID");
  }
  const parsed = payload.items.map(parseItem);
  if (parsed.some((item) => item === null)) {
    return baseProjection(false, "invalid", "RUNTIME_STATUS_PROJECTION_INVALID");
  }
  const unique = new Map<RuntimeStatusRole, RuntimeStatusPublicItem>();
  for (const item of parsed as RuntimeStatusPublicItem[]) {
    if (unique.has(item.role)) return baseProjection(false, "invalid", "RUNTIME_STATUS_PROJECTION_INVALID");
    unique.set(item.role, item);
  }
  const roles = Object.fromEntries(RUNTIME_STATUS_ROLES.map((role) => {
    const item = unique.get(role);
    return [role, item ? itemProjection(item) : emptyRole(role, "missing", "RUNTIME_STATUS_ROLE_MISSING")];
  })) as Record<RuntimeStatusRole, RuntimeRoleProjection>;
  return {
    schema: "masi.runtime-console.v1",
    sourceSchema: "masi.runtime-status-public.v1",
    generatedAt: payload.generated_at as string,
    valid: true,
    overallState: overallState(roles),
    qualificationStatus: "hold",
    qualificationReason: "RELEASE_QUALIFICATION_NOT_ATTESTED",
    roles,
  };
}
