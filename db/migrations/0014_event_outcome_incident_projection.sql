-- MASI-NIDS-vNext exact inference outcome and rebuildable Incident projection (v7).
-- Requirement IDs: FUNC-EVENT-001, DB-001, CONTRACT-001, MOD-CTRL-001.

BEGIN;

ALTER TABLE events
    ADD COLUMN IF NOT EXISTS result_identity_digest TEXT NOT NULL DEFAULT 'sha256:legacy',
    ADD COLUMN IF NOT EXISTS scores JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS predicted_label BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS decision TEXT NOT NULL DEFAULT 'abstain',
    ADD COLUMN IF NOT EXISTS out_of_distribution BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS abstain BOOLEAN NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS execution_status TEXT NOT NULL DEFAULT 'hold',
    ADD COLUMN IF NOT EXISTS execution_error_code TEXT NOT NULL DEFAULT 'LEGACY_UNOBSERVED',
    ADD COLUMN IF NOT EXISTS worker_id TEXT NOT NULL DEFAULT 'legacy-unobserved',
    ADD COLUMN IF NOT EXISTS worker_digest TEXT NOT NULL DEFAULT 'sha256:legacy',
    ADD COLUMN IF NOT EXISTS worker_attempt_id TEXT NOT NULL DEFAULT 'legacy-unobserved',
    ADD COLUMN IF NOT EXISTS inference_started_at_unix_ms BIGINT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS inference_completed_at_unix_ms BIGINT NOT NULL DEFAULT 1;

ALTER TABLE events
    ADD CONSTRAINT events_result_identity_digest_v7 CHECK (result_identity_digest ~ '^sha256:([0-9a-f]{64}|legacy)$'),
    ADD CONSTRAINT events_scores_array_v7 CHECK (jsonb_typeof(scores)='array' AND jsonb_array_length(scores) <= 4096),
    ADD CONSTRAINT events_predicted_label_v7 CHECK (predicted_label BETWEEN 0 AND 4294967295),
    ADD CONSTRAINT events_decision_v7 CHECK (decision IN ('benign','alert','abstain')),
    ADD CONSTRAINT events_execution_status_v7 CHECK (execution_status IN ('ok','hold','error')),
    ADD CONSTRAINT events_outcome_consistency_v7 CHECK (
        abstain = (decision='abstain')
        AND (NOT out_of_distribution OR abstain)
        AND (execution_status='ok' OR abstain)
        AND ((execution_status='ok' AND execution_error_code='') OR
             (execution_status<>'ok' AND execution_error_code<>''))
    ),
    ADD CONSTRAINT events_inference_time_v7 CHECK (
        inference_started_at_unix_ms >= 1 AND inference_completed_at_unix_ms >= inference_started_at_unix_ms
    );

CREATE TABLE IF NOT EXISTS incident_projection_events (
    projection_event_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
    decision TEXT NOT NULL CHECK (decision='alert'),
    predicted_label BIGINT NOT NULL CHECK (predicted_label BETWEEN 0 AND 4294967295),
    severity TEXT NOT NULL CHECK (severity IN ('info','low','medium','high','critical')),
    scope TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    projected_at_unix_ms BIGINT NOT NULL CHECK (projected_at_unix_ms >= 1),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS incident_projection_events_scope_time_idx
    ON incident_projection_events(scope,projected_at_unix_ms,projection_event_id);

UPDATE masi_schema_meta
SET value='7', applied_at=now(), checksum='computed-by-migration-runner',
    source_digest='db/migrations/0014_event_outcome_incident_projection.sql'
WHERE key='version';

COMMIT;
