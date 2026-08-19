-- MASI-NIDS-vNext exact plugin-statistics manifest/qualification fence (v8).
-- Requirement IDs: CONTRACT-PLUGIN-STAT-001, DB-PLUGIN-STAT-001, PLUGIN-STAT-001.

BEGIN;

ALTER TABLE plugin_statistics_definitions
    ADD COLUMN IF NOT EXISTS manifest_digest TEXT,
    ADD COLUMN IF NOT EXISTS qualification_id TEXT REFERENCES plugin_qualifications(qualification_id),
    ADD COLUMN IF NOT EXISTS qualification_digest TEXT;

ALTER TABLE plugin_statistics_definitions
    ADD CONSTRAINT plugin_statistics_definition_qualification_fence_v8 CHECK (
        (manifest_digest IS NULL AND qualification_id IS NULL AND qualification_digest IS NULL)
        OR
        (manifest_digest ~ '^sha256:[0-9a-f]{64}$'
         AND qualification_id IS NOT NULL
         AND qualification_digest ~ '^sha256:[0-9a-f]{64}$')
    );

CREATE INDEX IF NOT EXISTS plugin_statistics_definitions_qualification_idx
    ON plugin_statistics_definitions(plugin_id,binding_generation,qualification_id);

UPDATE masi_schema_meta
SET value='8', applied_at=now(), checksum='computed-by-migration-runner',
    source_digest='db/migrations/0015_plugin_statistics_qualification_fence.sql'
WHERE key='version';

COMMIT;
