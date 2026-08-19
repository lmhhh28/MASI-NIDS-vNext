-- MASI-NIDS-vNext target mutation idempotency and append-only lifecycle audit (v11).
-- Requirement IDs: FUNC-TARGET-FLEET-001, DB-TARGET-FLEET-001, SEC-002.

BEGIN;

ALTER TABLE targets
    ADD COLUMN IF NOT EXISTS actor_issuer TEXT,
    ADD COLUMN IF NOT EXISTS actor_subject TEXT,
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS targets_actor_idempotency_uidx
    ON targets(actor_issuer,actor_subject,idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS target_lifecycle_events (
    event_id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL REFERENCES targets(target_id),
    previous_status TEXT CHECK (previous_status IS NULL OR previous_status IN
        ('candidate','verified','active','disabled','quarantined','retired')),
    new_status TEXT NOT NULL CHECK (new_status IN
        ('candidate','verified','active','disabled','quarantined','retired')),
    scope TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    actor_issuer TEXT NOT NULL,
    actor_subject TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    occurred_at_unix_ms BIGINT NOT NULL CHECK (occurred_at_unix_ms >= 1),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS target_lifecycle_events_target_time_idx
    ON target_lifecycle_events(target_id,occurred_at_unix_ms,event_id);

UPDATE masi_schema_meta
SET value='11', applied_at=now(), checksum='computed-by-migration-runner',
    source_digest='db/migrations/0018_target_lifecycle_audit.sql'
WHERE key='version';

COMMIT;
