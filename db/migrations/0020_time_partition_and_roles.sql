-- Go Control Core time partitioning, global Event identity, and database role
-- separation (v13).
-- Requirement IDs: DB-001, DB-002, EVENT-001, SEC-001, TEST-GO-CTRL-001.
--
-- This is an initialization-stage expand/contract migration. It preserves all
-- existing rows in one transaction, replaces only the physical history tables,
-- and leaves current projections/ledgers untouched. Application processes are
-- not permitted to run this migration.

BEGIN;

-- A range-partitioned Event table cannot enforce a unique key that omits its
-- partition key. Keep the globally unique Event/idempotency identity in this
-- small Go-owned registry; the range partitions hold the complete Event fact.
-- Both rows are written in the same short ingest transaction.
CREATE TABLE event_identities (
    event_id                 TEXT PRIMARY KEY,
    event_idempotency_key    TEXT NOT NULL UNIQUE,
    input_digest             TEXT NOT NULL,
    output_digest            TEXT NOT NULL,
    event_time               TIMESTAMPTZ NOT NULL,
    committed_at_unix_ms     BIGINT NOT NULL CHECK (committed_at_unix_ms >= 1),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO event_identities(event_id,event_idempotency_key,input_digest,output_digest,
                             event_time,committed_at_unix_ms,created_at)
SELECT event_id,event_idempotency_key,input_digest,output_digest,event_time,
       committed_at_unix_ms,created_at
FROM events;

-- Existing incident references now bind the global identity registry rather
-- than a single physical partition.
ALTER TABLE incidents DROP CONSTRAINT incidents_first_event_id_fkey;
ALTER TABLE incidents DROP CONSTRAINT incidents_last_event_id_fkey;
ALTER TABLE incident_projection_events DROP CONSTRAINT incident_projection_events_event_id_fkey;

ALTER TABLE events RENAME TO events_unpartitioned_v12;
CREATE TABLE events (
    LIKE events_unpartitioned_v12 INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING STORAGE
) PARTITION BY RANGE (event_time);
ALTER TABLE events ADD CONSTRAINT events_partitioned_pkey PRIMARY KEY(event_time,event_id);
ALTER TABLE events ADD CONSTRAINT events_partitioned_idempotency_local
    UNIQUE(event_time,event_idempotency_key);
ALTER TABLE events ADD CONSTRAINT events_global_identity_fkey
    FOREIGN KEY(event_id) REFERENCES event_identities(event_id);

DO $partition_events$
DECLARE
    partition_start DATE := (date_trunc('month', CURRENT_DATE) - interval '12 months')::date;
    partition_end   DATE := (date_trunc('month', CURRENT_DATE) + interval '36 months')::date;
    month_start     DATE;
    child_name      TEXT;
BEGIN
    child_name := 'events_before_' || to_char(partition_start, 'YYYYMM');
    EXECUTE format('CREATE TABLE %I PARTITION OF events FOR VALUES FROM (MINVALUE) TO (%L)',
                   child_name, partition_start);
    month_start := partition_start;
    WHILE month_start < partition_end LOOP
        child_name := 'events_' || to_char(month_start, 'YYYYMM');
        EXECUTE format('CREATE TABLE %I PARTITION OF events FOR VALUES FROM (%L) TO (%L)',
                       child_name, month_start, (month_start + interval '1 month')::date);
        month_start := (month_start + interval '1 month')::date;
    END LOOP;
    child_name := 'events_after_' || to_char(partition_end, 'YYYYMM');
    EXECUTE format('CREATE TABLE %I PARTITION OF events FOR VALUES FROM (%L) TO (MAXVALUE)',
                   child_name, partition_end);
END
$partition_events$;

INSERT INTO events SELECT * FROM events_unpartitioned_v12;
DROP TABLE events_unpartitioned_v12;
CREATE INDEX events_scope_time_idx ON events(scope,event_time DESC,event_id DESC);
CREATE INDEX events_idempotency_lookup_idx ON events(event_idempotency_key,event_time);

ALTER TABLE incidents ADD CONSTRAINT incidents_first_event_identity_fkey
    FOREIGN KEY(first_event_id) REFERENCES event_identities(event_id);
ALTER TABLE incidents ADD CONSTRAINT incidents_last_event_identity_fkey
    FOREIGN KEY(last_event_id) REFERENCES event_identities(event_id);
ALTER TABLE incident_projection_events ADD CONSTRAINT incident_projection_event_identity_fkey
    FOREIGN KEY(event_id) REFERENCES event_identities(event_id);

-- Rule rollups already carry the time key in their primary key, so they can be
-- converted directly to native range partitions without changing Go SQL.
ALTER TABLE rule_rollups_5m RENAME TO rule_rollups_5m_unpartitioned_v12;
CREATE TABLE rule_rollups_5m (
    LIKE rule_rollups_5m_unpartitioned_v12 INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING STORAGE
) PARTITION BY RANGE(window_start_unix_ms);
ALTER TABLE rule_rollups_5m ADD CONSTRAINT rule_rollups_5m_partitioned_pkey
    PRIMARY KEY(window_start_unix_ms,epoch_id,rule_id);
ALTER TABLE rule_rollups_5m ADD CONSTRAINT rule_rollups_5m_epoch_fkey
    FOREIGN KEY(epoch_id) REFERENCES rule_observation_epochs(epoch_id);

ALTER TABLE rule_rollups_1h RENAME TO rule_rollups_1h_unpartitioned_v12;
CREATE TABLE rule_rollups_1h (
    LIKE rule_rollups_1h_unpartitioned_v12 INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING STORAGE
) PARTITION BY RANGE(window_start_unix_ms);
ALTER TABLE rule_rollups_1h ADD CONSTRAINT rule_rollups_1h_partitioned_pkey
    PRIMARY KEY(window_start_unix_ms,epoch_id,rule_id);
ALTER TABLE rule_rollups_1h ADD CONSTRAINT rule_rollups_1h_epoch_fkey
    FOREIGN KEY(epoch_id) REFERENCES rule_observation_epochs(epoch_id);

DO $partition_rollups$
DECLARE
    partition_start DATE := (date_trunc('month', CURRENT_DATE) - interval '24 months')::date;
    partition_end   DATE := (date_trunc('month', CURRENT_DATE) + interval '24 months')::date;
    month_start     DATE;
    start_ms        BIGINT;
    end_ms          BIGINT;
    base_name       TEXT;
BEGIN
    start_ms := (extract(epoch FROM partition_start::timestamptz) * 1000)::bigint;
    EXECUTE format('CREATE TABLE %I PARTITION OF rule_rollups_5m FOR VALUES FROM (MINVALUE) TO (%s)',
                   'rule_rollups_5m_before_' || to_char(partition_start, 'YYYYMM'), start_ms);
    EXECUTE format('CREATE TABLE %I PARTITION OF rule_rollups_1h FOR VALUES FROM (MINVALUE) TO (%s)',
                   'rule_rollups_1h_before_' || to_char(partition_start, 'YYYYMM'), start_ms);
    month_start := partition_start;
    WHILE month_start < partition_end LOOP
        start_ms := (extract(epoch FROM month_start::timestamptz) * 1000)::bigint;
        end_ms := (extract(epoch FROM (month_start + interval '1 month')::timestamptz) * 1000)::bigint;
        base_name := to_char(month_start, 'YYYYMM');
        EXECUTE format('CREATE TABLE %I PARTITION OF rule_rollups_5m FOR VALUES FROM (%s) TO (%s)',
                       'rule_rollups_5m_' || base_name, start_ms, end_ms);
        EXECUTE format('CREATE TABLE %I PARTITION OF rule_rollups_1h FOR VALUES FROM (%s) TO (%s)',
                       'rule_rollups_1h_' || base_name, start_ms, end_ms);
        month_start := (month_start + interval '1 month')::date;
    END LOOP;
    start_ms := (extract(epoch FROM partition_end::timestamptz) * 1000)::bigint;
    EXECUTE format('CREATE TABLE %I PARTITION OF rule_rollups_5m FOR VALUES FROM (%s) TO (MAXVALUE)',
                   'rule_rollups_5m_after_' || to_char(partition_end, 'YYYYMM'), start_ms);
    EXECUTE format('CREATE TABLE %I PARTITION OF rule_rollups_1h FOR VALUES FROM (%s) TO (MAXVALUE)',
                   'rule_rollups_1h_after_' || to_char(partition_end, 'YYYYMM'), start_ms);
END
$partition_rollups$;

INSERT INTO rule_rollups_5m SELECT * FROM rule_rollups_5m_unpartitioned_v12;
INSERT INTO rule_rollups_1h SELECT * FROM rule_rollups_1h_unpartitioned_v12;
DROP TABLE rule_rollups_5m_unpartitioned_v12;
DROP TABLE rule_rollups_1h_unpartitioned_v12;
CREATE INDEX rule_rollups_5m_rule_time ON rule_rollups_5m(rule_id,window_start_unix_ms DESC);
CREATE INDEX rule_rollups_1h_rule_time ON rule_rollups_1h(rule_id,window_start_unix_ms DESC);

-- Plugin-statistics history is append-only and independently retainable. Keep
-- its sequence stable while replacing the heap with range partitions.
ALTER SEQUENCE plugin_statistics_history_history_seq_seq OWNED BY NONE;
ALTER TABLE plugin_statistics_history RENAME TO plugin_statistics_history_unpartitioned_v12;
CREATE TABLE plugin_statistics_history (
    LIKE plugin_statistics_history_unpartitioned_v12 INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING STORAGE
) PARTITION BY RANGE(recorded_at);
ALTER TABLE plugin_statistics_history ADD CONSTRAINT plugin_statistics_history_partitioned_pkey
    PRIMARY KEY(recorded_at,history_seq);
ALTER TABLE plugin_statistics_history ADD CONSTRAINT plugin_statistics_history_artifact_fkey
    FOREIGN KEY(artifact_id) REFERENCES plugin_statistic_artifacts(artifact_id);

DO $partition_statistics$
DECLARE
    partition_start DATE := (date_trunc('month', CURRENT_DATE) - interval '24 months')::date;
    partition_end   DATE := (date_trunc('month', CURRENT_DATE) + interval '24 months')::date;
    month_start     DATE;
BEGIN
    EXECUTE format('CREATE TABLE %I PARTITION OF plugin_statistics_history FOR VALUES FROM (MINVALUE) TO (%L)',
                   'plugin_statistics_history_before_' || to_char(partition_start, 'YYYYMM'), partition_start);
    month_start := partition_start;
    WHILE month_start < partition_end LOOP
        EXECUTE format('CREATE TABLE %I PARTITION OF plugin_statistics_history FOR VALUES FROM (%L) TO (%L)',
                       'plugin_statistics_history_' || to_char(month_start, 'YYYYMM'), month_start,
                       (month_start + interval '1 month')::date);
        month_start := (month_start + interval '1 month')::date;
    END LOOP;
    EXECUTE format('CREATE TABLE %I PARTITION OF plugin_statistics_history FOR VALUES FROM (%L) TO (MAXVALUE)',
                   'plugin_statistics_history_after_' || to_char(partition_end, 'YYYYMM'), partition_end);
END
$partition_statistics$;

INSERT INTO plugin_statistics_history SELECT * FROM plugin_statistics_history_unpartitioned_v12;
DROP TABLE plugin_statistics_history_unpartitioned_v12;
ALTER SEQUENCE plugin_statistics_history_history_seq_seq OWNED BY plugin_statistics_history.history_seq;
CREATE INDEX plugin_statistics_history_def ON plugin_statistics_history(definition_id,history_seq DESC);

-- Cluster roles carry no login credentials. Deployment provisioning grants a
-- login role membership; no password/secret is embedded in a migration.
DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='masi_control_app') THEN
        CREATE ROLE masi_control_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='masi_control_readonly') THEN
        CREATE ROLE masi_control_readonly NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='masi_control_maintenance') THEN
        CREATE ROLE masi_control_maintenance NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END
$roles$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO masi_control_app,masi_control_readonly,masi_control_maintenance;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO masi_control_readonly;
GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA public TO masi_control_app;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO masi_control_app;
GRANT SELECT,DELETE ON rule_rollups_5m,rule_rollups_1h TO masi_control_maintenance;
GRANT DELETE ON rule_rollups_5m,rule_rollups_1h TO masi_control_app;

-- Immutable facts/history are insert+read only for the online application.
REVOKE UPDATE,DELETE ON
    masi_schema_meta,event_identities,events,evidence_refs,effect_proposals,effect_decisions,
    effect_attempts,firewall_revisions,plugin_manifests,plugin_qualifications,
    plugin_audit_events,plugin_statistics_definitions,plugin_statistic_schedules,
    plugin_statistic_input_bundles,plugin_statistic_artifacts,plugin_statistics_history,
    rule_observation_epochs,target_capability_observation_events,target_lifecycle_events,
    model_control_incarnations,model_revision_events,model_pool_observation_events,
    incident_projection_events
FROM masi_control_app;
DO $history_privileges$
BEGIN
    IF to_regclass('public.masi_migration_history') IS NOT NULL THEN
        REVOKE INSERT,UPDATE,DELETE ON masi_migration_history FROM masi_control_app;
    END IF;
    IF to_regclass('public.masi_test_migration_history') IS NOT NULL THEN
        REVOKE INSERT,UPDATE,DELETE ON masi_test_migration_history FROM masi_control_app;
    END IF;
END
$history_privileges$;

-- Child-table direct access cannot be used to evade the parent's immutable
-- privilege boundary. Access through the parent still uses the parent grants.
DO $partition_privileges$
DECLARE child REGCLASS;
BEGIN
    FOR child IN
        SELECT inhrelid::regclass FROM pg_inherits
        WHERE inhparent IN ('events'::regclass,'plugin_statistics_history'::regclass)
    LOOP
        EXECUTE format('REVOKE UPDATE,DELETE ON %s FROM masi_control_app', child);
    END LOOP;
END
$partition_privileges$;

ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO masi_control_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT,INSERT,UPDATE ON TABLES TO masi_control_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO masi_control_app;

UPDATE masi_schema_meta
SET value='13', applied_at=now(), checksum='computed-by-migration-runner',
    source_digest='db/migrations/0020_time_partition_and_roles.sql'
WHERE key='version';

COMMIT;
