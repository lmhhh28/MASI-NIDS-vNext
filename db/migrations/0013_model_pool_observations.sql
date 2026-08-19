-- MASI-NIDS-vNext append-only Central pool startup/readback observations (v6).
-- Requirement IDs: DB-MODEL-001, FUNC-INF-MODEL-001, REL-INF-POOL-001.

BEGIN;

CREATE TABLE IF NOT EXISTS model_pool_observation_events (
    observation_id TEXT PRIMARY KEY,
    record_digest TEXT NOT NULL CHECK (record_digest ~ '^sha256:[0-9a-f]{64}$'),
    pool_observation_digest TEXT NOT NULL CHECK (pool_observation_digest ~ '^sha256:[0-9a-f]{64}$'),
    logical_pool_id TEXT NOT NULL,
    pool_generation BIGINT NOT NULL CHECK (pool_generation >= 1),
    model_control_incarnation_id TEXT NOT NULL REFERENCES model_control_incarnations(incarnation_id),
    operation_id TEXT NOT NULL REFERENCES model_rollout_operations(operation_id),
    scope TEXT NOT NULL,
    readback_attempt_id TEXT NOT NULL,
    observed_at_unix_ms BIGINT NOT NULL CHECK (observed_at_unix_ms >= 1),
    ready_replicas INTEGER NOT NULL CHECK (ready_replicas BETWEEN 1 AND 256),
    loaded BOOLEAN NOT NULL,
    capacity_qualified BOOLEAN NOT NULL,
    min_ready_met BOOLEAN NOT NULL,
    failure_domain TEXT NOT NULL DEFAULT '',
    endpoint TEXT NOT NULL,
    tls_server_name TEXT NOT NULL,
    tls_identity_ref TEXT NOT NULL,
    eligible_workers JSONB NOT NULL CHECK (jsonb_typeof(eligible_workers)='array'),
    readback_body JSONB NOT NULL CHECK (jsonb_typeof(readback_body)='object'),
    capacity_body JSONB NOT NULL CHECK (jsonb_typeof(capacity_body)='object'),
    trace_id TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT model_pool_observation_generation_fk
        FOREIGN KEY (logical_pool_id,pool_generation)
        REFERENCES pool_generations(logical_pool_id,pool_generation)
);
CREATE INDEX IF NOT EXISTS model_pool_observation_events_pool_time_idx
    ON model_pool_observation_events(logical_pool_id,pool_generation,observed_at_unix_ms,observation_id);

CREATE TABLE IF NOT EXISTS model_pool_observations_current (
    logical_pool_id TEXT NOT NULL,
    pool_generation BIGINT NOT NULL,
    observation_id TEXT NOT NULL UNIQUE REFERENCES model_pool_observation_events(observation_id),
    record_digest TEXT NOT NULL CHECK (record_digest ~ '^sha256:[0-9a-f]{64}$'),
    pool_observation_digest TEXT NOT NULL CHECK (pool_observation_digest ~ '^sha256:[0-9a-f]{64}$'),
    observed_at_unix_ms BIGINT NOT NULL CHECK (observed_at_unix_ms >= 1),
    scope TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (logical_pool_id,pool_generation),
    CONSTRAINT model_pool_current_generation_fk
        FOREIGN KEY (logical_pool_id,pool_generation)
        REFERENCES pool_generations(logical_pool_id,pool_generation)
);

UPDATE masi_schema_meta
SET value='6', applied_at=now(), checksum='computed-by-migration-runner',
    source_digest='db/migrations/0013_model_pool_observations.sql'
WHERE key='version';

COMMIT;
