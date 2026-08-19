-- MASI-NIDS-vNext plugin statistics schema (v1).
--
-- Migration: 0008_plugin_statistics.sql
-- Version:   1 (applied after 0007_rule_observation.sql)
-- Owner:     Go Control Core (MOD-CTRL-001) — Go owns the canonical input
--            freeze, schedule/run/idempotency, artifact validation, the
--            plugin_statistics current/history projection, and read/export
--            authorization. Host/plugin/Web/Prometheus are NEVER writers of
--            these facts.
--
-- Invariants (contracts/plugin/statistics/v1):
--   * A definition body lives ONLY in the immutable plugin_platform manifest
--     revision; plugin_statistics rows reference the exact definition
--     identity/digest.
--   * plugin_statistic_runs is a durable NON-EFFECT ledger: it is never an
--     effect dispatcher claim source and never enters the effect queue.
--   * Idempotency: same key + same digest returns the original run; same key
--     + different digest is a conflict.
--   * A late result from an old binding_generation is audit-only and never
--     overwrites a newer generation's current projection.
--   * Schedule create/revise/disable are immutable append-only revisions.
--
-- No Host/plugin/HTTP/P4 calls during migration.

BEGIN;

-- Schedule revisions (immutable, append-only; the ACTIVE one is the latest
-- non-disabled revision).
CREATE TABLE IF NOT EXISTS plugin_statistic_schedules (
    schedule_revision  BIGINT PRIMARY KEY,
    schedule_id        TEXT NOT NULL,
    definition_id      TEXT NOT NULL,
    definition_digest  TEXT NOT NULL,
    interval_seconds   INTEGER CHECK (interval_seconds >= 60),
    disabled           BOOLEAN NOT NULL DEFAULT false,
    actor_ref          TEXT NOT NULL,
    trace_id           TEXT NOT NULL,
    reason_code        TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS plugin_statistic_schedules_active
    ON plugin_statistic_schedules (schedule_id, schedule_revision DESC);

-- The durable NON-EFFECT run ledger. Only the Go-internal statistics
-- dispatcher (bounded, derived from this ledger, CAS/fenced) advances rows.
CREATE TABLE IF NOT EXISTS plugin_statistic_runs (
    run_id             TEXT PRIMARY KEY,
    run_digest         TEXT NOT NULL,
    definition_id      TEXT NOT NULL,
    schedule_id        TEXT,
    idempotency_key    TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN
        ('pending','running','succeeded','failed','timeout')),
    binding_generation BIGINT NOT NULL CHECK (binding_generation >= 1),
    claim_owner        TEXT,
    claim_expires_unix_ms BIGINT,
    frozen_input_digest TEXT,
    started_at_unix_ms  BIGINT NOT NULL,
    finished_at_unix_ms BIGINT,
    artifact_id        TEXT,
    actor_ref          TEXT NOT NULL,
    reason_code        TEXT NOT NULL,
    trace_id           TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (idempotency_key, run_digest)
);
-- The UNIQUE (idempotency_key, run_digest) implements same-key-same-digest
-- idempotency at the storage layer; same key + different digest is a
-- uniqueness-scope conflict detected by the service.

CREATE TABLE IF NOT EXISTS plugin_statistic_artifacts (
    artifact_id        TEXT PRIMARY KEY,
    artifact_digest    TEXT NOT NULL UNIQUE,
    run_id             TEXT NOT NULL REFERENCES plugin_statistic_runs(run_id),
    definition_id      TEXT NOT NULL,
    definition_digest  TEXT NOT NULL,
    status             TEXT NOT NULL,
    quality            TEXT NOT NULL CHECK (quality IN
        ('valid','partial','gap','stale','no_data','not_measurable','invalid')),
    artifact           JSONB NOT NULL,
    bytes              INTEGER NOT NULL CHECK (bytes >= 0 AND bytes <= 4194304),
    truncated_rows     INTEGER NOT NULL DEFAULT 0,
    truncated_series   INTEGER NOT NULL DEFAULT 0,
    actor_ref          TEXT NOT NULL,
    trace_id           TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Current/history projection: current is the latest artifact per definition
-- under the ACTIVE binding generation (an old-generation late result never
-- overwrites it); history retains the bounded tail.
CREATE TABLE IF NOT EXISTS plugin_statistics_current (
    definition_id      TEXT PRIMARY KEY,
    binding_generation BIGINT NOT NULL,
    artifact_id        TEXT NOT NULL REFERENCES plugin_statistic_artifacts(artifact_id),
    run_id             TEXT NOT NULL,
    quality            TEXT NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS plugin_statistics_history (
    history_seq        BIGSERIAL PRIMARY KEY,
    definition_id      TEXT NOT NULL,
    binding_generation BIGINT NOT NULL,
    artifact_id        TEXT NOT NULL REFERENCES plugin_statistic_artifacts(artifact_id),
    run_id             TEXT NOT NULL,
    quality            TEXT NOT NULL,
    recorded_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS plugin_statistics_history_def
    ON plugin_statistics_history (definition_id, history_seq DESC);

COMMIT;
