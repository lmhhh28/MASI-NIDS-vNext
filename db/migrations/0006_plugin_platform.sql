-- MASI-NIDS-vNext plugin platform schema (v1).
--
-- Migration: 0006_plugin_platform.sql
-- Version:   1 (applied after 0005_model_platform.sql)
-- Owner:     Go Control Core (MOD-CTRL-001) — sole business writer of the
--            plugin_platform domain (catalog/manifest/qualification/binding/
--            revocation/audit).
--
-- Invariants (ADR-0018, go-control-core-design §10.1, contracts/plugin/v1):
--   * Kind is closed: analysis-agent | read-only-tool | pure-transform.
--     Unknown kind/major, wrong digest/publisher, capability expansion,
--     revoked artifact, unqualified runtime -> reject.
--   * Plugins NEVER write core schema, create Decision/Intent, or call Edge/P4.
--     (Enforced in application logic; the schema has no effect_intents FK.)
--   * Manifest revisions are immutable (append-only); a binding references an
--     exact manifest digest, not a mutable alias.
--   * Activation/revocation is append-only; a revoked artifact cannot become
--     active again without a new qualification.

BEGIN;

CREATE TABLE IF NOT EXISTS plugin_manifests (
    manifest_id        TEXT NOT NULL,
    manifest_revision  INTEGER NOT NULL CHECK (manifest_revision >= 1),
    manifest_digest    TEXT NOT NULL,
    plugin_id          TEXT NOT NULL,
    kind               TEXT NOT NULL CHECK (kind IN ('analysis-agent','read-only-tool','pure-transform')),
    publisher          TEXT NOT NULL,
    version            TEXT NOT NULL,
    capabilities       JSONB NOT NULL,
    resource_limits    JSONB NOT NULL,
    runtime_profile    TEXT NOT NULL CHECK (runtime_profile IN ('wasm-component/v1','grpc-service/v1')),
    wit_digest         TEXT,
    service_proto_digest TEXT,
    sbom_digest        TEXT NOT NULL,
    provenance_digest  TEXT NOT NULL,
    signature_status   TEXT NOT NULL CHECK (signature_status IN ('signed','unsigned','rejected')),
    actor_ref          TEXT NOT NULL,
    trace_id           TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (manifest_id, manifest_revision),
    UNIQUE (plugin_id, manifest_revision)
);

CREATE TABLE IF NOT EXISTS plugin_qualifications (
    qualification_id    TEXT PRIMARY KEY,
    plugin_id           TEXT NOT NULL,
    manifest_id         TEXT NOT NULL,
    manifest_revision   INTEGER NOT NULL,
    manifest_digest     TEXT NOT NULL,
    qualification_status TEXT NOT NULL CHECK (qualification_status IN ('qualified','unqualified','hold')),
    qualification_digest TEXT NOT NULL,
    qualified_at        TIMESTAMPTZ,
    actor_ref           TEXT NOT NULL,
    trace_id            TEXT NOT NULL,
    reason_code         TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS plugin_bindings (
    plugin_id           TEXT NOT NULL,
    binding_generation  INTEGER NOT NULL CHECK (binding_generation >= 1),
    manifest_id         TEXT NOT NULL,
    manifest_revision   INTEGER NOT NULL,
    manifest_digest     TEXT NOT NULL,
    config_digest       TEXT NOT NULL,
    capability_digest   TEXT NOT NULL,
    resource_profile_digest TEXT NOT NULL,
    activation_state    TEXT NOT NULL CHECK (activation_state IN
        ('active','drain','revoked','stale','unqualified')),
    qualification_status TEXT NOT NULL CHECK (qualification_status IN ('qualified','unqualified','hold')),
    actor_ref           TEXT NOT NULL,
    trace_id            TEXT NOT NULL,
    reason_code         TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (plugin_id, binding_generation)
);
-- Only one active binding per plugin at a time.
CREATE UNIQUE INDEX IF NOT EXISTS plugin_bindings_active_uidx
    ON plugin_bindings (plugin_id) WHERE activation_state = 'active';
-- An active binding must be qualified.
ALTER TABLE plugin_bindings ADD CONSTRAINT plugin_active_qualified CHECK (
    activation_state <> 'active' OR qualification_status = 'qualified'
);

COMMIT;