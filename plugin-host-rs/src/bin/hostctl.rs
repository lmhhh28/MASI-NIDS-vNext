//! Public-boundary client and deterministic fixture-envelope helper.

#![allow(clippy::print_stdout)]

use std::path::{Path, PathBuf};

use clap::{Parser, Subcommand};
use masi_plugin_host::admission::{
    PluginConfigDocument, PluginManifest, VerificationBundle, compute_capability_digest,
    compute_envelope_digest, compute_manifest_digest, compute_resource_profile_digest,
    sha256_bytes, verification_signature_payload,
};
use masi_plugin_host::config::read_secure_file;
use masi_plugin_host::contract::control_adapter::StatisticsExecutionRequest;
use masi_plugin_host::contract::control_adapter::plugin_statistics_executor_client::PluginStatisticsExecutorClient;
use masi_plugin_host::contract::host::plugin_host_control_client::PluginHostControlClient;
use masi_plugin_host::contract::host::*;
use serde::Deserialize;
use tonic::transport::{Certificate, Channel, ClientTlsConfig, Endpoint, Identity};

#[derive(Debug, Parser)]
#[command(name = "masi-plugin-hostctl")]
struct Args {
    /// Host gRPC HTTPS endpoint.
    #[arg(long, default_value = "https://127.0.0.1:7445")]
    endpoint: String,
    /// Host certificate DNS/SAN identity.
    #[arg(long, default_value = "plugin-host.test")]
    server_name: String,
    /// Client CA PEM.
    #[arg(long)]
    ca: Option<PathBuf>,
    /// Manager client certificate PEM.
    #[arg(long)]
    cert: Option<PathBuf>,
    /// Manager client private key PEM.
    #[arg(long)]
    key: Option<PathBuf>,
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Compute and atomically write a manifest with its canonical digest.
    FinalizeManifest {
        #[arg(long)]
        input: PathBuf,
        #[arg(long)]
        output: PathBuf,
    },
    /// Print the canonical config digest.
    ConfigDigest {
        #[arg(long)]
        input: PathBuf,
    },
    /// Print the canonical offline verification signature payload.
    BundlePayload {
        #[arg(long)]
        input: PathBuf,
    },
    /// Apply a binding spec through mTLS.
    Apply {
        #[arg(long)]
        spec: PathBuf,
    },
    /// Apply a fully materialized binding envelope through mTLS.
    ApplyEnvelope {
        #[arg(long)]
        input: PathBuf,
    },
    /// Roll back from the exact active generation to an observed lower generation.
    Rollback {
        #[arg(long)]
        spec: PathBuf,
        #[arg(long)]
        from_generation: u64,
        #[arg(long)]
        authorization_digest: String,
    },
    /// Execute a generic public-boundary invocation.
    Execute {
        #[arg(long)]
        spec: PathBuf,
    },
    /// Execute the existing Go statistics adapter.
    Statistics {
        #[arg(long)]
        spec: PathBuf,
    },
    /// Observe one exact binding.
    Get {
        #[arg(long)]
        plugin_id: String,
        #[arg(long)]
        generation: u64,
        #[arg(long, default_value = "hostctl-get")]
        trace_id: String,
    },
    /// List a bounded page.
    List {
        #[arg(long, default_value_t = 50)]
        page_size: u32,
        #[arg(long, default_value = "")]
        page_token: String,
        #[arg(long, default_value = "hostctl-list")]
        trace_id: String,
    },
    /// Drain one exact binding.
    Drain {
        #[arg(long)]
        plugin_id: String,
        #[arg(long)]
        generation: u64,
        #[arg(long, default_value_t = 10000)]
        deadline_ms: u32,
        #[arg(long)]
        authorization_digest: String,
        #[arg(long, default_value = "platform-admin:hostctl")]
        actor_ref: String,
        #[arg(long, default_value = "DRAIN_AUTHORIZED")]
        reason_code: String,
        #[arg(long, default_value = "hostctl-drain")]
        trace_id: String,
    },
    /// Revoke one exact binding.
    Revoke {
        #[arg(long)]
        plugin_id: String,
        #[arg(long)]
        generation: u64,
        #[arg(long)]
        revocation_digest: String,
        #[arg(long)]
        authorization_digest: String,
        #[arg(long, default_value = "platform-admin:hostctl")]
        actor_ref: String,
        #[arg(long, default_value = "REVOKE_AUTHORIZED")]
        reason_code: String,
        #[arg(long, default_value = "hostctl-revoke")]
        trace_id: String,
    },
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct BindingSpec {
    schema_version: String,
    operation_id: String,
    plugin_revision: String,
    manifest_path: PathBuf,
    artifact_path: Option<PathBuf>,
    artifact_digest: Option<String>,
    artifact_bytes: Option<u64>,
    artifact_name: String,
    config_path: PathBuf,
    qualification_digest: String,
    trust_policy_digest: String,
    verification_bundle_path: PathBuf,
    revocation_snapshot_digest: String,
    revocation_checked_at_unix_ms: i64,
    binding_generation: u64,
    binding_epoch: String,
    desired_state: String,
    service_endpoint_ref: String,
    issued_at_unix_ms: i64,
    expires_at_unix_ms: i64,
    actor_ref: String,
    trace_id: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ExecuteSpec {
    schema_version: String,
    invocation_id: String,
    plugin_id: String,
    binding_generation: u64,
    binding_epoch: String,
    capability_id: String,
    input_path: PathBuf,
    deadline_ms: u32,
    result_fence: String,
    trace_id: String,
    #[serde(default = "active_execution_mode")]
    execution_mode: String,
}

fn active_execution_mode() -> String {
    "active".to_owned()
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct StatisticsSpec {
    schema_version: String,
    run_id: String,
    lease_id: String,
    claim_generation: u64,
    result_fence: String,
    binding_generation: u64,
    definition_id: String,
    definition_digest: String,
    scope: String,
    expires_at_unix_ms: i64,
    deadline_ms: u32,
    input_path: PathBuf,
    frozen_input_digest: String,
    trace_id: String,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = Args::parse();
    match &args.command {
        Command::FinalizeManifest { input, output } => finalize_manifest(input, output)?,
        Command::ConfigDigest { input } => {
            let document: PluginConfigDocument =
                serde_json::from_slice(&read_secure_file(input, 65_536, false)?)?;
            println!("{}", sha256_bytes(&serde_json::to_vec(&document)?));
        }
        Command::BundlePayload { input } => {
            let bundle: VerificationBundle =
                serde_json::from_slice(&read_secure_file(input, 131_072, false)?)?;
            println!("{}", verification_signature_payload(&bundle));
        }
        _ => {
            let channel = connect(&args).await?;
            run_remote(channel, args.command).await?;
        }
    }
    Ok(())
}

fn finalize_manifest(input: &Path, output: &Path) -> Result<(), Box<dyn std::error::Error>> {
    let mut manifest: PluginManifest =
        serde_json::from_slice(&read_secure_file(input, 131_072, false)?)?;
    manifest.manifest_digest.clear();
    manifest.manifest_digest = compute_manifest_digest(&manifest)?;
    let encoded = serde_json::to_vec_pretty(&manifest)?;
    write_atomic_new(output, &encoded)?;
    println!("{}", manifest.manifest_digest);
    Ok(())
}

async fn run_remote(channel: Channel, command: Command) -> Result<(), Box<dyn std::error::Error>> {
    match command {
        Command::Apply { spec } => {
            let spec: BindingSpec =
                serde_json::from_slice(&read_secure_file(&spec, 1024 * 1024, false)?)?;
            if spec.schema_version != "plugin-host-binding-spec/v1" {
                return Err("binding spec version rejected".into());
            }
            let binding = build_binding(spec)?;
            let mut client = PluginHostControlClient::new(channel);
            let result = client
                .apply_binding(ApplyBindingRequest {
                    schema_version: "plugin-host-control/v1".to_owned(),
                    binding: Some(binding),
                })
                .await?
                .into_inner();
            println!("{}", serde_json::to_string(&result)?);
        }
        Command::ApplyEnvelope { input } => {
            let binding: BindingEnvelope =
                serde_json::from_slice(&read_secure_file(&input, 4 * 1024 * 1024, false)?)?;
            let result = PluginHostControlClient::new(channel)
                .apply_binding(ApplyBindingRequest {
                    schema_version: "plugin-host-control/v1".to_owned(),
                    binding: Some(binding),
                })
                .await?
                .into_inner();
            println!("{}", serde_json::to_string(&result)?);
        }
        Command::Rollback {
            spec,
            from_generation,
            authorization_digest,
        } => {
            let spec: BindingSpec =
                serde_json::from_slice(&read_secure_file(&spec, 1024 * 1024, false)?)?;
            if spec.schema_version != "plugin-host-binding-spec/v1" {
                return Err("binding spec version rejected".into());
            }
            let target_binding = build_binding(spec)?;
            let result = PluginHostControlClient::new(channel)
                .rollback_binding(RollbackBindingRequest {
                    schema_version: "plugin-host-control/v1".to_owned(),
                    operation_id: target_binding.operation_id.clone(),
                    from_generation,
                    target_binding: Some(target_binding.clone()),
                    authorization_digest,
                    trace_id: target_binding.trace_id,
                    actor_ref: target_binding.actor_ref,
                    reason_code: "ROLLBACK_AUTHORIZED".to_owned(),
                })
                .await?
                .into_inner();
            println!("{}", serde_json::to_string(&result)?);
        }
        Command::Execute { spec } => {
            let spec: ExecuteSpec =
                serde_json::from_slice(&read_secure_file(&spec, 1024 * 1024, false)?)?;
            if spec.schema_version != "plugin-host-execute-spec/v1" {
                return Err("execute spec version rejected".into());
            }
            let input = read_secure_file(&spec.input_path, 2 * 1024 * 1024, false)?;
            let request = ExecuteRequest {
                schema_version: "plugin-host-execute/v1".to_owned(),
                invocation_id: spec.invocation_id,
                plugin_id: spec.plugin_id,
                binding_generation: spec.binding_generation,
                binding_epoch: spec.binding_epoch,
                capability_id: spec.capability_id,
                input_digest: sha256_bytes(&input),
                input,
                deadline_ms: spec.deadline_ms,
                result_fence: spec.result_fence,
                trace_id: spec.trace_id,
                execution_mode: spec.execution_mode,
            };
            let result = PluginHostControlClient::new(channel)
                .execute(request)
                .await?
                .into_inner();
            println!("{}", serde_json::to_string(&result)?);
        }
        Command::Statistics { spec } => {
            let spec: StatisticsSpec =
                serde_json::from_slice(&read_secure_file(&spec, 1024 * 1024, false)?)?;
            if spec.schema_version != "plugin-host-statistics-spec/v1" {
                return Err("statistics spec version rejected".into());
            }
            let input_bundle_json = read_secure_file(&spec.input_path, 2 * 1024 * 1024, false)?;
            let request = StatisticsExecutionRequest {
                schema_version: "control-plugin-statistics-execution/v1".to_owned(),
                run_id: spec.run_id,
                lease_id: spec.lease_id,
                claim_generation: spec.claim_generation,
                result_fence: spec.result_fence,
                binding_generation: spec.binding_generation,
                definition_id: spec.definition_id,
                definition_digest: spec.definition_digest,
                scope: spec.scope,
                expires_at_unix_ms: spec.expires_at_unix_ms,
                deadline_ms: spec.deadline_ms,
                input_bundle_json,
                frozen_input_digest: spec.frozen_input_digest,
                trace_id: spec.trace_id,
            };
            let result = PluginStatisticsExecutorClient::new(channel)
                .execute_statistics(request)
                .await?
                .into_inner();
            println!("{}", serde_json::to_string(&result)?);
        }
        Command::Get {
            plugin_id,
            generation,
            trace_id,
        } => {
            let result = PluginHostControlClient::new(channel)
                .get_binding(GetBindingRequest {
                    schema_version: "plugin-host-control/v1".to_owned(),
                    plugin_id,
                    binding_generation: generation,
                    trace_id,
                })
                .await?
                .into_inner();
            println!("{}", serde_json::to_string(&result)?);
        }
        Command::List {
            page_size,
            page_token,
            trace_id,
        } => {
            let result = PluginHostControlClient::new(channel)
                .list_bindings(ListBindingsRequest {
                    schema_version: "plugin-host-control/v1".to_owned(),
                    page_size,
                    page_token,
                    trace_id,
                })
                .await?
                .into_inner();
            println!("{}", serde_json::to_string(&result)?);
        }
        Command::Drain {
            plugin_id,
            generation,
            deadline_ms,
            authorization_digest,
            actor_ref,
            reason_code,
            trace_id,
        } => {
            let mut client = PluginHostControlClient::new(channel);
            let observed = client
                .get_binding(GetBindingRequest {
                    schema_version: "plugin-host-control/v1".to_owned(),
                    plugin_id: plugin_id.clone(),
                    binding_generation: generation,
                    trace_id: trace_id.clone(),
                })
                .await?
                .into_inner();
            let result = client
                .drain_binding(DrainBindingRequest {
                    schema_version: "plugin-host-control/v1".to_owned(),
                    operation_id: format!("drain-{plugin_id}-{generation}"),
                    plugin_id,
                    binding_generation: generation,
                    deadline_ms,
                    trace_id,
                    actor_ref,
                    reason_code,
                    expected_envelope_digest: observed.envelope_digest,
                    authorization_digest,
                })
                .await?
                .into_inner();
            println!("{}", serde_json::to_string(&result)?);
        }
        Command::Revoke {
            plugin_id,
            generation,
            revocation_digest,
            authorization_digest,
            actor_ref,
            reason_code,
            trace_id,
        } => {
            let mut client = PluginHostControlClient::new(channel);
            let observed = client
                .get_binding(GetBindingRequest {
                    schema_version: "plugin-host-control/v1".to_owned(),
                    plugin_id: plugin_id.clone(),
                    binding_generation: generation,
                    trace_id: trace_id.clone(),
                })
                .await?
                .into_inner();
            let result = client
                .revoke_binding(RevokeBindingRequest {
                    schema_version: "plugin-host-control/v1".to_owned(),
                    operation_id: format!("revoke-{plugin_id}-{generation}"),
                    plugin_id,
                    binding_generation: generation,
                    revocation_digest,
                    deadline_ms: 10_000,
                    trace_id,
                    actor_ref,
                    reason_code,
                    expected_envelope_digest: observed.envelope_digest,
                    authorization_digest,
                })
                .await?
                .into_inner();
            println!("{}", serde_json::to_string(&result)?);
        }
        Command::FinalizeManifest { .. }
        | Command::ConfigDigest { .. }
        | Command::BundlePayload { .. } => {}
    }
    Ok(())
}

fn build_binding(spec: BindingSpec) -> Result<BindingEnvelope, Box<dyn std::error::Error>> {
    let manifest_json = read_secure_file(&spec.manifest_path, 131_072, false)?;
    let manifest: PluginManifest = serde_json::from_slice(&manifest_json)?;
    let config_json = read_secure_file(&spec.config_path, 65_536, false)?;
    let config_document: PluginConfigDocument = serde_json::from_slice(&config_json)?;
    let config_json = serde_json::to_vec(&config_document)?;
    let verification_bundle_json =
        read_secure_file(&spec.verification_bundle_path, 131_072, false)?;
    let (artifact_digest, artifact_bytes) = if let Some(path) = spec.artifact_path {
        let bytes = read_secure_file(&path, 16 * 1024 * 1024, false)?;
        (sha256_bytes(&bytes), bytes.len() as u64)
    } else {
        (
            spec.artifact_digest
                .ok_or("service artifact_digest required")?,
            spec.artifact_bytes
                .ok_or("service artifact_bytes required")?,
        )
    };
    let granted_capabilities: Vec<String> = manifest
        .capabilities
        .iter()
        .filter(|value| value.declared)
        .map(|value| value.capability_id.clone())
        .collect();
    let mut binding = BindingEnvelope {
        schema_version: "plugin-host-binding/v1".to_owned(),
        operation_id: spec.operation_id,
        plugin_id: manifest.plugin_id.clone(),
        plugin_revision: spec.plugin_revision,
        manifest_digest: manifest.manifest_digest.clone(),
        manifest_json,
        artifact_digest,
        artifact_bytes,
        artifact_name: spec.artifact_name,
        config_digest: sha256_bytes(&config_json),
        capability_digest: compute_capability_digest(&granted_capabilities),
        granted_capabilities,
        resource_profile_digest: compute_resource_profile_digest(&manifest.resource_limits)?,
        qualification_digest: spec.qualification_digest,
        trust_policy_digest: spec.trust_policy_digest,
        verification_bundle_digest: sha256_bytes(&verification_bundle_json),
        verification_bundle_json,
        revocation_snapshot_digest: spec.revocation_snapshot_digest,
        revocation_checked_at_unix_ms: spec.revocation_checked_at_unix_ms,
        binding_generation: spec.binding_generation,
        binding_epoch: spec.binding_epoch,
        scope: manifest.scope.clone(),
        kind: manifest.kind.clone(),
        runtime_profile: manifest.runtime_profile.clone(),
        desired_state: spec.desired_state,
        wit_digest: manifest.wit_digest.clone().unwrap_or_default(),
        service_proto_digest: manifest.service_proto_digest.clone().unwrap_or_default(),
        service_endpoint_ref: spec.service_endpoint_ref,
        statistics_definition_ids: Vec::new(),
        issued_at_unix_ms: spec.issued_at_unix_ms,
        expires_at_unix_ms: spec.expires_at_unix_ms,
        actor_ref: spec.actor_ref,
        trace_id: spec.trace_id,
        envelope_digest: String::new(),
        config_json,
        config_schema_id: "plugin-config/v1".to_owned(),
        statistics_definitions: manifest
            .statistics_definitions
            .iter()
            .map(|definition| StatisticsDefinitionBinding {
                definition_id: definition.definition_id.clone(),
                definition_revision: definition.definition_revision.clone(),
                definition_digest: definition.definition_digest.clone(),
            })
            .collect(),
    };
    binding.envelope_digest = compute_envelope_digest(&binding);
    Ok(binding)
}

async fn connect(args: &Args) -> Result<Channel, Box<dyn std::error::Error>> {
    let ca = args
        .ca
        .as_ref()
        .ok_or("--ca is required for remote commands")?;
    let cert = args
        .cert
        .as_ref()
        .ok_or("--cert is required for remote commands")?;
    let key = args
        .key
        .as_ref()
        .ok_or("--key is required for remote commands")?;
    let tls = ClientTlsConfig::new()
        .ca_certificate(Certificate::from_pem(read_secure_file(
            ca,
            1024 * 1024,
            false,
        )?))
        .identity(Identity::from_pem(
            read_secure_file(cert, 1024 * 1024, false)?,
            read_secure_file(key, 1024 * 1024, true)?,
        ))
        .domain_name(args.server_name.clone());
    Ok(Endpoint::from_shared(args.endpoint.clone())?
        .tls_config(tls)?
        .connect_timeout(std::time::Duration::from_secs(5))
        .timeout(std::time::Duration::from_secs(15))
        .connect()
        .await?)
}

fn write_atomic_new(path: &Path, bytes: &[u8]) -> Result<(), Box<dyn std::error::Error>> {
    if path.exists() || std::fs::symlink_metadata(path).is_ok() {
        return Err("refusing to overwrite output".into());
    }
    let parent = path.parent().ok_or("output parent missing")?;
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or("output filename invalid")?;
    let temporary = parent.join(format!(".{name}.{}.tmp", std::process::id()));
    if temporary.exists() || std::fs::symlink_metadata(&temporary).is_ok() {
        return Err("temporary output already exists".into());
    }
    std::fs::write(&temporary, bytes)?;
    std::fs::rename(temporary, path)?;
    Ok(())
}
