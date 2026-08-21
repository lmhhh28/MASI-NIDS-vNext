-- PostgreSQL State independent-module hardening (schema API v21, migration 0029).
-- Requirement IDs: MOD-DB-001, DB-001, DB-002, DB-003, DB-004, DB-005,
-- DB-006, DB-007, DB-GOV-001, DB-PLUGIN-001, DB-PLUGIN-STAT-001,
-- DB-MODEL-001, DB-RULE-001, DB-FW-001, DB-TARGET-FLEET-001,
-- REL-001, REL-002, REL-003, MIG-005, TEST-003, TEST-007, TEST-010.
--
-- This is an expand-only migration. Migrations 0001..0028 are immutable. It
-- closes the independent State review findings without adding a scheduler,
-- external side effect, second effect queue, or business decision to PostgreSQL.

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

-- ---------------------------------------------------------------------------
-- Exact model binding and restore writer fences.
-- ---------------------------------------------------------------------------

ALTER TABLE model_control_state
    ADD CONSTRAINT model_writer_requires_active_incarnation_v21
    CHECK (NOT writer_enabled OR active_incarnation_id IS NOT NULL);

CREATE UNIQUE INDEX model_control_incarnation_replaced_once_uidx
    ON model_control_incarnations(replaced_incarnation_id)
    WHERE replaced_incarnation_id IS NOT NULL;

CREATE UNIQUE INDEX model_revisions_id_digest_uidx
    ON model_revisions(model_revision_id,model_revision_digest);

CREATE UNIQUE INDEX pool_generation_exact_revision_uidx
    ON pool_generations(
        logical_pool_id,model_control_incarnation_id,pool_generation,model_revision_id
    );

ALTER TABLE shard_bindings
    ADD CONSTRAINT shard_current_exact_revision_fk_v21
    FOREIGN KEY(logical_pool_id,model_control_incarnation_id,current_generation,current_revision_id)
    REFERENCES pool_generations(
        logical_pool_id,model_control_incarnation_id,pool_generation,model_revision_id
    ) ON DELETE RESTRICT,
    ADD CONSTRAINT shard_previous_fields_complete_v21 CHECK (
        (previous_generation IS NULL AND previous_binding_generation IS NULL
         AND previous_revision_id IS NULL)
        OR
        (previous_generation IS NOT NULL AND previous_binding_generation IS NOT NULL
         AND previous_revision_id IS NOT NULL)
    );

ALTER TABLE shard_bindings
    ADD CONSTRAINT shard_previous_exact_revision_fk_v21
    FOREIGN KEY(logical_pool_id,model_control_incarnation_id,previous_generation,previous_revision_id)
    REFERENCES pool_generations(
        logical_pool_id,model_control_incarnation_id,pool_generation,model_revision_id
    ) ON DELETE RESTRICT;

-- ---------------------------------------------------------------------------
-- Target-control incarnation facts, monotonic assignments, and restore fence.
-- ---------------------------------------------------------------------------

CREATE TABLE target_control_incarnations (
    incarnation_id TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK(source IN ('initial','pitr','clone','rewind','legacy')),
    rotated_at_unix_ms BIGINT NOT NULL CHECK(rotated_at_unix_ms >= 1),
    replaced_incarnation_id TEXT REFERENCES target_control_incarnations(incarnation_id)
        ON DELETE RESTRICT,
    backup_manifest_digest TEXT,
    actor_ref TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK(replaced_incarnation_id IS NULL OR replaced_incarnation_id<>incarnation_id),
    CHECK(backup_manifest_digest IS NULL OR
          backup_manifest_digest ~ '^sha256:[0-9a-f]{64}$')
);
CREATE UNIQUE INDEX target_control_incarnation_replaced_once_uidx
    ON target_control_incarnations(replaced_incarnation_id)
    WHERE replaced_incarnation_id IS NOT NULL;

-- Preserve already-authored initialization fixtures as fenced legacy history.
INSERT INTO target_control_incarnations(
    incarnation_id,source,rotated_at_unix_ms,actor_ref,trace_id
)
SELECT incarnation_id,'legacy',1,'migration:0029','migration:0029:assignment'
FROM target_assignments
UNION
SELECT target_control_incarnation_id,'legacy',1,'migration:0029',
       'migration:0029:observation'
FROM target_capability_observations
UNION
SELECT target_control_incarnation_id,'legacy',1,'migration:0029',
       'migration:0029:observation-event'
FROM target_capability_observation_events
ON CONFLICT(incarnation_id) DO NOTHING;

CREATE TABLE target_control_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK(singleton),
    active_incarnation_id TEXT REFERENCES target_control_incarnations(incarnation_id)
        ON DELETE RESTRICT,
    writer_enabled BOOLEAN NOT NULL DEFAULT false,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK(NOT writer_enabled OR active_incarnation_id IS NOT NULL)
);
INSERT INTO target_control_state(singleton,active_incarnation_id,writer_enabled)
SELECT true,min(incarnation_id),false
FROM target_control_incarnations
HAVING count(*)=1
ON CONFLICT(singleton) DO NOTHING;
INSERT INTO target_control_state(singleton,writer_enabled)
VALUES(true,false) ON CONFLICT(singleton) DO NOTHING;

CREATE FUNCTION masi_validate_target_assignment_monotonic()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path=pg_catalog,public
AS $function$
DECLARE
    latest_generation BIGINT;
    latest_ceiling BIGINT;
    exact_existing BOOLEAN;
BEGIN
    -- Lock the stable target row so two handoffs cannot both observe the same
    -- latest assignment. This is a database CAS primitive, not an external lease.
    PERFORM 1 FROM public.targets WHERE target_id=NEW.target_id FOR UPDATE;

    SELECT max(assignment_generation),max(election_ceiling)
      INTO latest_generation,latest_ceiling
      FROM public.target_assignments
     WHERE target_id=NEW.target_id;

    IF latest_generation IS NULL THEN
        IF NEW.assignment_generation<>1 THEN
            RAISE EXCEPTION 'first assignment generation must be 1'
                USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.assignment_generation<=latest_generation THEN
        SELECT EXISTS(
            SELECT 1 FROM public.target_assignments a
             WHERE a.target_id=NEW.target_id
               AND a.assignment_generation=NEW.assignment_generation
               AND a.incarnation_id=NEW.incarnation_id
               AND a.lease_id=NEW.lease_id
               AND a.election_floor=NEW.election_floor
               AND a.election_ceiling=NEW.election_ceiling
               AND a.application_generation=NEW.application_generation
        ) INTO exact_existing;
        IF exact_existing THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'assignment generation is stale or conflicting'
            USING ERRCODE='23505';
    END IF;
    IF NEW.assignment_generation<>latest_generation+1 THEN
        RAISE EXCEPTION 'assignment generation must advance by exactly one'
            USING ERRCODE='23514';
    END IF;
    IF NEW.election_floor<=latest_ceiling THEN
        RAISE EXCEPTION 'new election floor must exceed every prior ceiling'
            USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER target_assignment_monotonic_v21
BEFORE INSERT ON target_assignments
FOR EACH ROW EXECUTE FUNCTION masi_validate_target_assignment_monotonic();

CREATE FUNCTION masi_reject_retired_target_reactivation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path=pg_catalog,public
AS $function$
BEGIN
    IF OLD.status='retired' AND NEW.status<>'retired' THEN
        RAISE EXCEPTION 'retired target identity cannot be reactivated'
            USING ERRCODE='23514';
    END IF;
    IF NEW.status='retired' AND NEW.retired_at IS NULL THEN
        RAISE EXCEPTION 'retired target requires retired_at'
            USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER target_retired_terminal_v21
BEFORE UPDATE OF status,retired_at ON targets
FOR EACH ROW EXECUTE FUNCTION masi_reject_retired_target_reactivation();

-- ---------------------------------------------------------------------------
-- Exact firewall, fleet, plugin, statistics, and rule-observation relations.
-- ---------------------------------------------------------------------------

CREATE UNIQUE INDEX firewall_revision_target_id_uidx
    ON firewall_revisions(target_id,revision_id);
ALTER TABLE firewall_revisions
    ADD CONSTRAINT firewall_revision_target_fk_v21
    FOREIGN KEY(target_id) REFERENCES targets(target_id) ON DELETE RESTRICT;
ALTER TABLE firewall_bindings
    ADD CONSTRAINT firewall_binding_current_target_fk_v21
    FOREIGN KEY(target_id,current_revision_id)
    REFERENCES firewall_revisions(target_id,revision_id) ON DELETE RESTRICT,
    ADD CONSTRAINT firewall_binding_previous_target_fk_v21
    FOREIGN KEY(target_id,previous_revision_id)
    REFERENCES firewall_revisions(target_id,revision_id) ON DELETE RESTRICT;
ALTER TABLE firewall_overlays
    ADD CONSTRAINT firewall_overlay_target_fk_v21
    FOREIGN KEY(target_id) REFERENCES targets(target_id) ON DELETE RESTRICT;
ALTER TABLE firewall_activations
    ADD CONSTRAINT firewall_activation_target_fk_v21
    FOREIGN KEY(target_id) REFERENCES targets(target_id) ON DELETE RESTRICT,
    ADD CONSTRAINT firewall_activation_desired_target_fk_v21
    FOREIGN KEY(target_id,desired_revision_id)
    REFERENCES firewall_revisions(target_id,revision_id) ON DELETE RESTRICT,
    ADD CONSTRAINT firewall_activation_expected_target_fk_v21
    FOREIGN KEY(target_id,expected_current_revision_id)
    REFERENCES firewall_revisions(target_id,revision_id) ON DELETE RESTRICT;

CREATE UNIQUE INDEX effect_intent_parent_kind_uidx
    ON effect_intents(effect_intent_id,is_fleet_parent);
ALTER TABLE fleet_operations
    ADD COLUMN parent_is_fleet_parent BOOLEAN NOT NULL DEFAULT true CHECK(parent_is_fleet_parent),
    ADD CONSTRAINT fleet_parent_exact_kind_fk_v21
    FOREIGN KEY(parent_intent_id,parent_is_fleet_parent)
    REFERENCES effect_intents(effect_intent_id,is_fleet_parent) ON DELETE RESTRICT;
ALTER TABLE fleet_child_intents
    ADD CONSTRAINT fleet_child_target_fk_v21
    FOREIGN KEY(target_id) REFERENCES targets(target_id) ON DELETE RESTRICT;

CREATE UNIQUE INDEX plugin_manifest_exact_identity_uidx
    ON plugin_manifests(manifest_id,manifest_revision,manifest_digest,plugin_id);
ALTER TABLE plugin_qualifications
    ADD CONSTRAINT plugin_qualification_exact_manifest_fk_v21
    FOREIGN KEY(manifest_id,manifest_revision,manifest_digest,plugin_id)
    REFERENCES plugin_manifests(manifest_id,manifest_revision,manifest_digest,plugin_id)
    ON DELETE RESTRICT;
ALTER TABLE plugin_bindings
    ADD CONSTRAINT plugin_binding_exact_manifest_fk_v21
    FOREIGN KEY(manifest_id,manifest_revision,manifest_digest,plugin_id)
    REFERENCES plugin_manifests(manifest_id,manifest_revision,manifest_digest,plugin_id)
    ON DELETE RESTRICT;

CREATE UNIQUE INDEX plugin_qualification_exact_identity_uidx
    ON plugin_qualifications(
        qualification_id,plugin_id,manifest_id,manifest_revision,manifest_digest,
        qualification_digest
    );
ALTER TABLE plugin_statistics_definitions
    ADD CONSTRAINT plugin_statistics_definition_manifest_fk_v21
    FOREIGN KEY(manifest_id,manifest_revision,manifest_digest,plugin_id)
    REFERENCES plugin_manifests(manifest_id,manifest_revision,manifest_digest,plugin_id)
    ON DELETE RESTRICT,
    ADD CONSTRAINT plugin_statistics_definition_qualification_fk_v21
    FOREIGN KEY(
        qualification_id,plugin_id,manifest_id,manifest_revision,manifest_digest,
        qualification_digest
    ) REFERENCES plugin_qualifications(
        qualification_id,plugin_id,manifest_id,manifest_revision,manifest_digest,
        qualification_digest
    ) ON DELETE RESTRICT;

CREATE UNIQUE INDEX plugin_statistics_definition_exact_uidx
    ON plugin_statistics_definitions(definition_id,definition_digest,binding_generation);
CREATE UNIQUE INDEX plugin_statistics_definition_id_digest_uidx
    ON plugin_statistics_definitions(definition_id,definition_digest);
ALTER TABLE plugin_statistic_schedules
    ADD CONSTRAINT plugin_statistics_schedule_definition_fk_v21
    FOREIGN KEY(definition_id,definition_digest)
    REFERENCES plugin_statistics_definitions(definition_id,definition_digest)
    ON DELETE RESTRICT;
ALTER TABLE plugin_statistic_runs
    ADD CONSTRAINT plugin_statistics_run_definition_fk_v21
    FOREIGN KEY(definition_id,definition_digest,binding_generation)
    REFERENCES plugin_statistics_definitions(definition_id,definition_digest,binding_generation)
    ON DELETE RESTRICT;
CREATE UNIQUE INDEX plugin_statistics_run_exact_uidx
    ON plugin_statistic_runs(run_id,definition_id,definition_digest,binding_generation);
ALTER TABLE plugin_statistic_input_bundles
    ADD COLUMN binding_generation BIGINT;
UPDATE plugin_statistic_input_bundles b
SET binding_generation=r.binding_generation
FROM plugin_statistic_runs r
WHERE r.run_id=b.run_id AND b.binding_generation IS NULL;
ALTER TABLE plugin_statistic_input_bundles ALTER COLUMN binding_generation SET NOT NULL;
ALTER TABLE plugin_statistic_input_bundles
    ADD CONSTRAINT plugin_statistics_input_run_fk_v21
    FOREIGN KEY(run_id,definition_id,definition_digest,binding_generation)
    REFERENCES plugin_statistic_runs(run_id,definition_id,definition_digest,binding_generation)
    ON DELETE RESTRICT;

ALTER TABLE plugin_statistic_artifacts
    ADD CONSTRAINT plugin_statistics_artifact_run_fk_v21
    FOREIGN KEY(run_id,definition_id,definition_digest,binding_generation)
    REFERENCES plugin_statistic_runs(run_id,definition_id,definition_digest,binding_generation)
    ON DELETE RESTRICT;
CREATE UNIQUE INDEX plugin_statistics_artifact_exact_uidx
    ON plugin_statistic_artifacts(artifact_id,run_id,definition_id,binding_generation);
ALTER TABLE plugin_statistics_current
    ADD CONSTRAINT plugin_statistics_current_artifact_fk_v21
    FOREIGN KEY(artifact_id,run_id,definition_id,binding_generation)
    REFERENCES plugin_statistic_artifacts(artifact_id,run_id,definition_id,binding_generation)
    ON DELETE RESTRICT;
ALTER TABLE plugin_statistics_history
    ADD CONSTRAINT plugin_statistics_history_artifact_fk_v21
    FOREIGN KEY(artifact_id,run_id,definition_id,binding_generation)
    REFERENCES plugin_statistic_artifacts(artifact_id,run_id,definition_id,binding_generation)
    ON DELETE RESTRICT;

CREATE FUNCTION masi_reject_nonadvancing_rule_sample()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path=pg_catalog,public
AS $function$
BEGIN
    IF NEW.sample_sequence<=OLD.sample_sequence THEN
        RAISE EXCEPTION 'rule sample sequence must advance'
            USING ERRCODE='23514';
    END IF;
    IF NEW.read_completed_at_unix_ms<OLD.read_completed_at_unix_ms THEN
        RAISE EXCEPTION 'rule sample completion time cannot move backward'
            USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER rule_observation_sequence_monotonic_v21
BEFORE UPDATE OF sample_sequence,read_completed_at_unix_ms ON rule_observations
FOR EACH ROW EXECUTE FUNCTION masi_reject_nonadvancing_rule_sample();

-- ---------------------------------------------------------------------------
-- Legal holds, bounded retention, and ahead-of-time partition maintenance.
-- ---------------------------------------------------------------------------

CREATE TABLE retention_holds (
    hold_id TEXT PRIMARY KEY,
    fact_domain TEXT NOT NULL CHECK(fact_domain IN
        ('events','rule-rollups','plugin-statistics','effect-audit','all')),
    scope TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    actor_ref TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK(expires_at IS NULL OR expires_at>starts_at)
);
CREATE INDEX retention_holds_active_idx
    ON retention_holds(fact_domain,scope,starts_at,expires_at);

CREATE OR REPLACE FUNCTION masi_sweep_event_retention(
    retention_cutoff TIMESTAMPTZ,batch_limit INTEGER
)
RETURNS TABLE(events_deleted BIGINT,identities_deleted BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=pg_catalog,public
AS $function$
BEGIN
    IF retention_cutoff IS NULL OR batch_limit<1 OR batch_limit>1000
       OR retention_cutoff>clock_timestamp()
       OR retention_cutoff<clock_timestamp()-interval '365 days' THEN
        RAISE EXCEPTION 'retention arguments outside bounds' USING ERRCODE='22023';
    END IF;
    DELETE FROM public.events e USING (
        SELECT tableoid,ctid FROM public.events candidate
        WHERE candidate.event_time<retention_cutoff
          AND NOT EXISTS(
              SELECT 1 FROM public.retention_holds h
              WHERE h.fact_domain IN ('events','all')
                AND h.starts_at<=clock_timestamp()
                AND (h.expires_at IS NULL OR h.expires_at>clock_timestamp())
                AND h.scope IN ('*',candidate.scope)
          )
        ORDER BY candidate.event_time LIMIT batch_limit
    ) expired
    WHERE e.tableoid=expired.tableoid AND e.ctid=expired.ctid;
    GET DIAGNOSTICS events_deleted=ROW_COUNT;

    DELETE FROM public.event_identities ei WHERE ei.ctid IN (
        SELECT x.ctid FROM public.event_identities x
        WHERE x.event_time<retention_cutoff
          AND NOT EXISTS(SELECT 1 FROM public.events e WHERE e.event_id=x.event_id)
          AND NOT EXISTS(SELECT 1 FROM public.incidents i
                         WHERE i.first_event_id=x.event_id OR i.last_event_id=x.event_id)
          AND NOT EXISTS(SELECT 1 FROM public.incident_projection_events p
                         WHERE p.event_id=x.event_id)
          AND NOT EXISTS(
              SELECT 1 FROM public.retention_holds h
              WHERE h.fact_domain IN ('events','all')
                AND h.starts_at<=clock_timestamp()
                AND (h.expires_at IS NULL OR h.expires_at>clock_timestamp())
          )
        ORDER BY x.event_time LIMIT batch_limit
    );
    GET DIAGNOSTICS identities_deleted=ROW_COUNT;
    RETURN NEXT;
END
$function$;

CREATE OR REPLACE FUNCTION masi_sweep_plugin_statistics_retention(
    retention_cutoff TIMESTAMPTZ,batch_limit INTEGER
)
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=pg_catalog,public
AS $function$
DECLARE deleted BIGINT:=0; affected BIGINT;
BEGIN
    IF retention_cutoff IS NULL OR batch_limit<1 OR batch_limit>1000
       OR retention_cutoff>clock_timestamp()
       OR retention_cutoff<clock_timestamp()-interval '365 days' THEN
        RAISE EXCEPTION 'retention arguments outside bounds' USING ERRCODE='22023';
    END IF;
    DELETE FROM public.plugin_statistics_history h USING (
        SELECT tableoid,ctid FROM public.plugin_statistics_history candidate
        WHERE candidate.recorded_at<retention_cutoff
          AND NOT EXISTS(
              SELECT 1 FROM public.retention_holds hold
              WHERE hold.fact_domain IN ('plugin-statistics','all')
                AND hold.starts_at<=clock_timestamp()
                AND (hold.expires_at IS NULL OR hold.expires_at>clock_timestamp())
                AND hold.scope IN ('*',candidate.scope)
          )
        ORDER BY candidate.recorded_at LIMIT batch_limit
    ) expired WHERE h.tableoid=expired.tableoid AND h.ctid=expired.ctid;
    GET DIAGNOSTICS affected=ROW_COUNT; deleted:=deleted+affected;

    DELETE FROM public.plugin_statistic_input_bundles b WHERE b.ctid IN (
        SELECT x.ctid FROM public.plugin_statistic_input_bundles x
        JOIN public.plugin_statistic_runs r ON r.run_id=x.run_id
        WHERE r.status IN ('succeeded','failed','cancelled','expired','fenced')
          AND r.created_at<retention_cutoff
          AND NOT EXISTS(
              SELECT 1 FROM public.retention_holds hold
              WHERE hold.fact_domain IN ('plugin-statistics','all')
                AND hold.starts_at<=clock_timestamp()
                AND (hold.expires_at IS NULL OR hold.expires_at>clock_timestamp())
                AND hold.scope IN ('*',r.scope)
          )
        ORDER BY r.created_at LIMIT batch_limit
    );
    GET DIAGNOSTICS affected=ROW_COUNT; deleted:=deleted+affected;

    DELETE FROM public.plugin_statistic_artifacts a WHERE a.ctid IN (
        SELECT x.ctid FROM public.plugin_statistic_artifacts x
        JOIN public.plugin_statistic_runs r ON r.run_id=x.run_id
        WHERE x.created_at<retention_cutoff
          AND NOT EXISTS(SELECT 1 FROM public.plugin_statistics_current c
                         WHERE c.artifact_id=x.artifact_id)
          AND NOT EXISTS(SELECT 1 FROM public.plugin_statistics_history h
                         WHERE h.artifact_id=x.artifact_id)
          AND NOT EXISTS(
              SELECT 1 FROM public.retention_holds hold
              WHERE hold.fact_domain IN ('plugin-statistics','all')
                AND hold.starts_at<=clock_timestamp()
                AND (hold.expires_at IS NULL OR hold.expires_at>clock_timestamp())
                AND hold.scope IN ('*',r.scope)
          )
        ORDER BY x.created_at LIMIT batch_limit
    );
    GET DIAGNOSTICS affected=ROW_COUNT; deleted:=deleted+affected;

    DELETE FROM public.plugin_statistic_runs r WHERE r.ctid IN (
        SELECT x.ctid FROM public.plugin_statistic_runs x
        WHERE x.created_at<retention_cutoff
          AND x.status IN ('succeeded','failed','cancelled','expired','fenced')
          AND NOT EXISTS(SELECT 1 FROM public.plugin_statistic_artifacts a
                         WHERE a.run_id=x.run_id)
          AND NOT EXISTS(SELECT 1 FROM public.plugin_statistics_current c
                         WHERE c.run_id=x.run_id)
          AND NOT EXISTS(SELECT 1 FROM public.plugin_statistics_history h
                         WHERE h.run_id=x.run_id)
          AND NOT EXISTS(
              SELECT 1 FROM public.retention_holds hold
              WHERE hold.fact_domain IN ('plugin-statistics','all')
                AND hold.starts_at<=clock_timestamp()
                AND (hold.expires_at IS NULL OR hold.expires_at>clock_timestamp())
                AND hold.scope IN ('*',x.scope)
          )
        ORDER BY x.created_at LIMIT batch_limit
    );
    GET DIAGNOSTICS affected=ROW_COUNT; deleted:=deleted+affected;
    RETURN deleted;
END
$function$;

CREATE FUNCTION masi_sweep_rule_rollup_retention(
    cutoff_5m_unix_ms BIGINT,cutoff_1h_unix_ms BIGINT,batch_limit INTEGER
)
RETURNS TABLE(rollups_5m_deleted BIGINT,rollups_1h_deleted BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=pg_catalog,public
AS $function$
BEGIN
    IF cutoff_5m_unix_ms IS NULL OR cutoff_5m_unix_ms<1
       OR cutoff_1h_unix_ms IS NULL OR cutoff_1h_unix_ms<1
       OR cutoff_5m_unix_ms>(extract(epoch FROM clock_timestamp())*1000)::bigint
       OR cutoff_1h_unix_ms>(extract(epoch FROM clock_timestamp())*1000)::bigint
       OR batch_limit<1 OR batch_limit>1000 THEN
        RAISE EXCEPTION 'retention arguments outside bounds' USING ERRCODE='22023';
    END IF;
    IF EXISTS(
        SELECT 1 FROM public.retention_holds h
        WHERE h.fact_domain IN ('rule-rollups','all')
          AND h.starts_at<=clock_timestamp()
          AND (h.expires_at IS NULL OR h.expires_at>clock_timestamp())
    ) THEN
        rollups_5m_deleted:=0; rollups_1h_deleted:=0; RETURN NEXT; RETURN;
    END IF;
    DELETE FROM public.rule_rollups_5m r USING (
        SELECT tableoid,ctid FROM public.rule_rollups_5m
        WHERE window_start_unix_ms<cutoff_5m_unix_ms
        ORDER BY window_start_unix_ms LIMIT batch_limit
    ) expired WHERE r.tableoid=expired.tableoid AND r.ctid=expired.ctid;
    GET DIAGNOSTICS rollups_5m_deleted=ROW_COUNT;
    DELETE FROM public.rule_rollups_1h r USING (
        SELECT tableoid,ctid FROM public.rule_rollups_1h
        WHERE window_start_unix_ms<cutoff_1h_unix_ms
        ORDER BY window_start_unix_ms LIMIT batch_limit
    ) expired WHERE r.tableoid=expired.tableoid AND r.ctid=expired.ctid;
    GET DIAGNOSTICS rollups_1h_deleted=ROW_COUNT;
    RETURN NEXT;
END
$function$;

-- Convert the original MAXVALUE catch-all partitions into DEFAULT partitions.
-- The original children begin after every explicit monthly partition, so this
-- is metadata-only for valid schemas and retains any future rows in quarantine.
DO $default_partitions$
DECLARE parent_name TEXT; child_name TEXT;
BEGIN
    FOREACH parent_name IN ARRAY ARRAY[
        'events','rule_rollups_5m','rule_rollups_1h','plugin_statistics_history'
    ] LOOP
        SELECT c.relname INTO child_name
        FROM pg_catalog.pg_inherits i
        JOIN pg_catalog.pg_class p ON p.oid=i.inhparent
        JOIN pg_catalog.pg_class c ON c.oid=i.inhrelid
        WHERE p.relname=parent_name
          AND pg_get_expr(c.relpartbound,c.oid) LIKE '%MAXVALUE%'
        LIMIT 1;
        IF child_name IS NOT NULL THEN
            EXECUTE format('ALTER TABLE public.%I DETACH PARTITION public.%I',parent_name,child_name);
            EXECUTE format('ALTER TABLE public.%I ATTACH PARTITION public.%I DEFAULT',parent_name,child_name);
        END IF;
        child_name:=NULL;
    END LOOP;
END
$default_partitions$;

CREATE FUNCTION masi_ensure_time_partitions(anchor_month DATE,months_ahead INTEGER)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=pg_catalog,public
AS $function$
DECLARE month_start DATE; next_month DATE; start_ms BIGINT; end_ms BIGINT;
        suffix TEXT; created INTEGER:=0; relation_name TEXT;
BEGIN
    IF anchor_month IS NULL OR anchor_month<>date_trunc('month',anchor_month)::date
       OR months_ahead<1 OR months_ahead>60 THEN
        RAISE EXCEPTION 'partition maintenance arguments outside bounds'
            USING ERRCODE='22023';
    END IF;
    FOR offset_index IN 0..months_ahead LOOP
        month_start:=(anchor_month+(offset_index||' months')::interval)::date;
        next_month:=(month_start+interval '1 month')::date;
        suffix:=to_char(month_start,'YYYYMM');
        relation_name:='events_'||suffix;
        IF to_regclass('public.'||relation_name) IS NULL THEN
            EXECUTE format('CREATE TABLE public.%I PARTITION OF public.events FOR VALUES FROM (%L) TO (%L)',
                           relation_name,month_start,next_month);
            created:=created+1;
        END IF;
        start_ms:=(extract(epoch FROM month_start::timestamptz)*1000)::bigint;
        end_ms:=(extract(epoch FROM next_month::timestamptz)*1000)::bigint;
        FOREACH relation_name IN ARRAY ARRAY['rule_rollups_5m','rule_rollups_1h'] LOOP
            relation_name:=relation_name||'_'||suffix;
            IF to_regclass('public.'||relation_name) IS NULL THEN
                EXECUTE format('CREATE TABLE public.%I PARTITION OF public.%I FOR VALUES FROM (%s) TO (%s)',
                               relation_name,split_part(relation_name,'_'||suffix,1),start_ms,end_ms);
                created:=created+1;
            END IF;
        END LOOP;
        relation_name:='plugin_statistics_history_'||suffix;
        IF to_regclass('public.'||relation_name) IS NULL THEN
            EXECUTE format('CREATE TABLE public.%I PARTITION OF public.plugin_statistics_history FOR VALUES FROM (%L) TO (%L)',
                           relation_name,month_start,next_month);
            created:=created+1;
        END IF;
    END LOOP;
    RETURN created;
END
$function$;

-- ---------------------------------------------------------------------------
-- Recovery audit and atomic model/target incarnation rotation.
-- ---------------------------------------------------------------------------

CREATE TABLE database_recovery_events (
    recovery_id TEXT PRIMARY KEY,
    recovery_kind TEXT NOT NULL CHECK(recovery_kind IN ('pitr','restore','clone','rewind')),
    backup_manifest_digest TEXT NOT NULL
        CHECK(backup_manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
    schema_chain_digest TEXT NOT NULL
        CHECK(schema_chain_digest ~ '^sha256:[0-9a-f]{64}$'),
    source_timeline_id BIGINT NOT NULL CHECK(source_timeline_id>=1),
    restored_timeline_id BIGINT NOT NULL CHECK(restored_timeline_id>=1),
    old_target_incarnation_id TEXT,
    new_target_incarnation_id TEXT NOT NULL REFERENCES target_control_incarnations(incarnation_id),
    old_model_incarnation_id TEXT,
    new_model_incarnation_id TEXT NOT NULL REFERENCES model_control_incarnations(incarnation_id),
    isolated BOOLEAN NOT NULL CHECK(isolated),
    writer_opened BOOLEAN NOT NULL DEFAULT false CHECK(NOT writer_opened),
    actor_ref TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    recovered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE FUNCTION masi_rotate_recovered_incarnations(
    new_target_incarnation TEXT,new_model_incarnation TEXT,
    rotation_source TEXT,backup_digest TEXT,schema_digest TEXT,
    source_timeline BIGINT,restored_timeline BIGINT,
    recovery_identity TEXT,recovery_actor TEXT,recovery_trace TEXT
)
RETURNS TABLE(target_incarnation_id TEXT,model_incarnation_id TEXT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=pg_catalog,public
AS $function$
DECLARE old_target TEXT; old_model TEXT; now_ms BIGINT;
BEGIN
    IF rotation_source NOT IN ('pitr','restore','clone','rewind')
       OR new_target_incarnation IS NULL OR new_model_incarnation IS NULL
       OR new_target_incarnation=new_model_incarnation
       OR backup_digest !~ '^sha256:[0-9a-f]{64}$'
       OR schema_digest !~ '^sha256:[0-9a-f]{64}$'
       OR source_timeline<1 OR restored_timeline<1
       OR recovery_identity IS NULL OR recovery_actor IS NULL OR recovery_trace IS NULL THEN
        RAISE EXCEPTION 'invalid recovery incarnation rotation input'
            USING ERRCODE='22023';
    END IF;
    SELECT active_incarnation_id INTO old_target
      FROM public.target_control_state WHERE singleton FOR UPDATE;
    SELECT active_incarnation_id INTO old_model
      FROM public.model_control_state WHERE singleton FOR UPDATE;
    IF (SELECT writer_enabled FROM public.target_control_state WHERE singleton)
       OR (SELECT writer_enabled FROM public.model_control_state WHERE singleton) THEN
        RAISE EXCEPTION 'writers must be disabled before recovery rotation'
            USING ERRCODE='55000';
    END IF;
    IF EXISTS(SELECT 1 FROM public.target_control_incarnations
              WHERE incarnation_id=new_target_incarnation)
       OR EXISTS(SELECT 1 FROM public.model_control_incarnations
                 WHERE incarnation_id=new_model_incarnation) THEN
        RAISE EXCEPTION 'recovery incarnation identity was already used'
            USING ERRCODE='23505';
    END IF;
    now_ms:=(extract(epoch FROM clock_timestamp())*1000)::bigint;
    INSERT INTO public.target_control_incarnations(
        incarnation_id,source,rotated_at_unix_ms,replaced_incarnation_id,
        backup_manifest_digest,actor_ref,trace_id
    ) VALUES(new_target_incarnation,
             CASE WHEN rotation_source='restore' THEN 'pitr' ELSE rotation_source END,
             now_ms,old_target,backup_digest,recovery_actor,recovery_trace);
    INSERT INTO public.model_control_incarnations(
        incarnation_id,source,rotated_at_unix_ms,replaced_incarnation_id,
        actor_ref,trace_id
    ) VALUES(new_model_incarnation,
             CASE WHEN rotation_source='restore' THEN 'pitr' ELSE rotation_source END,
             now_ms,old_model,recovery_actor,recovery_trace);
    UPDATE public.target_assignments
       SET revoked_at_unix_ms=COALESCE(revoked_at_unix_ms,now_ms)
     WHERE incarnation_id IS DISTINCT FROM new_target_incarnation;
    UPDATE public.target_control_state
       SET active_incarnation_id=new_target_incarnation,writer_enabled=false,updated_at=now()
     WHERE singleton;
    UPDATE public.model_control_state
       SET active_incarnation_id=new_model_incarnation,writer_enabled=false,updated_at=now()
     WHERE singleton;
    UPDATE public.plugin_statistics_current SET quality='stale',updated_at=now();
    INSERT INTO public.database_recovery_events(
        recovery_id,recovery_kind,backup_manifest_digest,schema_chain_digest,
        source_timeline_id,restored_timeline_id,old_target_incarnation_id,
        new_target_incarnation_id,old_model_incarnation_id,new_model_incarnation_id,
        isolated,writer_opened,actor_ref,trace_id
    ) VALUES(recovery_identity,rotation_source,backup_digest,schema_digest,
             source_timeline,restored_timeline,old_target,new_target_incarnation,
             old_model,new_model_incarnation,true,false,recovery_actor,recovery_trace);
    target_incarnation_id:=new_target_incarnation;
    model_incarnation_id:=new_model_incarnation;
    RETURN NEXT;
END
$function$;

-- ---------------------------------------------------------------------------
-- Least-privilege role groups. Login roles and credentials are provisioned by
-- deployment; migrations never embed a password or grant access to Edge,
-- Central Inference, Plugin Host, Web, Offline ML, or general plugins.
-- ---------------------------------------------------------------------------

DO $state_roles$
BEGIN
    IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='masi_analysis_private') THEN
        CREATE ROLE masi_analysis_private NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='masi_backup') THEN
        CREATE ROLE masi_backup NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE REPLICATION;
    END IF;
    IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='masi_monitoring') THEN
        CREATE ROLE masi_monitoring NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END
$state_roles$;

CREATE SCHEMA IF NOT EXISTS analysis_private AUTHORIZATION masi_analysis_private;
REVOKE ALL ON SCHEMA analysis_private FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM masi_analysis_private;
GRANT USAGE,CREATE ON SCHEMA analysis_private TO masi_analysis_private;
GRANT pg_monitor TO masi_monitoring;

REVOKE DELETE ON rule_rollups_5m,rule_rollups_1h FROM masi_control_app;
REVOKE ALL ON FUNCTION masi_sweep_rule_rollup_retention(BIGINT,BIGINT,INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION masi_ensure_time_partitions(DATE,INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION masi_rotate_recovered_incarnations(
    TEXT,TEXT,TEXT,TEXT,TEXT,BIGINT,BIGINT,TEXT,TEXT,TEXT
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION masi_sweep_rule_rollup_retention(BIGINT,BIGINT,INTEGER)
    TO masi_control_app,masi_control_maintenance;
GRANT EXECUTE ON FUNCTION masi_ensure_time_partitions(DATE,INTEGER)
    TO masi_control_maintenance;
GRANT EXECUTE ON FUNCTION masi_rotate_recovered_incarnations(
    TEXT,TEXT,TEXT,TEXT,TEXT,BIGINT,BIGINT,TEXT,TEXT,TEXT
) TO masi_control_maintenance;

GRANT SELECT,INSERT ON retention_holds TO masi_control_app;
GRANT SELECT ON retention_holds,database_recovery_events TO masi_control_readonly;
GRANT SELECT,INSERT ON database_recovery_events,target_control_incarnations
    TO masi_control_maintenance;
GRANT SELECT,UPDATE ON target_control_state,model_control_state
    TO masi_control_maintenance;
GRANT SELECT ON target_control_incarnations,target_control_state
    TO masi_control_app,masi_control_readonly;

REVOKE UPDATE,DELETE ON target_control_incarnations,retention_holds,
    database_recovery_events FROM masi_control_app;
REVOKE INSERT,UPDATE,DELETE ON database_recovery_events FROM masi_control_app;
DO $history_privileges_v21$
BEGIN
    IF to_regclass('public.masi_migration_history') IS NOT NULL THEN
        REVOKE INSERT,UPDATE,DELETE ON masi_migration_history FROM masi_control_app;
    END IF;
    IF to_regclass('public.masi_test_migration_history') IS NOT NULL THEN
        REVOKE INSERT,UPDATE,DELETE ON masi_test_migration_history FROM masi_control_app;
    END IF;
END
$history_privileges_v21$;

UPDATE masi_schema_meta
SET value='21',applied_at=now(),checksum='computed-by-migration-runner',
    source_digest='db/migrations/0029_postgresql_state_hardening.sql'
WHERE key='version';

COMMIT;
