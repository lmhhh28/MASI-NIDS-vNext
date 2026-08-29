BEGIN;

-- Earlier expand migrations used an all-zero value only to add NOT NULL
-- columns to then-empty greenfield tables. It must never remain a durable fact.
-- Refuse an upgrade with legacy rows instead of inventing a semantic digest;
-- such a database requires an explicit, audited backfill before this migration.
DO $block$
BEGIN
    IF EXISTS (
        SELECT 1 FROM firewall_activations
        WHERE expected_cas_digest='sha256:0000000000000000000000000000000000000000000000000000000000000000'
    ) OR EXISTS (
        SELECT 1 FROM fleet_operations
        WHERE operation_digest='sha256:0000000000000000000000000000000000000000000000000000000000000000'
           OR completed_vector_digest='sha256:0000000000000000000000000000000000000000000000000000000000000000'
    ) OR EXISTS (
        SELECT 1 FROM analysis_artifacts
        WHERE input_digest='sha256:0000000000000000000000000000000000000000000000000000000000000000'
           OR tool_trajectory_digest='sha256:0000000000000000000000000000000000000000000000000000000000000000'
    ) THEN
        RAISE EXCEPTION 'zero digest facts require an audited semantic backfill before schema v22'
            USING ERRCODE='23514';
    END IF;
END
$block$;

ALTER TABLE firewall_activations
    ALTER COLUMN expected_cas_digest DROP DEFAULT,
    ADD CONSTRAINT firewall_activation_expected_cas_nonzero_v22 CHECK(
        expected_cas_digest<>'sha256:0000000000000000000000000000000000000000000000000000000000000000'
    );
ALTER TABLE fleet_operations
    ALTER COLUMN operation_digest DROP DEFAULT,
    ALTER COLUMN completed_vector_digest DROP DEFAULT,
    ADD CONSTRAINT fleet_operation_digest_nonzero_v22 CHECK(
        operation_digest<>'sha256:0000000000000000000000000000000000000000000000000000000000000000'
        AND completed_vector_digest<>'sha256:0000000000000000000000000000000000000000000000000000000000000000'
    );
ALTER TABLE analysis_artifacts
    ALTER COLUMN input_digest DROP DEFAULT,
    ALTER COLUMN tool_trajectory_digest DROP DEFAULT,
    ADD CONSTRAINT analysis_artifact_digest_nonzero_v22 CHECK(
        input_digest<>'sha256:0000000000000000000000000000000000000000000000000000000000000000'
        AND tool_trajectory_digest<>'sha256:0000000000000000000000000000000000000000000000000000000000000000'
    );

UPDATE masi_schema_meta
SET value='22',applied_at=now(),checksum='computed-by-migration-runner',
    source_digest='db/migrations/0031_reject_zero_digest_defaults.sql'
WHERE key='version';

COMMIT;
