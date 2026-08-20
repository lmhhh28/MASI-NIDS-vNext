-- Go Control Core mandatory public-boundary completion (schema v19).
-- Requirement IDs: FUNC-EVIDENCE-001, FUNC-EFFECT-001, CONTRACT-AGENT-001,
-- AGENT-002, AGENT-005, AGENT-006, AGENT-007, DB-GOV-001, OBS-001.

BEGIN;

-- Immutable bounded capture requests are policy facts. Actual execution stays
-- on effect_intents; this table intentionally has no claim/lease columns.
CREATE TABLE bounded_capture_requests (
    capture_id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL REFERENCES targets(target_id),
    scope TEXT NOT NULL,
    capture_digest TEXT NOT NULL UNIQUE
        CHECK(capture_digest ~ '^sha256:[0-9a-f]{64}$'),
    capture_adapter_id TEXT NOT NULL
        CHECK(capture_adapter_id='bounded-flow-evidence/v1'),
    duration_ms INTEGER NOT NULL CHECK(duration_ms BETWEEN 1 AND 300000),
    sample_limit INTEGER NOT NULL CHECK(sample_limit BETWEEN 1 AND 10000),
    byte_limit BIGINT NOT NULL CHECK(byte_limit BETWEEN 1 AND 16777216),
    expires_at_unix_ms BIGINT NOT NULL CHECK(expires_at_unix_ms >= 1),
    max_concurrent_on_target INTEGER NOT NULL CHECK(max_concurrent_on_target BETWEEN 1 AND 4),
    payload_mode TEXT NOT NULL CHECK(payload_mode='flow-metadata-only'),
    filter JSONB NOT NULL CHECK(jsonb_typeof(filter)='object'),
    spec JSONB NOT NULL CHECK(jsonb_typeof(spec)='object'),
    state TEXT NOT NULL CHECK(state IN
        ('planned','authorized','claimed','executing','applied','hold','unknown','expired')),
    effect_intent_id TEXT UNIQUE REFERENCES effect_intents(effect_intent_id)
        DEFERRABLE INITIALLY DEFERRED,
    operation_id TEXT UNIQUE,
    actor_ref TEXT NOT NULL,
    actor_issuer TEXT NOT NULL,
    actor_subject TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    created_at_unix_ms BIGINT NOT NULL CHECK(created_at_unix_ms >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT bounded_capture_intent_state_v19 CHECK(
      (state='planned' AND effect_intent_id IS NULL AND operation_id IS NULL) OR
      (state<>'planned' AND effect_intent_id IS NOT NULL AND operation_id IS NOT NULL)
    )
);
CREATE INDEX bounded_capture_requests_target_state_idx
    ON bounded_capture_requests(target_id,state,expires_at_unix_ms);

-- Applied capture metadata is append-only and binds the assignment/application
-- generation, exact capture window, session, content and observed-flow digests.
-- Raw packet bytes are never stored here.
CREATE TABLE bounded_capture_results (
    capture_id TEXT PRIMARY KEY REFERENCES bounded_capture_requests(capture_id),
    effect_intent_id TEXT NOT NULL UNIQUE REFERENCES effect_intents(effect_intent_id),
    operation_id TEXT NOT NULL UNIQUE,
    target_id TEXT NOT NULL REFERENCES targets(target_id),
    target_control_incarnation_id TEXT NOT NULL,
    target_assignment_generation BIGINT NOT NULL CHECK(target_assignment_generation >= 1),
    application_generation BIGINT NOT NULL CHECK(application_generation >= 1),
    actor_runtime_epoch TEXT NOT NULL,
    capture_digest TEXT NOT NULL CHECK(capture_digest ~ '^sha256:[0-9a-f]{64}$'),
    capture_session_id TEXT NOT NULL,
    started_at_unix_ms BIGINT NOT NULL CHECK(started_at_unix_ms >= 1),
    finished_at_unix_ms BIGINT NOT NULL CHECK(finished_at_unix_ms >= started_at_unix_ms),
    observed_samples INTEGER NOT NULL CHECK(observed_samples BETWEEN 0 AND 10000),
    observed_bytes BIGINT NOT NULL CHECK(observed_bytes BETWEEN 0 AND 16777216),
    content_digest TEXT NOT NULL CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
    observed_flow_digest TEXT NOT NULL CHECK(observed_flow_digest ~ '^sha256:[0-9a-f]{64}$'),
    capture_window_digest TEXT NOT NULL CHECK(capture_window_digest ~ '^sha256:[0-9a-f]{64}$'),
    readback_digest TEXT NOT NULL CHECK(readback_digest ~ '^sha256:[0-9a-f]{64}$'),
    result_digest TEXT NOT NULL CHECK(result_digest ~ '^sha256:[0-9a-f]{64}$'),
    truncated BOOLEAN NOT NULL,
    gap BOOLEAN NOT NULL,
    evidence_id TEXT NOT NULL UNIQUE,
    trace_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE evidence_refs DROP CONSTRAINT evidence_refs_kind_check;
ALTER TABLE evidence_refs ADD CONSTRAINT evidence_refs_kind_v19 CHECK
    (kind IN ('plugin-artifact','agent-artifact','observation','audit','bounded-capture'));

-- MCP audit is append-only and records identity/allowlist/outcome metadata only,
-- never request bodies, tool results, credentials, cookies, or packet content.
CREATE TABLE mcp_access_audit (
    audit_id TEXT PRIMARY KEY,
    plugin_id TEXT NOT NULL,
    binding_generation INTEGER NOT NULL CHECK(binding_generation >= 1),
    manifest_digest TEXT NOT NULL CHECK(manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
    scope TEXT NOT NULL,
    protocol_version TEXT NOT NULL CHECK(protocol_version='2025-11-25'),
    method TEXT NOT NULL,
    capability_id TEXT,
    request_digest TEXT NOT NULL CHECK(request_digest ~ '^sha256:[0-9a-f]{64}$'),
    response_digest TEXT CHECK(response_digest IS NULL OR response_digest ~ '^sha256:[0-9a-f]{64}$'),
    outcome TEXT NOT NULL CHECK(outcome IN ('allowed','denied','failed')),
    reason_code TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    created_at_unix_ms BIGINT NOT NULL CHECK(created_at_unix_ms >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY(plugin_id,binding_generation)
      REFERENCES plugin_bindings(plugin_id,binding_generation)
);

-- Go-owned outbound A2A request/projection. The Analysis service remains the
-- task executor and never receives core PostgreSQL credentials.
CREATE TABLE analysis_task_requests (
    task_id TEXT PRIMARY KEY,
    plugin_id TEXT NOT NULL,
    binding_generation INTEGER NOT NULL CHECK(binding_generation >= 1),
    manifest_digest TEXT NOT NULL CHECK(manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
    scope TEXT NOT NULL,
    input_digest TEXT NOT NULL CHECK(input_digest ~ '^sha256:[0-9a-f]{64}$'),
    request_digest TEXT NOT NULL CHECK(request_digest ~ '^sha256:[0-9a-f]{64}$'),
    peer_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('submitted','working','succeeded','limited','insufficient_evidence','failed','fenced')),
    poll_count INTEGER NOT NULL DEFAULT 0 CHECK(poll_count BETWEEN 0 AND 3),
    deadline_unix_ms BIGINT NOT NULL CHECK(deadline_unix_ms >= 1),
    last_response_digest TEXT CHECK(last_response_digest IS NULL OR last_response_digest ~ '^sha256:[0-9a-f]{64}$'),
    actor_ref TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    created_at_unix_ms BIGINT NOT NULL CHECK(created_at_unix_ms >= 1),
    updated_at_unix_ms BIGINT NOT NULL CHECK(updated_at_unix_ms >= created_at_unix_ms),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY(plugin_id,binding_generation)
      REFERENCES plugin_bindings(plugin_id,binding_generation)
);
CREATE INDEX analysis_task_requests_scope_status_idx
    ON analysis_task_requests(scope,status,task_id);

CREATE TABLE analysis_artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES analysis_task_requests(task_id),
    plugin_id TEXT NOT NULL,
    binding_generation INTEGER NOT NULL CHECK(binding_generation >= 1),
    scope TEXT NOT NULL,
    artifact_digest TEXT NOT NULL UNIQUE CHECK(artifact_digest ~ '^sha256:[0-9a-f]{64}$'),
    media_type TEXT NOT NULL CHECK(media_type IN ('application/json','text/markdown')),
    body JSONB NOT NULL CHECK(jsonb_typeof(body)='object'),
    body_bytes INTEGER NOT NULL CHECK(body_bytes BETWEEN 2 AND 65536),
    non_executable BOOLEAN NOT NULL CHECK(non_executable),
    deployment_eligible BOOLEAN NOT NULL CHECK(NOT deployment_eligible),
    trace_id TEXT NOT NULL,
    created_at_unix_ms BIGINT NOT NULL CHECK(created_at_unix_ms >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY(plugin_id,binding_generation)
      REFERENCES plugin_bindings(plugin_id,binding_generation)
);

REVOKE ALL ON bounded_capture_requests,bounded_capture_results,mcp_access_audit,
    analysis_task_requests,analysis_artifacts FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE ON bounded_capture_requests,analysis_task_requests TO masi_control_app;
GRANT SELECT,INSERT ON bounded_capture_results,mcp_access_audit,analysis_artifacts TO masi_control_app;
GRANT SELECT ON bounded_capture_requests,bounded_capture_results,mcp_access_audit,
    analysis_task_requests,analysis_artifacts TO masi_control_readonly;

UPDATE masi_schema_meta
SET value='19',applied_at=now(),checksum='computed-by-migration-runner',
    source_digest='db/migrations/0026_control_public_boundaries.sql'
WHERE key='version';

COMMIT;
