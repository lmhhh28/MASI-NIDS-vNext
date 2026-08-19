-- MASI-NIDS-vNext Go Control hardening expansion (schema v2).
--
-- This migration is append-only. It closes the authorization, claim/fence,
-- projection-scope, model-incarnation, and plugin-statistics gaps discovered by
-- the MOD-CTRL-001 independent-module review. Existing v1 migrations remain
-- immutable.

BEGIN;

-- Persist the complete OIDC subject. actor_ref is deliberately opaque and is
-- retained only as a stable display/audit key; it is never parsed back into an
-- identity.
ALTER TABLE effect_proposals
    ADD COLUMN IF NOT EXISTS actor_issuer TEXT NOT NULL DEFAULT 'legacy:unknown',
    ADD COLUMN IF NOT EXISTS actor_subject TEXT NOT NULL DEFAULT 'legacy:unknown',
    ADD COLUMN IF NOT EXISTS actor_level TEXT NOT NULL DEFAULT 'analyst',
    ADD COLUMN IF NOT EXISTS target_ids TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE effect_proposals ADD CONSTRAINT effect_proposals_actor_level_v2
    CHECK (actor_level IN ('analyst','operator','scoped-operator','platform-admin'));
CREATE UNIQUE INDEX IF NOT EXISTS effect_proposals_actor_idempotency_uidx
    ON effect_proposals(actor_ref,idempotency_key);

ALTER TABLE effect_decisions
    ADD COLUMN IF NOT EXISTS actor_issuer TEXT NOT NULL DEFAULT 'legacy:unknown',
    ADD COLUMN IF NOT EXISTS actor_subject TEXT NOT NULL DEFAULT 'legacy:unknown';

ALTER TABLE effect_intents
    ADD COLUMN IF NOT EXISTS actor_issuer TEXT NOT NULL DEFAULT 'legacy:unknown',
    ADD COLUMN IF NOT EXISTS actor_subject TEXT NOT NULL DEFAULT 'legacy:unknown',
    ADD COLUMN IF NOT EXISTS gate_open BOOLEAN NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS not_before_unix_ms BIGINT NOT NULL DEFAULT 0;
ALTER TABLE effect_intents DROP CONSTRAINT IF EXISTS effect_intents_claim_state_check;
ALTER TABLE effect_intents ADD CONSTRAINT effect_intents_claim_state_v2 CHECK (
    claim_state IS NULL OR claim_state IN
        ('unclaimed','claimed','fenced','executing','unknown','hold','blocked','finalized')
);
CREATE INDEX IF NOT EXISTS effect_intents_reclaim_idx
    ON effect_intents (claim_expires_at_unix_ms)
    WHERE claim_state = 'claimed' AND NOT is_fleet_parent;
CREATE UNIQUE INDEX IF NOT EXISTS effect_intents_operation_uidx
    ON effect_intents(operation_id) WHERE NOT is_fleet_parent;

-- Every browser projection has a first-class authorization scope.
ALTER TABLE events
    ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'global',
    ADD COLUMN IF NOT EXISTS model_revision_digest TEXT NOT NULL DEFAULT 'sha256:unknown',
    ADD COLUMN IF NOT EXISTS model_bundle_digest TEXT NOT NULL DEFAULT 'sha256:unknown',
    ADD COLUMN IF NOT EXISTS feature_contract_digest TEXT NOT NULL DEFAULT 'sha256:unknown',
    ADD COLUMN IF NOT EXISTS label_contract_digest TEXT NOT NULL DEFAULT 'sha256:unknown',
    ADD COLUMN IF NOT EXISTS output_adapter_digest TEXT NOT NULL DEFAULT 'sha256:unknown',
    ADD COLUMN IF NOT EXISTS wire_contract_digest TEXT NOT NULL DEFAULT 'sha256:unknown',
    ADD COLUMN IF NOT EXISTS runtime_profile_digest TEXT NOT NULL DEFAULT 'sha256:unknown';
ALTER TABLE events
    ADD COLUMN IF NOT EXISTS startup_envelope_digest TEXT NOT NULL DEFAULT 'sha256:unknown',
    ADD COLUMN IF NOT EXISTS pool_observation_digest TEXT NOT NULL DEFAULT 'sha256:unknown',
    ADD COLUMN IF NOT EXISTS binding_digest TEXT NOT NULL DEFAULT 'sha256:unknown',
    ADD COLUMN IF NOT EXISTS optimization_profile_digest TEXT NOT NULL DEFAULT 'sha256:unknown';
ALTER TABLE evidence_refs ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'global';
CREATE INDEX IF NOT EXISTS events_scope_time_idx ON events (scope, event_time DESC);
CREATE INDEX IF NOT EXISTS evidence_refs_scope_time_idx ON evidence_refs (scope, created_at DESC);

-- Claim-time target and assignment fence facts. Only exact, current, unexpired
-- observations can authorize an external effect call.
ALTER TABLE target_assignments
    ADD COLUMN IF NOT EXISTS actor_issuer TEXT NOT NULL DEFAULT 'legacy:unknown',
    ADD COLUMN IF NOT EXISTS actor_subject TEXT NOT NULL DEFAULT 'legacy:unknown',
    ADD COLUMN IF NOT EXISTS revoked_at_unix_ms BIGINT;
CREATE TABLE IF NOT EXISTS target_capability_observations (
    target_id TEXT PRIMARY KEY REFERENCES targets(target_id),
    target_control_incarnation_id TEXT NOT NULL,
    assignment_generation BIGINT NOT NULL CHECK (assignment_generation >= 1),
    application_generation BIGINT NOT NULL CHECK (application_generation >= 1),
    p4info_digest TEXT NOT NULL,
    pipeline_digest TEXT NOT NULL,
    capacity_digest TEXT NOT NULL,
    capacity_available BOOLEAN NOT NULL,
    observed_at_unix_ms BIGINT NOT NULL,
    expires_at_unix_ms BIGINT NOT NULL,
    CHECK (expires_at_unix_ms > observed_at_unix_ms)
);

ALTER TABLE fleet_child_intents
    ADD COLUMN IF NOT EXISTS gate_open BOOLEAN NOT NULL DEFAULT false;
CREATE UNIQUE INDEX IF NOT EXISTS fleet_child_intents_child_uidx
    ON fleet_child_intents (child_intent_id);
ALTER TABLE fleet_child_intents DROP CONSTRAINT IF EXISTS fleet_child_intents_child_fk;
ALTER TABLE fleet_child_intents ADD CONSTRAINT fleet_child_intents_child_fk
    FOREIGN KEY (child_intent_id) REFERENCES effect_intents(effect_intent_id);

-- Overlay expiry creates one durable delete intent. The unique partial index
-- prevents every sweep from creating another operation for the same rule.
ALTER TABLE firewall_overlays
    ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'global',
    ADD COLUMN IF NOT EXISTS delete_effect_digest TEXT,
    ADD COLUMN IF NOT EXISTS delete_decision_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS firewall_overlay_delete_intent_uidx
    ON firewall_overlays (delete_intent_id) WHERE delete_intent_id IS NOT NULL;
ALTER TABLE firewall_activations DROP CONSTRAINT IF EXISTS activation_applied_exact;
ALTER TABLE firewall_activations ADD CONSTRAINT activation_applied_exact_v2 CHECK (
    result <> 'applied' OR
    (default_readback='exact' AND selector_readback='new-bank' AND mismatched_entries=0
     AND current_stage IN ('current_committed','previous_retained','cleanup'))
);

-- The active model-control incarnation is a singleton writer gate. A restored
-- database must rotate it before writers or ingest are admitted.
CREATE TABLE IF NOT EXISTS model_control_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    active_incarnation_id TEXT REFERENCES model_control_incarnations(incarnation_id),
    writer_enabled BOOLEAN NOT NULL DEFAULT false,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO model_control_state (singleton, writer_enabled)
VALUES (true, false) ON CONFLICT (singleton) DO NOTHING;
ALTER TABLE model_revisions ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'global';
ALTER TABLE shard_bindings ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'global';
ALTER TABLE pool_generations
    ADD COLUMN IF NOT EXISTS model_revision_digest TEXT NOT NULL DEFAULT 'sha256:legacy',
    ADD COLUMN IF NOT EXISTS model_bundle_digest TEXT NOT NULL DEFAULT 'sha256:legacy',
    ADD COLUMN IF NOT EXISTS feature_contract_digest TEXT NOT NULL DEFAULT 'sha256:legacy',
    ADD COLUMN IF NOT EXISTS label_contract_digest TEXT NOT NULL DEFAULT 'sha256:legacy',
    ADD COLUMN IF NOT EXISTS output_adapter_digest TEXT NOT NULL DEFAULT 'sha256:legacy',
    ADD COLUMN IF NOT EXISTS wire_profile_digest TEXT NOT NULL DEFAULT 'sha256:legacy',
    ADD COLUMN IF NOT EXISTS runtime_profile_digest TEXT NOT NULL DEFAULT 'sha256:legacy',
    ADD COLUMN IF NOT EXISTS optimization_profile_digest TEXT NOT NULL DEFAULT 'sha256:legacy';
ALTER TABLE model_rollout_operations
    ADD COLUMN IF NOT EXISTS request_digest TEXT NOT NULL DEFAULT 'sha256:legacy',
    ADD COLUMN IF NOT EXISTS model_control_incarnation_id TEXT,
    ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'global';
CREATE UNIQUE INDEX IF NOT EXISTS model_rollout_request_uidx
    ON model_rollout_operations (operation_id, request_digest);

-- Plugin catalog/binding scope and append-only audit facts.
ALTER TABLE plugin_manifests ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'global';
ALTER TABLE plugin_bindings ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'global';
CREATE TABLE IF NOT EXISTS plugin_audit_events (
    audit_id TEXT PRIMARY KEY,
    plugin_id TEXT NOT NULL,
    binding_generation BIGINT,
    action TEXT NOT NULL,
    scope TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    actor_issuer TEXT NOT NULL,
    actor_subject TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Qualified statistics definitions are immutable Go-owned projections of the
-- exact manifest definition and its host-owned input allowlist.
CREATE TABLE IF NOT EXISTS plugin_statistics_definitions (
    definition_id TEXT PRIMARY KEY,
    definition_digest TEXT NOT NULL UNIQUE,
    definition JSONB NOT NULL,
    plugin_id TEXT NOT NULL,
    plugin_revision TEXT NOT NULL,
    manifest_id TEXT NOT NULL,
    manifest_revision INTEGER NOT NULL,
    binding_generation BIGINT NOT NULL CHECK (binding_generation >= 1),
    producer_kind TEXT NOT NULL CHECK (producer_kind IN ('pure-transform','read-only-tool')),
    host_projection_refs TEXT[] NOT NULL,
    external_capability_ids TEXT[] NOT NULL DEFAULT '{}',
    display_hint TEXT NOT NULL CHECK (display_hint IN
      ('metric-card','status','timeseries','bar','heatmap','table','text','evidence-list')),
    scope TEXT NOT NULL,
    data_class TEXT NOT NULL,
    deadline_ms INTEGER NOT NULL CHECK (deadline_ms BETWEEN 1 AND 10000),
    revoked BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS plugin_statistic_input_bundles (
    run_id TEXT PRIMARY KEY REFERENCES plugin_statistic_runs(run_id),
    definition_id TEXT NOT NULL,
    definition_digest TEXT NOT NULL,
    frozen_input_digest TEXT NOT NULL,
    bundle JSONB NOT NULL,
    bytes INTEGER NOT NULL CHECK (bytes BETWEEN 1 AND 2097152),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE plugin_statistic_schedules
    ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'global',
    ADD COLUMN IF NOT EXISTS data_class TEXT NOT NULL DEFAULT 'internal',
    ADD COLUMN IF NOT EXISTS actor_issuer TEXT NOT NULL DEFAULT 'legacy:unknown',
    ADD COLUMN IF NOT EXISTS actor_subject TEXT NOT NULL DEFAULT 'legacy:unknown',
    ADD COLUMN IF NOT EXISTS target_set_digest TEXT NOT NULL DEFAULT 'sha256:legacy';
ALTER TABLE plugin_statistic_schedules DROP CONSTRAINT IF EXISTS plugin_statistic_schedules_pkey;
ALTER TABLE plugin_statistic_schedules ADD CONSTRAINT plugin_statistic_schedules_pkey_v2
    PRIMARY KEY(schedule_id,schedule_revision);
ALTER TABLE plugin_statistic_runs
    ADD COLUMN IF NOT EXISTS definition_digest TEXT NOT NULL DEFAULT 'sha256:legacy',
    ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'global',
    ADD COLUMN IF NOT EXISTS data_class TEXT NOT NULL DEFAULT 'internal',
    ADD COLUMN IF NOT EXISTS claim_lease_id TEXT,
    ADD COLUMN IF NOT EXISTS claim_generation BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS result_fence TEXT,
    ADD COLUMN IF NOT EXISTS actor_issuer TEXT NOT NULL DEFAULT 'legacy:unknown',
    ADD COLUMN IF NOT EXISTS actor_subject TEXT NOT NULL DEFAULT 'legacy:unknown';
ALTER TABLE plugin_statistic_runs ADD COLUMN IF NOT EXISTS request_digest TEXT NOT NULL DEFAULT 'sha256:legacy';
ALTER TABLE plugin_statistic_runs ADD COLUMN IF NOT EXISTS target_set_digest TEXT NOT NULL DEFAULT 'sha256:legacy';
ALTER TABLE plugin_statistic_runs DROP CONSTRAINT IF EXISTS plugin_statistic_runs_status_check;
ALTER TABLE plugin_statistic_runs ADD CONSTRAINT plugin_statistic_runs_status_v2 CHECK
    (status IN ('pending','running','succeeded','failed','cancelled','expired','fenced'));
ALTER TABLE plugin_statistic_runs DROP CONSTRAINT IF EXISTS plugin_statistic_runs_idempotency_key_run_digest_key;
CREATE UNIQUE INDEX IF NOT EXISTS plugin_statistic_runs_idempotency_uidx
    ON plugin_statistic_runs (idempotency_key);
ALTER TABLE plugin_statistic_artifacts
    ADD COLUMN IF NOT EXISTS binding_generation BIGINT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS result_fence TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE plugin_statistic_artifacts DROP CONSTRAINT IF EXISTS plugin_statistic_artifacts_bytes_check;
ALTER TABLE plugin_statistic_artifacts ADD CONSTRAINT plugin_statistic_artifacts_bytes_v2
    CHECK (bytes >= 1 AND bytes <= 1048576);
ALTER TABLE plugin_statistics_current ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'global';
ALTER TABLE plugin_statistics_history ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'global';

-- P4 counters are unsigned 64-bit. PostgreSQL BIGINT cannot represent their
-- complete range, so canonical counters and deltas use NUMERIC(20,0).
ALTER TABLE rule_observations
    ALTER COLUMN packets TYPE NUMERIC(20,0),
    ALTER COLUMN bytes TYPE NUMERIC(20,0),
    ALTER COLUMN eligible_packets TYPE NUMERIC(20,0),
    ALTER COLUMN direct_delta_packets TYPE NUMERIC(20,0),
    ALTER COLUMN direct_delta_bytes TYPE NUMERIC(20,0),
    ALTER COLUMN eligible_delta_packets TYPE NUMERIC(20,0);
ALTER TABLE rule_rollups_5m
    ALTER COLUMN direct_packets TYPE NUMERIC(20,0),
    ALTER COLUMN direct_bytes TYPE NUMERIC(20,0),
    ALTER COLUMN eligible_packets TYPE NUMERIC(20,0);
ALTER TABLE rule_rollups_1h
    ALTER COLUMN direct_packets TYPE NUMERIC(20,0),
    ALTER COLUMN direct_bytes TYPE NUMERIC(20,0),
    ALTER COLUMN eligible_packets TYPE NUMERIC(20,0);
CREATE INDEX IF NOT EXISTS rule_epoch_target_identity_idx
    ON rule_observation_epochs (target_id, entity_id, rule_id, observation_epoch, reset_epoch);
ALTER TABLE rule_observation_epochs
    DROP CONSTRAINT IF EXISTS rule_observation_epochs_entity_id_rule_id_observation_epoch_reset_epoch_key;
ALTER TABLE rule_observation_epochs ADD CONSTRAINT rule_observation_epochs_target_identity_v2
    UNIQUE (target_id, entity_id, rule_id, observation_epoch, reset_epoch);

UPDATE masi_schema_meta
SET value = '2', applied_at = now(),
    checksum = 'computed-by-migration-runner',
    source_digest = 'db/migrations/0009_control_hardening.sql'
WHERE key = 'version';

COMMIT;
