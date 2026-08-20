-- Analysis A2A 1.0 polling identity and ambiguity hardening (schema v20).
-- Requirement IDs: CONTRACT-AGENT-001, AGENT-001, AGENT-002, AGENT-005,
-- AGENT-006, AGENT-007, AGENT-008.

BEGIN;

ALTER TABLE analysis_task_requests DROP CONSTRAINT analysis_task_requests_status_check;
ALTER TABLE analysis_task_requests ADD CONSTRAINT analysis_task_requests_status_v20 CHECK(status IN
    ('submitted','working','succeeded','limited','insufficient_evidence','failed','fenced','unknown'));
ALTER TABLE analysis_task_requests
    ADD COLUMN a2a_version TEXT NOT NULL DEFAULT '1.0' CHECK(a2a_version='1.0'),
    ADD COLUMN remote_task_id TEXT,
    ADD COLUMN remote_context_id TEXT,
    ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT '',
    ADD COLUMN response_bytes INTEGER NOT NULL DEFAULT 0 CHECK(response_bytes BETWEEN 0 AND 131072),
    ADD COLUMN input_bundle JSONB NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(input_bundle)='object');
ALTER TABLE analysis_task_requests ADD CONSTRAINT analysis_remote_task_pair_v20 CHECK(
    (remote_task_id IS NULL AND remote_context_id IS NULL) OR
    (remote_task_id IS NOT NULL AND remote_context_id IS NOT NULL)
);
CREATE UNIQUE INDEX analysis_task_actor_idempotency_uidx
    ON analysis_task_requests(actor_ref,idempotency_key);
CREATE UNIQUE INDEX analysis_task_peer_remote_uidx
    ON analysis_task_requests(peer_id,remote_task_id) WHERE remote_task_id IS NOT NULL;

ALTER TABLE analysis_artifacts
    ADD COLUMN artifact_sequence INTEGER NOT NULL DEFAULT 0 CHECK(artifact_sequence BETWEEN 0 AND 7),
    ADD COLUMN input_digest TEXT NOT NULL
        DEFAULT 'sha256:0000000000000000000000000000000000000000000000000000000000000000'
        CHECK(input_digest ~ '^sha256:[0-9a-f]{64}$'),
    ADD COLUMN tool_trajectory_digest TEXT NOT NULL
        DEFAULT 'sha256:0000000000000000000000000000000000000000000000000000000000000000'
        CHECK(tool_trajectory_digest ~ '^sha256:[0-9a-f]{64}$'),
    ADD COLUMN analysis_outcome TEXT NOT NULL DEFAULT 'failed'
        CHECK(analysis_outcome IN ('succeeded','limited','insufficient_evidence','failed'));
ALTER TABLE analysis_artifacts ADD CONSTRAINT analysis_artifact_task_sequence_uidx
    UNIQUE(task_id,artifact_sequence);

UPDATE masi_schema_meta
SET value='20',applied_at=now(),checksum='computed-by-migration-runner',
    source_digest='db/migrations/0027_analysis_a2a_polling.sql'
WHERE key='version';

COMMIT;
