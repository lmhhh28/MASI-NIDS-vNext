-- MASI-NIDS-vNext durable Fleet child/parent projection (schema v4).
-- Requirement IDs: CONTRACT-FLEET-EFFECT-001, FUNC-TARGET-FLEET-001,
-- REL-TARGET-FLEET-001, DB-TARGET-FLEET-001.

BEGIN;

ALTER TABLE fleet_operations
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE fleet_child_intents
    DROP CONSTRAINT IF EXISTS fleet_child_intents_status_check;
ALTER TABLE fleet_child_intents
    ADD CONSTRAINT fleet_child_intents_status_v4 CHECK (status IN
        ('pending','claimed','executing','applied','hold','unknown','reconciling','failed','skipped','blocked'));

CREATE INDEX IF NOT EXISTS fleet_child_intents_projection_idx
    ON fleet_child_intents(fleet_operation_id,wave_index,status,child_intent_id);

UPDATE masi_schema_meta
SET value='4', applied_at=now(), checksum='computed-by-migration-runner',
    source_digest='db/migrations/0011_fleet_projection.sql'
WHERE key='version';

COMMIT;
