\set ON_ERROR_STOP on

INSERT INTO retention_holds(
    hold_id,fact_domain,scope,reason_code,starts_at,expires_at,actor_ref,trace_id
) VALUES(
    'module-pitr-keep','events','module-pitr','before-target',clock_timestamp(),
    clock_timestamp()+interval '8 hours','module-gate','module-pitr'
);
SELECT pg_create_restore_point('masi_module_pitr_target');
INSERT INTO retention_holds(
    hold_id,fact_domain,scope,reason_code,starts_at,expires_at,actor_ref,trace_id
) VALUES(
    'module-pitr-drift','events','module-pitr','after-target',clock_timestamp(),
    clock_timestamp()+interval '8 hours','module-gate','module-pitr'
);
SELECT pg_switch_wal();
SELECT pg_switch_wal();
