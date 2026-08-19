-- MASI-NIDS-vNext target capability observation history and current CAS (v3).
--
-- Migration: 0010_target_observation_history.sql
-- Owner: Go Control Core (MOD-CTRL-001), sole business writer.
-- Requirement IDs: CONTRACT-TARGET-001, DB-TARGET-FLEET-001,
-- FUNC-TARGET-FLEET-001, REL-TARGET-FLEET-001.

BEGIN;

ALTER TABLE target_assignments
    ADD CONSTRAINT target_assignment_lease_profile_v3
    CHECK (expires_at_unix_ms - issued_at_unix_ms BETWEEN 1000 AND 300000);

-- Preserve the existing table as the per-target current projection. History is
-- append-only in target_capability_observation_events below.
ALTER TABLE target_capability_observations
    ADD COLUMN IF NOT EXISTS actor_runtime_epoch TEXT NOT NULL DEFAULT 'legacy:unknown',
    ADD COLUMN IF NOT EXISTS profile_digest TEXT NOT NULL DEFAULT 'sha256:legacy',
    ADD COLUMN IF NOT EXISTS lease_valid BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS p4_connected BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS primary_actor BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS pipeline_exact BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS high_priority_queue_depth BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS telemetry_queue_depth BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS observation_queue_depth BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS source_wal_bytes BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS input_wal_bytes BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS result_wal_bytes BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_successful_read_unix_ms BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS freshness TEXT NOT NULL DEFAULT 'not-observed',
    ADD COLUMN IF NOT EXISTS reason_code TEXT NOT NULL DEFAULT 'NOT_OBSERVED',
    ADD COLUMN IF NOT EXISTS observation_id TEXT NOT NULL DEFAULT 'legacy:unknown',
    ADD COLUMN IF NOT EXISTS observation_digest TEXT NOT NULL DEFAULT 'sha256:legacy',
    ADD COLUMN IF NOT EXISTS trace_id TEXT NOT NULL DEFAULT 'legacy:unknown';

ALTER TABLE target_capability_observations
    ADD CONSTRAINT target_capability_current_freshness_v3
    CHECK (freshness IN ('not-observed','fresh','stale','gap')),
    ADD CONSTRAINT target_capability_current_resource_v3
    CHECK (high_priority_queue_depth BETWEEN 0 AND 128
       AND telemetry_queue_depth BETWEEN 0 AND 64
       AND observation_queue_depth BETWEEN 0 AND 8
       AND source_wal_bytes BETWEEN 0 AND 1073741824
       AND input_wal_bytes BETWEEN 0 AND 2147483648
       AND result_wal_bytes BETWEEN 0 AND 2147483648);
CREATE UNIQUE INDEX IF NOT EXISTS target_capability_current_observation_uidx
    ON target_capability_observations(observation_id);

CREATE TABLE IF NOT EXISTS target_capability_observation_events (
    observation_id TEXT PRIMARY KEY,
    observation_digest TEXT NOT NULL,
    target_id TEXT NOT NULL REFERENCES targets(target_id),
    target_control_incarnation_id TEXT NOT NULL,
    assignment_generation BIGINT NOT NULL CHECK (assignment_generation >= 1),
    actor_runtime_epoch TEXT NOT NULL,
    application_generation BIGINT NOT NULL CHECK (application_generation >= 1),
    p4info_digest TEXT NOT NULL,
    pipeline_digest TEXT NOT NULL,
    profile_digest TEXT NOT NULL,
    capacity_digest TEXT NOT NULL,
    capacity_available BOOLEAN NOT NULL,
    lease_valid BOOLEAN NOT NULL,
    p4_connected BOOLEAN NOT NULL,
    primary_actor BOOLEAN NOT NULL,
    pipeline_exact BOOLEAN NOT NULL,
    high_priority_queue_depth BIGINT NOT NULL CHECK (high_priority_queue_depth BETWEEN 0 AND 128),
    telemetry_queue_depth BIGINT NOT NULL CHECK (telemetry_queue_depth BETWEEN 0 AND 64),
    observation_queue_depth BIGINT NOT NULL CHECK (observation_queue_depth BETWEEN 0 AND 8),
    source_wal_bytes BIGINT NOT NULL CHECK (source_wal_bytes BETWEEN 0 AND 1073741824),
    input_wal_bytes BIGINT NOT NULL CHECK (input_wal_bytes BETWEEN 0 AND 2147483648),
    result_wal_bytes BIGINT NOT NULL CHECK (result_wal_bytes BETWEEN 0 AND 2147483648),
    last_successful_read_unix_ms BIGINT NOT NULL CHECK (last_successful_read_unix_ms >= 0),
    freshness TEXT NOT NULL CHECK (freshness IN ('not-observed','fresh','stale','gap')),
    reason_code TEXT NOT NULL,
    observed_at_unix_ms BIGINT NOT NULL CHECK (observed_at_unix_ms >= 1),
    expires_at_unix_ms BIGINT NOT NULL,
    trace_id TEXT NOT NULL,
    current_eligible BOOLEAN NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (expires_at_unix_ms > observed_at_unix_ms),
    CHECK (expires_at_unix_ms - observed_at_unix_ms <= 300000)
);
CREATE INDEX IF NOT EXISTS target_capability_events_target_time_idx
    ON target_capability_observation_events(target_id, observed_at_unix_ms DESC, observation_id);

UPDATE masi_schema_meta
SET value='3', applied_at=now(), checksum='computed-by-migration-runner',
    source_digest='db/migrations/0010_target_observation_history.sql'
WHERE key='version';

COMMIT;
