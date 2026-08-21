//! Real out-of-process Host-managed service conformance fixture.

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use clap::Parser;
use masi_plugin_host::admission::sha256_bytes;
use masi_plugin_host::config::{read_secure_file, validate_digest, validate_no_symlink_path};
use masi_plugin_host::contract::service::host_managed_plugin_server::{
    HostManagedPlugin, HostManagedPluginServer,
};
use masi_plugin_host::contract::service::*;
use masi_plugin_host::{HostError, ReasonCode};
use serde::Deserialize;
use tokio::net::UnixListener;
use tokio_stream::wrappers::UnixListenerStream;
use tonic::{Request, Response, Status};

#[derive(Debug, Parser)]
struct Args {
    #[arg(long)]
    config: PathBuf,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FixtureConfig {
    schema_version: String,
    socket_path: PathBuf,
    plugin_id: String,
    plugin_revision: String,
    artifact_digest: String,
    config_digest: String,
    capability_digest: String,
    binding_generation: u64,
    binding_epoch: String,
    service_proto_digest: String,
    workload_identity: String,
    behavior: String,
}

#[derive(Clone)]
struct FixtureService {
    config: Arc<FixtureConfig>,
    accepting: Arc<AtomicBool>,
}

#[tonic::async_trait]
impl HostManagedPlugin for FixtureService {
    async fn handshake(
        &self,
        request: Request<HandshakeRequest>,
    ) -> Result<Response<HandshakeReply>, Status> {
        let request = request.into_inner();
        let c = &self.config;
        if request.schema_version != "plugin-service-handshake/v1"
            || request.plugin_id != c.plugin_id
            || request.plugin_revision != c.plugin_revision
            || request.artifact_digest != c.artifact_digest
            || request.config_digest != c.config_digest
            || request.capability_digest != c.capability_digest
            || request.binding_generation != c.binding_generation
            || request.binding_epoch != c.binding_epoch
            || request.service_proto_digest != c.service_proto_digest
            || request.challenge_nonce.is_empty()
        {
            return Err(Status::failed_precondition("SERVICE_IDENTITY_MISMATCH"));
        }
        Ok(Response::new(HandshakeReply {
            schema_version: "plugin-service-handshake-result/v1".to_owned(),
            plugin_id: c.plugin_id.clone(),
            plugin_revision: c.plugin_revision.clone(),
            artifact_digest: c.artifact_digest.clone(),
            config_digest: c.config_digest.clone(),
            capability_digest: c.capability_digest.clone(),
            binding_generation: c.binding_generation,
            binding_epoch: c.binding_epoch.clone(),
            service_proto_digest: c.service_proto_digest.clone(),
            challenge_nonce: request.challenge_nonce,
            workload_identity: c.workload_identity.clone(),
            status: "ready".to_owned(),
            reason_code: "OK".to_owned(),
            trace_id: request.trace_id,
        }))
    }

    async fn execute(
        &self,
        request: Request<ServiceExecuteRequest>,
    ) -> Result<Response<ServiceExecuteReply>, Status> {
        let request = request.into_inner();
        let c = &self.config;
        if !self.accepting.load(Ordering::Acquire) {
            return Err(Status::unavailable("DRAINING"));
        }
        let input_digest_matches = if request.capability_id == "plugin.statistics.execute" {
            serde_json::from_slice::<serde_json::Value>(&request.input)
                .ok()
                .and_then(|value| {
                    value
                        .get("frozen_input_digest")
                        .and_then(serde_json::Value::as_str)
                        .map(str::to_owned)
                })
                .as_deref()
                == Some(request.input_digest.as_str())
        } else {
            sha256_bytes(&request.input) == request.input_digest
        };
        if request.schema_version != "plugin-service-execute/v1"
            || request.plugin_id != c.plugin_id
            || request.binding_generation != c.binding_generation
            || request.binding_epoch != c.binding_epoch
            || request.input.is_empty()
            || !input_digest_matches
            || !matches!(request.execution_mode.as_str(), "active" | "shadow")
        {
            return Err(Status::failed_precondition("FENCED_OR_DIGEST_MISMATCH"));
        }
        let output = match c.behavior.as_str() {
            "echo" | "wrong-digest" => request.input.clone(),
            "bounded-echo" => request.input[..request.input.len().min(1_048_576)].to_vec(),
            "statistics" => statistics_candidate(&request.input)?,
            "slow" => {
                tokio::time::sleep(Duration::from_millis(100)).await;
                request.input.clone()
            }
            "hang" => {
                tokio::time::sleep(Duration::from_millis(
                    u64::from(request.deadline_ms).saturating_mul(4).max(100),
                ))
                .await;
                request.input.clone()
            }
            "trap" => return Err(Status::internal("PLUGIN_TRAP")),
            "oversize" => vec![b'x'; 1_048_577],
            _ => return Err(Status::failed_precondition("UNKNOWN_FIXTURE_BEHAVIOR")),
        };
        let mut digest = sha256_bytes(&output);
        if c.behavior == "wrong-digest" {
            digest = format!("sha256:{}", "0".repeat(64));
        }
        Ok(Response::new(ServiceExecuteReply {
            schema_version: "plugin-service-execute-result/v1".to_owned(),
            invocation_id: request.invocation_id,
            plugin_id: request.plugin_id,
            binding_generation: request.binding_generation,
            binding_epoch: request.binding_epoch,
            input_digest: request.input_digest,
            output_digest: digest,
            output,
            result_fence: request.result_fence,
            status: "succeeded".to_owned(),
            reason_code: "OK".to_owned(),
            trace_id: request.trace_id,
            execution_mode: request.execution_mode,
        }))
    }

    async fn health(
        &self,
        request: Request<HealthRequest>,
    ) -> Result<Response<HealthReply>, Status> {
        let request = request.into_inner();
        let exact = request.schema_version == "plugin-service-health/v1"
            && request.plugin_id == self.config.plugin_id
            && request.binding_generation == self.config.binding_generation;
        Ok(Response::new(HealthReply {
            schema_version: "plugin-service-health-result/v1".to_owned(),
            plugin_id: self.config.plugin_id.clone(),
            binding_generation: self.config.binding_generation,
            ready: exact && self.accepting.load(Ordering::Acquire),
            live: true,
            status: if exact && self.accepting.load(Ordering::Acquire) {
                "ready"
            } else {
                "draining"
            }
            .to_owned(),
            reason_code: if exact { "OK" } else { "FENCED" }.to_owned(),
            trace_id: request.trace_id,
        }))
    }

    async fn drain(
        &self,
        request: Request<ServiceDrainRequest>,
    ) -> Result<Response<ServiceDrainReply>, Status> {
        let request = request.into_inner();
        if self.config.behavior == "drain-fail" {
            return Err(Status::unavailable("DRAIN_FAILED"));
        }
        if self.config.behavior == "drain-hang" {
            tokio::time::sleep(Duration::from_millis(
                u64::from(request.deadline_ms).saturating_mul(4).max(100),
            ))
            .await;
        }
        self.accepting.store(false, Ordering::Release);
        Ok(Response::new(ServiceDrainReply {
            schema_version: "plugin-service-drain-result/v1".to_owned(),
            plugin_id: self.config.plugin_id.clone(),
            binding_generation: self.config.binding_generation,
            drained: true,
            status: "drained".to_owned(),
            reason_code: "OK".to_owned(),
            trace_id: request.trace_id,
        }))
    }

    async fn disable(
        &self,
        request: Request<ServiceDisableRequest>,
    ) -> Result<Response<ServiceDisableReply>, Status> {
        let request = request.into_inner();
        if self.config.behavior == "disable-fail" {
            return Err(Status::unavailable("DISABLE_FAILED"));
        }
        if self.config.behavior == "disable-hang" {
            tokio::time::sleep(Duration::from_secs(30)).await;
        }
        self.accepting.store(false, Ordering::Release);
        Ok(Response::new(ServiceDisableReply {
            schema_version: "plugin-service-disable-result/v1".to_owned(),
            plugin_id: self.config.plugin_id.clone(),
            binding_generation: self.config.binding_generation,
            disabled: true,
            status: "disabled".to_owned(),
            reason_code: "OK".to_owned(),
            trace_id: request.trace_id,
        }))
    }
}

fn statistics_candidate(input: &[u8]) -> Result<Vec<u8>, Status> {
    let value: serde_json::Value =
        serde_json::from_slice(input).map_err(|_| Status::invalid_argument("INVALID_INPUT"))?;
    let rows = value
        .get("rows")
        .and_then(serde_json::Value::as_array)
        .map_or(0, Vec::len);
    serde_json::to_vec(&serde_json::json!({
        "quality": if rows == 0 { "no_data" } else { "valid" },
        "metrics": [{
            "metric_id": "row-count",
            "metric_kind": "gauge",
            "temporality": "delta",
            "value": rows as f64,
            "unit": "rows"
        }],
        "series": [],
        "tables": [],
        "truncation": {"truncated_rows": 0, "truncated_series": 0, "reason_code": "NONE"},
        "reason_code": "STATISTICS_COMPUTED"
    }))
    .map_err(|_| Status::internal("SERIALIZATION_FAILED"))
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .json()
        .with_env_filter("info")
        .init();
    let args = Args::parse();
    let raw = read_secure_file(&args.config, 1024 * 1024, false)?;
    let config: FixtureConfig = serde_json::from_slice(&raw)?;
    if config.schema_version != "plugin-service-fixture-config/v1" {
        return Err(HostError::new(
            ReasonCode::UnknownVersion,
            "fixture config version rejected",
        )
        .into());
    }
    for digest in [
        &config.artifact_digest,
        &config.config_digest,
        &config.capability_digest,
        &config.service_proto_digest,
    ] {
        validate_digest(digest)?;
    }
    let parent = config
        .socket_path
        .parent()
        .ok_or_else(|| anyhow::anyhow!("socket parent missing"))?;
    std::fs::create_dir_all(parent)?;
    std::fs::set_permissions(parent, std::os::unix::fs::PermissionsExt::from_mode(0o750))?;
    validate_no_symlink_path(parent, true)?;
    if config.socket_path.exists() {
        let metadata = std::fs::symlink_metadata(&config.socket_path)?;
        if !std::os::unix::fs::FileTypeExt::is_socket(&metadata.file_type()) {
            return Err(anyhow::anyhow!(
                "refusing to replace non-socket fixture path"
            ));
        }
        std::fs::remove_file(&config.socket_path)?;
    }
    let listener = UnixListener::bind(&config.socket_path)?;
    std::fs::set_permissions(
        &config.socket_path,
        std::os::unix::fs::PermissionsExt::from_mode(0o660),
    )?;
    let socket_path = config.socket_path.clone();
    let service = FixtureService {
        config: Arc::new(config),
        accepting: Arc::new(AtomicBool::new(true)),
    };
    tonic::transport::Server::builder()
        .add_service(
            HostManagedPluginServer::new(service)
                .max_decoding_message_size(4_194_304)
                .max_encoding_message_size(2_097_152),
        )
        .serve_with_incoming_shutdown(UnixListenerStream::new(listener), wait_for_shutdown())
        .await?;
    if socket_path.exists() {
        std::fs::remove_file(socket_path)?;
    }
    Ok(())
}

async fn wait_for_shutdown() {
    #[cfg(unix)]
    {
        if let Ok(mut terminate) =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            tokio::select! {
                _ = tokio::signal::ctrl_c() => {},
                _ = terminate.recv() => {},
            }
            return;
        }
    }
    let _ = tokio::signal::ctrl_c().await;
}
