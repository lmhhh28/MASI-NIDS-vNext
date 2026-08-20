-- Go Control Core review hardening (schema v18).
-- Requirement IDs: DB-GOV-001, DB-MODEL-001, DB-FW-001,
-- DB-TARGET-FLEET-001, DB-RULE-001, SEC-001, REL-003.
--
-- This migration is append-only. It closes exact-identity, incarnation, selector
-- CAS, fleet-gate, rule-readback, least-privilege, and retention gaps discovered
-- after the first Control Core operational evidence run.

BEGIN;

-- An intent must bind the exact proposal digest and an exact Decision belonging
-- to that proposal. Independent single-column foreign keys are insufficient.
ALTER TABLE effect_intents ADD COLUMN proposal_digest TEXT;
UPDATE effect_intents i
SET proposal_digest = p.proposal_digest
FROM effect_proposals p
WHERE p.proposal_id=i.proposal_id AND i.proposal_digest IS NULL;
DO $intent_binding_check$
BEGIN
    IF EXISTS (
        SELECT 1 FROM effect_intents i
        JOIN effect_decisions d ON d.decision_id=i.decision_id
        WHERE i.proposal_digest IS NULL OR d.proposal_id<>i.proposal_id
           OR d.proposal_digest<>i.proposal_digest
    ) THEN
        RAISE EXCEPTION 'effect intent proposal/decision binding is inconsistent';
    END IF;
END
$intent_binding_check$;
ALTER TABLE effect_intents ALTER COLUMN proposal_digest SET NOT NULL;
CREATE UNIQUE INDEX effect_decisions_exact_binding_uidx
    ON effect_decisions(decision_id,proposal_id,proposal_digest);
ALTER TABLE effect_intents ADD CONSTRAINT effect_intents_exact_proposal_fk
    FOREIGN KEY(proposal_id,proposal_digest)
    REFERENCES effect_proposals(proposal_id,proposal_digest);
ALTER TABLE effect_intents ADD CONSTRAINT effect_intents_exact_decision_fk
    FOREIGN KEY(decision_id,proposal_id,proposal_digest)
    REFERENCES effect_decisions(decision_id,proposal_id,proposal_digest);

-- Attempt numbers are an append-only sequence per intent. Concurrent finalize /
-- reconcile paths cannot create the same logical attempt twice.
ALTER TABLE effect_attempts ADD CONSTRAINT effect_attempts_intent_number_uidx
    UNIQUE(intent_id,attempt_number);

-- Per-entry exact readback manifest. Epoch creation is permitted only from one
-- of these Edge-proven entries, never from caller-supplied rule/digest fields.
ALTER TABLE effect_attempts
    ADD COLUMN readback_manifest_digest TEXT,
    ADD COLUMN readback_entry_count INTEGER NOT NULL DEFAULT 0
        CHECK(readback_entry_count BETWEEN 0 AND 4096);
ALTER TABLE effect_attempts ADD CONSTRAINT effect_attempts_manifest_digest_check
    CHECK(readback_manifest_digest IS NULL OR
          readback_manifest_digest ~ '^sha256:[0-9a-f]{64}$');
CREATE TABLE effect_attempt_readback_entries (
    attempt_id TEXT NOT NULL REFERENCES effect_attempts(attempt_id) ON DELETE RESTRICT,
    intent_id TEXT NOT NULL REFERENCES effect_intents(effect_intent_id) ON DELETE RESTRICT,
    operation_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    entry_index INTEGER NOT NULL CHECK(entry_index BETWEEN 0 AND 4095),
    entity_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    canonical_entry_digest TEXT NOT NULL
        CHECK(canonical_entry_digest ~ '^sha256:[0-9a-f]{64}$'),
    match_priority_action_digest TEXT NOT NULL
        CHECK(match_priority_action_digest ~ '^sha256:[0-9a-f]{64}$'),
    table_id BIGINT NOT NULL CHECK(table_id BETWEEN 1 AND 4294967295),
    direct_counter_id BIGINT NOT NULL CHECK(direct_counter_id BETWEEN 1 AND 4294967295),
    bank INTEGER NOT NULL CHECK(bank BETWEEN 0 AND 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(attempt_id,entity_id,rule_id),
    UNIQUE(attempt_id,canonical_entry_digest),
    UNIQUE(attempt_id,entry_index)
);
CREATE INDEX effect_attempt_readback_entries_identity_idx
    ON effect_attempt_readback_entries(target_id,operation_id,entity_id,rule_id);

-- One versioned selector binding and at most one open baseline operation per
-- target. The activation row freezes the exact expected selector/current fact.
ALTER TABLE firewall_bindings ADD COLUMN binding_version BIGINT NOT NULL DEFAULT 1
    CHECK(binding_version >= 1);
ALTER TABLE firewall_activations
    ADD COLUMN expected_binding_version BIGINT NOT NULL DEFAULT 0
        CHECK(expected_binding_version >= 0),
    ADD COLUMN expected_current_revision_id TEXT REFERENCES firewall_revisions(revision_id),
    ADD COLUMN expected_active_bank INTEGER NOT NULL DEFAULT 0
        CHECK(expected_active_bank BETWEEN 0 AND 1),
    ADD COLUMN expected_cas_digest TEXT NOT NULL
        DEFAULT 'sha256:0000000000000000000000000000000000000000000000000000000000000000'
        CHECK(expected_cas_digest ~ '^sha256:[0-9a-f]{64}$');
UPDATE firewall_activations a
SET expected_binding_version=b.binding_version,
    expected_current_revision_id=b.current_revision_id,
    expected_active_bank=b.active_bank,
    expected_cas_digest=b.cas_digest
FROM firewall_bindings b
WHERE b.target_id=a.target_id AND a.result IN ('prepared','reconciling');
CREATE UNIQUE INDEX firewall_one_open_activation_per_target_uidx
    ON firewall_activations(target_id)
    WHERE result IN ('prepared','reconciling');

-- Fleet operations persist one canonical frozen-vector digest. Manual wave
-- gates append an exact Decision binding the completed vector.
ALTER TABLE fleet_operations
    ADD COLUMN operation_digest TEXT NOT NULL
        DEFAULT 'sha256:0000000000000000000000000000000000000000000000000000000000000000'
        CHECK(operation_digest ~ '^sha256:[0-9a-f]{64}$'),
    ADD COLUMN completed_vector_digest TEXT NOT NULL
        DEFAULT 'sha256:0000000000000000000000000000000000000000000000000000000000000000'
        CHECK(completed_vector_digest ~ '^sha256:[0-9a-f]{64}$');
ALTER TABLE fleet_operations ADD CONSTRAINT fleet_operations_parent_intent_fk
    FOREIGN KEY(parent_intent_id) REFERENCES effect_intents(effect_intent_id);
CREATE UNIQUE INDEX effect_intents_fleet_exact_mapping_uidx
    ON effect_intents(fleet_operation_id,target_id,effect_digest,effect_intent_id);
ALTER TABLE fleet_child_intents ADD CONSTRAINT fleet_child_exact_intent_fk
    FOREIGN KEY(fleet_operation_id,target_id,effect_digest,child_intent_id)
    REFERENCES effect_intents(fleet_operation_id,target_id,effect_digest,effect_intent_id);
CREATE TABLE fleet_wave_gate_decisions (
    fleet_operation_id TEXT NOT NULL REFERENCES fleet_operations(fleet_operation_id),
    current_wave INTEGER NOT NULL CHECK(current_wave BETWEEN 0 AND 63),
    next_wave INTEGER NOT NULL CHECK(next_wave=current_wave+1 AND next_wave BETWEEN 1 AND 63),
    completed_vector_digest TEXT NOT NULL
        CHECK(completed_vector_digest ~ '^sha256:[0-9a-f]{64}$'),
    proposal_id TEXT NOT NULL REFERENCES effect_proposals(proposal_id),
    decision_id TEXT NOT NULL REFERENCES effect_decisions(decision_id),
    actor_ref TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    created_at_unix_ms BIGINT NOT NULL CHECK(created_at_unix_ms >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(fleet_operation_id,current_wave,next_wave),
    UNIQUE(decision_id)
);

-- Pool-generation identity includes model-control incarnation. The migration
-- refuses to guess if existing rows cannot be mapped unambiguously.
ALTER TABLE pool_generations ADD COLUMN model_control_incarnation_id TEXT;
UPDATE pool_generations pg
SET model_control_incarnation_id=c.incarnation_id
FROM (
    SELECT logical_pool_id,pool_generation,min(model_control_incarnation_id) AS incarnation_id
    FROM model_pool_observation_events
    GROUP BY logical_pool_id,pool_generation
    HAVING count(DISTINCT model_control_incarnation_id)=1
) c
WHERE c.logical_pool_id=pg.logical_pool_id AND c.pool_generation=pg.pool_generation
  AND pg.model_control_incarnation_id IS NULL;
UPDATE pool_generations pg
SET model_control_incarnation_id=c.incarnation_id
FROM (
    SELECT logical_pool_id,generation,min(model_control_incarnation_id) AS incarnation_id
    FROM (
        SELECT logical_pool_id,current_generation AS generation,model_control_incarnation_id
        FROM shard_bindings
        UNION ALL
        SELECT logical_pool_id,previous_generation AS generation,model_control_incarnation_id
        FROM shard_bindings WHERE previous_generation IS NOT NULL
    ) candidates
    GROUP BY logical_pool_id,generation
    HAVING count(DISTINCT model_control_incarnation_id)=1
) c
WHERE c.logical_pool_id=pg.logical_pool_id AND c.generation=pg.pool_generation
  AND pg.model_control_incarnation_id IS NULL;
UPDATE pool_generations pg
SET model_control_incarnation_id=mcs.active_incarnation_id
FROM model_control_state mcs
WHERE mcs.singleton AND mcs.active_incarnation_id IS NOT NULL
  AND pg.model_control_incarnation_id IS NULL
  AND (SELECT count(*) FROM model_control_incarnations)=1;
DO $pool_incarnation_check$
BEGIN
    IF EXISTS(SELECT 1 FROM pool_generations WHERE model_control_incarnation_id IS NULL) THEN
        RAISE EXCEPTION 'pool generation incarnation cannot be inferred uniquely';
    END IF;
    IF EXISTS(
        SELECT 1 FROM model_pool_observation_events e
        JOIN pool_generations pg USING(logical_pool_id,pool_generation)
        WHERE e.model_control_incarnation_id<>pg.model_control_incarnation_id
    ) THEN
        RAISE EXCEPTION 'pool observation incarnation conflicts with pool generation';
    END IF;
END
$pool_incarnation_check$;
ALTER TABLE pool_generations ALTER COLUMN model_control_incarnation_id SET NOT NULL;
ALTER TABLE pool_generations ADD CONSTRAINT pool_generations_incarnation_fk
    FOREIGN KEY(model_control_incarnation_id)
    REFERENCES model_control_incarnations(incarnation_id);

ALTER TABLE model_pool_observations_current ADD COLUMN model_control_incarnation_id TEXT;
UPDATE model_pool_observations_current c
SET model_control_incarnation_id=e.model_control_incarnation_id
FROM model_pool_observation_events e
WHERE e.observation_id=c.observation_id;
DO $pool_current_incarnation_check$
BEGIN
    IF EXISTS(SELECT 1 FROM model_pool_observations_current WHERE model_control_incarnation_id IS NULL) THEN
        RAISE EXCEPTION 'current pool observation incarnation is missing';
    END IF;
END
$pool_current_incarnation_check$;
ALTER TABLE model_pool_observations_current
    ALTER COLUMN model_control_incarnation_id SET NOT NULL;

ALTER TABLE shard_bindings DROP CONSTRAINT shard_current_generation_fk;
ALTER TABLE model_pool_observation_events DROP CONSTRAINT model_pool_observation_generation_fk;
ALTER TABLE model_pool_observations_current DROP CONSTRAINT model_pool_current_generation_fk;
ALTER TABLE pool_generations DROP CONSTRAINT pool_generations_pkey;
ALTER TABLE model_pool_observations_current DROP CONSTRAINT model_pool_observations_current_pkey;
ALTER TABLE pool_generations ADD CONSTRAINT pool_generations_pkey
    PRIMARY KEY(logical_pool_id,model_control_incarnation_id,pool_generation);
ALTER TABLE shard_bindings ADD CONSTRAINT shard_current_generation_incarnation_fk
    FOREIGN KEY(logical_pool_id,model_control_incarnation_id,current_generation)
    REFERENCES pool_generations(logical_pool_id,model_control_incarnation_id,pool_generation);
ALTER TABLE model_pool_observation_events ADD CONSTRAINT model_pool_observation_incarnation_fk
    FOREIGN KEY(logical_pool_id,model_control_incarnation_id,pool_generation)
    REFERENCES pool_generations(logical_pool_id,model_control_incarnation_id,pool_generation);
CREATE UNIQUE INDEX model_pool_observation_event_incarnation_uidx
    ON model_pool_observation_events(observation_id,model_control_incarnation_id);
ALTER TABLE model_pool_observations_current ADD CONSTRAINT model_pool_observations_current_pkey
    PRIMARY KEY(logical_pool_id,model_control_incarnation_id,pool_generation);
ALTER TABLE model_pool_observations_current ADD CONSTRAINT model_pool_current_incarnation_fk
    FOREIGN KEY(logical_pool_id,model_control_incarnation_id,pool_generation)
    REFERENCES pool_generations(logical_pool_id,model_control_incarnation_id,pool_generation);
ALTER TABLE model_pool_observations_current ADD CONSTRAINT model_pool_current_event_incarnation_fk
    FOREIGN KEY(observation_id,model_control_incarnation_id)
    REFERENCES model_pool_observation_events(observation_id,model_control_incarnation_id);

ALTER TABLE model_control_incarnations ADD CONSTRAINT model_control_incarnation_not_self
    CHECK(replaced_incarnation_id IS NULL OR replaced_incarnation_id<>incarnation_id);
ALTER TABLE model_control_incarnations ADD CONSTRAINT model_control_incarnation_replacement_fk
    FOREIGN KEY(replaced_incarnation_id)
    REFERENCES model_control_incarnations(incarnation_id) ON DELETE RESTRICT;

-- Enforce Event identity/digest agreement without adding a reverse FK that
-- would obstruct bounded Event-before-identity retention.
CREATE FUNCTION masi_validate_event_identity_match()
RETURNS TRIGGER LANGUAGE plpgsql SET search_path=pg_catalog,public AS $function$
BEGIN
    IF NOT EXISTS(
        SELECT 1 FROM public.event_identities i
        WHERE i.event_id=NEW.event_id AND i.event_time=NEW.event_time
          AND i.input_digest=NEW.input_digest AND i.output_digest=NEW.output_digest
    ) THEN
        RAISE EXCEPTION 'event identity/digest mismatch' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END
$function$;
CREATE CONSTRAINT TRIGGER events_identity_digest_match
AFTER INSERT OR UPDATE OF event_id,event_time,input_digest,output_digest ON events
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION masi_validate_event_identity_match();

-- New tables default to append+read only. Mutable projections must receive an
-- explicit UPDATE grant in the migration that creates them.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE UPDATE ON TABLES FROM masi_control_app;

-- Retention functions reject future cutoffs and cutoffs beyond the frozen
-- maximum retention window. The online role cannot turn them into arbitrary
-- canonical deletion primitives.
CREATE OR REPLACE FUNCTION masi_sweep_event_retention(retention_cutoff TIMESTAMPTZ, batch_limit INTEGER)
RETURNS TABLE(events_deleted BIGINT, identities_deleted BIGINT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $function$
BEGIN
    IF retention_cutoff IS NULL OR batch_limit < 1 OR batch_limit > 1000
       OR retention_cutoff > clock_timestamp()
       OR retention_cutoff < clock_timestamp()-interval '365 days' THEN
        RAISE EXCEPTION 'retention arguments outside bounds' USING ERRCODE='22023';
    END IF;
    DELETE FROM public.events e USING (
        SELECT tableoid,ctid FROM public.events
        WHERE event_time<retention_cutoff ORDER BY event_time LIMIT batch_limit
    ) expired
    WHERE e.tableoid=expired.tableoid AND e.ctid=expired.ctid;
    GET DIAGNOSTICS events_deleted = ROW_COUNT;
    DELETE FROM public.event_identities ei WHERE ei.ctid IN (
        SELECT x.ctid FROM public.event_identities x
        WHERE x.event_time<retention_cutoff
          AND NOT EXISTS(SELECT 1 FROM public.events e WHERE e.event_id=x.event_id)
          AND NOT EXISTS(SELECT 1 FROM public.incidents i WHERE i.first_event_id=x.event_id OR i.last_event_id=x.event_id)
          AND NOT EXISTS(SELECT 1 FROM public.incident_projection_events p WHERE p.event_id=x.event_id)
        ORDER BY x.event_time LIMIT batch_limit
    );
    GET DIAGNOSTICS identities_deleted = ROW_COUNT;
    RETURN NEXT;
END
$function$;

CREATE OR REPLACE FUNCTION masi_sweep_plugin_statistics_retention(retention_cutoff TIMESTAMPTZ, batch_limit INTEGER)
RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $function$
DECLARE deleted BIGINT := 0; affected BIGINT;
BEGIN
    IF retention_cutoff IS NULL OR batch_limit < 1 OR batch_limit > 1000
       OR retention_cutoff > clock_timestamp()
       OR retention_cutoff < clock_timestamp()-interval '365 days' THEN
        RAISE EXCEPTION 'retention arguments outside bounds' USING ERRCODE='22023';
    END IF;
    DELETE FROM public.plugin_statistics_history h USING (
        SELECT tableoid,ctid FROM public.plugin_statistics_history
        WHERE recorded_at<retention_cutoff ORDER BY recorded_at LIMIT batch_limit
    ) expired WHERE h.tableoid=expired.tableoid AND h.ctid=expired.ctid;
    GET DIAGNOSTICS affected=ROW_COUNT; deleted:=deleted+affected;
    DELETE FROM public.plugin_statistic_input_bundles b WHERE b.ctid IN (
        SELECT x.ctid FROM public.plugin_statistic_input_bundles x
        JOIN public.plugin_statistic_runs r ON r.run_id=x.run_id
        WHERE r.status IN ('succeeded','failed','cancelled','expired','fenced')
          AND r.created_at<retention_cutoff ORDER BY r.created_at LIMIT batch_limit
    );
    GET DIAGNOSTICS affected=ROW_COUNT; deleted:=deleted+affected;
    DELETE FROM public.plugin_statistic_artifacts a WHERE a.ctid IN (
        SELECT x.ctid FROM public.plugin_statistic_artifacts x
        WHERE x.created_at<retention_cutoff
          AND NOT EXISTS(SELECT 1 FROM public.plugin_statistics_current c WHERE c.artifact_id=x.artifact_id)
          AND NOT EXISTS(SELECT 1 FROM public.plugin_statistics_history h WHERE h.artifact_id=x.artifact_id)
        ORDER BY x.created_at LIMIT batch_limit
    );
    GET DIAGNOSTICS affected=ROW_COUNT; deleted:=deleted+affected;
    DELETE FROM public.plugin_statistic_runs r WHERE r.ctid IN (
        SELECT x.ctid FROM public.plugin_statistic_runs x
        WHERE x.created_at<retention_cutoff
          AND x.status IN ('succeeded','failed','cancelled','expired','fenced')
          AND NOT EXISTS(SELECT 1 FROM public.plugin_statistic_artifacts a WHERE a.run_id=x.run_id)
          AND NOT EXISTS(SELECT 1 FROM public.plugin_statistics_current c WHERE c.run_id=x.run_id)
          AND NOT EXISTS(SELECT 1 FROM public.plugin_statistics_history h WHERE h.run_id=x.run_id)
        ORDER BY x.created_at LIMIT batch_limit
    );
    GET DIAGNOSTICS affected=ROW_COUNT; deleted:=deleted+affected;
    RETURN deleted;
END
$function$;

REVOKE ALL ON effect_attempt_readback_entries,fleet_wave_gate_decisions FROM PUBLIC;
GRANT SELECT,INSERT ON effect_attempt_readback_entries,fleet_wave_gate_decisions TO masi_control_app;
GRANT SELECT ON effect_attempt_readback_entries,fleet_wave_gate_decisions TO masi_control_readonly;
GRANT UPDATE ON firewall_bindings,firewall_activations,fleet_operations TO masi_control_app;

UPDATE masi_schema_meta
SET value='18',applied_at=now(),checksum='computed-by-migration-runner',
    source_digest='db/migrations/0025_go_control_review_hardening.sql'
WHERE key='version';

COMMIT;
