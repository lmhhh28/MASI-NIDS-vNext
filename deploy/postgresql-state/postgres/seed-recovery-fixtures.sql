\set ON_ERROR_STOP on

BEGIN;
INSERT INTO target_control_incarnations(
    incarnation_id,source,rotated_at_unix_ms,actor_ref,trace_id
) VALUES('target-incarnation-before-pitr','initial',1,'module-gate','module-recovery-seed');
UPDATE target_control_state
SET active_incarnation_id='target-incarnation-before-pitr',writer_enabled=false,
    updated_at=clock_timestamp()
WHERE singleton;
INSERT INTO model_control_incarnations(
    incarnation_id,source,rotated_at_unix_ms,actor_ref,trace_id
) VALUES('model-incarnation-before-pitr','initial',1,'module-gate','module-recovery-seed');
UPDATE model_control_state
SET active_incarnation_id='model-incarnation-before-pitr',writer_enabled=false,
    updated_at=clock_timestamp()
WHERE singleton;
INSERT INTO retention_holds(
    hold_id,fact_domain,scope,reason_code,starts_at,expires_at,actor_ref,trace_id
) VALUES(
    'module-failover-marker','events','module-failover','streaming-proof',
    clock_timestamp(),clock_timestamp()+interval '8 hours','module-gate','module-failover'
);
COMMIT;
