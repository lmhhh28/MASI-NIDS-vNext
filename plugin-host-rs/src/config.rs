//! Strict, bounded Host configuration and secure file loading.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::OpenOptions;
use std::io::Read;
use std::net::SocketAddr;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{HostError, HostResult, ReasonCode};

const MAX_CONFIG_BYTES: usize = 1024 * 1024;

/// Strict Host configuration.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct HostConfig {
    /// Contract schema version.
    pub schema_version: String,
    /// Stable workload identity.
    pub host_id: String,
    /// Exact runtime profile.
    pub profile_id: String,
    /// Manager-facing mTLS gRPC listener.
    pub listen_address: String,
    /// Loopback/container health and metrics listener.
    pub health_address: String,
    /// Read-only component cache root.
    pub artifact_cache_root: PathBuf,
    /// Server mTLS configuration.
    pub server_tls: ServerTlsConfig,
    /// Offline publisher and revocation policy.
    pub trust: TrustConfig,
    /// Hard Host and runtime bounds.
    pub limits: Limits,
    /// Explicit Host-managed service endpoints.
    pub service_endpoints: Vec<ServiceEndpoint>,
}

/// Manager-facing mTLS identity configuration.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ServerTlsConfig {
    /// PEM server certificate.
    pub certificate_path: PathBuf,
    /// PEM private key; never logged.
    pub private_key_path: PathBuf,
    /// PEM client CA.
    pub client_ca_path: PathBuf,
    /// Exact client leaf certificate DER SHA-256 allowlist.
    pub allowed_client_certificate_sha256: Vec<String>,
}

/// Offline trust configuration.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TrustConfig {
    /// Exact canonical trust policy digest.
    pub trust_policy_digest: String,
    /// Maximum age of the revocation observation.
    pub revocation_max_age_ms: i64,
    /// Accepted publisher identities and Ed25519 public keys.
    pub publishers: Vec<PublisherTrust>,
}

/// One accepted publisher identity.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PublisherTrust {
    /// Stable publisher identity.
    pub publisher_identity: String,
    /// Raw 32-byte Ed25519 public key encoded as lower-case hex.
    pub ed25519_public_key_hex: String,
}

/// Global and per-binding resource limits.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Limits {
    /// Maximum observed bindings.
    pub max_bindings: usize,
    /// Global queued+running invocation cap.
    pub global_queue_depth: usize,
    /// Per-binding queue cap.
    pub per_binding_queue_depth: usize,
    /// Per-binding execution cap.
    pub per_binding_in_flight: usize,
    /// Manager control frame cap.
    pub max_control_message_bytes: usize,
    /// Input cap.
    pub max_input_bytes: usize,
    /// Output cap.
    pub max_output_bytes: usize,
    /// Total invocation deadline cap.
    pub max_deadline_ms: u64,
    /// Maximum semaphore wait before explicit backpressure.
    pub queue_wait_ms: u64,
    /// Wasm fuel per call.
    pub wasm_fuel: u64,
    /// Wasm linear memory cap.
    pub wasm_linear_memory_bytes: usize,
    /// Wasm table element cap.
    pub wasm_table_elements: usize,
    /// Wasm instance cap.
    pub wasm_instances: usize,
    /// Wasm memory count cap.
    pub wasm_memories: usize,
    /// Wasm table count cap.
    pub wasm_tables: usize,
    /// Wasm stack cap.
    pub wasm_stack_bytes: usize,
    /// Epoch ticker interval.
    pub epoch_tick_ms: u64,
    /// Failures before the per-binding circuit opens.
    pub failure_threshold: u32,
    /// Circuit-open interval.
    pub circuit_open_ms: u64,
    /// Restart accounting window.
    pub restart_window_ms: u64,
    /// Restart attempts allowed inside the window.
    pub max_restarts_in_window: usize,
    /// Quarantine interval after restart budget exhaustion.
    pub quarantine_ms: u64,
    /// Default binding drain deadline.
    pub drain_deadline_ms: u64,
    /// Whole-Host shutdown deadline.
    pub shutdown_deadline_ms: u64,
}

/// One allowlisted, already-deployed Host-managed service endpoint.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ServiceEndpoint {
    /// Stable reference used by the binding envelope.
    pub endpoint_ref: String,
    /// Only `uds` is qualified in the first module scope.
    pub transport: String,
    /// Absolute per-binding socket path.
    pub uds_path: PathBuf,
    /// Expected socket producer UID.
    pub expected_uid: u32,
    /// Expected socket producer GID.
    pub expected_gid: u32,
    /// Exact workload identity returned by the typed handshake.
    pub expected_workload_identity: String,
    /// Exact public service protocol digest.
    pub service_proto_digest: String,
    /// Exact already-deployed OCI artifact digest.
    pub artifact_digest: String,
}

impl HostConfig {
    /// Load and validate a strict configuration from a regular, no-symlink file.
    pub fn load(path: &Path) -> HostResult<Self> {
        let raw = read_secure_file(path, MAX_CONFIG_BYTES, false)?;
        let value: Self = serde_json::from_slice(&raw).map_err(|error| {
            HostError::new(ReasonCode::InvalidArgument, format!("config JSON: {error}"))
        })?;
        value.validate()?;
        Ok(value)
    }

    /// Parse the Manager listener address.
    pub fn listen_socket(&self) -> HostResult<SocketAddr> {
        self.listen_address.parse().map_err(|error| {
            HostError::new(
                ReasonCode::InvalidArgument,
                format!("listen_address: {error}"),
            )
        })
    }

    /// Parse the health listener address.
    pub fn health_socket(&self) -> HostResult<SocketAddr> {
        self.health_address.parse().map_err(|error| {
            HostError::new(
                ReasonCode::InvalidArgument,
                format!("health_address: {error}"),
            )
        })
    }

    /// Create an endpoint lookup that rejects duplicate references.
    pub fn endpoint_map(&self) -> HostResult<BTreeMap<String, ServiceEndpoint>> {
        let mut map = BTreeMap::new();
        for endpoint in &self.service_endpoints {
            if map
                .insert(endpoint.endpoint_ref.clone(), endpoint.clone())
                .is_some()
            {
                return Err(HostError::new(
                    ReasonCode::InvalidArgument,
                    "duplicate service endpoint_ref",
                ));
            }
        }
        Ok(map)
    }

    fn validate(&self) -> HostResult<()> {
        if self.schema_version != "plugin-host-config/v1"
            || self.profile_id != "plugin-runtime-host/v1"
        {
            return Err(HostError::new(
                ReasonCode::UnknownVersion,
                "Host config schema/profile mismatch",
            ));
        }
        if self.host_id.is_empty() || self.host_id.len() > 128 {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "host_id is malformed",
            ));
        }
        self.listen_socket()?;
        self.health_socket()?;
        validate_absolute_regular_root(&self.artifact_cache_root)?;
        if self.trust.revocation_max_age_ms < 1 || self.trust.revocation_max_age_ms > 300_000 {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "revocation_max_age_ms exceeds profile",
            ));
        }
        validate_digest(&self.trust.trust_policy_digest)?;
        if self.trust.publishers.is_empty() || self.trust.publishers.len() > 32 {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "publisher allowlist size invalid",
            ));
        }
        let mut publishers = BTreeSet::new();
        for publisher in &self.trust.publishers {
            if publisher.publisher_identity.is_empty()
                || !publishers.insert(publisher.publisher_identity.clone())
                || publisher.ed25519_public_key_hex.len() != 64
                || hex::decode(&publisher.ed25519_public_key_hex).is_err()
            {
                return Err(HostError::new(
                    ReasonCode::InvalidArgument,
                    "publisher trust entry malformed or duplicate",
                ));
            }
        }
        let mut clients = BTreeSet::new();
        for digest in &self.server_tls.allowed_client_certificate_sha256 {
            validate_digest(digest)?;
            if !clients.insert(digest) {
                return Err(HostError::new(
                    ReasonCode::InvalidArgument,
                    "duplicate manager certificate digest",
                ));
            }
        }
        if clients.is_empty() {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "manager certificate allowlist is empty",
            ));
        }
        validate_limits(&self.limits)?;
        let _ = self.endpoint_map()?;
        for endpoint in &self.service_endpoints {
            if endpoint.transport != "uds" || !endpoint.uds_path.is_absolute() {
                return Err(HostError::new(
                    ReasonCode::InvalidArgument,
                    "only absolute UDS endpoints are qualified",
                ));
            }
            if endpoint.expected_workload_identity.is_empty()
                || endpoint.expected_workload_identity.len() > 128
                || !endpoint
                    .expected_workload_identity
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"._:-/".contains(&byte))
            {
                return Err(HostError::new(
                    ReasonCode::InvalidArgument,
                    "service workload identity is malformed",
                ));
            }
            validate_digest(&endpoint.service_proto_digest)?;
            validate_digest(&endpoint.artifact_digest)?;
        }
        Ok(())
    }
}

fn validate_limits(limits: &Limits) -> HostResult<()> {
    let valid = (1..=32).contains(&limits.max_bindings)
        && (1..=64).contains(&limits.global_queue_depth)
        && (1..=32).contains(&limits.per_binding_queue_depth)
        && (1..=2).contains(&limits.per_binding_in_flight)
        && (1024..=4_194_304).contains(&limits.max_control_message_bytes)
        && (1..=2_097_152).contains(&limits.max_input_bytes)
        && (1..=1_048_576).contains(&limits.max_output_bytes)
        && (1..=10_000).contains(&limits.max_deadline_ms)
        && (1..=1000).contains(&limits.queue_wait_ms)
        && (1000..=50_000_000).contains(&limits.wasm_fuel)
        && (65_536..=67_108_864).contains(&limits.wasm_linear_memory_bytes)
        && (1..=10_000).contains(&limits.wasm_table_elements)
        && (1..=32).contains(&limits.wasm_instances)
        && (1..=2).contains(&limits.wasm_memories)
        && (1..=4).contains(&limits.wasm_tables)
        && (65_536..=2_097_152).contains(&limits.wasm_stack_bytes)
        && (1..=100).contains(&limits.epoch_tick_ms)
        && (1..=16).contains(&limits.failure_threshold)
        && limits.circuit_open_ms == 1000
        && limits.restart_window_ms == 600_000
        && limits.max_restarts_in_window == 5
        && limits.quarantine_ms == 900_000
        && (100..=60_000).contains(&limits.drain_deadline_ms)
        && (100..=60_000).contains(&limits.shutdown_deadline_ms);
    if !valid {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            "Host limits exceed plugin-runtime-host/v1",
        ));
    }
    Ok(())
}

/// Validate a lower-case SHA-256 digest.
pub fn validate_digest(value: &str) -> HostResult<()> {
    let suffix = value
        .strip_prefix("sha256:")
        .ok_or_else(|| HostError::new(ReasonCode::InvalidArgument, "digest must use sha256"))?;
    if suffix.len() != 64
        || !suffix
            .bytes()
            .all(|value| value.is_ascii_digit() || (b'a'..=b'f').contains(&value))
        || suffix.bytes().all(|value| value == b'0')
    {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            "digest is not canonical lower-case SHA-256",
        ));
    }
    Ok(())
}

/// Read a bounded regular file while rejecting symlink path components and races.
pub fn read_secure_file(path: &Path, max_bytes: usize, private: bool) -> HostResult<Vec<u8>> {
    validate_no_symlink_path(path, true)?;
    let before = std::fs::metadata(path).map_err(|error| {
        HostError::new(
            ReasonCode::InvalidArgument,
            format!("file metadata: {error}"),
        )
    })?;
    if !before.is_file() {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            "path is not a regular file",
        ));
    }
    if private && before.permissions().mode() & 0o077 != 0 {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            "private file is group/other accessible",
        ));
    }
    if before.len() > max_bytes as u64 {
        return Err(HostError::new(
            ReasonCode::ResourceExhausted,
            "file exceeds byte limit",
        ));
    }
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)
        .map_err(|error| {
            HostError::new(
                ReasonCode::InvalidArgument,
                format!("secure file open: {error}"),
            )
        })?;
    let opened = file.metadata().map_err(|error| {
        HostError::new(
            ReasonCode::InvalidArgument,
            format!("opened file metadata: {error}"),
        )
    })?;
    if opened.dev() != before.dev() || opened.ino() != before.ino() || opened.len() != before.len()
    {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            "file changed during secure open",
        ));
    }
    let mut bytes = Vec::with_capacity(opened.len() as usize);
    file.by_ref()
        .take((max_bytes + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|error| {
            HostError::new(
                ReasonCode::InvalidArgument,
                format!("secure file read: {error}"),
            )
        })?;
    if bytes.len() > max_bytes {
        return Err(HostError::new(
            ReasonCode::ResourceExhausted,
            "file exceeds byte limit",
        ));
    }
    Ok(bytes)
}

/// Reject relative, parent, symlink and non-directory cache roots.
pub fn validate_absolute_regular_root(path: &Path) -> HostResult<()> {
    if !path.is_absolute() {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            "path must be absolute",
        ));
    }
    validate_no_symlink_path(path, true)?;
    let metadata = std::fs::metadata(path).map_err(|error| {
        HostError::new(
            ReasonCode::InvalidArgument,
            format!("root metadata: {error}"),
        )
    })?;
    if !metadata.is_dir() {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            "artifact cache root is not a directory",
        ));
    }
    Ok(())
}

/// Reject every symbolic-link component in an absolute path.
pub fn validate_no_symlink_path(path: &Path, require_leaf: bool) -> HostResult<()> {
    if !path.is_absolute() {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            "path must be absolute",
        ));
    }
    let mut current = PathBuf::from("/");
    let components: Vec<_> = path.components().collect();
    for (index, component) in components.iter().enumerate() {
        match component {
            Component::RootDir => continue,
            Component::Normal(value) => current.push(value),
            _ => {
                return Err(HostError::new(
                    ReasonCode::InvalidArgument,
                    "path contains non-normal component",
                ));
            }
        }
        let is_leaf = index + 1 == components.len();
        match std::fs::symlink_metadata(&current) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err(HostError::new(
                    ReasonCode::InvalidArgument,
                    "symbolic-link path component rejected",
                ));
            }
            Ok(_) => {}
            Err(error)
                if is_leaf && !require_leaf && error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(HostError::new(
                    ReasonCode::InvalidArgument,
                    format!("path component metadata: {error}"),
                ));
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::symlink;

    use super::*;

    #[test]
    fn rejects_symlink_boundary() -> Result<(), Box<dyn std::error::Error>> {
        let temp = tempfile::tempdir()?;
        let target = temp.path().join("target");
        std::fs::write(&target, b"safe")?;
        let link = temp.path().join("link");
        symlink(&target, &link)?;
        let Err(error) = read_secure_file(&link, 16, false) else {
            return Err("symlink boundary unexpectedly accepted".into());
        };
        assert_eq!(error.reason, ReasonCode::InvalidArgument);
        Ok(())
    }

    #[test]
    fn digest_is_strict() {
        assert!(validate_digest(&format!("sha256:{}", "a".repeat(64))).is_ok());
        assert!(validate_digest(&format!("sha256:{}", "A".repeat(64))).is_err());
        assert!(validate_digest(&format!("sha256:{}", "0".repeat(64))).is_err());
        assert!(validate_digest("sha1:abc").is_err());
    }
}
