-- Control API lifecycle/detail hardening (schema v21).
-- Requirement IDs: FUNC-API-001, FUNC-GOV-001, FUNC-TARGET-FLEET-001,
-- CONTRACT-TARGET-001, CONTRACT-EFFECT-001.

BEGIN;

ALTER TABLE effect_proposals
    ADD COLUMN superseded_by_proposal_id TEXT REFERENCES effect_proposals(proposal_id),
    ADD COLUMN superseded_at_unix_ms BIGINT CHECK(superseded_at_unix_ms IS NULL OR superseded_at_unix_ms >= 1),
    ADD COLUMN superseded_by_actor_ref TEXT,
    ADD COLUMN supersede_reason_code TEXT;
ALTER TABLE effect_proposals ADD CONSTRAINT effect_proposal_supersede_tuple_v21 CHECK(
    (superseded_by_proposal_id IS NULL AND superseded_at_unix_ms IS NULL AND
     superseded_by_actor_ref IS NULL AND supersede_reason_code IS NULL) OR
    (superseded_by_proposal_id IS NOT NULL AND superseded_at_unix_ms IS NOT NULL AND
     superseded_by_actor_ref IS NOT NULL AND supersede_reason_code IS NOT NULL AND
     superseded_by_proposal_id<>proposal_id)
);
CREATE UNIQUE INDEX effect_proposal_superseded_by_uidx
    ON effect_proposals(superseded_by_proposal_id)
    WHERE superseded_by_proposal_id IS NOT NULL;

ALTER TABLE target_lifecycle_events DROP CONSTRAINT target_lifecycle_events_previous_status_check;
ALTER TABLE target_lifecycle_events DROP CONSTRAINT target_lifecycle_events_new_status_check;
ALTER TABLE target_lifecycle_events ADD CONSTRAINT target_lifecycle_previous_status_v21 CHECK(
    previous_status IS NULL OR previous_status IN
    ('candidate','verified','active','draining','disabled','quarantined','retired'));
ALTER TABLE target_lifecycle_events ADD CONSTRAINT target_lifecycle_new_status_v21 CHECK(
    new_status IN ('candidate','verified','active','draining','disabled','quarantined','retired'));

GRANT UPDATE ON effect_proposals TO masi_control_app;

UPDATE masi_schema_meta
SET value='21',applied_at=now(),checksum='computed-by-migration-runner',
    source_digest='db/migrations/0028_control_api_lifecycle.sql'
WHERE key='version';

COMMIT;
