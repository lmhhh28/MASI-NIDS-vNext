-- MASI-NIDS-vNext firewall policy schema (v1).
--
-- Migration: 0003_firewall.sql
-- Version:   1 (applied after 0002_effect_governance.sql)
-- Owner:     Go Control Core (MOD-CTRL-001) — sole business writer of the
--            firewall logical domain.
--
-- Invariants enforced by this schema (ADR-0014, contracts/p4/firewall-policy/v1):
--   * Policy/binding facts live in the core schema, NOT held by Edge/P4/plugin.
--   * Go receives only normalized business fields; Edge compiles to P4 entities.
--   * One selector per target (firewall_bindings is keyed by target_id).
--   * Baseline activation is a phase sequence on firewall_activations; it is
--     NOT a second workflow engine — the same effect_intents queue and the same
--     Edge writer are used.
--   * Response overlay does NOT modify the baseline selector; overlay TTL
--     expiry is a PG-deadline-driven durable delete intent, not a Go timer.
--
-- No P4/Edge/Central/Host/plugin/HTTP calls during migration.

BEGIN;

CREATE TABLE IF NOT EXISTS firewall_revisions (
    revision_id      TEXT PRIMARY KEY,
    revision_digest  TEXT NOT NULL UNIQUE,
    target_id        TEXT NOT NULL,
    default_action   TEXT NOT NULL CHECK (default_action IN ('permit-and-continue','drop')),
    rules            JSONB NOT NULL,
    scope            TEXT NOT NULL,
    actor_ref        TEXT NOT NULL,
    reason_code      TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Revisions are immutable; the revision_digest is the canonical binding.

CREATE TABLE IF NOT EXISTS firewall_bindings (
    target_id            TEXT PRIMARY KEY,
    current_revision_id  TEXT REFERENCES firewall_revisions(revision_id),
    previous_revision_id TEXT REFERENCES firewall_revisions(revision_id),
    active_bank          INTEGER NOT NULL CHECK (active_bank BETWEEN 0 AND 1),
    selector_state       TEXT NOT NULL CHECK (selector_state IN ('stable','switching','reconciling')),
    operation_id         TEXT,
    cas_digest           TEXT NOT NULL,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One selector per target. current is set only after inactive-bank readback +
-- selector readback + CAS (ADR-0014). No double-current.

CREATE TABLE IF NOT EXISTS firewall_overlays (
    overlay_rule_id      TEXT PRIMARY KEY,
    target_id            TEXT NOT NULL,
    rule                 JSONB NOT NULL,
    expires_at_unix_ms   BIGINT NOT NULL,
    deleted              BOOLEAN NOT NULL DEFAULT false,
    delete_intent_id     TEXT,
    operation_id         TEXT NOT NULL,
    actor_ref            TEXT NOT NULL,
    trace_id             TEXT NOT NULL,
    reason_code          TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS firewall_overlays_expiry_idx
    ON firewall_overlays (expires_at_unix_ms) WHERE deleted = false;
-- Overlay TTL expiry is PG-deadline-driven; a durable delete intent is created
-- when expires_at_unix_ms passes. No Go memory timer is the source of truth.

CREATE TABLE IF NOT EXISTS firewall_activations (
    operation_id        TEXT PRIMARY KEY,
    target_id           TEXT NOT NULL,
    desired_revision_id TEXT NOT NULL REFERENCES firewall_revisions(revision_id),
    current_stage       TEXT NOT NULL CHECK (current_stage IN (
        'prepared','inactive_writing','inactive_verified','selector_switching',
        'selector_verified','current_committed','previous_retained','cleanup')),
    completed_stages    JSONB NOT NULL,
    expected_entries    INTEGER NOT NULL CHECK (expected_entries BETWEEN 0 AND 4096),
    observed_entries    INTEGER NOT NULL CHECK (observed_entries BETWEEN 0 AND 4096),
    mismatched_entries  INTEGER NOT NULL CHECK (mismatched_entries BETWEEN 0 AND 4096),
    default_readback    TEXT NOT NULL CHECK (default_readback IN ('exact','missing','mismatch')),
    selector_readback   TEXT NOT NULL CHECK (selector_readback IN ('old-bank','new-bank','missing','mismatch')),
    result              TEXT NOT NULL CHECK (result IN ('prepared','applied','reconciling','hold')),
    reason_code         TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- applied requires exact readback, zero mismatch, current_committed stage.
ALTER TABLE firewall_activations ADD CONSTRAINT activation_applied_exact CHECK (
    result <> 'applied' OR
    (default_readback = 'exact' AND selector_readback = 'new-bank' AND mismatched_entries = 0
     AND current_stage = 'current_committed')
);

COMMIT;