-- Least-privilege bounded retention functions (v16).
-- Requirement IDs: DB-004, SEC-001, TEST-GO-CTRL-001.

BEGIN;

CREATE FUNCTION masi_sweep_event_retention(retention_cutoff TIMESTAMPTZ, batch_limit INTEGER)
RETURNS TABLE(events_deleted BIGINT, identities_deleted BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF retention_cutoff IS NULL OR batch_limit < 1 OR batch_limit > 1000 THEN
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

CREATE FUNCTION masi_sweep_plugin_statistics_retention(retention_cutoff TIMESTAMPTZ, batch_limit INTEGER)
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE deleted BIGINT := 0; affected BIGINT;
BEGIN
    IF retention_cutoff IS NULL OR batch_limit < 1 OR batch_limit > 1000 THEN
        RAISE EXCEPTION 'retention arguments outside bounds' USING ERRCODE='22023';
    END IF;
    DELETE FROM public.plugin_statistics_history h USING (
        SELECT tableoid,ctid FROM public.plugin_statistics_history
        WHERE recorded_at<retention_cutoff ORDER BY recorded_at LIMIT batch_limit
    ) expired
    WHERE h.tableoid=expired.tableoid AND h.ctid=expired.ctid;
    GET DIAGNOSTICS affected = ROW_COUNT; deleted := deleted + affected;

    DELETE FROM public.plugin_statistic_input_bundles b WHERE b.ctid IN (
        SELECT x.ctid FROM public.plugin_statistic_input_bundles x
        JOIN public.plugin_statistic_runs r ON r.run_id=x.run_id
        WHERE r.status IN ('succeeded','failed','cancelled','expired','fenced')
          AND r.created_at<retention_cutoff
        ORDER BY r.created_at LIMIT batch_limit
    );
    GET DIAGNOSTICS affected = ROW_COUNT; deleted := deleted + affected;

    DELETE FROM public.plugin_statistic_artifacts a WHERE a.ctid IN (
        SELECT x.ctid FROM public.plugin_statistic_artifacts x
        WHERE x.created_at<retention_cutoff
          AND NOT EXISTS(SELECT 1 FROM public.plugin_statistics_current c WHERE c.artifact_id=x.artifact_id)
          AND NOT EXISTS(SELECT 1 FROM public.plugin_statistics_history h WHERE h.artifact_id=x.artifact_id)
        ORDER BY x.created_at LIMIT batch_limit
    );
    GET DIAGNOSTICS affected = ROW_COUNT; deleted := deleted + affected;

    DELETE FROM public.plugin_statistic_runs r WHERE r.ctid IN (
        SELECT x.ctid FROM public.plugin_statistic_runs x
        WHERE x.created_at<retention_cutoff
          AND x.status IN ('succeeded','failed','cancelled','expired','fenced')
          AND NOT EXISTS(SELECT 1 FROM public.plugin_statistic_artifacts a WHERE a.run_id=x.run_id)
          AND NOT EXISTS(SELECT 1 FROM public.plugin_statistics_current c WHERE c.run_id=x.run_id)
          AND NOT EXISTS(SELECT 1 FROM public.plugin_statistics_history h WHERE h.run_id=x.run_id)
        ORDER BY x.created_at LIMIT batch_limit
    );
    GET DIAGNOSTICS affected = ROW_COUNT; deleted := deleted + affected;
    RETURN deleted;
END
$function$;

REVOKE ALL ON FUNCTION masi_sweep_event_retention(TIMESTAMPTZ,INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION masi_sweep_plugin_statistics_retention(TIMESTAMPTZ,INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION masi_sweep_event_retention(TIMESTAMPTZ,INTEGER) TO masi_control_app;
GRANT EXECUTE ON FUNCTION masi_sweep_plugin_statistics_retention(TIMESTAMPTZ,INTEGER) TO masi_control_app;

UPDATE masi_schema_meta
SET value='16', applied_at=now(), checksum='computed-by-migration-runner',
    source_digest='db/migrations/0023_bounded_retention_functions.sql'
WHERE key='version';

COMMIT;
