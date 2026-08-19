-- MASI-NIDS-vNext durable post-CAS Edge journal acknowledgement outbox (v10).
-- Requirement IDs: FUNC-EFFECT-001, REL-001, DB-GOV-001, TEST-007.

BEGIN;

CREATE TABLE IF NOT EXISTS effect_acknowledgements (
    operation_id TEXT PRIMARY KEY,
    effect_intent_id TEXT NOT NULL REFERENCES effect_intents(effect_intent_id),
    target_id TEXT NOT NULL,
    result_digest TEXT NOT NULL CHECK (result_digest ~ '^sha256:[0-9a-f]{64}$'),
    canonical_effect_reference TEXT NOT NULL,
    committed_at_unix_ms BIGINT NOT NULL CHECK (committed_at_unix_ms >= 1),
    state TEXT NOT NULL CHECK (state IN ('pending','acked','exhausted')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 8),
    next_attempt_at_unix_ms BIGINT NOT NULL CHECK (next_attempt_at_unix_ms >= 1),
    acked_at_unix_ms BIGINT,
    last_error TEXT NOT NULL DEFAULT '',
    trace_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT effect_ack_state_v10 CHECK (
        (state='acked' AND acked_at_unix_ms IS NOT NULL) OR
        (state<>'acked' AND acked_at_unix_ms IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS effect_acknowledgements_pending_idx
    ON effect_acknowledgements(next_attempt_at_unix_ms,committed_at_unix_ms)
    WHERE state='pending';

UPDATE masi_schema_meta
SET value='10', applied_at=now(), checksum='computed-by-migration-runner',
    source_digest='db/migrations/0017_effect_ack_outbox.sql'
WHERE key='version';

COMMIT;
