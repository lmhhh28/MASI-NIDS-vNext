-- Durable same-origin API mutation idempotency ledger (v14).
-- Requirement IDs: API-001, SEC-001, DB-002, TEST-GO-CTRL-001.

BEGIN;

CREATE TABLE api_mutation_idempotency (
    actor_issuer       TEXT NOT NULL,
    actor_subject      TEXT NOT NULL,
    idempotency_key    TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
    request_digest     TEXT NOT NULL CHECK (request_digest ~ '^sha256:[0-9a-f]{64}$'),
    status             TEXT NOT NULL CHECK (status IN ('in_progress','completed')),
    response_status    INTEGER CHECK (response_status BETWEEN 100 AND 599),
    response_body      BYTEA CHECK (response_body IS NULL OR octet_length(response_body) <= 2097152),
    started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at       TIMESTAMPTZ,
    PRIMARY KEY(actor_issuer,actor_subject,idempotency_key),
    CHECK ((status='completed') = (response_status IS NOT NULL AND response_body IS NOT NULL AND completed_at IS NOT NULL))
);
CREATE INDEX api_mutation_idempotency_retention_idx
    ON api_mutation_idempotency(completed_at)
    WHERE status='completed';

GRANT SELECT,INSERT,UPDATE,DELETE ON api_mutation_idempotency TO masi_control_app;
GRANT SELECT ON api_mutation_idempotency TO masi_control_readonly;

UPDATE masi_schema_meta
SET value='14', applied_at=now(), checksum='computed-by-migration-runner',
    source_digest='db/migrations/0021_api_mutation_idempotency.sql'
WHERE key='version';

COMMIT;
