-- MASI-NIDS-vNext effect governance schema (v1).
--
-- Migration: 0002_effect_governance.sql
-- Version:   1 (applied after 0001_core_event.sql)
-- Owner:     Go Control Core (MOD-CTRL-001) — sole business writer.
--
-- Invariants enforced by this schema (ADR-0001/0004, AGENTS.md §不可破坏的架构约束):
--   * Only effect_intents is claimable by the effect dispatcher. Proposals,
--     Decisions, and fleet parents are NEVER work queues — they have no
--     claim_state column.
--   * No second dispatcher/outbox/effect-queue: there is ONE durable ledger
--     (effect_intents); FOR UPDATE SKIP LOCKED is the single claim mechanism.
--   * One terminal Decision per proposal (unique constraint).
--   * Fleet parent intents are non-claimable (is_fleet_parent = true,
--     claim_state NULL); per-target child intents are claimable.

BEGIN;

CREATE TABLE IF NOT EXISTS effect_proposals (
    proposal_id        TEXT PRIMARY KEY,
    proposal_digest    TEXT NOT NULL UNIQUE,
    actor_ref          TEXT NOT NULL,
    scope              TEXT NOT NULL,
    risk_level         TEXT NOT NULL CHECK (risk_level IN ('R0','R1','R2','R3')),
    effect_kind        TEXT NOT NULL,
    target_set_digest  TEXT NOT NULL,
    policy_digest      TEXT NOT NULL,
    evidence_refs      JSONB NOT NULL,
    expires_at_unix_ms BIGINT NOT NULL,
    note               TEXT NOT NULL CHECK (length(note) <= 2048),
    created_at_unix_ms BIGINT NOT NULL,
    trace_id           TEXT NOT NULL,
    reason_code        TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Proposals are immutable; the proposal_digest is the canonical binding.
-- No claim_state column: a proposal is never a work queue.

CREATE TABLE IF NOT EXISTS effect_decisions (
    decision_id        TEXT PRIMARY KEY,
    proposal_id        TEXT NOT NULL REFERENCES effect_proposals(proposal_id),
    proposal_digest    TEXT NOT NULL,
    actor_ref          TEXT NOT NULL,
    risk_level         TEXT NOT NULL CHECK (risk_level IN ('R0','R1','R2','R3')),
    decision           TEXT NOT NULL CHECK (decision IN ('approve','reject')),
    authz_context      JSONB NOT NULL,
    decision_digest    TEXT NOT NULL,
    expires_at_unix_ms BIGINT NOT NULL,
    created_at_unix_ms BIGINT NOT NULL,
    trace_id           TEXT NOT NULL,
    reason_code        TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
    -- A decision binds the exact proposal digest via a composite foreign key
    -- (ADR-0004), added after the table below. PostgreSQL forbids subqueries
    -- in CHECK constraints, so this cross-row invariant is enforced by FK.
);
-- One terminal decision per proposal (append-only; a second approve is a
-- unique violation, not a silent overwrite).
CREATE UNIQUE INDEX IF NOT EXISTS effect_decisions_proposal_uidx
    ON effect_decisions (proposal_id);
-- Composite (proposal_id, proposal_digest) target for the decision FK. The
-- proposal_id PRIMARY KEY already guarantees this pair is unique; the explicit
-- unique index is required for a composite foreign key to reference it.
CREATE UNIQUE INDEX IF NOT EXISTS effect_proposals_id_digest_uidx
    ON effect_proposals (proposal_id, proposal_digest);
ALTER TABLE effect_decisions
    ADD CONSTRAINT effect_decisions_binds_proposal_digest_fk
    FOREIGN KEY (proposal_id, proposal_digest)
    REFERENCES effect_proposals (proposal_id, proposal_digest);
-- No claim_state column: a decision is never a work queue.

CREATE TABLE IF NOT EXISTS effect_intents (
    effect_intent_id        TEXT PRIMARY KEY,
    operation_id            TEXT NOT NULL,
    proposal_id             TEXT NOT NULL REFERENCES effect_proposals(proposal_id),
    decision_id             TEXT NOT NULL REFERENCES effect_decisions(decision_id),
    target_id               TEXT NOT NULL,
    fleet_operation_id      TEXT,
    is_fleet_parent         BOOLEAN NOT NULL DEFAULT false,
    fence                   JSONB NOT NULL,
    effect_digest           TEXT NOT NULL,
    authorization_digest    TEXT NOT NULL,
    effect_kind             TEXT NOT NULL,
    risk_level              TEXT NOT NULL CHECK (risk_level IN ('R0','R1','R2','R3')),
    required_write_atomicity TEXT NOT NULL DEFAULT 'CONTINUE_ON_ERROR'
        CHECK (required_write_atomicity IN ('CONTINUE_ON_ERROR','DATAPLANE_ATOMIC')),
    deadline_unix_ms        BIGINT NOT NULL,
    -- claim_state: ONLY effect_intents is claimable. Fleet parents have NULL
    -- claim_state (non-claimable); children have a claimable state.
    claim_state             TEXT CHECK (claim_state IN
        ('unclaimed','claimed','fenced','executing','unknown','finalized')),
    claim_lease_id          TEXT,
    claim_expires_at_unix_ms BIGINT,
    actor_ref               TEXT NOT NULL,
    trace_id                TEXT NOT NULL,
    reason_code             TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- A fleet parent is never claimable (claim_state must be NULL).
    -- A non-parent must have a non-null claim_state.
    CONSTRAINT fleet_parent_nonclaimable CHECK (
        (is_fleet_parent AND claim_state IS NULL) OR
        (NOT is_fleet_parent AND claim_state IS NOT NULL)
    )
);
-- The dispatcher claims unclaimed non-parent intents via FOR UPDATE SKIP LOCKED.
CREATE INDEX IF NOT EXISTS effect_intents_claim_idx
    ON effect_intents (claim_state) WHERE claim_state = 'unclaimed' AND NOT is_fleet_parent;
-- Per-(fleet_operation_id, target_id) idempotent child intent.
CREATE UNIQUE INDEX IF NOT EXISTS effect_intents_fleet_child_uidx
    ON effect_intents (fleet_operation_id, target_id, effect_digest)
    WHERE NOT is_fleet_parent AND fleet_operation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS effect_attempts (
    attempt_id         TEXT PRIMARY KEY,
    intent_id          TEXT NOT NULL REFERENCES effect_intents(effect_intent_id),
    operation_id       TEXT NOT NULL,
    attempt_number     INTEGER NOT NULL CHECK (attempt_number BETWEEN 1 AND 64),
    status             TEXT NOT NULL CHECK (status IN
        ('applied','hold','unknown','reconciling')),
    plan_digest        TEXT NOT NULL,
    readback_digest    TEXT,
    expected_entries   INTEGER NOT NULL CHECK (expected_entries BETWEEN 0 AND 4096),
    observed_entries   INTEGER NOT NULL CHECK (observed_entries BETWEEN 0 AND 4096),
    mismatched_entries INTEGER NOT NULL CHECK (mismatched_entries BETWEEN 0 AND 4096),
    active_bank        INTEGER CHECK (active_bank BETWEEN 0 AND 1),
    started_at_unix_ms BIGINT NOT NULL,
    finished_at_unix_ms BIGINT,
    trace_id           TEXT NOT NULL,
    reason_code        TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- applied requires exact readback and zero mismatch.
    CONSTRAINT applied_exact_readback CHECK (
        status <> 'applied' OR
        (readback_digest IS NOT NULL AND mismatched_entries = 0 AND finished_at_unix_ms IS NOT NULL)
    )
);

COMMIT;