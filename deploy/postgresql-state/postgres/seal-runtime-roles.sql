\set ON_ERROR_STOP on

-- Run once after migrations and destructive module-only tests. This removes
-- bootstrap privileges without coupling role provisioning to application start.
BEGIN;
REVOKE TEMPORARY ON DATABASE masi_state_module_test FROM PUBLIC;
REVOKE CONNECT ON DATABASE masi_state_module_test FROM PUBLIC;
GRANT CONNECT ON DATABASE masi_state_module_test
    TO masi_migration_login,masi_app_login,masi_monitoring_login;
GRANT masi_control_app TO masi_app_login;

REVOKE pg_monitor FROM masi_migration_login CASCADE;
REVOKE masi_analysis_private FROM masi_migration_login CASCADE;
ALTER ROLE masi_migration_login NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOINHERIT CONNECTION LIMIT 4;
ALTER ROLE masi_migration_login SET statement_timeout='2min';
ALTER ROLE masi_migration_login SET lock_timeout='5s';
ALTER ROLE masi_app_login NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION INHERIT CONNECTION LIMIT 64;
ALTER ROLE masi_app_login SET statement_timeout='30s';
ALTER ROLE masi_app_login SET lock_timeout='5s';
ALTER ROLE masi_replication_login NOSUPERUSER NOCREATEDB NOCREATEROLE
    REPLICATION NOINHERIT CONNECTION LIMIT 4;
ALTER ROLE masi_monitoring_login NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION INHERIT CONNECTION LIMIT 4;
-- PostgreSQL forbids the current session from dropping its own SUPERUSER bit.
-- Disable login instead; no runtime workload receives membership in this role.
ALTER ROLE masi_bootstrap NOLOGIN;
COMMIT;
