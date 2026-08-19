-- MASI-NIDS-vNext model platform schema (v1).
--
-- Migration: 0005_model_platform.sql
-- Version:   1 (applied after 0004, before 0006)
-- Owner:     Go Control Core (MOD-CTRL-001) — sole business writer of the
--            model_platform domain. Central Inference only reads the startup
--            envelope / readback; Edge consumes the route/binding; Offline ML
--            produces the immutable bundle. None of them write these facts.
--
-- Invariants (go-control-core-design §9, ADR-0017, MIG-INF-001):
--   * loaded/Ready/deployment status != current. The current binding is set
--     ONLY by a short PG CAS after Central startup/warmup/readback/capacity and
--     Edge per-shard route-withdraw/drain + committed-binding handshake.
--   * same-generation replica does not change route; CPU<->CUDA / model /
--     backend change uses a NEW pool generation.
--   * Rollback is a NEW durable operation to a still-qualified, unrevoked,
--     reader-runtime-compatible EXACT previous — never an automatic switch on
--     inference error.
--   * PITR/restore/clone/rewind rotates a NEVER-USED model-control incarnation
--     before opening writer; old assignments/envelope/action/readback/handshake
--     with the same numeric generation are invalidated.
--   * No Central/Edge/Gateway/Triton/HTTP calls during migration.

BEGIN;

CREATE TABLE IF NOT EXISTS model_revisions (
    model_revision_id        TEXT PRIMARY KEY,
    model_revision_digest    TEXT NOT NULL UNIQUE,
    model_bundle_digest      TEXT NOT NULL,
    feature_contract_digest  TEXT NOT NULL,
    label_contract_digest    TEXT NOT NULL,
    output_adapter_digest    TEXT NOT NULL,
    qualification_status     TEXT NOT NULL CHECK (qualification_status IN
        ('qualified','unqualified','hold','revoked')),
    qualified_at_unix_ms     BIGINT,
    reader_runtime_profile   TEXT NOT NULL,
    actor_ref                TEXT NOT NULL,
    trace_id                 TEXT NOT NULL,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Model-control incarnation: never reused. Rotated on PITR/clone/rewind BEFORE
-- a writer is reopened. The same numeric generation under a retired incarnation
-- is invalid (incarnation + generation + binding_generation form the fence).
CREATE TABLE IF NOT EXISTS model_control_incarnations (
    incarnation_id   TEXT PRIMARY KEY,
    source           TEXT NOT NULL CHECK (source IN ('initial','pitr','clone','rewind')),
    rotated_at_unix_ms BIGINT NOT NULL,
    replaced_incarnation_id TEXT,
    actor_ref        TEXT NOT NULL,
    trace_id         TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS logical_pools (
    logical_pool_id      TEXT PRIMARY KEY,
    current_generation   BIGINT NOT NULL DEFAULT 1 CHECK (current_generation >= 1),
    availability_profile TEXT NOT NULL,
    runtime_profile      TEXT NOT NULL,
    actor_ref            TEXT NOT NULL,
    trace_id             TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pool_generations (
    logical_pool_id          TEXT NOT NULL REFERENCES logical_pools(logical_pool_id),
    pool_generation          BIGINT NOT NULL CHECK (pool_generation >= 1),
    model_revision_id        TEXT NOT NULL REFERENCES model_revisions(model_revision_id),
    startup_envelope_digest  TEXT NOT NULL,
    pool_observation_digest  TEXT NOT NULL,
    binding_digest           TEXT NOT NULL,
    status                   TEXT NOT NULL CHECK (status IN
        ('warming','active','draining','retired','quarantined')),
    min_ready_replicas       INTEGER NOT NULL DEFAULT 1,
    capacity_qualified       BOOLEAN NOT NULL DEFAULT false,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (logical_pool_id, pool_generation)
);

-- Per-shard desired/current/previous binding. The current_binding_generation
-- is set ONLY by CAS after committed-binding handshake. resume_state records
-- whether the shard is current, resuming (handshake failed after CAS), or
-- unavailable. cas_digest guards the CAS against stale writers.
CREATE TABLE IF NOT EXISTS shard_bindings (
    shard_id                 TEXT PRIMARY KEY,
    logical_pool_id          TEXT NOT NULL REFERENCES logical_pools(logical_pool_id),
    model_control_incarnation_id TEXT NOT NULL REFERENCES model_control_incarnations(incarnation_id),
    current_generation       BIGINT NOT NULL,
    previous_generation      BIGINT,
    current_binding_generation BIGINT NOT NULL CHECK (current_binding_generation >= 1),
    previous_binding_generation BIGINT,
    current_revision_id      TEXT NOT NULL,
    previous_revision_id     TEXT,
    route_epoch              BIGINT NOT NULL DEFAULT 1 CHECK (route_epoch >= 1),
    resume_state             TEXT NOT NULL CHECK (resume_state IN
        ('current','resume_pending','unavailable')),
    loaded                   BOOLEAN NOT NULL DEFAULT false,
    ready                    BOOLEAN NOT NULL DEFAULT false,
    cas_digest               TEXT NOT NULL,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- current generation must reference an existing pool_generation. Enforced
    -- by a composite foreign key (pool_generations PK is (logical_pool_id,
    -- pool_generation)); PostgreSQL forbids subqueries/EXISTS in CHECK.
    CONSTRAINT shard_current_generation_fk
        FOREIGN KEY (logical_pool_id, current_generation)
        REFERENCES pool_generations (logical_pool_id, pool_generation)
);

CREATE TABLE IF NOT EXISTS model_rollout_operations (
    operation_id        TEXT PRIMARY KEY,
    logical_pool_id     TEXT NOT NULL REFERENCES logical_pools(logical_pool_id),
    target_generation   BIGINT NOT NULL CHECK (target_generation >= 1),
    previous_generation BIGINT,
    operation_kind      TEXT NOT NULL CHECK (operation_kind IN
        ('rollout','rollback','recovery','pitr-recovery')),
    status              TEXT NOT NULL CHECK (status IN
        ('planned','staging','warming','route-withdrawing','cas-committing',
         'handshake-pending','applied','resume_pending','failed','aborted')),
    started_at_unix_ms  BIGINT NOT NULL,
    finished_at_unix_ms BIGINT,
    actor_ref           TEXT NOT NULL,
    reason_code         TEXT NOT NULL,
    trace_id            TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;