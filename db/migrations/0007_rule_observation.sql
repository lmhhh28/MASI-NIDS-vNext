-- MASI-NIDS-vNext rule observation schema (v1).
--
-- Migration: 0007_rule_observation.sql
-- Version:   1 (applied after 0006_plugin_platform.sql)
-- Owner:     Go Control Core (MOD-CTRL-001) — per-rule latest/rollup/status/
--            outcome reference is written ONLY by Go into PostgreSQL.
--            Prometheus/Grafana/Edge memory/logs are NOT rule fact sources.
--
-- Invariants (contracts/p4/rule-observation/v1, TEST-P4-FW-001, golden
-- control-rule-formula-status-0001):
--   * An observation epoch is IMMUTABLE and created only AFTER the effect's
--     exact entry-installation readback converges.
--   * Effect execution / installation readback / per-entry direct-counter
--     dataplane match / packet-action outcome are SEPARATE dimensions; counter
--     growth never proves action success; no-hit, no-eligible-traffic,
--     stale/reset/gap/not-measurable are never collapsed into 0% or failure.
--   * A late sample never crosses an epoch; rollups never stitch across
--     generation/reset-epoch boundaries.
--   * No per-rule digest/match/IP/five-tuple may become a Prometheus label;
--     this schema is the ONLY canonical rollup storage.
--
-- No P4/Edge/Central/Host/plugin/HTTP calls during migration.

BEGIN;

-- One immutable epoch per (entity, rule) per observation_epoch. Created only
-- after exact installation readback of the owning effect operation.
CREATE TABLE IF NOT EXISTS rule_observation_epochs (
    epoch_id                       TEXT PRIMARY KEY,
    effect_intent_id               TEXT NOT NULL,
    operation_id                   TEXT NOT NULL,
    entity_id                      TEXT NOT NULL,
    rule_id                        TEXT NOT NULL,
    target_id                      TEXT NOT NULL,
    canonical_entry_digest         TEXT NOT NULL,
    match_priority_action_digest   TEXT NOT NULL,
    observation_epoch              BIGINT NOT NULL CHECK (observation_epoch >= 1),
    reset_epoch                    BIGINT NOT NULL CHECK (reset_epoch >= 1),
    installation_readback          TEXT NOT NULL CHECK (installation_readback IN
        ('exact','missing','mismatch','not-measurable')),
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (entity_id, rule_id, observation_epoch, reset_epoch)
);
-- Late samples under an older (observation_epoch, reset_epoch) are rejected:
-- they never update a newer epoch's facts.

-- Latest per-rule observed sample within the current epoch (the projector
-- keeps one row per epoch+rule; history lives in the rollups below).
CREATE TABLE IF NOT EXISTS rule_observations (
    epoch_id                TEXT PRIMARY KEY REFERENCES rule_observation_epochs(epoch_id),
    sample_sequence         BIGINT NOT NULL CHECK (sample_sequence >= 1),
    read_completed_at_unix_ms BIGINT NOT NULL,
    packets                 BIGINT NOT NULL CHECK (packets >= 0),
    bytes                   BIGINT NOT NULL CHECK (bytes >= 0),
    eligible_packets        BIGINT NOT NULL CHECK (eligible_packets >= 0),
    direct_delta_packets    BIGINT NOT NULL,
    direct_delta_bytes      BIGINT NOT NULL,
    eligible_delta_packets  BIGINT NOT NULL,
    rate                    DOUBLE PRECISION NOT NULL CHECK (rate >= 0 AND rate <= 1),
    coverage                DOUBLE PRECISION NOT NULL CHECK (coverage >= 0 AND coverage <= 1),
    quality_status          TEXT NOT NULL CHECK (quality_status IN
        ('valid','partial','gap','stale','no-eligible-traffic','not-covered',
         'not-measurable','invalid','reset')),
    quality_reasons         TEXT[] NOT NULL DEFAULT '{}',
    outcome_status          TEXT NOT NULL CHECK (outcome_status IN
        ('observed','not-observed','not-measurable','stale','invalid')),
    outcome_expected        TEXT NOT NULL CHECK (outcome_expected IN
        ('forwarded','dropped','mirrored','not-measurable')),
    outcome_actual          TEXT NOT NULL CHECK (outcome_actual IN
        ('forwarded','dropped','mirrored','not-observed','not-measurable')),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 5-minute rollups. The window key carries the epoch identity so windows never
-- stitch across generation/reset boundaries (requirement profile partitioning;
-- retention is applied by the bounded sweep, not by unbounded growth).
CREATE TABLE IF NOT EXISTS rule_rollups_5m (
    window_start_unix_ms   BIGINT NOT NULL,
    epoch_id               TEXT NOT NULL REFERENCES rule_observation_epochs(epoch_id),
    observation_epoch      BIGINT NOT NULL,
    reset_epoch            BIGINT NOT NULL,
    rule_id                TEXT NOT NULL,
    direct_packets         BIGINT NOT NULL,
    direct_bytes           BIGINT NOT NULL,
    eligible_packets       BIGINT NOT NULL,
    sample_count           INTEGER NOT NULL CHECK (sample_count >= 0),
    quality_status         TEXT NOT NULL,
    quality_reasons        TEXT[] NOT NULL DEFAULT '{}',
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (window_start_unix_ms, epoch_id, rule_id)
);

-- 1-hour rollups (same non-stitching window key).
CREATE TABLE IF NOT EXISTS rule_rollups_1h (
    window_start_unix_ms   BIGINT NOT NULL,
    epoch_id               TEXT NOT NULL REFERENCES rule_observation_epochs(epoch_id),
    observation_epoch      BIGINT NOT NULL,
    reset_epoch            BIGINT NOT NULL,
    rule_id                TEXT NOT NULL,
    direct_packets         BIGINT NOT NULL,
    direct_bytes           BIGINT NOT NULL,
    eligible_packets       BIGINT NOT NULL,
    sample_count           INTEGER NOT NULL CHECK (sample_count >= 0),
    quality_status         TEXT NOT NULL,
    quality_reasons        TEXT[] NOT NULL DEFAULT '{}',
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (window_start_unix_ms, epoch_id, rule_id)
);

CREATE INDEX IF NOT EXISTS rule_rollups_5m_rule_time
    ON rule_rollups_5m (rule_id, window_start_unix_ms DESC);
CREATE INDEX IF NOT EXISTS rule_rollups_1h_rule_time
    ON rule_rollups_1h (rule_id, window_start_unix_ms DESC);
CREATE INDEX IF NOT EXISTS rule_observations_rule
    ON rule_observations (epoch_id);

COMMIT;
