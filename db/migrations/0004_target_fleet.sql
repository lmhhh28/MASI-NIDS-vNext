-- MASI-NIDS-vNext target + fleet schema (v1).
--
-- Migration: 0004_target_fleet.sql
-- Version:   1 (applied after 0002_effect_governance.sql; 0003 reserved for firewall)
-- Owner:     Go Control Core (MOD-CTRL-001) — sole business writer of the
--            target_fleet logical domain.
--
-- Invariants enforced by this schema (ADR-0015, AGENTS.md §不可破坏的架构约束):
--   * Stable target_id is never reused. Display name/endpoint/device_id/
--     hostname/serial are attributes; rename/address/chassis change cannot
--     reuse the ID; a retired ID is never reassigned.
--   * Same-active (p4runtime_endpoint, device_id, role) is unique among active
--     targets (partial unique index).
--   * Fleet parent is never claimable; per-target child intents are claimable
--     (the parent/child intent rows themselves live in effect_intents from
--     migration 0002; this table records the fleet operation + child mapping +
--     wave gates).
--   * No cross-target atomic commit; no majority-success = applied. Aggregate
--     status is projected from the FULL child vector (any unknown -> reconciling).
--   * No P4/Edge/Central/Host/gNMI/HTTP calls during migration.

BEGIN;

CREATE TABLE IF NOT EXISTS targets (
    target_id             TEXT PRIMARY KEY,
    display_name          TEXT NOT NULL,
    p4runtime_endpoint    TEXT NOT NULL,
    device_id             BIGINT NOT NULL CHECK (device_id >= 1),
    role                  TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN
        ('candidate','verified','active','draining','disabled','quarantined','retired')),
    desired_profile_digest TEXT NOT NULL,
    credential_ref        TEXT NOT NULL,
    scope                 TEXT NOT NULL,
    provenance            JSONB,
    actor_ref             TEXT NOT NULL,
    trace_id              TEXT NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    retired_at            TIMESTAMPTZ,
    -- A retired target can never return to an active/verified state.
    CONSTRAINT retired_is_terminal CHECK (
        status <> 'retired' OR retired_at IS NOT NULL
    )
);

-- Same-active identity is unique among active targets: a second active target
-- with the same (endpoint, device_id, role) is a conflict, not a silent
-- duplicate. Rename/address/chassis change must NOT reuse a target_id; that is
-- enforced by the application (target_id is never reassigned).
CREATE UNIQUE INDEX IF NOT EXISTS targets_active_identity_uidx
    ON targets (p4runtime_endpoint, device_id, role)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS target_assignments (
    target_id                 TEXT NOT NULL REFERENCES targets(target_id),
    assignment_generation     BIGINT NOT NULL CHECK (assignment_generation >= 1),
    incarnation_id            TEXT NOT NULL,
    edge_workload_ref         TEXT NOT NULL,
    lease_id                  TEXT NOT NULL,
    issued_at_unix_ms         BIGINT NOT NULL CHECK (issued_at_unix_ms >= 1),
    expires_at_unix_ms        BIGINT NOT NULL CHECK (expires_at_unix_ms >= 1),
    election_floor            BIGINT NOT NULL CHECK (election_floor >= 1),
    election_ceiling          BIGINT NOT NULL CHECK (election_ceiling >= 1),
    actor_runtime_epoch       TEXT NOT NULL,
    application_generation    BIGINT NOT NULL CHECK (application_generation >= 1),
    actor_ref                 TEXT NOT NULL,
    trace_id                  TEXT NOT NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (target_id, assignment_generation),
    -- Lease expires after issued (TARGET-LEASE-MONOTONIC).
    CONSTRAINT lease_monotonic CHECK (expires_at_unix_ms > issued_at_unix_ms),
    -- Election range is valid (TARGET-ELECTION-RANGE).
    CONSTRAINT election_range CHECK (election_ceiling >= election_floor)
);
-- Non-reusable lease: one lease_id per (target_id, incarnation_id).
CREATE UNIQUE INDEX IF NOT EXISTS target_assignments_lease_nonreuse_uidx
    ON target_assignments (target_id, incarnation_id, lease_id);
-- Handoff: each new assignment_generation's election_floor must be strictly
-- above the prior max ceiling (enforced by the application on CAS).

CREATE TABLE IF NOT EXISTS fleet_operations (
    fleet_operation_id    TEXT PRIMARY KEY,
    target_set_digest     TEXT NOT NULL,
    wave_count            INTEGER NOT NULL CHECK (wave_count BETWEEN 1 AND 64),
    waves                 JSONB NOT NULL,
    parent_intent_id      TEXT NOT NULL,
    aggregate_status      TEXT NOT NULL CHECK (aggregate_status IN
        ('planned','running','partial','reconciling','applied','failed','aborted')),
    actor_ref             TEXT NOT NULL,
    scope                 TEXT NOT NULL,
    reason_code           TEXT NOT NULL,
    trace_id              TEXT NOT NULL,
    created_at_unix_ms    BIGINT NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- The frozen target set is immutable per operation.
CREATE UNIQUE INDEX IF NOT EXISTS fleet_operations_target_set_uidx
    ON fleet_operations (fleet_operation_id, target_set_digest);

CREATE TABLE IF NOT EXISTS fleet_child_intents (
    fleet_operation_id    TEXT NOT NULL REFERENCES fleet_operations(fleet_operation_id),
    target_id             TEXT NOT NULL,
    effect_digest         TEXT NOT NULL,
    child_intent_id       TEXT NOT NULL,
    wave_index            INTEGER NOT NULL CHECK (wave_index BETWEEN 0 AND 63),
    status                TEXT NOT NULL CHECK (status IN
        ('pending','claimed','executing','applied','hold','unknown','reconciling','failed','skipped')),
    reason_code           TEXT NOT NULL,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (fleet_operation_id, target_id, effect_digest)
);
CREATE INDEX IF NOT EXISTS fleet_child_intents_op_wave_idx
    ON fleet_child_intents (fleet_operation_id, wave_index);

COMMIT;