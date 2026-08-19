-- MASI-NIDS-vNext immutable normalized P4 payload on the sole effect queue (v9).
-- Requirement IDs: FUNC-EFFECT-001, DB-GOV-001, FUNC-FW-001, CONTRACT-EFFECT-001.

BEGIN;

ALTER TABLE effect_intents
    ADD COLUMN IF NOT EXISTS effect_payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE effect_intents
    ADD CONSTRAINT effect_intents_payload_object_v9 CHECK (jsonb_typeof(effect_payload)='object');

CREATE UNIQUE INDEX IF NOT EXISTS effect_intents_operation_uidx
    ON effect_intents(operation_id);

UPDATE masi_schema_meta
SET value='9', applied_at=now(), checksum='computed-by-migration-runner',
    source_digest='db/migrations/0016_effect_payload.sql'
WHERE key='version';

COMMIT;
