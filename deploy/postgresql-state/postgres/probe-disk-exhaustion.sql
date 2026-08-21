\set ON_ERROR_STOP on

-- The fault-only PostgreSQL instance stores PGDATA on a 256 MiB tmpfs. The
-- inner subtransaction must encounter SQLSTATE 53100 and then release its
-- transient relation so that the server can still answer a readback query.
DO $probe$
DECLARE
    disk_full_observed BOOLEAN := FALSE;
BEGIN
    EXECUTE 'CREATE UNLOGGED TABLE masi_disk_exhaustion_probe(payload BYTEA)';
    EXECUTE 'ALTER TABLE masi_disk_exhaustion_probe ALTER COLUMN payload SET STORAGE EXTERNAL';
    BEGIN
        EXECUTE $statement$
            INSERT INTO masi_disk_exhaustion_probe(payload)
            SELECT repeat('x', 1048576)::bytea
            FROM generate_series(1, 512)
        $statement$;
    EXCEPTION
        WHEN disk_full THEN
            disk_full_observed := TRUE;
            RAISE NOTICE 'MASI_EXPECTED_DISK_FULL_SQLSTATE_53100';
    END;

    EXECUTE 'DROP TABLE masi_disk_exhaustion_probe';

    IF NOT disk_full_observed THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0001',
            MESSAGE = 'bounded disk exhaustion was not observed';
    END IF;
END
$probe$;

SELECT current_database() AS database,
       current_setting('server_version_num')::INTEGER AS server_version_num,
       pg_is_in_recovery() AS in_recovery,
       to_regclass('public.masi_disk_exhaustion_probe') IS NULL AS probe_rolled_back,
       1 AS post_fault_readback;
