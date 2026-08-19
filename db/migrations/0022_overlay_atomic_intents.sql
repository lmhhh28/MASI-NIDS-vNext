-- Atomic response-overlay fact + upsert/delete effect intent linkage (v15).
-- Requirement IDs: ARCH-FW-001, FUNC-GOV-001, DB-002, TEST-P4-FW-001.

BEGIN;

ALTER TABLE firewall_overlays
    ADD COLUMN upsert_intent_id TEXT REFERENCES effect_intents(effect_intent_id);
CREATE UNIQUE INDEX firewall_overlays_upsert_intent_uidx
    ON firewall_overlays(upsert_intent_id) WHERE upsert_intent_id IS NOT NULL;

UPDATE masi_schema_meta
SET value='15', applied_at=now(), checksum='computed-by-migration-runner',
    source_digest='db/migrations/0022_overlay_atomic_intents.sql'
WHERE key='version';

COMMIT;
