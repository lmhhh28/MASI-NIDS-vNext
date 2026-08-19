-- Fleet-portable normalized firewall policy digest (v17).
-- Requirement IDs: ARCH-FW-001, CONTRACT-FLEET-EFFECT-001, TEST-TARGET-FLEET-001.

BEGIN;

ALTER TABLE firewall_revisions DROP CONSTRAINT firewall_revisions_revision_digest_key;
ALTER TABLE firewall_revisions ADD CONSTRAINT firewall_revisions_target_digest_key
    UNIQUE(target_id,revision_digest);

UPDATE masi_schema_meta
SET value='17', applied_at=now(), checksum='computed-by-migration-runner',
    source_digest='db/migrations/0024_fleet_policy_digest.sql'
WHERE key='version';

COMMIT;
