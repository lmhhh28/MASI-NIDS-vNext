-- MASI-NIDS-vNext model revision identity and append-only lifecycle audit (v5).
-- Requirement IDs: DB-MODEL-001, FUNC-INF-MODEL-001, REL-INF-001.

BEGIN;

ALTER TABLE model_revisions
    ADD COLUMN IF NOT EXISTS actor_issuer TEXT NOT NULL DEFAULT 'legacy:unknown',
    ADD COLUMN IF NOT EXISTS actor_subject TEXT NOT NULL DEFAULT 'legacy:unknown';

CREATE TABLE IF NOT EXISTS model_revision_events (
    event_id TEXT PRIMARY KEY,
    model_revision_id TEXT NOT NULL REFERENCES model_revisions(model_revision_id),
    model_revision_digest TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('REGISTERED','REVOKED')),
    previous_status TEXT NOT NULL CHECK (previous_status IN ('qualified','unqualified','hold','revoked')),
    new_status TEXT NOT NULL CHECK (new_status IN ('qualified','unqualified','hold','revoked')),
    scope TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    actor_issuer TEXT NOT NULL,
    actor_subject TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    occurred_at_unix_ms BIGINT NOT NULL CHECK (occurred_at_unix_ms >= 1),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS model_revision_events_revision_time_idx
    ON model_revision_events(model_revision_id,occurred_at_unix_ms,event_id);

UPDATE masi_schema_meta
SET value='5', applied_at=now(), checksum='computed-by-migration-runner',
    source_digest='db/migrations/0012_model_revision_audit.sql'
WHERE key='version';

COMMIT;
