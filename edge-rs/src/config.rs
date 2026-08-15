//! Strict startup configuration and frozen implementation bounds.

use std::{
    collections::BTreeMap,
    net::{IpAddr, SocketAddr},
    path::{Path, PathBuf},
};

use ipnet::IpNet;
use serde::{Deserialize, Serialize};

use crate::{EdgeError, EdgeResult, digest};

/// Deployment claim scope. Module-test relaxations never apply to production.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum DeploymentTier {
    /// Isolated deterministic module black-box environment.
    ModuleTest,
    /// Fully runnable single failure domain, never production-qualified.
    OperationalSingleDomain,
    /// HA tier; qualification still depends on external evidence.
    ProductionHa,
}

/// TLS server identity for the Edge public control boundary.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ServerTlsConfig {
    /// Server certificate PEM.
    pub certificate_path: PathBuf,
    /// Server private key PEM.
    pub private_key_path: PathBuf,
    /// CA used to authenticate control clients.
    pub client_ca_path: PathBuf,
    /// Stable workload identity reference (not a secret).
    pub identity_ref: String,
    /// Exact DER SHA-256 allowlist for authorized EdgeControl clients.
    pub allowed_client_certificate_sha256: Vec<String>,
}

/// Outbound mTLS identity.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ClientTlsConfig {
    /// Trust anchor PEM.
    pub ca_path: PathBuf,
    /// Client certificate PEM.
    pub certificate_path: PathBuf,
    /// Client private key PEM.
    pub private_key_path: PathBuf,
    /// Exact expected DNS SAN.
    pub server_name: String,
    /// Stable credential reference.
    pub identity_ref: String,
}

/// Static Go Control sink boundary.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ControlSinkConfig {
    /// Registry-controlled HTTPS endpoint.
    pub endpoint: String,
    /// Dedicated control-sink identity.
    pub tls: ClientTlsConfig,
}

/// Endpoint policy used for every resolved external target.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EndpointPolicyConfig {
    /// Explicit approved management networks.
    pub allowed_management_cidrs: Vec<IpNet>,
    /// Exact loopback addresses permitted only in module tests.
    pub module_test_loopback_allowlist: Vec<IpAddr>,
    /// Bounded DNS resolution duration.
    pub resolution_deadline_ms: u64,
}

/// Frozen runtime resource limits. Every queue and durable store is bounded.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeLimits {
    /// Maximum active target actors.
    pub max_targets: usize,
    /// High priority control/effect commands per actor.
    pub actor_high_queue: usize,
    /// Buffered P4 stream requests per actor.
    pub p4_stream_request_queue: usize,
    /// Buffered P4 stream responses per actor.
    pub p4_stream_response_queue: usize,
    /// Durable DigestList records awaiting P4Runtime acknowledgement per actor.
    pub pending_digest_ack_queue: usize,
    /// Maximum P4 updates per Write.
    pub p4_updates_per_write: usize,
    /// Maximum entities per Read.
    pub p4_entities_per_read: usize,
    /// Maximum bytes accepted from a P4 Read stream.
    pub p4_read_response_bytes: usize,
    /// Maximum entities accepted across one P4 Read response stream.
    pub p4_read_response_entities: usize,
    /// P4 connection deadline.
    pub p4_connect_deadline_ms: u64,
    /// P4 arbitration deadline.
    pub p4_arbitration_deadline_ms: u64,
    /// P4 unary request deadline.
    pub p4_rpc_deadline_ms: u64,
    /// Maximum protobuf record written to any WAL.
    pub wal_record_bytes: usize,
    /// WAL segment rotation bound.
    pub wal_segment_bytes: u64,
    /// Oldest uncheckpointed WAL record age before a sticky target HOLD.
    pub wal_max_age_seconds: u64,
    /// Source WAL bytes per target.
    pub source_wal_bytes: u64,
    /// Source WAL records per target.
    pub source_wal_records: u64,
    /// Input WAL bytes per target.
    pub input_wal_bytes: u64,
    /// Input WAL records per target.
    pub input_wal_records: u64,
    /// In-memory input identities retained until canonical commit ACK.
    pub pending_input_identities: usize,
    /// Result WAL bytes per target.
    pub result_wal_bytes: u64,
    /// Result WAL records per target.
    pub result_wal_records: u64,
    /// Effect journal bytes per target.
    pub journal_bytes: u64,
    /// Effect journal records per target.
    pub journal_records: u64,
    /// Route journal bytes per target.
    pub route_journal_bytes: u64,
    /// Route journal records per target.
    pub route_journal_records: u64,
    /// Source polling period.
    pub telemetry_poll_interval_ms: u64,
    /// Event-time window width.
    pub window_duration_ms: u64,
    /// Allowed lateness after watermark.
    pub allowed_lateness_ms: u64,
    /// Processing-time silence required before an event-time source is idle.
    pub idle_source_timeout_ms: u64,
    /// Maximum open windows per source.
    pub max_open_windows: usize,
    /// Inference records per unary batch.
    pub inference_batch_records: usize,
    /// Inference request/response bytes.
    pub inference_message_bytes: usize,
    /// Inference request deadline.
    pub inference_deadline_ms: u64,
    /// Total same-generation attempts, including the initial request.
    pub inference_max_attempts: u32,
    /// Result records per Go commit request.
    pub control_batch_records: usize,
    /// Go request/response bytes.
    pub control_message_bytes: usize,
    /// Go RPC deadline.
    pub control_deadline_ms: u64,
    /// Rule counter polling interval.
    pub observation_interval_ms: u64,
    /// Buffered rule observation batches.
    pub observation_batches: usize,
    /// Maximum physical baseline rules.
    pub baseline_rules: usize,
    /// Maximum response overlay rules.
    pub overlay_rules: usize,
    /// Maximum preflight lifetime.
    pub preflight_validity_ms: u64,
    /// Total preflight deadline.
    pub preflight_deadline_ms: u64,
}

/// Complete process configuration.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EdgeConfig {
    /// Must be `edge-config/v1`.
    pub schema_version: String,
    /// Stable process incarnation for logs/status, never a target identity.
    pub edge_instance_id: String,
    /// Public gRPC listen address.
    pub listen_address: SocketAddr,
    /// Deployment claim scope.
    pub deployment_tier: DeploymentTier,
    /// Target-scoped durable root.
    pub data_dir: PathBuf,
    /// Public server mTLS configuration.
    pub server_tls: ServerTlsConfig,
    /// Static Go Control sink configuration.
    pub control_sink: ControlSinkConfig,
    /// Local credential registry addressed only by opaque public identity refs.
    pub client_identities: BTreeMap<String, ClientTlsConfig>,
    /// Registry endpoint policy.
    pub endpoint_policy: EndpointPolicyConfig,
    /// All explicit resource limits.
    pub limits: RuntimeLimits,
    /// Exact telemetry source qualification-profile digest.
    pub telemetry_source_profile_digest: String,
    /// Exact Edge feature/window profile digest.
    pub feature_profile_digest: String,
    /// Logging filter, e.g. `info,masi_edge=debug`.
    pub log_filter: String,
}

impl EdgeConfig {
    /// Load, strictly decode, and validate a configuration file.
    pub fn load(path: &Path) -> EdgeResult<(Self, String)> {
        let metadata = std::fs::symlink_metadata(path)
            .map_err(|source| EdgeError::io("stat edge config", source))?;
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return Err(EdgeError::invalid(
                "config_path",
                "configuration must be a regular non-symlink file",
            ));
        }
        if metadata.len() > 1_048_576 {
            return Err(EdgeError::exhausted(
                "CONFIG_TOO_LARGE",
                "configuration exceeds 1 MiB",
            ));
        }
        let payload =
            std::fs::read(path).map_err(|source| EdgeError::io("read edge config", source))?;
        let config: Self = serde_json::from_slice(&payload)
            .map_err(|error| EdgeError::invalid("config", error.to_string()))?;
        config.validate()?;
        Ok((config, digest::sha256(&payload)))
    }

    /// Validate cross-field bounds without performing network access.
    pub fn validate(&self) -> EdgeResult<()> {
        if self.schema_version != "edge-config/v1" {
            return Err(EdgeError::UnknownMajor(self.schema_version.clone()));
        }
        digest::validate_identity(&self.edge_instance_id, "edge_instance_id")?;
        digest::validate_identity(&self.server_tls.identity_ref, "server_tls.identity_ref")?;
        if self.server_tls.allowed_client_certificate_sha256.is_empty()
            || self.server_tls.allowed_client_certificate_sha256.len() > 32
        {
            return Err(EdgeError::invalid(
                "server_tls.allowed_client_certificate_sha256",
                "exact client certificate allowlist size must be in [1,32]",
            ));
        }
        for certificate_digest in &self.server_tls.allowed_client_certificate_sha256 {
            digest::validate_sha256(
                certificate_digest,
                "server_tls.allowed_client_certificate_sha256",
            )?;
        }
        digest::validate_identity(
            &self.control_sink.tls.identity_ref,
            "control_sink.tls.identity_ref",
        )?;
        validate_server_name(&self.control_sink.tls.server_name)?;
        if self.server_tls.identity_ref == self.control_sink.tls.identity_ref {
            return Err(EdgeError::invalid(
                "control_sink.tls.identity_ref",
                "inbound and outbound credentials must use distinct references",
            ));
        }
        if self.client_identities.is_empty() || self.client_identities.len() > 128 {
            return Err(EdgeError::invalid(
                "client_identities",
                "credential registry size must be in [1,128]",
            ));
        }
        for (identity_ref, identity) in &self.client_identities {
            digest::validate_identity(identity_ref, "client_identities.key")?;
            digest::validate_identity(&identity.identity_ref, "client_identities.identity_ref")?;
            if identity_ref != &identity.identity_ref {
                return Err(EdgeError::invalid(
                    "client_identities.identity_ref",
                    "registry key must equal the credential identity_ref",
                ));
            }
            if identity_ref == &self.server_tls.identity_ref
                || identity_ref == &self.control_sink.tls.identity_ref
            {
                return Err(EdgeError::invalid(
                    "client_identities.identity_ref",
                    "dynamic-boundary credential must differ from server/control identities",
                ));
            }
            validate_server_name(&identity.server_name)?;
        }
        if self.endpoint_policy.allowed_management_cidrs.is_empty()
            && self.deployment_tier != DeploymentTier::ModuleTest
        {
            return Err(EdgeError::invalid(
                "endpoint_policy.allowed_management_cidrs",
                "production-capable tiers require an explicit management allowlist",
            ));
        }
        if !(1..=10_000).contains(&self.endpoint_policy.resolution_deadline_ms) {
            return Err(EdgeError::invalid(
                "endpoint_policy.resolution_deadline_ms",
                "must be in [1,10000]",
            ));
        }
        self.limits.validate()?;
        digest::validate_sha256(
            &self.telemetry_source_profile_digest,
            "telemetry_source_profile_digest",
        )?;
        digest::validate_sha256(&self.feature_profile_digest, "feature_profile_digest")?;
        let mut private_key_paths = vec![
            &self.server_tls.private_key_path,
            &self.control_sink.tls.private_key_path,
        ];
        private_key_paths.extend(
            self.client_identities
                .values()
                .map(|identity| &identity.private_key_path),
        );
        ensure_distinct_paths(&private_key_paths)?;
        Ok(())
    }
}

impl RuntimeLimits {
    fn validate(&self) -> EdgeResult<()> {
        bounded("max_targets", self.max_targets, 1, 32)?;
        bounded("actor_high_queue", self.actor_high_queue, 1, 128)?;
        bounded(
            "p4_stream_request_queue",
            self.p4_stream_request_queue,
            1,
            128,
        )?;
        bounded(
            "p4_stream_response_queue",
            self.p4_stream_response_queue,
            1,
            4096,
        )?;
        bounded(
            "pending_digest_ack_queue",
            self.pending_digest_ack_queue,
            1,
            256,
        )?;
        bounded("p4_updates_per_write", self.p4_updates_per_write, 1, 256)?;
        bounded("p4_entities_per_read", self.p4_entities_per_read, 1, 256)?;
        bounded(
            "p4_read_response_bytes",
            self.p4_read_response_bytes,
            1024,
            1_048_576,
        )?;
        bounded(
            "p4_read_response_entities",
            self.p4_read_response_entities,
            1,
            4_096,
        )?;
        bounded("wal_record_bytes", self.wal_record_bytes, 1024, 4_194_304)?;
        bounded("max_open_windows", self.max_open_windows, 1, 64)?;
        bounded(
            "inference_batch_records",
            self.inference_batch_records,
            1,
            256,
        )?;
        bounded("control_batch_records", self.control_batch_records, 1, 256)?;
        bounded("observation_batches", self.observation_batches, 1, 8)?;
        bounded("baseline_rules", self.baseline_rules, 1, 4096)?;
        bounded("overlay_rules", self.overlay_rules, 1, 1024)?;
        if self.wal_segment_bytes < self.wal_record_bytes as u64 + 36
            || self.wal_segment_bytes > 67_108_864
        {
            return Err(EdgeError::invalid(
                "wal_segment_bytes",
                "must fit one maximum record and be no greater than 64 MiB",
            ));
        }
        bounded_u64("wal_max_age_seconds", self.wal_max_age_seconds, 1, 86_400)?;
        for (field, value, maximum) in [
            ("source_wal_bytes", self.source_wal_bytes, 1_073_741_824),
            ("input_wal_bytes", self.input_wal_bytes, 2_147_483_648),
            ("result_wal_bytes", self.result_wal_bytes, 2_147_483_648),
            ("journal_bytes", self.journal_bytes, 536_870_912),
            ("route_journal_bytes", self.route_journal_bytes, 67_108_864),
        ] {
            if value < self.wal_segment_bytes || value > maximum {
                return Err(EdgeError::invalid(
                    field,
                    format!("must be at least one segment and no greater than {maximum}"),
                ));
            }
        }
        for (field, value, maximum) in [
            ("source_wal_records", self.source_wal_records, 1_048_576),
            ("input_wal_records", self.input_wal_records, 524_288),
            ("result_wal_records", self.result_wal_records, 524_288),
            ("journal_records", self.journal_records, 262_144),
            ("route_journal_records", self.route_journal_records, 4_096),
        ] {
            if value == 0 || value > maximum {
                return Err(EdgeError::invalid(
                    field,
                    format!("must be in [1,{maximum}]"),
                ));
            }
        }
        bounded(
            "pending_input_identities",
            self.pending_input_identities,
            1,
            524_288,
        )?;
        if (self.pending_input_identities as u64) < self.input_wal_records {
            return Err(EdgeError::invalid(
                "pending_input_identities",
                "must be at least input_wal_records so memory admission cannot discard a durable final window before WAL admission",
            ));
        }
        let minimum_read_response_entities = self
            .p4_entities_per_read
            .max(self.baseline_rules)
            .max(self.overlay_rules);
        if self.p4_read_response_entities < minimum_read_response_entities {
            return Err(EdgeError::invalid(
                "p4_read_response_entities",
                "must cover the largest qualified request or table capacity",
            ));
        }
        bounded_u64(
            "window_duration_ms",
            self.window_duration_ms,
            if cfg!(test) { 10 } else { 100 },
            60_000,
        )?;
        if self.allowed_lateness_ms > 60_000 {
            return Err(EdgeError::invalid(
                "allowed_lateness_ms",
                "must be no greater than 60000",
            ));
        }
        if !(1..=3).contains(&self.inference_max_attempts) {
            return Err(EdgeError::invalid(
                "inference_max_attempts",
                "must be in [1,3]",
            ));
        }
        if self.preflight_validity_ms == 0 || self.preflight_validity_ms > 30_000 {
            return Err(EdgeError::invalid(
                "preflight_validity_ms",
                "must be in [1,30000]",
            ));
        }
        if self.preflight_deadline_ms == 0 || self.preflight_deadline_ms > 10_000 {
            return Err(EdgeError::invalid(
                "preflight_deadline_ms",
                "must be in [1,10000]",
            ));
        }
        for (field, value) in [
            ("p4_connect_deadline_ms", self.p4_connect_deadline_ms),
            (
                "p4_arbitration_deadline_ms",
                self.p4_arbitration_deadline_ms,
            ),
            ("p4_rpc_deadline_ms", self.p4_rpc_deadline_ms),
            (
                "telemetry_poll_interval_ms",
                self.telemetry_poll_interval_ms,
            ),
            ("inference_deadline_ms", self.inference_deadline_ms),
            ("control_deadline_ms", self.control_deadline_ms),
            ("observation_interval_ms", self.observation_interval_ms),
            ("idle_source_timeout_ms", self.idle_source_timeout_ms),
        ] {
            bounded_u64(field, value, 1, 60_000)?;
        }
        for (field, value) in [
            ("inference_message_bytes", self.inference_message_bytes),
            ("control_message_bytes", self.control_message_bytes),
        ] {
            bounded(field, value, 1024, 4_194_304)?;
        }
        Ok(())
    }
}

fn bounded(field: &'static str, value: usize, min: usize, max: usize) -> EdgeResult<()> {
    if !(min..=max).contains(&value) {
        return Err(EdgeError::invalid(
            field,
            format!("must be in [{min},{max}]"),
        ));
    }
    Ok(())
}

fn bounded_u64(field: &'static str, value: u64, min: u64, max: u64) -> EdgeResult<()> {
    if !(min..=max).contains(&value) {
        return Err(EdgeError::invalid(
            field,
            format!("must be in [{min},{max}]"),
        ));
    }
    Ok(())
}

/// Validate a TLS server name without resolving it.
pub fn validate_server_name(value: &str) -> EdgeResult<()> {
    if value.is_empty()
        || value.len() > 253
        || value.contains('*')
        || value.contains('/')
        || value.parse::<IpAddr>().is_ok()
    {
        return Err(EdgeError::invalid(
            "tls.server_name",
            "must be a bounded exact DNS name without wildcard",
        ));
    }
    Ok(())
}

fn ensure_distinct_paths(paths: &[&PathBuf]) -> EdgeResult<()> {
    for (index, left) in paths.iter().enumerate() {
        for right in paths.iter().skip(index + 1) {
            if left == right {
                return Err(EdgeError::invalid(
                    "tls.private_key_path",
                    "separate boundary identities must not share a private-key path",
                ));
            }
        }
    }
    Ok(())
}

/// Read a bounded PEM file and reject symlinks. Private keys must not be
/// accessible by group or other users on Unix.
pub fn read_pem(path: &Path, private_key: bool) -> EdgeResult<Vec<u8>> {
    let metadata = std::fs::symlink_metadata(path)
        .map_err(|source| EdgeError::io("stat TLS material", source))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(EdgeError::TlsIdentityMismatch(
            "TLS material must be a regular non-symlink file".into(),
        ));
    }
    if metadata.len() == 0 || metadata.len() > 1_048_576 {
        return Err(EdgeError::TlsIdentityMismatch(
            "TLS material length is outside [1,1048576]".into(),
        ));
    }
    #[cfg(unix)]
    if private_key {
        use std::os::unix::fs::PermissionsExt as _;
        if metadata.permissions().mode() & 0o077 != 0 {
            return Err(EdgeError::TlsIdentityMismatch(
                "private key grants group or other permissions".into(),
            ));
        }
    }
    std::fs::read(path).map_err(|source| EdgeError::io("read TLS material", source))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn server_name_rejects_wildcard_and_ip() {
        assert!(validate_server_name("masi-switch.test").is_ok());
        assert!(validate_server_name("*.test").is_err());
        assert!(validate_server_name("127.0.0.1").is_err());
    }
}
