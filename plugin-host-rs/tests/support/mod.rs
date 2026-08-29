#![allow(dead_code, missing_docs)]

use std::fs::Permissions;
use std::net::{SocketAddr, TcpListener};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use base64::Engine as _;
use masi_plugin_host::admission::{
    Capability, Compatibility, LifecycleContract, ManifestLimits, PluginConfigDocument,
    PluginManifest, StatisticsDefinitionRef, VerificationBundle, compute_capability_digest,
    compute_envelope_digest, compute_manifest_digest, compute_resource_profile_digest,
    qualified_config_schema_digest, qualified_service_proto_digest, qualified_wit_digest,
    sha256_bytes, verification_signature_payload,
};
use masi_plugin_host::config::{
    HostConfig, Limits, PublisherTrust, ServerTlsConfig, ServiceEndpoint, TrustConfig,
};
use masi_plugin_host::contract::host::{BindingEnvelope, StatisticsDefinitionBinding};
use rcgen::{
    BasicConstraints, CertificateParams, CertifiedIssuer, DnType, ExtendedKeyUsagePurpose, IsCa,
    KeyPair, KeyUsagePurpose,
};
use ring::signature::{Ed25519KeyPair, KeyPair as _};

pub const PLUGIN_SCOPE: &str = "tenant:test";
pub const WASM_PLUGIN_ID: &str = "plugin.wasm.fixture";
pub const WASM_STATISTICS_PLUGIN_ID: &str = "plugin.wasm.statistics.fixture";
pub const SERVICE_PLUGIN_ID: &str = "plugin.service.fixture";
pub const STATISTICS_DEFINITION_ID: &str = "fixture.row-count";
pub const WORKLOAD_IDENTITY: &str = "spiffe://masi.test/plugin/service-fixture";

pub struct TestEnvironment {
    pub temporary: tempfile::TempDir,
    pub config: HostConfig,
    pub config_path: PathBuf,
    pub ca_pem: Vec<u8>,
    pub manager_certificate_pem: Vec<u8>,
    pub manager_private_key_pem: Vec<u8>,
    pub other_certificate_pem: Vec<u8>,
    pub other_private_key_pem: Vec<u8>,
    pub listen_address: SocketAddr,
    pub health_address: SocketAddr,
    pub service_socket: PathBuf,
    pub service_artifact_digest: String,
}

impl TestEnvironment {
    pub fn new(with_service: bool) -> Result<Self, Box<dyn std::error::Error>> {
        let temporary = tempfile::tempdir()?;
        let artifact_cache = temporary.path().join("artifacts");
        std::fs::create_dir(&artifact_cache)?;
        let tls_dir = temporary.path().join("tls");
        std::fs::create_dir(&tls_dir)?;
        let tls = generate_tls_material()?;
        let certificate_path = tls_dir.join("server.pem");
        let private_key_path = tls_dir.join("server-key.pem");
        let client_ca_path = tls_dir.join("ca.pem");
        std::fs::write(&certificate_path, &tls.server_certificate_pem)?;
        write_private(&private_key_path, &tls.server_private_key_pem)?;
        std::fs::write(&client_ca_path, &tls.ca_pem)?;

        let service_dir = temporary.path().join("service");
        std::fs::create_dir(&service_dir)?;
        std::fs::set_permissions(&service_dir, Permissions::from_mode(0o750))?;
        let service_socket = service_dir.join("generation-1.sock");
        let service_artifact_digest = digest("service-fixture-oci-artifact");
        let metadata = std::fs::metadata(&service_dir)?;
        let service_endpoints = if with_service {
            vec![ServiceEndpoint {
                endpoint_ref: "fixture-generation-1".to_owned(),
                transport: "uds".to_owned(),
                uds_path: service_socket.clone(),
                expected_uid: metadata.uid(),
                expected_gid: metadata.gid(),
                expected_workload_identity: WORKLOAD_IDENTITY.to_owned(),
                service_proto_digest: qualified_service_proto_digest().to_owned(),
                artifact_digest: service_artifact_digest.clone(),
            }]
        } else {
            Vec::new()
        };
        let publisher_key = publisher_key()?;
        let config = HostConfig {
            schema_version: "plugin-host-config/v1".to_owned(),
            host_id: "plugin-host-test-1".to_owned(),
            profile_id: "plugin-runtime-host/v1".to_owned(),
            listen_address: free_address()?.to_string(),
            health_address: free_address()?.to_string(),
            artifact_cache_root: artifact_cache,
            server_tls: ServerTlsConfig {
                certificate_path,
                private_key_path,
                client_ca_path,
                allowed_client_certificate_sha256: vec![sha256_bytes(&tls.manager_certificate_der)],
            },
            trust: TrustConfig {
                trust_policy_digest: digest("fixture-trust-policy"),
                revocation_max_age_ms: 300_000,
                publishers: vec![PublisherTrust {
                    publisher_identity: "publisher.test".to_owned(),
                    ed25519_public_key_hex: hex::encode(publisher_key.public_key().as_ref()),
                }],
            },
            limits: qualified_limits(),
            service_endpoints,
        };
        let listen_address = config.listen_socket()?;
        let health_address = config.health_socket()?;
        let config_path = temporary.path().join("host-config.json");
        std::fs::write(&config_path, serde_json::to_vec_pretty(&config)?)?;
        Ok(Self {
            temporary,
            config,
            config_path,
            ca_pem: tls.ca_pem,
            manager_certificate_pem: tls.manager_certificate_pem,
            manager_private_key_pem: tls.manager_private_key_pem,
            other_certificate_pem: tls.other_certificate_pem,
            other_private_key_pem: tls.other_private_key_pem,
            listen_address,
            health_address,
            service_socket,
            service_artifact_digest,
        })
    }

    pub fn wasm_binding(
        &self,
        generation: u64,
        desired_state: &str,
    ) -> Result<BindingEnvelope, Box<dyn std::error::Error>> {
        let artifact_name = format!("wasm-fixture-{generation}.wasm");
        let artifact = qualified_component()?;
        std::fs::write(
            self.config.artifact_cache_root.join(&artifact_name),
            &artifact,
        )?;
        build_binding(
            &self.config,
            BindingInput {
                plugin_id: WASM_PLUGIN_ID,
                plugin_revision: &format!("wasm-revision-{generation}"),
                generation,
                desired_state,
                runtime_profile: "wasm-component/v1",
                kind: "pure-transform",
                artifact_digest: sha256_bytes(&artifact),
                artifact_bytes: artifact.len() as u64,
                artifact_name: &artifact_name,
                service_endpoint_ref: "",
                capabilities: vec![Capability {
                    capability_id: "plugin.transform.execute".to_owned(),
                    capability_kind: "pure-transform".to_owned(),
                    declared: true,
                }],
                statistics_definitions: Vec::new(),
            },
        )
    }

    pub fn service_binding(
        &self,
        desired_state: &str,
    ) -> Result<BindingEnvelope, Box<dyn std::error::Error>> {
        build_binding(
            &self.config,
            BindingInput {
                plugin_id: SERVICE_PLUGIN_ID,
                plugin_revision: "service-revision-1",
                generation: 1,
                desired_state,
                runtime_profile: "grpc-service/v1",
                kind: "pure-transform",
                artifact_digest: self.service_artifact_digest.clone(),
                artifact_bytes: 1,
                artifact_name: "",
                service_endpoint_ref: "fixture-generation-1",
                capabilities: vec![Capability {
                    capability_id: "plugin.transform.execute".to_owned(),
                    capability_kind: "pure-transform".to_owned(),
                    declared: true,
                }],
                statistics_definitions: Vec::new(),
            },
        )
    }

    pub fn service_statistics_binding(
        &self,
        desired_state: &str,
    ) -> Result<BindingEnvelope, Box<dyn std::error::Error>> {
        build_binding(
            &self.config,
            BindingInput {
                plugin_id: SERVICE_PLUGIN_ID,
                plugin_revision: "service-statistics-revision-1",
                generation: 1,
                desired_state,
                runtime_profile: "grpc-service/v1",
                kind: "pure-transform",
                artifact_digest: self.service_artifact_digest.clone(),
                artifact_bytes: 1,
                artifact_name: "",
                service_endpoint_ref: "fixture-generation-1",
                capabilities: vec![Capability {
                    capability_id: "plugin.statistics.execute".to_owned(),
                    capability_kind: "host-projection".to_owned(),
                    declared: true,
                }],
                statistics_definitions: vec![StatisticsDefinitionBinding {
                    definition_id: STATISTICS_DEFINITION_ID.to_owned(),
                    definition_revision: "fixture-row-count-v1".to_owned(),
                    definition_digest: digest("fixture-statistics-definition"),
                }],
            },
        )
    }

    pub fn wasm_statistics_binding(
        &self,
        generation: u64,
        desired_state: &str,
    ) -> Result<BindingEnvelope, Box<dyn std::error::Error>> {
        self.wasm_statistics_binding_custom(
            generation,
            desired_state,
            &format!("wasm-statistics-revision-{generation}"),
            "fixture-row-count-v1",
            &digest("fixture-statistics-definition"),
        )
    }

    pub fn wasm_statistics_binding_custom(
        &self,
        generation: u64,
        desired_state: &str,
        plugin_revision: &str,
        definition_revision: &str,
        definition_digest: &str,
    ) -> Result<BindingEnvelope, Box<dyn std::error::Error>> {
        let artifact_name = format!("wasm-statistics-fixture-{generation}.wasm");
        let artifact = statistics_component()?;
        std::fs::write(
            self.config.artifact_cache_root.join(&artifact_name),
            &artifact,
        )?;
        build_binding(
            &self.config,
            BindingInput {
                plugin_id: WASM_STATISTICS_PLUGIN_ID,
                plugin_revision,
                generation,
                desired_state,
                runtime_profile: "wasm-component/v1",
                kind: "pure-transform",
                artifact_digest: sha256_bytes(&artifact),
                artifact_bytes: artifact.len() as u64,
                artifact_name: &artifact_name,
                service_endpoint_ref: "",
                capabilities: vec![Capability {
                    capability_id: "plugin.statistics.execute".to_owned(),
                    capability_kind: "host-projection".to_owned(),
                    declared: true,
                }],
                statistics_definitions: vec![StatisticsDefinitionBinding {
                    definition_id: STATISTICS_DEFINITION_ID.to_owned(),
                    definition_revision: definition_revision.to_owned(),
                    definition_digest: definition_digest.to_owned(),
                }],
            },
        )
    }

    pub fn write_service_fixture_config(
        &self,
        binding: &BindingEnvelope,
        behavior: &str,
    ) -> Result<PathBuf, Box<dyn std::error::Error>> {
        let path = self
            .temporary
            .path()
            .join(format!("service-{behavior}.json"));
        let value = serde_json::json!({
            "schema_version": "plugin-service-fixture-config/v1",
            "socket_path": self.service_socket,
            "plugin_id": binding.plugin_id,
            "plugin_revision": binding.plugin_revision,
            "artifact_digest": binding.artifact_digest,
            "config_digest": binding.config_digest,
            "capability_digest": binding.capability_digest,
            "binding_generation": binding.binding_generation,
            "binding_epoch": binding.binding_epoch,
            "service_proto_digest": binding.service_proto_digest,
            "workload_identity": WORKLOAD_IDENTITY,
            "behavior": behavior
        });
        std::fs::write(&path, serde_json::to_vec_pretty(&value)?)?;
        Ok(path)
    }
}

struct BindingInput<'a> {
    plugin_id: &'a str,
    plugin_revision: &'a str,
    generation: u64,
    desired_state: &'a str,
    runtime_profile: &'a str,
    kind: &'a str,
    artifact_digest: String,
    artifact_bytes: u64,
    artifact_name: &'a str,
    service_endpoint_ref: &'a str,
    capabilities: Vec<Capability>,
    statistics_definitions: Vec<StatisticsDefinitionBinding>,
}

fn build_binding(
    config: &HostConfig,
    input: BindingInput<'_>,
) -> Result<BindingEnvelope, Box<dyn std::error::Error>> {
    let now = unix_ms()?;
    let plugin_config = PluginConfigDocument {
        schema_version: "masi-plugin-config/v1".to_owned(),
        config_id: format!("config-{}-{}", input.plugin_id, input.generation),
        values: serde_json::Map::new(),
        secret_refs: Vec::new(),
    };
    let config_json = serde_json::to_vec(&plugin_config)?;
    let manifest_limits = ManifestLimits {
        cpu_milli: 100,
        memory_bytes: 67_108_864,
        pid_count: 4,
        fd_count: 32,
        disk_bytes: 0,
        linear_memory_bytes: if input.runtime_profile == "wasm-component/v1" {
            67_108_864
        } else {
            0
        },
        table_elements: if input.runtime_profile == "wasm-component/v1" {
            10_000
        } else {
            0
        },
        instance_count: 2,
        batch_records: 10_000,
        in_flight_bytes: 4_194_304,
        concurrency: 2,
        deadline_ms: 10_000,
        retry_max_attempts: 1,
        output_bytes: 1_048_576,
        queue_depth: 32,
    };
    let mut manifest = PluginManifest {
        schema_version: "masi-plugin-manifest/v1".to_owned(),
        manifest_id: format!("manifest-{}-{}", input.plugin_id, input.generation),
        manifest_revision: 1,
        manifest_digest: String::new(),
        plugin_id: input.plugin_id.to_owned(),
        plugin_revision: input.plugin_revision.to_owned(),
        kind: input.kind.to_owned(),
        publisher: "publisher.test".to_owned(),
        version: "1.0.0".to_owned(),
        scope: PLUGIN_SCOPE.to_owned(),
        artifact_digest: input.artifact_digest.clone(),
        artifact_media_type: if input.runtime_profile == "wasm-component/v1" {
            "application/wasm"
        } else {
            "application/vnd.oci.image.manifest.v1+json"
        }
        .to_owned(),
        entrypoint: if input.runtime_profile == "wasm-component/v1" {
            "masi:plugin-transform@1.0.0#transform"
        } else {
            "masi.plugin.service.v1.HostManagedPlugin"
        }
        .to_owned(),
        supported_platforms: vec![
            if input.runtime_profile == "wasm-component/v1" {
                "wasm32-wasip2"
            } else {
                "linux/amd64"
            }
            .to_owned(),
        ],
        host_api_version: "plugin-host-control/v1".to_owned(),
        input_contract_digest: if input.runtime_profile == "wasm-component/v1" {
            qualified_wit_digest()
        } else {
            qualified_service_proto_digest()
        }
        .to_owned(),
        output_contract_digest: if input.runtime_profile == "wasm-component/v1" {
            qualified_wit_digest()
        } else {
            qualified_service_proto_digest()
        }
        .to_owned(),
        config_schema_id: "plugin-config/v1".to_owned(),
        config_schema_version: "masi-plugin-config/v1".to_owned(),
        config_schema_digest: qualified_config_schema_digest().to_owned(),
        capabilities: input.capabilities,
        skill_ids: Vec::new(),
        mcp_tool_ids: Vec::new(),
        mcp_resource_ids: Vec::new(),
        a2a_peer_ids: Vec::new(),
        network_egress_capability_ids: Vec::new(),
        filesystem_preopens: Vec::new(),
        secret_ref_ids: Vec::new(),
        statistics_definitions: input
            .statistics_definitions
            .iter()
            .map(|definition| StatisticsDefinitionRef {
                definition_id: definition.definition_id.clone(),
                definition_revision: definition.definition_revision.clone(),
                definition_digest: definition.definition_digest.clone(),
            })
            .collect(),
        resource_limits: manifest_limits,
        runtime_profile: input.runtime_profile.to_owned(),
        wit_digest: (input.runtime_profile == "wasm-component/v1")
            .then(|| qualified_wit_digest().to_owned()),
        service_proto_digest: (input.runtime_profile == "grpc-service/v1")
            .then(|| qualified_service_proto_digest().to_owned()),
        sbom_digest: digest("fixture-sbom"),
        provenance_digest: digest("fixture-provenance"),
        signature_status: "signed".to_owned(),
        verification_policy_digest: config.trust.trust_policy_digest.clone(),
        verification_bundle_profile: "plugin-verification-bundle/v1".to_owned(),
        owner_ref: "plugin-host-module-owner".to_owned(),
        support_level: "first-party".to_owned(),
        compatibility: Compatibility {
            host_api_min: "plugin-host-control/v1".to_owned(),
            host_api_max: "plugin-host-control/v1".to_owned(),
            runtime_profile: input.runtime_profile.to_owned(),
            migration_required: false,
            rollback_compatible_revisions: vec![
                "wasm-revision-1".to_owned(),
                "wasm-revision-2".to_owned(),
                "service-revision-1".to_owned(),
                "statistics-revision-1".to_owned(),
            ],
        },
        lifecycle_contract: LifecycleContract {
            startup: "plugin-startup/fail-closed-v1".to_owned(),
            readiness: "plugin-readiness/exact-binding-v1".to_owned(),
            liveness: "plugin-liveness/progress-v1".to_owned(),
            drain: "plugin-drain/bounded-v1".to_owned(),
            failure: "plugin-failure/stable-reason-v1".to_owned(),
            fallback: "plugin-fallback/none-v1".to_owned(),
        },
        observability_contract_digest: digest("plugin-observability-contract-v1"),
        actor_ref: "platform-admin:test".to_owned(),
        reason_code: "QUALIFIED".to_owned(),
        trace_id: format!("trace-binding-{}", input.generation),
        created_at_unix_ms: now.saturating_sub(1000),
    };
    manifest.manifest_digest = compute_manifest_digest(&manifest)?;
    let manifest_json = serde_json::to_vec(&manifest)?;
    let mut bundle = VerificationBundle {
        schema_version: "plugin-verification-bundle/v1".to_owned(),
        artifact_digest: input.artifact_digest.clone(),
        manifest_digest: manifest.manifest_digest.clone(),
        publisher_identity: manifest.publisher.clone(),
        sbom_digest: manifest.sbom_digest.clone(),
        provenance_digest: manifest.provenance_digest.clone(),
        trust_policy_digest: config.trust.trust_policy_digest.clone(),
        verified_at_unix_ms: now.saturating_sub(1000),
        expires_at_unix_ms: now.saturating_add(7_200_000),
        revoked: false,
        verifier: "organization-ed25519/v1".to_owned(),
        signature_base64: String::new(),
    };
    let key = publisher_key()?;
    bundle.signature_base64 = base64::engine::general_purpose::STANDARD.encode(
        key.sign(verification_signature_payload(&bundle).as_bytes())
            .as_ref(),
    );
    let verification_bundle_json = serde_json::to_vec(&bundle)?;
    let granted_capabilities: Vec<String> = manifest
        .capabilities
        .iter()
        .map(|capability| capability.capability_id.clone())
        .collect();
    let mut binding = BindingEnvelope {
        schema_version: "plugin-host-binding/v1".to_owned(),
        operation_id: format!("operation-{}-{}", input.plugin_id, input.generation),
        plugin_id: input.plugin_id.to_owned(),
        plugin_revision: input.plugin_revision.to_owned(),
        manifest_digest: manifest.manifest_digest.clone(),
        manifest_json,
        artifact_digest: input.artifact_digest,
        artifact_bytes: input.artifact_bytes,
        artifact_name: input.artifact_name.to_owned(),
        config_digest: sha256_bytes(&config_json),
        capability_digest: compute_capability_digest(&granted_capabilities),
        granted_capabilities,
        resource_profile_digest: compute_resource_profile_digest(&manifest.resource_limits)?,
        qualification_digest: digest("fixture-qualification"),
        trust_policy_digest: config.trust.trust_policy_digest.clone(),
        verification_bundle_digest: sha256_bytes(&verification_bundle_json),
        verification_bundle_json,
        revocation_snapshot_digest: digest("fixture-revocation-snapshot"),
        revocation_checked_at_unix_ms: now,
        binding_generation: input.generation,
        binding_epoch: format!("epoch-{}-{}", input.plugin_id, input.generation),
        scope: PLUGIN_SCOPE.to_owned(),
        kind: input.kind.to_owned(),
        runtime_profile: input.runtime_profile.to_owned(),
        desired_state: input.desired_state.to_owned(),
        wit_digest: manifest.wit_digest.clone().unwrap_or_default(),
        service_proto_digest: manifest.service_proto_digest.clone().unwrap_or_default(),
        service_endpoint_ref: input.service_endpoint_ref.to_owned(),
        statistics_definition_ids: Vec::new(),
        issued_at_unix_ms: now.saturating_sub(1000),
        expires_at_unix_ms: now.saturating_add(3_600_000),
        actor_ref: "platform-admin:test".to_owned(),
        trace_id: format!("trace-binding-{}", input.generation),
        envelope_digest: String::new(),
        config_json,
        config_schema_id: "plugin-config/v1".to_owned(),
        statistics_definitions: input.statistics_definitions,
    };
    binding.envelope_digest = compute_envelope_digest(&binding);
    Ok(binding)
}

pub fn refresh_binding(
    binding: &BindingEnvelope,
    desired_state: &str,
) -> Result<BindingEnvelope, Box<dyn std::error::Error>> {
    let now = unix_ms()?;
    let mut refreshed = binding.clone();
    refreshed.operation_id = format!("{}-refresh-{desired_state}", binding.operation_id);
    refreshed.desired_state = desired_state.to_owned();
    refreshed.revocation_snapshot_digest = digest(&format!("revocation-refresh-{now}"));
    refreshed.revocation_checked_at_unix_ms = now;
    refreshed.issued_at_unix_ms = now.saturating_sub(1);
    refreshed.expires_at_unix_ms = now.saturating_add(3_600_000);
    refreshed.trace_id = format!("trace-refresh-{desired_state}-{now}");
    refreshed.envelope_digest.clear();
    refreshed.envelope_digest = compute_envelope_digest(&refreshed);
    Ok(refreshed)
}

pub fn qualified_limits() -> Limits {
    Limits {
        max_bindings: 32,
        global_queue_depth: 64,
        per_binding_queue_depth: 32,
        per_binding_in_flight: 2,
        max_control_message_bytes: 4_194_304,
        max_input_bytes: 2_097_152,
        max_output_bytes: 1_048_576,
        max_deadline_ms: 10_000,
        queue_wait_ms: 1000,
        wasm_fuel: 50_000_000,
        wasm_linear_memory_bytes: 67_108_864,
        wasm_table_elements: 10_000,
        wasm_instances: 32,
        wasm_memories: 2,
        wasm_tables: 4,
        wasm_stack_bytes: 2_097_152,
        epoch_tick_ms: 10,
        failure_threshold: 3,
        circuit_open_ms: 1000,
        restart_window_ms: 600_000,
        max_restarts_in_window: 5,
        quarantine_ms: 900_000,
        drain_deadline_ms: 10_000,
        shutdown_deadline_ms: 10_000,
    }
}

pub fn qualified_component() -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    Ok(wat::parse_str(
        r#"(component
          (core module $m
            (memory (export "memory") 33)
            (func (export "transform") (param i32 i32) (result i32) i32.const 0)
            (func (export "cabi_realloc") (param i32 i32 i32 i32) (result i32) i32.const 64))
          (core instance $i (instantiate $m))
          (func $f (param "input" (list u8)) (result (result (list u8) (error string)))
            (canon lift (core func $i "transform") (memory $i "memory") (realloc (func $i "cabi_realloc"))))
          (export "transform" (func $f)))"#,
    )?)
}

pub fn statistics_component() -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    let candidate = br#"{"quality":"valid","metrics":[{"metric_id":"row-count","metric_kind":"gauge","temporality":"delta","value":2.0,"unit":"rows"}],"series":[],"tables":[],"truncation":{"truncated_rows":0,"truncated_series":0,"reason_code":"NONE"},"reason_code":"STATISTICS_COMPUTED"}"#;
    let candidate_pointer = 64u32;
    let candidate_length = u32::try_from(candidate.len())?;
    let mut descriptor = [0u8; 12];
    descriptor[4..8].copy_from_slice(&candidate_pointer.to_le_bytes());
    descriptor[8..12].copy_from_slice(&candidate_length.to_le_bytes());
    let descriptor_text = wat_bytes(&descriptor);
    let candidate_text = wat_bytes(candidate);
    let source = format!(
        r#"(component
          (core module $m
            (memory (export "memory") 33)
            (data (i32.const 0) "{descriptor_text}")
            (data (i32.const 64) "{candidate_text}")
            (func (export "transform") (param i32 i32) (result i32) i32.const 0)
            (func (export "cabi_realloc") (param i32 i32 i32 i32) (result i32) i32.const 8192))
          (core instance $i (instantiate $m))
          (func $f (param "input" (list u8)) (result (result (list u8) (error string)))
            (canon lift (core func $i "transform") (memory $i "memory") (realloc (func $i "cabi_realloc"))))
          (export "transform" (func $f)))"#
    );
    Ok(wat::parse_str(source)?)
}

fn wat_bytes(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("\\{byte:02x}")).collect()
}

pub fn digest(label: &str) -> String {
    sha256_bytes(label.as_bytes())
}

pub fn unix_ms() -> Result<i64, Box<dyn std::error::Error>> {
    Ok(i64::try_from(
        SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis(),
    )?)
}

fn publisher_key() -> Result<Ed25519KeyPair, Box<dyn std::error::Error>> {
    Ok(Ed25519KeyPair::from_seed_unchecked(&[7u8; 32])
        .map_err(|_| std::io::Error::other("publisher test key rejected"))?)
}

fn free_address() -> Result<SocketAddr, Box<dyn std::error::Error>> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    Ok(listener.local_addr()?)
}

fn write_private(path: &Path, bytes: &[u8]) -> Result<(), Box<dyn std::error::Error>> {
    std::fs::write(path, bytes)?;
    std::fs::set_permissions(path, Permissions::from_mode(0o600))?;
    Ok(())
}

struct TlsMaterial {
    ca_pem: Vec<u8>,
    server_certificate_pem: Vec<u8>,
    server_private_key_pem: Vec<u8>,
    manager_certificate_pem: Vec<u8>,
    manager_certificate_der: Vec<u8>,
    manager_private_key_pem: Vec<u8>,
    other_certificate_pem: Vec<u8>,
    other_private_key_pem: Vec<u8>,
}

fn generate_tls_material() -> Result<TlsMaterial, Box<dyn std::error::Error>> {
    let mut ca_params = CertificateParams::new(Vec::<String>::new())?;
    ca_params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    ca_params
        .distinguished_name
        .push(DnType::CommonName, "MASI Plugin Host Test CA");
    ca_params.key_usages = vec![
        KeyUsagePurpose::KeyCertSign,
        KeyUsagePurpose::DigitalSignature,
        KeyUsagePurpose::CrlSign,
    ];
    let ca = CertifiedIssuer::self_signed(ca_params, KeyPair::generate()?)?;
    let ca_pem = ca.pem().into_bytes();

    let server = issue_leaf(&ca, "plugin-host.test", ExtendedKeyUsagePurpose::ServerAuth)?;
    let manager = issue_leaf(&ca, "manager.test", ExtendedKeyUsagePurpose::ClientAuth)?;
    let other = issue_leaf(
        &ca,
        "other-manager.test",
        ExtendedKeyUsagePurpose::ClientAuth,
    )?;
    Ok(TlsMaterial {
        ca_pem,
        server_certificate_pem: server.certificate_pem,
        server_private_key_pem: server.private_key_pem,
        manager_certificate_pem: manager.certificate_pem,
        manager_certificate_der: manager.certificate_der,
        manager_private_key_pem: manager.private_key_pem,
        other_certificate_pem: other.certificate_pem,
        other_private_key_pem: other.private_key_pem,
    })
}

struct LeafMaterial {
    certificate_pem: Vec<u8>,
    certificate_der: Vec<u8>,
    private_key_pem: Vec<u8>,
}

fn issue_leaf(
    issuer: &CertifiedIssuer<'_, KeyPair>,
    common_name: &str,
    usage: ExtendedKeyUsagePurpose,
) -> Result<LeafMaterial, Box<dyn std::error::Error>> {
    let mut params = CertificateParams::new(vec![common_name.to_owned()])?;
    params
        .distinguished_name
        .push(DnType::CommonName, common_name);
    params.key_usages = vec![KeyUsagePurpose::DigitalSignature];
    params.extended_key_usages = vec![usage];
    let key = KeyPair::generate()?;
    let certificate = params.signed_by(&key, issuer)?;
    Ok(LeafMaterial {
        certificate_pem: certificate.pem().into_bytes(),
        certificate_der: certificate.der().as_ref().to_vec(),
        private_key_pem: key.serialize_pem().into_bytes(),
    })
}
