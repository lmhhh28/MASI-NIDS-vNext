-- MASI-NIDS-vNext core + event schema (v1).
--
-- Migration: 0001_core_event.sql
-- Version:   1
-- Owner:     Go Control Core (MOD-CTRL-001) — sole business writer of the
--            core/effect_governance/firewall/rule_observation/target_fleet/
--            model_platform/plugin_platform/plugin_statistics domains.
--
-- Per docs/design/modules/postgresql-state-design.md §5:
--   * Applied migrations are immutable; this file carries version + checksum.
--   * Migration runs by an independent job/binary (MOD-DB-001, not yet built);
--     the Go application does NOT auto-run migrations — it only checks schema
--     compatibility at startup (migration_reader.go, fail-closed).
--   * No P4/Edge/Central/Host/plugin/HTTP calls during migration.
--   * Destructive tests only on a DB whose name contains "test".
--
-- Expand/contract: v1 establishes the initial canonical schema. Future
-- versions expand (add columns/tables) before contracting; current/previous
-- reader-writer compatibility is maintained per MIG-005.

BEGIN;

-- Schema metadata: the single source of the applied schema version. The Go
-- application's migration_reader reads `version` at startup and fails closed
-- on mismatch (unknown/wrong version).
CREATE TABLE IF NOT EXISTS masi_schema_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    checksum   TEXT NOT NULL,
    source_digest TEXT NOT NULL
);

-- Canonical Event fact. Go is the sole writer. Hot fields (identity, status,
-- generation, time, scope) are strong-typed columns; non-hot extensions use
-- JSONB validated by the application against contracts/event/v1.
CREATE TABLE IF NOT EXISTS events (
    event_id                     TEXT PRIMARY KEY,
    event_idempotency_key        TEXT NOT NULL,
    input_digest                 TEXT NOT NULL,
    output_digest                TEXT NOT NULL,
    canonical_event_id           TEXT,
    model_control_incarnation_id TEXT NOT NULL,
    shard_id                     TEXT NOT NULL,
    route_epoch                  BIGINT NOT NULL CHECK (route_epoch >= 1),
    logical_pool_id              TEXT NOT NULL,
    pool_generation              BIGINT NOT NULL CHECK (pool_generation >= 1),
    binding_generation           BIGINT NOT NULL CHECK (binding_generation >= 1),
    source_window_identity       JSONB NOT NULL,
    quality                      TEXT NOT NULL CHECK (quality IN (
        'valid','partial','gap','stale','invalid','reset','not-covered','not-measurable'
    )),
    commit_status                TEXT NOT NULL CHECK (commit_status IN (
        'committed','idempotent','conflict','rejected'
    )),
    event_time                   TIMESTAMPTZ NOT NULL,
    committed_at_unix_ms         BIGINT,
    ingest_batch_digest          TEXT NOT NULL,
    actor_ref                    TEXT,
    trace_id                     TEXT NOT NULL,
    reason_code                  TEXT NOT NULL,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotency: one canonical Event per event_idempotency_key. A replay with
-- the SAME (input_digest, output_digest) returns the original event
-- (commit_status=idempotent). A second submission with the SAME key but a
-- DIFFERENT digest is a stable conflict (commit_status=conflict, no new
-- canonical Event; the existing row is preserved). Late/wrong generation is
-- rejected before insert. The digest comparison is performed by the application
-- inside the short ingest transaction.
CREATE UNIQUE INDEX IF NOT EXISTS events_idempotency_key_uidx
    ON events (event_idempotency_key);

-- canonical_event_id is set only for committed/idempotent; NULL for
-- conflict/rejected (no canonical Event).
ALTER TABLE events ADD CONSTRAINT events_canonical_iff_committed CHECK (
    (commit_status IN ('committed','idempotent')) = (canonical_event_id IS NOT NULL)
);

-- Incident rollup (Phase 2 stub table; full aggregation wired with rule/model
-- projection). Incidents reference canonical events; an incident is never a
-- second writer of event facts.
CREATE TABLE IF NOT EXISTS incidents (
    incident_id    TEXT PRIMARY KEY,
    severity       TEXT NOT NULL CHECK (severity IN ('info','low','medium','high','critical')),
    status         TEXT NOT NULL,
    first_event_id TEXT REFERENCES events(event_id),
    last_event_id  TEXT REFERENCES events(event_id),
    scope          TEXT NOT NULL,
    actor_ref      TEXT,
    trace_id       TEXT NOT NULL,
    reason_code    TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Evidence references: plugin/agent artifacts are references only, never
-- direct writes to events/effects (go-control-core-design §4).
CREATE TABLE IF NOT EXISTS evidence_refs (
    evidence_id    TEXT PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('plugin-artifact','agent-artifact','observation','audit')),
    reference_digest TEXT NOT NULL,
    source         TEXT NOT NULL,
    trace_id       TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO masi_schema_meta (key, value, checksum, source_digest)
VALUES ('version', '1', 'placeholder-checksum', 'placeholder-source-digest')
ON CONFLICT (key) DO NOTHING;

COMMIT;