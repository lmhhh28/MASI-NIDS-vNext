-- MASI-NIDS-vNext ordered multi-shard model rollout vector and recovery identity (v12).
-- Requirement IDs: FUNC-INF-MODEL-001, DB-MODEL-001, TEST-INF-001.

BEGIN;

ALTER TABLE model_rollout_operations
    ADD COLUMN IF NOT EXISTS shard_id TEXT,
    ADD COLUMN IF NOT EXISTS target_revision_id TEXT,
    ADD COLUMN IF NOT EXISTS edge_workload_ref TEXT;
CREATE INDEX IF NOT EXISTS model_rollout_operations_shard_status_idx
    ON model_rollout_operations(shard_id,status,started_at_unix_ms);

CREATE TABLE IF NOT EXISTS model_rollout_groups (
    group_id TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL CHECK (request_digest ~ '^sha256:[0-9a-f]{64}$'),
    logical_pool_id TEXT NOT NULL REFERENCES logical_pools(logical_pool_id),
    target_generation BIGINT NOT NULL CHECK (target_generation >= 1),
    target_revision_id TEXT NOT NULL REFERENCES model_revisions(model_revision_id),
    ordered_shards JSONB NOT NULL CHECK (jsonb_typeof(ordered_shards)='array'),
    rollout_template JSONB NOT NULL CHECK (jsonb_typeof(rollout_template)='object'),
    status TEXT NOT NULL CHECK (status IN ('planned','rolling','rolling_mixed','completed','failed','failed_mixed')),
    next_index INTEGER NOT NULL DEFAULT 0 CHECK (next_index >= 0),
    model_control_incarnation_id TEXT NOT NULL REFERENCES model_control_incarnations(incarnation_id),
    scope TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    created_at_unix_ms BIGINT NOT NULL CHECK (created_at_unix_ms >= 1),
    finished_at_unix_ms BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS model_rollout_group_shards (
    group_id TEXT NOT NULL REFERENCES model_rollout_groups(group_id),
    shard_index INTEGER NOT NULL CHECK (shard_index >= 0),
    shard_id TEXT NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('planned','running','applied','resume_pending','failed','blocked')),
    previous_generation BIGINT,
    current_generation BIGINT,
    route_epoch BIGINT,
    reason_code TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(group_id,shard_id),
    UNIQUE(group_id,shard_index)
);
CREATE INDEX IF NOT EXISTS model_rollout_group_shards_status_idx
    ON model_rollout_group_shards(group_id,status,shard_index);

UPDATE masi_schema_meta
SET value='12', applied_at=now(), checksum='computed-by-migration-runner',
    source_digest='db/migrations/0019_model_rollout_groups.sql'
WHERE key='version';

COMMIT;
