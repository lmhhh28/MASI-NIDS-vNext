//! Exact binding admission, offline publisher verification and artifact closure.

use std::collections::BTreeSet;
use std::path::{Component, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use base64::Engine as _;
use prost::Message as _;
use ring::signature::{self, UnparsedPublicKey};
use serde::{Deserialize, Serialize};
use sha2::{Digest as _, Sha256};

use crate::config::{HostConfig, ServiceEndpoint, read_secure_file, validate_digest};
use crate::contract::host::BindingEnvelope;
use crate::error::{HostError, HostResult, ReasonCode};

const MANIFEST_SCHEMA: &str = "masi-plugin-manifest/v1";
const BINDING_SCHEMA: &str = "plugin-host-binding/v1";
const CONFIG_SCHEMA: &str = "masi-plugin-config/v1";
const MAX_MANIFEST_BYTES: usize = 128 * 1024;
const MAX_CONFIG_BYTES: usize = 64 * 1024;
const MAX_VERIFICATION_BYTES: usize = 128 * 1024;

/// Strict public plugin manifest used for Host admission.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PluginManifest {
    /// Contract schema.
    pub schema_version: String,
    /// Immutable manifest identity.
    pub manifest_id: String,
    /// Append-only revision.
    pub manifest_revision: u32,
    /// Canonical body digest.
    pub manifest_digest: String,
    /// Stable plugin identity.
    pub plugin_id: String,
    /// Immutable plugin revision identity used by bindings and provenance.
    pub plugin_revision: String,
    /// Closed kind.
    pub kind: String,
    /// Stable publisher identity.
    pub publisher: String,
    /// Human/package version (not the binding identity).
    pub version: String,
    /// Exact authorized scope.
    pub scope: String,
    /// Exact OCI manifest or Wasm component digest.
    pub artifact_digest: String,
    /// Closed artifact media type.
    pub artifact_media_type: String,
    /// Exact kind entrypoint.
    pub entrypoint: String,
    /// Closed supported platform set.
    pub supported_platforms: Vec<String>,
    /// Exact Host API major/profile.
    pub host_api_version: String,
    /// Exact kind input contract digest.
    pub input_contract_digest: String,
    /// Exact kind output contract digest.
    pub output_contract_digest: String,
    /// Exact config schema identity.
    pub config_schema_id: String,
    /// Exact config schema version.
    pub config_schema_version: String,
    /// Exact config schema bytes digest.
    pub config_schema_digest: String,
    /// Requested capabilities.
    pub capabilities: Vec<Capability>,
    /// Declared skills; empty for the Host-managed first-release profiles.
    pub skill_ids: Vec<String>,
    /// Declared MCP tools; empty because Agent/tool business traffic is direct.
    pub mcp_tool_ids: Vec<String>,
    /// Declared MCP resources; empty for Host-managed execution.
    pub mcp_resource_ids: Vec<String>,
    /// Declared A2A peers; empty because Analysis is not proxied by the Host.
    pub a2a_peer_ids: Vec<String>,
    /// Closed egress capability identities.
    pub network_egress_capability_ids: Vec<String>,
    /// Exact filesystem preopens; empty for the first-release Host profiles.
    pub filesystem_preopens: Vec<String>,
    /// Declared secret references; empty for pure transform fixtures.
    pub secret_ref_ids: Vec<String>,
    /// Immutable qualified statistics definitions.
    pub statistics_definitions: Vec<StatisticsDefinitionRef>,
    /// Requested per-binding limits.
    pub resource_limits: ManifestLimits,
    /// Exact runtime profile.
    pub runtime_profile: String,
    /// Exact WIT source digest for Wasm.
    pub wit_digest: Option<String>,
    /// Exact service protobuf digest for Host-managed services.
    pub service_proto_digest: Option<String>,
    /// Exact SBOM digest.
    pub sbom_digest: String,
    /// Exact provenance digest.
    pub provenance_digest: String,
    /// Must be `signed` after Manager verification.
    pub signature_status: String,
    /// Verification policy subject.
    pub verification_policy_digest: String,
    /// Offline verification bundle contract profile.
    pub verification_bundle_profile: String,
    /// Stable owner identity.
    pub owner_ref: String,
    /// Closed support level.
    pub support_level: String,
    /// Explicit host/runtime/rollback compatibility metadata.
    pub compatibility: Compatibility,
    /// Exact lifecycle method semantics.
    pub lifecycle_contract: LifecycleContract,
    /// Exact observability contract digest.
    pub observability_contract_digest: String,
    /// Audit actor reference.
    pub actor_ref: String,
    /// Stable reason.
    pub reason_code: String,
    /// Trace reference.
    pub trace_id: String,
    /// Manifest production time.
    pub created_at_unix_ms: i64,
}

/// Exact immutable statistics definition identity carried by a manifest.
#[derive(Clone, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(deny_unknown_fields)]
pub struct StatisticsDefinitionRef {
    /// Stable definition identity.
    pub definition_id: String,
    /// Immutable definition revision identity.
    pub definition_revision: String,
    /// Exact qualified definition digest.
    pub definition_digest: String,
}

/// Closed compatibility metadata used by explicit rollback admission.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Compatibility {
    /// Minimum accepted Host API profile.
    pub host_api_min: String,
    /// Maximum accepted Host API profile.
    pub host_api_max: String,
    /// Exact runtime profile.
    pub runtime_profile: String,
    /// Whether an external migration is required before activation.
    pub migration_required: bool,
    /// Exact earlier plugin revisions that can receive a rollback pointer.
    pub rollback_compatible_revisions: Vec<String>,
}

/// Closed startup/readiness/liveness/drain/failure/fallback contract set.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct LifecycleContract {
    /// Fail-closed startup contract.
    pub startup: String,
    /// Exact-binding readiness contract.
    pub readiness: String,
    /// Progress-based liveness contract.
    pub liveness: String,
    /// Bounded drain contract.
    pub drain: String,
    /// Stable failure-reason contract.
    pub failure: String,
    /// Explicit no-fallback contract.
    pub fallback: String,
}

/// Requested manifest capability.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Capability {
    /// Stable capability identity.
    pub capability_id: String,
    /// Closed capability kind.
    pub capability_kind: String,
    /// Explicit request bit; false is never interpreted as an implicit grant.
    pub declared: bool,
}

/// Requested per-binding limits.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ManifestLimits {
    /// CPU milli-units applied by the deployment profile.
    pub cpu_milli: u32,
    /// Memory bound.
    pub memory_bytes: u64,
    /// Process bound for service plugins.
    pub pid_count: u32,
    /// File descriptor bound.
    pub fd_count: u32,
    /// Ephemeral disk bound.
    pub disk_bytes: u64,
    /// Wasm linear-memory ceiling; zero for service profiles.
    pub linear_memory_bytes: u64,
    /// Wasm table-element ceiling; zero for service profiles.
    pub table_elements: u32,
    /// Maximum simultaneously instantiated runtime instances.
    pub instance_count: u32,
    /// Maximum records in one typed batch.
    pub batch_records: u32,
    /// Maximum aggregate bytes admitted in flight.
    pub in_flight_bytes: u64,
    /// Per-binding execution concurrency.
    pub concurrency: u32,
    /// Total deadline bound.
    pub deadline_ms: u32,
    /// First-release Host-managed methods never retry.
    pub retry_max_attempts: u32,
    /// Output bound.
    pub output_bytes: u64,
    /// Queue bound.
    pub queue_depth: u32,
}

/// Bounded config document. Values cannot carry endpoints, paths or credentials.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PluginConfigDocument {
    /// Contract schema.
    pub schema_version: String,
    /// Immutable config identity.
    pub config_id: String,
    /// Scalar-only settings.
    pub values: serde_json::Map<String, serde_json::Value>,
    /// Opaque secret references; Host never receives the referenced secret for Wasm.
    pub secret_refs: Vec<String>,
}

/// Offline artifact verification bundle.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct VerificationBundle {
    /// Contract schema.
    pub schema_version: String,
    /// Exact artifact subject.
    pub artifact_digest: String,
    /// Exact manifest subject.
    pub manifest_digest: String,
    /// Stable publisher.
    pub publisher_identity: String,
    /// Exact SBOM digest.
    pub sbom_digest: String,
    /// Exact provenance digest.
    pub provenance_digest: String,
    /// Trust policy used by the verifier.
    pub trust_policy_digest: String,
    /// Verification observation time.
    pub verified_at_unix_ms: i64,
    /// Bundle validity deadline.
    pub expires_at_unix_ms: i64,
    /// Revocation result.
    pub revoked: bool,
    /// Qualified verifier profile.
    pub verifier: String,
    /// Ed25519 signature over [`verification_signature_payload`].
    pub signature_base64: String,
}

/// Fully checked binding used to instantiate one runtime.
#[derive(Clone, Debug)]
pub struct ValidatedBinding {
    /// Exact original binding envelope.
    pub envelope: BindingEnvelope,
    /// Strict manifest body.
    pub manifest: PluginManifest,
    /// Strict config body.
    pub plugin_config: PluginConfigDocument,
    /// Wasm component bytes, only for `wasm-component/v1`.
    pub component_bytes: Option<Vec<u8>>,
    /// Resolved allowlisted endpoint, only for `grpc-service/v1`.
    pub service_endpoint: Option<ServiceEndpoint>,
}

/// Validate an exact Manager binding without fetching from the network.
pub fn validate_binding(
    config: &HostConfig,
    binding: BindingEnvelope,
) -> HostResult<ValidatedBinding> {
    if binding.schema_version != BINDING_SCHEMA {
        return Err(HostError::new(
            ReasonCode::UnknownVersion,
            "binding schema version rejected",
        ));
    }
    validate_identity(&binding.plugin_id, "plugin_id")?;
    validate_identity(&binding.plugin_revision, "plugin_revision")?;
    validate_identity(&binding.binding_epoch, "binding_epoch")?;
    validate_identity(&binding.scope, "scope")?;
    validate_identity(&binding.actor_ref, "actor_ref")?;
    validate_identity(&binding.trace_id, "trace_id")?;
    if binding.binding_generation < 1
        || binding.issued_at_unix_ms < 1
        || binding.expires_at_unix_ms <= binding.issued_at_unix_ms
    {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            "binding generation/time envelope invalid",
        ));
    }
    let expected_envelope = compute_envelope_digest(&binding);
    if binding.envelope_digest != expected_envelope {
        return Err(HostError::new(
            ReasonCode::DigestMismatch,
            "binding envelope digest mismatch",
        ));
    }
    for digest in [
        &binding.manifest_digest,
        &binding.artifact_digest,
        &binding.config_digest,
        &binding.capability_digest,
        &binding.resource_profile_digest,
        &binding.qualification_digest,
        &binding.trust_policy_digest,
        &binding.verification_bundle_digest,
        &binding.revocation_snapshot_digest,
    ] {
        validate_digest(digest)?;
    }
    if binding.trust_policy_digest != config.trust.trust_policy_digest {
        return Err(HostError::new(
            ReasonCode::PublisherUntrusted,
            "binding trust policy is not the configured exact policy",
        ));
    }
    let now = unix_ms()?;
    if now > binding.expires_at_unix_ms {
        return Err(HostError::new(
            ReasonCode::Fenced,
            "binding envelope expired",
        ));
    }
    if binding.revocation_checked_at_unix_ms < 1
        || now.saturating_sub(binding.revocation_checked_at_unix_ms)
            > config.trust.revocation_max_age_ms
    {
        return Err(HostError::new(
            ReasonCode::TrustStale,
            "binding revocation observation is stale",
        ));
    }

    let manifest: PluginManifest =
        decode_bounded_json(&binding.manifest_json, MAX_MANIFEST_BYTES, "manifest")?;
    validate_manifest(config, &binding, &manifest)?;
    let plugin_config: PluginConfigDocument =
        decode_bounded_json(&binding.config_json, MAX_CONFIG_BYTES, "plugin config")?;
    validate_plugin_config(&binding, &manifest, &plugin_config)?;
    let bundle: VerificationBundle = decode_bounded_json(
        &binding.verification_bundle_json,
        MAX_VERIFICATION_BYTES,
        "verification bundle",
    )?;
    validate_bundle(config, &binding, &manifest, &bundle, now)?;

    let endpoint_map = config.endpoint_map()?;
    let (component_bytes, service_endpoint) = match binding.runtime_profile.as_str() {
        "wasm-component/v1" => {
            if binding.kind != "pure-transform" || binding.wit_digest != qualified_wit_digest() {
                return Err(HostError::new(
                    ReasonCode::UnknownRuntimeProfile,
                    "Wasm binding is not the qualified pure-transform world",
                ));
            }
            if !binding.service_endpoint_ref.is_empty() || binding.artifact_name.is_empty() {
                return Err(HostError::new(
                    ReasonCode::InvalidArgument,
                    "Wasm binding endpoint/artifact reference invalid",
                ));
            }
            if !plugin_config.secret_refs.is_empty() {
                return Err(HostError::new(
                    ReasonCode::CapabilityDenied,
                    "pure-transform may not receive secret references",
                ));
            }
            let artifact = resolve_artifact_path(config, &binding.artifact_name)?;
            let bytes = read_secure_file(
                &artifact,
                config
                    .limits
                    .max_control_message_bytes
                    .max(config.limits.max_input_bytes),
                false,
            )?;
            if bytes.len() as u64 != binding.artifact_bytes
                || sha256_bytes(&bytes) != binding.artifact_digest
            {
                return Err(HostError::new(
                    ReasonCode::DigestMismatch,
                    "Wasm component bytes/size do not match binding",
                ));
            }
            (Some(bytes), None)
        }
        "grpc-service/v1" => {
            if binding.kind != "pure-transform" && binding.kind != "read-only-tool" {
                return Err(HostError::new(
                    ReasonCode::UnknownKind,
                    "Host-managed service kind rejected",
                ));
            }
            if !binding.artifact_name.is_empty() || binding.service_endpoint_ref.is_empty() {
                return Err(HostError::new(
                    ReasonCode::InvalidArgument,
                    "service binding endpoint/artifact reference invalid",
                ));
            }
            let endpoint = endpoint_map
                .get(&binding.service_endpoint_ref)
                .cloned()
                .ok_or_else(|| {
                    HostError::new(
                        ReasonCode::CapabilityDenied,
                        "service endpoint_ref is not allowlisted",
                    )
                })?;
            if endpoint.artifact_digest != binding.artifact_digest
                || endpoint.service_proto_digest != binding.service_proto_digest
            {
                return Err(HostError::new(
                    ReasonCode::DigestMismatch,
                    "service endpoint identity drift",
                ));
            }
            (None, Some(endpoint))
        }
        _ => {
            return Err(HostError::new(
                ReasonCode::UnknownRuntimeProfile,
                "unknown runtime profile",
            ));
        }
    };

    Ok(ValidatedBinding {
        envelope: binding,
        manifest,
        plugin_config,
        component_bytes,
        service_endpoint,
    })
}

fn validate_manifest(
    config: &HostConfig,
    binding: &BindingEnvelope,
    manifest: &PluginManifest,
) -> HostResult<()> {
    if manifest.schema_version != MANIFEST_SCHEMA {
        return Err(HostError::new(
            ReasonCode::UnknownVersion,
            "manifest schema version rejected",
        ));
    }
    if manifest.manifest_revision < 1
        || manifest.plugin_id != binding.plugin_id
        || manifest.plugin_revision != binding.plugin_revision
        || manifest.kind != binding.kind
        || manifest.runtime_profile != binding.runtime_profile
        || manifest.scope != binding.scope
        || manifest.signature_status != "signed"
        || manifest.created_at_unix_ms < 1
    {
        return Err(HostError::new(
            ReasonCode::Fenced,
            "manifest identity/scope/runtime mismatch",
        ));
    }
    for value in [
        &manifest.manifest_id,
        &manifest.plugin_id,
        &manifest.plugin_revision,
        &manifest.publisher,
        &manifest.version,
        &manifest.scope,
        &manifest.config_schema_id,
        &manifest.config_schema_version,
        &manifest.owner_ref,
        &manifest.actor_ref,
        &manifest.trace_id,
    ] {
        validate_identity(value, "manifest identity")?;
    }
    for digest in [
        &manifest.artifact_digest,
        &manifest.input_contract_digest,
        &manifest.output_contract_digest,
        &manifest.config_schema_digest,
        &manifest.sbom_digest,
        &manifest.provenance_digest,
        &manifest.verification_policy_digest,
        &manifest.observability_contract_digest,
    ] {
        validate_digest(digest)?;
    }
    if manifest.artifact_digest != binding.artifact_digest
        || manifest.host_api_version != "plugin-host-control/v1"
        || manifest.config_schema_id != binding.config_schema_id
        || manifest.config_schema_version != CONFIG_SCHEMA
        || manifest.config_schema_digest != qualified_config_schema_digest()
        || manifest.verification_policy_digest != config.trust.trust_policy_digest
        || manifest.verification_bundle_profile != "plugin-verification-bundle/v1"
        || !matches!(
            manifest.support_level.as_str(),
            "first-party" | "supported" | "best-effort"
        )
    {
        return Err(HostError::new(
            ReasonCode::Fenced,
            "manifest artifact/host/config/support contract mismatch",
        ));
    }
    let qualified_runtime_contract = if binding.runtime_profile == "wasm-component/v1" {
        qualified_wit_digest()
    } else {
        qualified_service_proto_digest()
    };
    if manifest.input_contract_digest != qualified_runtime_contract
        || manifest.output_contract_digest != qualified_runtime_contract
        || manifest.compatibility.host_api_min != "plugin-host-control/v1"
        || manifest.compatibility.host_api_max != "plugin-host-control/v1"
        || manifest.compatibility.runtime_profile != binding.runtime_profile
        || manifest.compatibility.migration_required
        || manifest.lifecycle_contract.startup != "plugin-startup/fail-closed-v1"
        || manifest.lifecycle_contract.readiness != "plugin-readiness/exact-binding-v1"
        || manifest.lifecycle_contract.liveness != "plugin-liveness/progress-v1"
        || manifest.lifecycle_contract.drain != "plugin-drain/bounded-v1"
        || manifest.lifecycle_contract.failure != "plugin-failure/stable-reason-v1"
        || manifest.lifecycle_contract.fallback != "plugin-fallback/none-v1"
    {
        return Err(HostError::new(
            ReasonCode::UnknownRuntimeProfile,
            "manifest contract/compatibility profile mismatch",
        ));
    }
    for values in [
        &manifest.skill_ids,
        &manifest.mcp_tool_ids,
        &manifest.mcp_resource_ids,
        &manifest.a2a_peer_ids,
        &manifest.network_egress_capability_ids,
        &manifest.secret_ref_ids,
        &manifest.compatibility.rollback_compatible_revisions,
    ] {
        let mut seen = BTreeSet::new();
        for value in values {
            validate_identity(value, "manifest declaration")?;
            if !seen.insert(value) {
                return Err(HostError::new(
                    ReasonCode::InvalidArgument,
                    "manifest declaration is duplicated",
                ));
            }
        }
    }
    if !manifest.skill_ids.is_empty()
        || !manifest.mcp_tool_ids.is_empty()
        || !manifest.mcp_resource_ids.is_empty()
        || !manifest.a2a_peer_ids.is_empty()
        || !manifest.network_egress_capability_ids.is_empty()
        || !manifest.filesystem_preopens.is_empty()
        || !manifest.secret_ref_ids.is_empty()
    {
        return Err(HostError::new(
            ReasonCode::CapabilityDenied,
            "first-release Host-managed manifest requests ambient/direct-agent capability",
        ));
    }
    if manifest.manifest_digest != binding.manifest_digest
        || compute_manifest_digest(manifest)? != binding.manifest_digest
    {
        return Err(HostError::new(
            ReasonCode::DigestMismatch,
            "manifest canonical digest mismatch",
        ));
    }
    if manifest.publisher.is_empty()
        || !config
            .trust
            .publishers
            .iter()
            .any(|value| value.publisher_identity == manifest.publisher)
    {
        return Err(HostError::new(
            ReasonCode::PublisherUntrusted,
            "publisher is not allowlisted",
        ));
    }
    let allowed_capability_kinds = [
        "read-source",
        "host-projection",
        "read-only-tool",
        "pure-transform",
        "external-source-readonly",
    ];
    let mut declared = BTreeSet::new();
    for capability in &manifest.capabilities {
        validate_identity(&capability.capability_id, "capability_id")?;
        if !capability.declared
            || !allowed_capability_kinds.contains(&capability.capability_kind.as_str())
            || !declared.insert(capability.capability_id.clone())
        {
            return Err(HostError::new(
                ReasonCode::CapabilityDenied,
                "manifest capability is undeclared, unknown or duplicated",
            ));
        }
        if capability.capability_kind == "external-source-readonly"
            && manifest.kind != "read-only-tool"
        {
            return Err(HostError::new(
                ReasonCode::CapabilityDenied,
                "external source requires read-only-tool",
            ));
        }
    }
    let granted: BTreeSet<_> = binding.granted_capabilities.iter().cloned().collect();
    if granted.len() != binding.granted_capabilities.len() || !granted.is_subset(&declared) {
        return Err(HostError::new(
            ReasonCode::CapabilityDenied,
            "binding capability expands manifest declaration",
        ));
    }
    if compute_capability_digest(&binding.granted_capabilities) != binding.capability_digest {
        return Err(HostError::new(
            ReasonCode::DigestMismatch,
            "capability digest mismatch",
        ));
    }
    validate_manifest_limits(config, &manifest.resource_limits, &manifest.runtime_profile)?;
    if compute_resource_profile_digest(&manifest.resource_limits)?
        != binding.resource_profile_digest
    {
        return Err(HostError::new(
            ReasonCode::DigestMismatch,
            "resource profile digest mismatch",
        ));
    }
    if binding.runtime_profile == "wasm-component/v1"
        && (manifest.wit_digest.as_deref() != Some(binding.wit_digest.as_str())
            || manifest.service_proto_digest.is_some()
            || manifest.artifact_media_type != "application/wasm"
            || manifest.entrypoint != "masi:plugin-transform@1.0.0#transform"
            || manifest.supported_platforms != ["wasm32-wasip2"])
    {
        return Err(HostError::new(
            ReasonCode::DigestMismatch,
            "Wasm WIT/service profile mismatch",
        ));
    }
    if binding.runtime_profile == "grpc-service/v1"
        && (manifest.service_proto_digest.as_deref() != Some(binding.service_proto_digest.as_str())
            || manifest.wit_digest.is_some()
            || manifest.artifact_media_type != "application/vnd.oci.image.manifest.v1+json"
            || manifest.entrypoint != "masi.plugin.service.v1.HostManagedPlugin"
            || manifest.supported_platforms != ["linux/amd64"])
    {
        return Err(HostError::new(
            ReasonCode::DigestMismatch,
            "service protobuf/WIT profile mismatch",
        ));
    }
    if !binding.statistics_definition_ids.is_empty() {
        return Err(HostError::new(
            ReasonCode::UnknownVersion,
            "ID-only statistics binding is not qualified",
        ));
    }
    let mut manifest_definitions = BTreeSet::new();
    for definition in &manifest.statistics_definitions {
        validate_identity(&definition.definition_id, "statistics definition")?;
        validate_identity(
            &definition.definition_revision,
            "statistics definition revision",
        )?;
        validate_digest(&definition.definition_digest)?;
        if !manifest_definitions.insert((
            definition.definition_id.clone(),
            definition.definition_revision.clone(),
            definition.definition_digest.clone(),
        )) {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "statistics definition identity/digest is duplicated",
            ));
        }
    }
    let mut binding_definitions = BTreeSet::new();
    for definition in &binding.statistics_definitions {
        validate_identity(&definition.definition_id, "statistics definition")?;
        validate_identity(
            &definition.definition_revision,
            "statistics definition revision",
        )?;
        validate_digest(&definition.definition_digest)?;
        if !binding_definitions.insert((
            definition.definition_id.clone(),
            definition.definition_revision.clone(),
            definition.definition_digest.clone(),
        )) {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "binding statistics definition is duplicated",
            ));
        }
    }
    if manifest_definitions != binding_definitions {
        return Err(HostError::new(
            ReasonCode::DigestMismatch,
            "binding statistics definition identity/digest set differs from manifest",
        ));
    }
    Ok(())
}

fn validate_manifest_limits(
    config: &HostConfig,
    limits: &ManifestLimits,
    runtime_profile: &str,
) -> HostResult<()> {
    let max_in_flight_bytes = config
        .limits
        .max_input_bytes
        .saturating_mul(config.limits.per_binding_in_flight);
    if limits.cpu_milli < 1
        || limits.memory_bytes < 1
        || limits.pid_count < 1
        || limits.fd_count < 1
        || limits.instance_count < 1
        || limits.instance_count as usize > config.limits.wasm_instances
        || limits.batch_records < 1
        || limits.batch_records > 10_000
        || limits.in_flight_bytes < 1
        || limits.in_flight_bytes as usize > max_in_flight_bytes
        || limits.concurrency < 1
        || limits.concurrency as usize > config.limits.per_binding_in_flight
        || limits.deadline_ms < 1
        || u64::from(limits.deadline_ms) > config.limits.max_deadline_ms
        || limits.output_bytes < 1
        || limits.output_bytes as usize > config.limits.max_output_bytes
        || limits.queue_depth < 1
        || limits.queue_depth as usize > config.limits.per_binding_queue_depth
        || limits.retry_max_attempts != 1
        || (runtime_profile == "wasm-component/v1"
            && (limits.linear_memory_bytes < 1
                || limits.linear_memory_bytes as usize > config.limits.wasm_linear_memory_bytes
                || limits.table_elements < 1
                || usize::try_from(limits.table_elements).unwrap_or(usize::MAX)
                    > config.limits.wasm_table_elements))
        || (runtime_profile == "grpc-service/v1"
            && (limits.linear_memory_bytes != 0 || limits.table_elements != 0))
    {
        return Err(HostError::new(
            ReasonCode::ResourceExhausted,
            "manifest resource request exceeds Host profile",
        ));
    }
    Ok(())
}

fn validate_plugin_config(
    binding: &BindingEnvelope,
    manifest: &PluginManifest,
    document: &PluginConfigDocument,
) -> HostResult<()> {
    if binding.config_schema_id != "plugin-config/v1" || document.schema_version != CONFIG_SCHEMA {
        return Err(HostError::new(
            ReasonCode::UnknownVersion,
            "plugin config schema rejected",
        ));
    }
    validate_identity(&document.config_id, "config_id")?;
    if document.values.len() > 64 || document.secret_refs.len() > 16 {
        return Err(HostError::new(
            ReasonCode::ResourceExhausted,
            "plugin config exceeds field bounds",
        ));
    }
    let raw = serde_json::to_vec(document).map_err(|error| {
        HostError::new(
            ReasonCode::InvalidArgument,
            format!("config canonicalization: {error}"),
        )
    })?;
    if sha256_bytes(&raw) != binding.config_digest {
        return Err(HostError::new(
            ReasonCode::DigestMismatch,
            "plugin config digest mismatch",
        ));
    }
    let forbidden = [
        "http://",
        "https://",
        "postgres://",
        "postgresql://",
        "mysql://",
        "mongodb://",
        "../",
        "-----BEGIN",
        "Bearer ",
    ];
    for (key, value) in &document.values {
        validate_identity(key, "config key")?;
        if let Some(value) = value.as_str()
            && (value.len() > 4096 || forbidden.iter().any(|needle| value.contains(needle)))
        {
            return Err(HostError::new(
                ReasonCode::CapabilityDenied,
                "plugin config contains endpoint, path or credential-like data",
            ));
        }
        if !(value.is_string() || value.is_number() || value.is_boolean() || value.is_null()) {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "plugin config values must be scalar",
            ));
        }
    }
    let mut secret_refs = BTreeSet::new();
    for value in &document.secret_refs {
        validate_identity(value, "secret_ref")?;
        if !secret_refs.insert(value) {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "duplicate secret_ref",
            ));
        }
    }
    let declared_secret_refs: BTreeSet<_> = manifest.secret_ref_ids.iter().collect();
    let configured_secret_refs: BTreeSet<_> = document.secret_refs.iter().collect();
    if declared_secret_refs != configured_secret_refs {
        return Err(HostError::new(
            ReasonCode::CapabilityDenied,
            "plugin config secret references differ from manifest declaration",
        ));
    }
    Ok(())
}

fn validate_bundle(
    config: &HostConfig,
    binding: &BindingEnvelope,
    manifest: &PluginManifest,
    bundle: &VerificationBundle,
    now: i64,
) -> HostResult<()> {
    if bundle.schema_version != "plugin-verification-bundle/v1"
        || bundle.verifier != "organization-ed25519/v1"
    {
        return Err(HostError::new(
            ReasonCode::UnknownVersion,
            "verification bundle profile rejected",
        ));
    }
    if sha256_bytes(&binding.verification_bundle_json) != binding.verification_bundle_digest {
        return Err(HostError::new(
            ReasonCode::DigestMismatch,
            "verification bundle digest mismatch",
        ));
    }
    if bundle.revoked {
        return Err(HostError::new(ReasonCode::Revoked, "artifact is revoked"));
    }
    if now > bundle.expires_at_unix_ms
        || bundle.verified_at_unix_ms < 1
        || bundle.expires_at_unix_ms <= bundle.verified_at_unix_ms
    {
        return Err(HostError::new(
            ReasonCode::TrustStale,
            "verification bundle is expired or malformed",
        ));
    }
    if bundle.artifact_digest != binding.artifact_digest
        || bundle.manifest_digest != binding.manifest_digest
        || bundle.publisher_identity != manifest.publisher
        || bundle.sbom_digest != manifest.sbom_digest
        || bundle.provenance_digest != manifest.provenance_digest
        || bundle.trust_policy_digest != config.trust.trust_policy_digest
    {
        return Err(HostError::new(
            ReasonCode::DigestMismatch,
            "verification bundle subject mismatch",
        ));
    }
    let publisher = config
        .trust
        .publishers
        .iter()
        .find(|value| value.publisher_identity == bundle.publisher_identity)
        .ok_or_else(|| {
            HostError::new(
                ReasonCode::PublisherUntrusted,
                "verification publisher is not configured",
            )
        })?;
    let public_key = hex::decode(&publisher.ed25519_public_key_hex).map_err(|_| {
        HostError::new(
            ReasonCode::PublisherUntrusted,
            "publisher public key is malformed",
        )
    })?;
    let signature = base64::engine::general_purpose::STANDARD
        .decode(&bundle.signature_base64)
        .map_err(|_| {
            HostError::new(
                ReasonCode::PublisherUntrusted,
                "verification signature is malformed",
            )
        })?;
    UnparsedPublicKey::new(&signature::ED25519, public_key)
        .verify(
            verification_signature_payload(bundle).as_bytes(),
            &signature,
        )
        .map_err(|_| {
            HostError::new(
                ReasonCode::PublisherUntrusted,
                "verification bundle signature rejected",
            )
        })?;
    Ok(())
}

/// Canonical signature payload for the offline verification bundle.
#[must_use]
pub fn verification_signature_payload(bundle: &VerificationBundle) -> String {
    format!(
        "{}|{}|{}|{}|{}|{}|{}|{}|{}",
        bundle.artifact_digest,
        bundle.manifest_digest,
        bundle.publisher_identity,
        bundle.sbom_digest,
        bundle.provenance_digest,
        bundle.trust_policy_digest,
        bundle.verified_at_unix_ms,
        bundle.expires_at_unix_ms,
        bundle.revoked
    )
}

/// Compute the exact Go-compatible manifest digest.
pub fn compute_manifest_digest(manifest: &PluginManifest) -> HostResult<String> {
    let mut hasher = Sha256::new();
    hasher.update(
        format!(
            "{}|{}|{}|{}|{}|{}",
            manifest.manifest_id,
            manifest.manifest_revision,
            manifest.plugin_id,
            manifest.kind,
            manifest.publisher,
            manifest.version
        )
        .as_bytes(),
    );
    hasher.update(serde_json::to_vec(&manifest.capabilities).map_err(|error| {
        HostError::new(
            ReasonCode::InvalidArgument,
            format!("capabilities canonicalization: {error}"),
        )
    })?);
    hasher.update(
        serde_json::to_vec(&manifest.resource_limits).map_err(|error| {
            HostError::new(
                ReasonCode::InvalidArgument,
                format!("limits canonicalization: {error}"),
            )
        })?,
    );
    hasher.update(
        format!(
            "|{}|{}|{}|{}|{}|{}|{}",
            manifest.runtime_profile,
            manifest.wit_digest.as_deref().unwrap_or_default(),
            manifest.service_proto_digest.as_deref().unwrap_or_default(),
            manifest.sbom_digest,
            manifest.provenance_digest,
            manifest.signature_status,
            manifest.scope
        )
        .as_bytes(),
    );
    Ok(format!("sha256:{}", hex::encode(hasher.finalize())))
}

/// Compute a deterministic binding-envelope digest using the protobuf source.
#[must_use]
pub fn compute_envelope_digest(binding: &BindingEnvelope) -> String {
    let mut canonical = binding.clone();
    canonical.envelope_digest.clear();
    sha256_bytes(&canonical.encode_to_vec())
}

/// Compute the sorted exact capability grant digest.
#[must_use]
pub fn compute_capability_digest(values: &[String]) -> String {
    let mut values = values.to_vec();
    values.sort();
    sha256_bytes(values.join("\n").as_bytes())
}

/// Compute the exact resource profile digest.
pub fn compute_resource_profile_digest(limits: &ManifestLimits) -> HostResult<String> {
    let raw = serde_json::to_vec(limits).map_err(|error| {
        HostError::new(
            ReasonCode::InvalidArgument,
            format!("limits canonicalization: {error}"),
        )
    })?;
    Ok(sha256_bytes(&raw))
}

/// SHA-256 in the repository's canonical text form.
#[must_use]
pub fn sha256_bytes(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    format!("sha256:{}", hex::encode(digest))
}

/// Exact WIT digest frozen by `wasm-component/v1`.
#[must_use]
pub const fn qualified_wit_digest() -> &'static str {
    "sha256:43e8eac37b165c4cfd84280cfe0e17cd6ca203abb2aa55b41c921890cef0567b"
}

/// Exact service protocol digest frozen by `grpc-service/v1`.
#[must_use]
pub const fn qualified_service_proto_digest() -> &'static str {
    "sha256:d64a04a8d9c1f0e0d58d4259f5c8c1342aec640ec31699e5f05ace7c50f6bf3e"
}

/// Exact strict plugin config JSON Schema digest frozen by the Host profile.
#[must_use]
pub const fn qualified_config_schema_digest() -> &'static str {
    "sha256:216bed348fe5ff7555ea800ebb7a780bbe544043ddfe72063144384af379c132"
}

fn resolve_artifact_path(config: &HostConfig, name: &str) -> HostResult<PathBuf> {
    let relative = PathBuf::from(name);
    let mut components = relative.components();
    let first = components.next();
    if !matches!(first, Some(Component::Normal(_)))
        || components.next().is_some()
        || name.len() > 255
    {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            "artifact_name must be one safe filename",
        ));
    }
    Ok(config.artifact_cache_root.join(name))
}

fn decode_bounded_json<T: for<'de> Deserialize<'de>>(
    raw: &[u8],
    max: usize,
    name: &str,
) -> HostResult<T> {
    if raw.is_empty() || raw.len() > max {
        return Err(HostError::new(
            ReasonCode::ResourceExhausted,
            format!("{name} exceeds byte bound"),
        ));
    }
    serde_json::from_slice(raw).map_err(|error| {
        HostError::new(
            ReasonCode::InvalidArgument,
            format!("{name} JSON rejected: {error}"),
        )
    })
}

fn validate_identity(value: &str, field: &str) -> HostResult<()> {
    if value.is_empty()
        || value.len() > 256
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._:-/".contains(&byte))
    {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            format!("{field} is malformed"),
        ));
    }
    Ok(())
}

fn unix_ms() -> HostResult<i64> {
    let duration = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| HostError::new(ReasonCode::Internal, "system clock precedes epoch"))?;
    i64::try_from(duration.as_millis())
        .map_err(|_| HostError::new(ReasonCode::Internal, "system clock overflow"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capability_digest_is_order_independent() {
        let a = vec!["b".to_owned(), "a".to_owned()];
        let b = vec!["a".to_owned(), "b".to_owned()];
        assert_eq!(compute_capability_digest(&a), compute_capability_digest(&b));
    }

    #[test]
    fn envelope_digest_excludes_only_claimed_digest() {
        let mut binding = BindingEnvelope {
            schema_version: BINDING_SCHEMA.to_owned(),
            plugin_id: "plugin.test".to_owned(),
            ..Default::default()
        };
        let digest = compute_envelope_digest(&binding);
        binding.envelope_digest = digest.clone();
        assert_eq!(compute_envelope_digest(&binding), digest);
        binding.plugin_id = "plugin.other".to_owned();
        assert_ne!(compute_envelope_digest(&binding), digest);
    }
}
