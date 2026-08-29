BEGIN;

-- A DEFAULT partition can legitimately quarantine future rows. PostgreSQL
-- refuses to add a new explicit range while matching rows remain in DEFAULT,
-- so partition maintenance must move those rows transactionally instead of
-- assuming an empty catch-all.
CREATE FUNCTION masi_create_time_partition(
    parent_name TEXT,
    relation_name TEXT,
    key_name TEXT,
    lower_time TIMESTAMPTZ,
    upper_time TIMESTAMPTZ,
    lower_ms BIGINT,
    upper_ms BIGINT,
    use_milliseconds BOOLEAN
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=pg_catalog,public
AS $function$
DECLARE
    default_child TEXT;
    relation_attached BOOLEAN;
BEGIN
    IF parent_name='events' THEN
        IF key_name<>'event_time' OR use_milliseconds THEN
            RAISE EXCEPTION 'invalid events partition descriptor' USING ERRCODE='22023';
        END IF;
    ELSIF parent_name='plugin_statistics_history' THEN
        IF key_name<>'recorded_at' OR use_milliseconds THEN
            RAISE EXCEPTION 'invalid plugin history partition descriptor' USING ERRCODE='22023';
        END IF;
    ELSIF parent_name IN ('rule_rollups_5m','rule_rollups_1h') THEN
        IF key_name<>'window_start_unix_ms' OR NOT use_milliseconds THEN
            RAISE EXCEPTION 'invalid rule rollup partition descriptor' USING ERRCODE='22023';
        END IF;
    ELSE
        RAISE EXCEPTION 'partition parent is not allowlisted' USING ERRCODE='22023';
    END IF;
    IF relation_name !~ ('^'||parent_name||'_[0-9]{6}$')
       OR lower_time IS NULL OR upper_time IS NULL OR lower_time>=upper_time
       OR lower_ms IS NULL OR upper_ms IS NULL OR lower_ms>=upper_ms THEN
        RAISE EXCEPTION 'partition identity or range is invalid' USING ERRCODE='22023';
    END IF;

    IF to_regclass('public.'||relation_name) IS NOT NULL THEN
        SELECT EXISTS(
            SELECT 1 FROM pg_catalog.pg_inherits i
            JOIN pg_catalog.pg_class p ON p.oid=i.inhparent
            JOIN pg_catalog.pg_class c ON c.oid=i.inhrelid
            JOIN pg_catalog.pg_namespace pn ON pn.oid=p.relnamespace
            JOIN pg_catalog.pg_namespace cn ON cn.oid=c.relnamespace
            WHERE pn.nspname='public' AND cn.nspname='public'
              AND p.relname=parent_name AND c.relname=relation_name
        ) INTO relation_attached;
        IF NOT relation_attached THEN
            RAISE EXCEPTION 'partition relation exists outside expected parent'
                USING ERRCODE='42P07';
        END IF;
        RETURN FALSE;
    END IF;

    SELECT c.relname INTO default_child
    FROM pg_catalog.pg_inherits i
    JOIN pg_catalog.pg_class p ON p.oid=i.inhparent
    JOIN pg_catalog.pg_class c ON c.oid=i.inhrelid
    JOIN pg_catalog.pg_namespace pn ON pn.oid=p.relnamespace
    JOIN pg_catalog.pg_namespace cn ON cn.oid=c.relnamespace
    WHERE pn.nspname='public' AND cn.nspname='public'
      AND p.relname=parent_name
      AND pg_get_expr(c.relpartbound,c.oid)='DEFAULT'
    LIMIT 1;
    IF default_child IS NULL THEN
        RAISE EXCEPTION 'default partition is missing for %',parent_name
            USING ERRCODE='55000';
    END IF;

    -- ALTER TABLE takes the parent lock for the surrounding migration/function
    -- transaction. Any failure rolls back the detach, new child and row move.
    EXECUTE format('ALTER TABLE public.%I DETACH PARTITION public.%I',
                   parent_name,default_child);
    IF use_milliseconds THEN
        EXECUTE format('CREATE TABLE public.%I PARTITION OF public.%I FOR VALUES FROM (%s) TO (%s)',
                       relation_name,parent_name,lower_ms,upper_ms);
        EXECUTE format('INSERT INTO public.%I SELECT * FROM public.%I WHERE %I >= $1 AND %I < $2',
                       parent_name,default_child,key_name,key_name)
            USING lower_ms,upper_ms;
        EXECUTE format('DELETE FROM public.%I WHERE %I >= $1 AND %I < $2',
                       default_child,key_name,key_name)
            USING lower_ms,upper_ms;
    ELSE
        EXECUTE format('CREATE TABLE public.%I PARTITION OF public.%I FOR VALUES FROM (%L) TO (%L)',
                       relation_name,parent_name,lower_time,upper_time);
        EXECUTE format('INSERT INTO public.%I SELECT * FROM public.%I WHERE %I >= $1 AND %I < $2',
                       parent_name,default_child,key_name,key_name)
            USING lower_time,upper_time;
        EXECUTE format('DELETE FROM public.%I WHERE %I >= $1 AND %I < $2',
                       default_child,key_name,key_name)
            USING lower_time,upper_time;
    END IF;
    EXECUTE format('ALTER TABLE public.%I ATTACH PARTITION public.%I DEFAULT',
                   parent_name,default_child);
    RETURN TRUE;
END
$function$;

CREATE OR REPLACE FUNCTION masi_ensure_time_partitions(anchor_month DATE,months_ahead INTEGER)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=pg_catalog,public
AS $function$
DECLARE
    month_start DATE;
    next_month DATE;
    lower_time TIMESTAMPTZ;
    upper_time TIMESTAMPTZ;
    start_ms BIGINT;
    end_ms BIGINT;
    suffix TEXT;
    created INTEGER:=0;
BEGIN
    IF anchor_month IS NULL OR anchor_month<>date_trunc('month',anchor_month)::date
       OR months_ahead<1 OR months_ahead>60 THEN
        RAISE EXCEPTION 'partition maintenance arguments outside bounds'
            USING ERRCODE='22023';
    END IF;
    FOR offset_index IN 0..months_ahead LOOP
        month_start:=(anchor_month+(offset_index||' months')::interval)::date;
        next_month:=(month_start+interval '1 month')::date;
        lower_time:=month_start::timestamp AT TIME ZONE 'UTC';
        upper_time:=next_month::timestamp AT TIME ZONE 'UTC';
        start_ms:=(extract(epoch FROM lower_time)*1000)::bigint;
        end_ms:=(extract(epoch FROM upper_time)*1000)::bigint;
        suffix:=to_char(month_start,'YYYYMM');

        IF masi_create_time_partition('events','events_'||suffix,'event_time',
                                      lower_time,upper_time,start_ms,end_ms,FALSE) THEN
            created:=created+1;
        END IF;
        IF masi_create_time_partition('rule_rollups_5m','rule_rollups_5m_'||suffix,
                                      'window_start_unix_ms',lower_time,upper_time,
                                      start_ms,end_ms,TRUE) THEN
            created:=created+1;
        END IF;
        IF masi_create_time_partition('rule_rollups_1h','rule_rollups_1h_'||suffix,
                                      'window_start_unix_ms',lower_time,upper_time,
                                      start_ms,end_ms,TRUE) THEN
            created:=created+1;
        END IF;
        IF masi_create_time_partition('plugin_statistics_history',
                                      'plugin_statistics_history_'||suffix,'recorded_at',
                                      lower_time,upper_time,start_ms,end_ms,FALSE) THEN
            created:=created+1;
        END IF;
    END LOOP;
    RETURN created;
END
$function$;

REVOKE ALL ON FUNCTION masi_create_time_partition(
    TEXT,TEXT,TEXT,TIMESTAMPTZ,TIMESTAMPTZ,BIGINT,BIGINT,BOOLEAN
) FROM PUBLIC;
REVOKE ALL ON FUNCTION masi_ensure_time_partitions(DATE,INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION masi_ensure_time_partitions(DATE,INTEGER)
    TO masi_control_maintenance;

UPDATE masi_schema_meta
SET value='21',applied_at=now(),checksum='computed-by-migration-runner',
    source_digest='db/migrations/0030_partition_default_drain.sql'
WHERE key='version';

COMMIT;
