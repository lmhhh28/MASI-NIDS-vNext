/* ── Backend API response types ── */
import type { Locale } from "@/lib/messages";

export type NidsDecision = "normal" | "anomaly" | "unknown";

// Auth
export interface LoginResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

export interface UserInfo {
  id: string;
  username: string;
  role: "admin" | "analyze";
  enabled?: boolean;
  created_at?: string;
}

// Events
export interface NidsEvent {
  event_context?: "legacy_event_v2_per_flow";
  score_semantics?: "legacy_flow_reconstruction_error_not_p_value";
  id: string;
  source_id: string;
  source_run_id: string;
  source_event_id: string;
  online_run_id: string;
  train_run_id: string | null;
  decision: NidsDecision | string | null;
  nearest_class: string | null;
  flow_id: string | null;
  src_ip: string | null;
  dst_ip: string | null;
  src_port: number | null;
  dst_port: number | null;
  protocol: string | null;
  timestamp_start: number | null;
  timestamp_end: number | null;
  legacy_score: number | null;
  directional_evidence_id?: string | null;
  directional_evidence_status?: string | null;
  directional_evidence_confidence?: number | null;
  event_hash: string;
  raw_event: Record<string, unknown>;
  ingested_at: string;
}

/**
 * Envelope returned by `GET /api/events?include_total=1`.
 *
 * Backend keeps the array shape as default for backward compatibility; the
 * paginated dashboard table opts in to this envelope so it can render total
 * page counts without a separate count endpoint.
 */
export interface NidsEventListResponse {
  event_context?: "legacy_event_v2_per_flow";
  score_semantics?: "legacy_flow_reconstruction_error_not_p_value";
  items: NidsEvent[];
  total: number;
  limit: number;
  offset: number;
}

export interface EventStats {
  event_context?: "legacy_event_v2_per_flow";
  score_semantics?: "legacy_flow_reconstruction_error_not_p_value";
  total_events: number;
  anomaly_events: number;
  unknown_events: number;
  active_alerts: number;
}

// Event / incident v3
export type EventV3Kind =
  | "NORMAL_HEARTBEAT"
  | "ANOMALY_ENTER"
  | "ANOMALY_UPDATE"
  | "UNKNOWN"
  | "RECOVERY";

export type ModelRoleV3 = "champion" | "shadow";

export interface EventV3Summary {
  event_key_sha256: string;
  event_id: string;
  event_kind: EventV3Kind;
  incident_id: string | null;
  source_run_id: string;
  segment_id: string;
  created_at: string;
  window_start_at: string;
  window_end_at: string;
  window_sequence: number;
  target_uuid: string;
  runtime_generation: number;
  protected_target_id: string;
  dst_ip: string;
  ip_protocol: number;
  dst_port: number | null;
  pipeline_bundle_id: string;
  bundle_variant_id: string;
  feature_contract_id: string;
  model_release_id: string;
  model_role: ModelRoleV3;
  decision: NidsDecision;
  nearest_class: string;
  compatibility: number;
  reject_reason: string;
  completeness: number;
  ingested_at: string;
}

export interface EventV4WorkflowCapability {
  eligible: boolean;
  policy_id: "masi.event-v4-workflow-admission.v1";
  template_name: "inspect_only";
  reason_code: string | null;
}

export interface EventV3Detail extends EventV3Summary {
  event: Record<string, unknown>;
  workflow_admission: EventV4WorkflowCapability;
}

export interface EventV3ListResponse {
  items: EventV3Summary[];
  next_cursor: string | null;
}

export type RuntimeStatusRole = "telemetry_v3" | "inference_v3" | "p4_agent";
export type RuntimeOperationalStatus =
  | "starting"
  | "running"
  | "hold"
  | "degraded"
  | "failed"
  | "stopped"
  | "unknown";
export type RuntimeReleaseStatus = "pass" | "hold" | "unknown";

export interface TelemetryRuntimeMetrics {
  kind: "telemetry_v3";
  runtime_generation: string;
  bank_age_seconds: number;
  rotation_lateness_seconds: number;
  pending_phase: string | null;
  output_bytes: string;
  output_capacity_bytes: string;
  output_horizon_seconds: string;
  journal_bytes: string | null;
  journal_operations: string | null;
  quality: "healthy" | "degraded" | "unknown";
  next_rotation_ready: boolean;
  active_segment_sequence: string | null;
  active_segment_bytes: string | null;
  active_segment_rotations: string | null;
  oldest_unacknowledged_sequence: string | null;
  unacknowledged_segments: string | null;
  unacknowledged_bytes: string | null;
  ack_high_water_sequence: string | null;
  deletion_candidates: string | null;
  segment_handoff_phase: string | null;
}

export interface InferenceRuntimeMetrics {
  kind: "inference_v3";
  source_lag_records: string | null;
  wal_bytes: string;
  wal_capacity_bytes: string;
  queue_depth: string;
  queue_capacity: string;
  group_size: string | null;
  fsync_rate_hz: number | null;
  event_retry_count: string | null;
  checkpoint_sequence: string;
  model_release_id: string;
  model_scope_id: string;
  model_gate: "pass" | "hold" | "unknown";
}

export interface P4AgentRuntimeMetrics {
  kind: "p4_agent";
  primary: boolean;
  session_state: "no_session" | "primary" | "standby" | "degraded";
  generation: string;
  operation_state: "idle" | "active" | "unknown" | "degraded";
  journal_bytes: string;
  journal_operations: string;
  bulk_queue_depth: string;
  control_waiters: string;
  p4_rpc_entity: string | null;
  p4_rpc_latency_ms: number | null;
}

export type RuntimeStatusMetrics =
  | TelemetryRuntimeMetrics
  | InferenceRuntimeMetrics
  | P4AgentRuntimeMetrics;

export interface RuntimeStatusPublicItem {
  role: RuntimeStatusRole;
  process_instance_id: string;
  config_id: string;
  sequence: string;
  observed_at: string;
  received_at: string;
  expires_at: string;
  fresh: boolean;
  operational_status: RuntimeOperationalStatus;
  release_status: RuntimeReleaseStatus;
  reason_codes: string[];
  metrics: RuntimeStatusMetrics;
}

export interface RuntimeStatusPublicResponse {
  schema: "masi.runtime-status-public.v1";
  generated_at: string;
  items: RuntimeStatusPublicItem[];
}

export type IncidentV3Status = "active" | "unknown" | "recovered";

export interface IncidentV3 {
  incident_id: string;
  target_uuid: string;
  runtime_generation: number;
  protected_target_id: string;
  source_run_id: string;
  pipeline_bundle_id: string;
  feature_contract_id: string;
  model_release_id: string;
  model_role: ModelRoleV3;
  status: IncidentV3Status;
  active_class: string | null;
  opened_at: string;
  last_event_at: string;
  recovered_at: string | null;
  first_event_key_sha256: string;
  last_event_key_sha256: string;
  last_window_sequence: number;
  event_count: number;
  updated_at: string;
}

export interface OperationsSummary {
  generated_at: string;
  cache_ttl_seconds: number;
  events: {
    total: number;
    anomaly: number;
    unknown: number;
    active_alerts: number;
    latest_ingested_at: string | null;
  };
  workflows: {
    active: number;
    admission_pending: number;
    oldest_active_seconds: number;
    queue: {
      ready: number;
      running: number;
      failed: number;
      oldest_ready_seconds: number;
    };
    workers: { active: number; stale: number; inflight: number };
  };
  p4: {
    unknown_requests: number;
    unknown_deployments: number;
    rollback_unresolved: number;
  };
  operator_batches: { active: number; stopping: number };
  background_jobs: {
    scope: "durable_cluster";
    running: number;
    failing: number;
    items: Array<{
      job_name: string;
      status: "running" | "success" | "failure";
      started_at: string | null;
      finished_at: string | null;
      last_success_at: string | null;
      run_count: number;
      consecutive_failures: number;
      error_code: string | null;
      error_detail: string | null;
      updated_at: string;
    }>;
  };
  database: {
    scope: "process_instance";
    pool_size: number;
    pool_available: number;
    requests_waiting: number;
    acquire_wait_p95_ms: number;
    acquire_timeouts: number;
  };
  maintenance: {
    workflow: boolean;
    p4: boolean;
    background_tasks_enabled: boolean;
  };
}

export type OperatorBatchKind =
  | "resolve_evidence"
  | "start_inspect_workflow"
  | "start_block_workflow";

export interface OperatorBatchCreate {
  kind: OperatorBatchKind;
  event_ids: string[];
  ttl_seconds?: number | null;
  locale: Locale;
}

export interface OperatorBatchItem {
  id: string;
  event_id: string;
  sequence: number;
  status: "pending" | "claimed" | "succeeded" | "failed" | "cancelled" | string;
  attempt_count: number;
  max_attempts: number;
  lease_until: string | null;
  result_type: "workflow" | "directional_evidence" | string | null;
  result_id: string | null;
  error_code: string | null;
  error_detail: string | null;
  updated_at: string;
}

export interface OperatorBatch {
  id: string;
  kind: OperatorBatchKind;
  actor_user_id: string | null;
  actor_username: string;
  actor_role: string;
  status: string;
  state_version: number;
  stop_requested: boolean;
  total_count: number;
  pending_count: number;
  running_count: number;
  succeeded_count: number;
  failed_count: number;
  cancelled_count: number;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  items: OperatorBatchItem[];
}

export interface RuntimeConfig {
  background_tasks_enabled: boolean;
  auto_ingest_seconds: number;
  auto_ingest_enabled: boolean;
  workflow_engine: "v1" | "v2" | string;
  workflow_maintenance: boolean;
  p4_maintenance: boolean;
}

export interface AlertGroup {
  id: string;
  group_key: string;
  status: string;
  severity: string;
  event_count: number;
  created_at: string;
  updated_at: string;
}

export interface EventSourceCreate {
  name: string;
  endpoint: string;
  mode?: "poll" | "sse";
  loopback_only?: boolean;
}

export interface EventSourceResponse {
  id: string;
  name: string;
  endpoint: string;
  mode: "poll" | "sse" | string;
  loopback_only: boolean;
  created_at: string;
}

export interface IngestOnceRequest {
  source_id: string;
  mode?: string | null;
  limit?: number;
}

export interface IngestOnceResponse {
  source_id: string;
  source_run_id: string | null;
  inserted: number;
  duplicate: number;
  conflicts: Record<string, unknown>[];
  last_event_id: number;
  gaps: Record<string, unknown>[];
}

// Workflows
export interface WorkflowRow {
  id: string;
  revision: number;
  status: string;
  template_name: string;
  locale?: Locale | string;
  requested_intent: string | null;
  requested_template_params?: WorkflowTemplateParams;
  intent?: WorkflowIntent | null;
  actor_user_id: string | null;
  source_event_id: string | null;
  source_event_schema_version?: number | null;
  source_event_hash?: string | null;
  event_admission_policy_id?: string | null;
  alert_group_id: string | null;
  review_packet_hash: string | null;
  approved_at: string | null;
  approval_user_id: string | null;
  created_at: string;
  updated_at: string;
  workflow_id?: string;
  engine_version?: number;
  revision_id?: string | null;
  state_version?: number;
  execution_status?: string | null;
  review_status?: string;
  deployment_status?: string;
  desired_state?: string;
  current_review_packet_id?: string | null;
  current_deployment_intent_id?: string | null;
  terminal_at?: string | null;
  materialization_error?: string | null;
  materialization_attempts?: number;
  materialization_next_retry_at?: string | null;
  materialization_dead_lettered_at?: string | null;
  next_actions?: string[];
  next_action?: string;
}

export interface WorkflowListResponse {
  items: WorkflowRow[];
  total: number;
  limit: number;
  offset: number;
}

export interface WorkflowResponse extends WorkflowRow {
  artifacts: Record<string, unknown>[];
  agent_steps: Record<string, unknown>[];
  p4_rule_plans: Record<string, unknown>[];
  command?: Record<string, unknown> | null;
  review_packet?: Record<string, unknown> | null;
  deployment?: Record<string, unknown> | null;
}

export interface WorkflowSummary {
  id: string;
  revision: number;
  status: string;
  template_name: string;
  locale: Locale | string;
  requested_template_params: WorkflowTemplateParams;
  intent: WorkflowIntent | null;
  source_event_id: string | null;
  source_event_schema_version?: number | null;
  source_event_hash?: string | null;
  event_admission_policy_id?: string | null;
  next_action: string;
  key_artifacts: Record<string, unknown>[];
  review_packet: Record<string, unknown> | null;
  approval_request: Record<string, unknown> | null;
  plan: Record<string, unknown> | null;
  risk: Record<string, unknown> | null;
  report: Record<string, unknown> | null;
  deployment: Record<string, unknown> | null;
  workflow_id?: string;
  engine_version?: number;
  revision_id?: string | null;
  state_version?: number;
  execution_status?: string | null;
  review_status?: string;
  deployment_status?: string;
  desired_state?: string;
  materialization_error?: string | null;
  materialization_attempts?: number;
  materialization_next_retry_at?: string | null;
  materialization_dead_lettered_at?: string | null;
  current_deployment_intent_id?: string | null;
  next_actions?: string[];
  nodes?: Record<string, unknown>[];
  review_summary?: Record<string, unknown> | null;
}

export interface WorkflowTemplateParams {
  ttl_seconds?: number | null;
}

export interface WorkflowIntent {
  template_name?: string | null;
  target?: Record<string, unknown>;
  ttl_seconds?: number | string | null;
  ttl_seconds_source?: string | null;
  rationale?: string | null;
  intent_valid?: boolean | null;
  rejection_reason?: string | null;
  block_code?: string | null;
}

export interface WorkflowStartRequest {
  nids_event_id?: string | null;
  alert_group_id?: string | null;
  template_name?: string;
  requested_intent?: string | null;
  template_params?: WorkflowTemplateParams | null;
  deployment_id?: string | null;
  locale?: Locale | null;
}

export interface WorkflowReviewRequest {
  decision: "approve" | "edit" | "reject";
  comment?: string | null;
  edited_intent?: string | null;
  edited_template_params?: WorkflowTemplateParams | null;
  review_packet_id?: string | null;
  review_packet_hash?: string | null;
  artifact_ids?: string[] | null;
  plan_revision?: number | null;
  risk_policy_version?: number | null;
  p4info_hash?: string | null;
  table_schema_hash?: string | null;
  expected_revision?: number | null;
  expected_state_version?: number | null;
}

export interface WorkflowDeployRequest {
  switch_id: string;
  plan_id?: string | null;
  expected_revision?: number | null;
  expected_state_version?: number | null;
  review_packet_id?: string | null;
  review_packet_hash?: string | null;
  plan_hash?: string | null;
}

// P4
export interface P4Switch {
  id: string;
  name: string;
  grpc_addr: string;
  device_id: number;
  p4info_path: string;
  p4info_hash: string | null;
  pipeline_owner: "external" | "digest_controller" | "backend_p4_manager" | "p4_agent";
  read_state: string;
  writes_enabled: boolean;
  runtime_state: "unverified" | "verified" | "changed" | "unavailable";
  runtime_generation_version: number;
  runtime_verified_at?: string | null;
  agent_state: "unregistered" | "connecting" | "primary" | "standby" | "changed" | "unavailable";
  agent_session_version: number;
  agent_verified_at?: string | null;
  runtime_p4info_hash?: string | null;
  p4info_artifact_hash?: string | null;
}

export interface P4SwitchCreate {
  name: string;
  grpc_addr?: string;
  device_id?: number;
  p4info_path: string;
  pipeline_owner?: string;
}

// Signed pipeline bundle / stable target control plane v3
export type PipelineAdapterKind = "bmv2_p4runtime" | "p4_dpdk";
export type PipelineActivationMode =
  | "p4runtime_reconcile_and_commit"
  | "immutable_cold_boot";
export type PipelineGenerationState = "unbound" | "verified" | "changed" | "unavailable";
export type PipelineTransportMode = "plaintext" | "tls" | "mtls";
export type Uint64String = string;

export interface PipelineCompilerV3 {
  name: string;
  version: string;
  oci_image_digest: string;
}

export interface PipelineTableAllowlistsV3 {
  readable: string[];
  observation_write: string[];
  mitigation_write: string[];
}

export interface PipelineBundleVariantV3 {
  bundle_id: string;
  variant_id: string;
  adapter_kind: PipelineAdapterKind;
  architecture: "v1model" | "psa";
  activation_mode: PipelineActivationMode;
  pipeline_cookie: Uint64String;
  p4_source_sha256: string;
  p4info_path: string;
  p4info_sha256: string;
  p4info_size_bytes: number;
  device_config_path: string;
  device_config_sha256: string;
  device_config_size_bytes: number;
  compiler: PipelineCompilerV3;
  capabilities: string[];
  table_allowlists: PipelineTableAllowlistsV3;
}

export interface PipelineBundleV3 {
  bundle_id: string;
  schema_version: 2;
  bundle_name: string;
  bundle_version: string;
  source_revision: string;
  signing_key_id: string;
  manifest: Record<string, unknown>;
  store_relative_path: string;
  status: "available" | "retired";
  bundle_created_at: string;
  imported_by_user_id: string | null;
  imported_at: string;
  variants: PipelineBundleVariantV3[];
}

export interface PipelineTransportSecurityV3 {
  mode: PipelineTransportMode;
  server_name?: string | null;
  ca_bundle_ref?: string | null;
  client_certificate_ref?: string | null;
}

export interface PipelineTargetRegistrationRequestV3 {
  schema_version: 3;
  target_uuid: string;
  connect_uri: string;
  device_id: Uint64String;
  role: string;
  transport_security: PipelineTransportSecurityV3;
}

export interface PipelineTargetV3 {
  target_uuid: string;
  target_identity_sha256: string;
  registration: PipelineTargetRegistrationRequestV3;
  connect_uri: string;
  device_id: Uint64String;
  p4_role: string;
  transport_security_mode: PipelineTransportMode;
  enabled: boolean;
  desired_bundle_id: string | null;
  desired_variant_id: string | null;
  active_bundle_id: string | null;
  active_variant_id: string | null;
  active_pipeline_cookie: Uint64String | null;
  runtime_generation: number;
  generation_state: PipelineGenerationState;
  registration_version: number;
  created_by_user_id: string | null;
  updated_by_user_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface PipelineActivationRequestV3 {
  schema_version: 1;
  bundle_id: string;
  variant_id: string;
}

export type PipelineActivationStatus =
  | "pending"
  | "retry_wait"
  | "executing"
  | "outcome_unknown"
  | "restart_required"
  | "applied"
  | "rejected"
  | "failed"
  | "superseded";

export interface PipelineActivationResponseV3 {
  id: string;
  target_uuid: string;
  bundle_id: string;
  variant_id: string;
  activation_mode: PipelineActivationMode;
  status: PipelineActivationStatus;
  expected_registration_version: number;
  expected_runtime_generation: number;
  idempotency_key: string;
  request_sha256: string;
  requested_by_user_id: string | null;
  observed_pipeline_cookie: Uint64String | null;
  observed_live_p4info_sha256: string | null;
  observed_agent_session_version: number | null;
  observation_ready: boolean | null;
  configured_digest_count: number | null;
  error_code: string | null;
  attempt_count: number;
  next_retry_at: string | null;
  last_attempt_at: string | null;
  created_at: string;
  updated_at: string;
  terminal_at: string | null;
}

export interface P4Table {
  id: number;
  name: string;
  match_fields: P4MatchField[];
  actions: P4Action[];
  size: number;
  schema_hash: string;
}

export interface P4MatchField {
  id: number;
  name: string;
  bitwidth: number;
  match_type: string;
}

export interface P4Action {
  id: number;
  name: string;
  params: P4ActionParam[];
}

export interface P4ActionParam {
  id: number;
  name: string;
  bitwidth: number;
}

export interface P4FormSchema {
  table_name: string;
  schema_hash: string;
  match_fields: P4FormField[];
  actions: P4FormAction[];
}

export interface P4FormField {
  name: string;
  match_type: string;
  bitwidth: number;
  widget: "ipv4" | "mac" | "port" | "uint";
  required: boolean;
}

export interface P4FormAction {
  name: string;
  params: P4ActionParam[];
}

export interface P4Entry {
  table_name: string;
  match_fields: Record<string, unknown>;
  action_name: string;
  action_params: Record<string, unknown>;
  priority: number | null;
  entry_id: string;
  entry_hash: string;
}

export interface P4EntryRequest {
  operation: "insert" | "modify" | "delete";
  match_fields: Record<string, unknown>;
  action_name: string;
  action_params?: Record<string, unknown>;
  priority?: number | null;
  owner_type?: string;
  ttl_seconds?: number | null;
  directional_evidence_id?: string | null;
}

export interface P4ValidationResponse {
  valid: boolean;
  code: string;
  errors: string[];
  normalized_entry: Record<string, unknown>;
  expected_post_state_hash: string | null;
}

export interface P4DeploymentResponse {
  id: string;
  request_id: string;
  status: string;
  operation: string;
  readback_hash: string | null;
  rollback_state: string | null;
  result: P4DeploymentResult;
}

export interface P4DeploymentResult {
  readback_hash?: string | null;
  entry_id?: string | null;
  writer_mode?: "real_p4runtime" | "demo_fake" | string;
  verification_scope?: "write_rpc_and_backend_state" | "backend_state_only" | string;
  [key: string]: unknown;
}

export interface P4Deployment {
  id: string;
  deployment_request_id: string;
  request_type?: string | null;
  request_actor_user_id?: string | null;
  request_actor_role?: string | null;
  owner_type?: "manual" | "workflow" | string;
  original_workflow_id?: string | null;
  original_plan_id?: string | null;
  switch_id: string;
  table_name: string;
  operation: string;
  pre_state_hash: string | null;
  expected_post_state_hash: string | null;
  readback_hash: string | null;
  rollback_state: string | null;
  status: string;
  result_json?: string | null;
  result: P4DeploymentResult;
  created_at: string;
  updated_at: string;
}

export interface P4DeploymentListResponse {
  items: P4Deployment[];
  total: number;
  limit: number;
  offset: number;
}

export interface P4DeploymentRequestResolution {
  request_id: string;
  status: string;
  switch_id: string;
  table_name: string;
  operation: string;
  attempt_count: number;
  max_attempts: number;
  lease_until: string | null;
  rpc_started_at: string | null;
  rpc_finished_at: string | null;
  last_error_at: string | null;
  terminal_at: string | null;
  created_at: string;
  updated_at: string;
  deployment: {
    id: string;
    status: string;
    operation: string;
    readback_hash: string | null;
    rollback_state: string | null;
    verification_scope: string | null;
    created_at: string;
    updated_at: string;
  } | null;
}

export interface P4EntriesResponse {
  state_status: "known" | "unavailable";
  state_source: string;
  verification: string;
  entries: P4Entry[] | null;
  reason?: string;
}

// Audit
export interface AuditLog {
  id: string;
  ts: string;
  request_id: string | null;
  actor_user_id: string | null;
  actor_role: string | null;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  channel: string;
  status: string;
  details: Record<string, unknown>;
}

export interface AuditLogListResponse {
  items: AuditLog[];
  total: number;
  limit: number;
  offset: number;
}

// MCP
export interface MCPToolCall {
  tool_name: string;
  arguments: Record<string, unknown>;
}

export interface MCPToolResponse {
  result: unknown;
}

// Admin
export interface AdminUser {
  id: string;
  username: string;
  role: string;
  enabled: boolean | number;
  created_at: string;
}

export type StructuredOutputMode = "auto" | "function_calling" | "json_schema" | "json_mode" | "json_prompt";

export interface LLMConfig {
  id: string;
  provider: string;
  model: string;
  base_url: string | null;
  api_key_secret_ref: string | null;
  temperature: number;
  max_tokens: number;
  timeout_seconds: number;
  structured_output_mode: StructuredOutputMode;
  effective_structured_output_mode: string;
  structured_output_override: string | null;
  enabled: boolean | number;
  version: number;
}

export interface RiskPolicy {
  id: string;
  version: number;
  auto_apply_enabled: boolean | number;
  min_directional_confidence: number;
  low_risk_ttl_seconds: number;
  active_rule_cap: number;
  max_cleanup_lag_seconds: number;
  updated_at: string;
}

export interface RiskPolicySummary {
  version: number;
  min_directional_confidence: number;
}

export interface ProtectedTarget {
  id: string;
  target_type: "cidr" | "port";
  value: string;
  reason: string | null;
  enabled: boolean | number;
  created_at: string;
}

export interface ProtectedTargetPatch {
  target_type?: "cidr" | "port";
  value?: string;
  reason?: string | null;
  enabled?: boolean;
}

export interface SemanticTemplate {
  id: string;
  name: string;
  description: string | null;
  enabled: boolean | number;
  admin_only: boolean | number;
  created_at: string;
}

export interface SemanticTemplatePatch {
  name?: string;
  description?: string;
  enabled?: boolean;
  admin_only?: boolean;
}

export interface AdminDeleteResult {
  deleted: boolean;
  id: string;
}

export interface DemoTrafficStatus {
  enabled: boolean;
  available: boolean;
  running: boolean;
  runtime_state: "booting" | "ready" | "replaying" | "draining" | "degraded" | "error" | "unavailable";
  default_train_run_id: string;
  default_online_run_id: string;
  active_operation_id: string | null;
  active_online_run_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  online_health: Record<string, unknown> | null;
  detail: string | null;
}

export interface DemoTrafficRunRequest {
  duration_seconds: number;
  profile: "all" | "normal" | "syn_flood" | "udp_flood";
  online_run_id: string;
  train_run_id: string;
}

export interface DemoTrafficCleanupPreviewRequest {
  online_run_id: string;
  reset_backend_state: boolean;
}

export interface DemoTrafficCleanupRequest {
  preview_id: string;
  scope_hash: string;
}

export type DemoOperationStatus =
  | "queued"
  | "claimed"
  | "running"
  | "draining"
  | "succeeded"
  | "failed"
  | "outcome_unknown"
  | "cancelled";

export interface DemoOperationResponse {
  operation_id: string;
  kind: "run" | "stop" | "cleanup" | "recover" | "seal";
  status: DemoOperationStatus;
  phase: string | null;
  online_run_id: string | null;
  attempt_count: number;
  result: Record<string, unknown>;
  error_code: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export interface DemoTrafficCleanupPreview {
  preview_id: string;
  online_run_id: string;
  reset_backend_state: boolean;
  backend_event_schema: "event_v2";
  global_event_count_before: number;
  scope_hash: string;
  expires_at: string;
  scoped_ids: Record<string, string[]>;
  row_counts: Record<string, number>;
  paths: Array<{ path?: string; exists?: boolean; [key: string]: unknown }>;
  active_run_state: Record<string, unknown>;
}

// Directional Evidence
export interface DirectionalEvidence {
  id: string;
  nids_event_id: string | null;
  source_run_id: string;
  source_type: string;
  status: string;
  confidence: number;
  observed_src_ip: string;
  observed_dst_ip: string;
  observed_src_port: number;
  observed_dst_port: number;
  observed_protocol: number;
  selected_direction: string;
  evidence_hash: string;
  summary: Record<string, unknown>;
  created_at: string;
}


/* ── Admin extras (LLM / users / event source / P4 switch) ── */

export interface LLMConfigPatch {
  provider?: string;
  model?: string;
  base_url?: string | null;
  api_key_secret_ref?: string | null;
  temperature?: number;
  max_tokens?: number;
  timeout_seconds?: number;
  structured_output_mode?: StructuredOutputMode;
  enabled?: boolean;
}

export interface LLMTestResult {
  ok: boolean;
  error: string | null;
  redacted_secret_ref: string | null;
  phase: string;
  effective_structured_output_mode: string | null;
  structured_output_override: string | null;
  elapsed_ms: number;
  provider_called: boolean;
}

export interface AdminUserPatch {
  enabled?: boolean;
  role?: "admin" | "analyze";
  expected_enabled: boolean;
  expected_role: "admin" | "analyze";
  change_reason: string;
}

export interface EventSourcePatch {
  name?: string;
  endpoint?: string;
  mode?: "poll" | "sse";
  loopback_only?: boolean;
}

export interface EventSourceDeleteResult {
  deleted: boolean;
  retained_event_count: number;
}

export interface P4SwitchDeleteResult {
  deleted: boolean;
  switch_id: string;
  deleted_entries: number;
  deleted_snapshots: number;
  deleted_table_schemas: number;
}

/* ── MCP tool manifest ── */

export interface MCPToolManifestEntry {
  name: string;
  description: string;
  role_required: "analyze" | "admin";
  side_effect: "read" | "validate" | "workflow_command" | "p4_write";
  input_schema: Record<string, unknown>;
  output_schema_hint: string;
}

export interface MCPToolManifestResponse {
  count: number;
  tools: MCPToolManifestEntry[];
}

/* ── Closed-loop observation ── */

export interface ObservationStartRequest {
  observe_seconds: number;
}

export interface ClosedLoopObservation {
  id: string;
  deployment_id: string;
  workflow_id: string | null;
  plan_match_fields: Record<string, unknown>;
  observe_window_seconds: number;
  started_at: string;
  finished_at: string | null;
  anomaly_count_before: number;
  anomaly_count_after: number | null;
  status: "running" | "completed" | string;
  notes: string[];
}

/* ── TTL cleanup ── */

export interface TTLCleanupCandidate {
  entry_id: string;
  table_name: string;
  switch_id: string;
  ttl_expires_at: string;
  deployment_id: string;
}

export interface TTLCleanupRunResponse {
  job_id: string;
  dry_run: boolean;
  expired: number;
  candidates: TTLCleanupCandidate[];
  rolled_back: Array<TTLCleanupCandidate & { result: Record<string, unknown> }>;
  skipped: Array<TTLCleanupCandidate & { reason: string }>;
  failures: Array<TTLCleanupCandidate & { code: string; detail: unknown }>;
  deferred: TTLCleanupCandidate[];
}

/* ── Raw digest index cleanup ── */

export interface RawDigestIndexCleanupRunCandidate {
  source_run_id: string;
  online_run_id: string;
  run_instance_id: string | null;
  raw_digest_path: string | null;
  created_at: string | null;
  sample_count: number;
  delete_sample_count: number;
  evidence_linked_sample_count: number;
}

export interface RawDigestIndexCleanupRunResponse {
  dry_run: boolean;
  current_run_ids: string[];
  pruned_run_ids: string[];
  candidate_sample_count: number;
  candidate_index_state_count: number;
  deleted_sample_count: number;
  retained_current_sample_count: number;
  retained_evidence_linked_sample_count: number;
  deleted_index_state_count: number;
  estimated_freed_pages: number;
  estimated_freed_bytes: number;
  page_size: number;
  dbstat_available: boolean;
  cache_total_sample_count: number;
  cache_candidate_sample_count: number;
  cache_deleted_sample_count: number;
  cache_retained_current_sample_count: number;
  cache_total_index_state_count: number;
  cache_candidate_index_state_count: number;
  cache_deleted_index_state_count: number;
  runs: RawDigestIndexCleanupRunCandidate[];
}

/* ── Workflow templates (frontend create form) ── */

export interface WorkflowTemplateOption {
  name: string;
  description: string;
  admin_only: boolean;
  supports_ttl_seconds: boolean;
}

export interface TemplatesListMeta {
  llm_overlay_enabled: boolean;
}

export interface TemplatesListResponse {
  items: WorkflowTemplateOption[];
  meta: TemplatesListMeta;
}

/* ── Notices (dashboard announcement banners) ── */

export interface NoticeItem {
  id: string;
  category: "server_issues" | "operation_guide";
  content: string;
  content_hash: string;
  locale: string;
}

export interface NoticesResponse {
  items: NoticeItem[];
}

/* ── Phase 6: Dashboard live samples (DashboardLiveSampleV1) ── */

export interface DashboardLiveSampleV1 {
  sample_id: string;
  sample_content_sha256: string;
  source_run_id: string;
  target_uuid: string;
  runtime_generation: number;
  agent_session_version: number;
  model_role: string;
  model_release_id: string;
  bucket_start_at: string;
  bucket_end_at: string;
  packets_per_second: number | null;
  bytes_per_second: number | null;
  packet_count: number;
  byte_count: number;
  protocol: number | null;
  latest_decision: string;
  normal_count: number;
  anomaly_count: number;
  unknown_count: number;
  min_completeness: number | null;
  quality_degraded: boolean;
  quality_reason: string | null;
}

export interface LiveFreshness {
  status: "known" | "stale" | "unknown";
  age_seconds: number | null;
  stale_after_seconds: number;
  reason_code: string | null;
}

export interface LiveEventMarker {
  event_id: string;
  event_kind: string;
  observed_at: string;
}

export interface LiveCurrentIncident {
  incident_id: string;
  status: string;
  first_seen_at: string;
}

export interface LiveV3Response {
  schema: string;
  generated_at: string;
  target_uuid: string;
  runtime_generation: number;
  model_role: string;
  model_release_id: string;
  window: string;
  step: string;
  watermark_at: string;
  latest_sample_at: string | null;
  freshness: LiveFreshness;
  samples: DashboardLiveSampleV1[];
  event_markers: LiveEventMarker[];
  current_incident: LiveCurrentIncident | null;
}

/* ── Phase 6: Agent analysis trace (AgentAnalysisTraceV1) ── */

export interface TraceTopologyNode {
  node_id: string;
  internal_name: string;
  display_key: string;
  kind: "deterministic" | "llm" | "mcp_round" | "validator" | "guard" | "fallback";
  parallel_group_id: string | null;
  fanout_index: number | null;
  stable_ordinal: number;
}

export interface TraceTopologyEdge {
  from_node_id: string;
  to_node_id: string;
  stable_ordinal: number;
  condition_key: string | null;
}

export interface TraceTopology {
  nodes: TraceTopologyNode[];
  edges: TraceTopologyEdge[];
}

export interface TraceEvent {
  sequence_no: number;
  event_id: string;
  occurred_at: string;
  node_id: string;
  parent_node_ids: string[];
  parallel_group_id: string | null;
  event_kind: string;
  phase: string;
  call_id: string | null;
  call_index: number | null;
  callee_key: string | null;
  elapsed_ms: number | null;
  outcome: string;
  error_code: string | null;
  request_sha256: string | null;
  response_sha256: string | null;
  message_code: string;
  bounded_redacted_args: Record<string, unknown>;
  redaction_profile_version: number;
  trace_id: string;
}

export interface AnalysisTraceV1Response {
  schema_version: number;
  workflow_id: string;
  revision_id: string;
  node_run_id: string;
  node_attempt_id: string | null;
  attempt: number;
  analysis_run_id: string | null;
  trace_id: string | null;
  graph_version: string;
  graph_topology_sha256: string;
  evidence_bundle_sha256: string;
  provider_snapshot_sha256: string | null;
  prompt_bundle_sha256: string | null;
  tool_policy_sha256: string;
  trace_state: "not_started" | "active" | "terminal" | "expired" | "unavailable";
  outer_attempt_status: string;
  started_at: string | null;
  completed_at: string | null;
  topology: TraceTopology;
  first_sequence_no: number | null;
  last_sequence_no: number | null;
  terminal_sequence_no: number | null;
  events: TraceEvent[];
  next_after_sequence_no: number | null;
  trace_complete: boolean;
  terminal_trace_sha256: string | null;
  retention_expires_at: string;
  generated_at: string;
}

/* ── Phase 6: FlowEvidenceV3 summary (read-only list) ── */

export interface FlowEvidenceV3Summary {
  flow_evidence_id: string;
  evidence_sha256: string;
  status: string;
  event_id: string;
  incident_id: string | null;
  target_uuid: string;
  switch_id: string;
  runtime_generation: number;
  agent_session_version: number;
  observed_src_ip: string;
  observed_dst_ip: string;
  observed_src_port: number;
  observed_dst_port: number;
  observed_protocol: number;
  selected_direction: string;
  sample_count: number;
  confidence: number;
  capture_window_start: string;
  capture_window_end: string;
  first_observed_at: string;
  last_observed_at: string;
}

export interface FlowEvidenceV3ListResponse {
  schema: string;
  event_id: string;
  items: FlowEvidenceV3Summary[];
  count: number;
}

export interface FlowCaptureRequest {
  event_id: string;
  target_uuid: string;
  switch_id: string;
  duration_ms: number;
  sample_cap: number;
}

export interface FlowCaptureResponse {
  operation_id: string;
  status: "queued" | "running" | "applied" | "failed" | "unknown";
  request_id: string;
  event_id: string;
  target_uuid: string;
  switch_id: string;
  source_run_id: string | null;
  duration_ms: number;
  sample_cap: number;
  capture_tag: string | null;
  created_at: string;
  updated_at: string;
  error_code?: string | null;
}

/* ── Phase 6: Demo run projection ── */

export interface DemoRunProjectionResponse {
  schema: string;
  operation_id: string;
  projection: Record<string, unknown>;
}
