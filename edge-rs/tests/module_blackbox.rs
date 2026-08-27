//! Real-process module black-box, recovery, fault, and bounded-load rehearsals.

#![recursion_limit = "256"]

mod support;

use std::{
    collections::{BTreeMap, HashSet},
    error::Error,
    fs,
    fs::OpenOptions,
    io::Write as _,
    net::{SocketAddr, TcpListener},
    path::{Path, PathBuf},
    process::{Command as StdCommand, Stdio},
    sync::{Mutex, OnceLock},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use masi_edge::{
    config::{
        ClientTlsConfig, ControlSinkConfig, DeploymentTier, EdgeConfig, EndpointPolicyConfig,
        RuntimeLimits, ServerTlsConfig,
    },
    contract::edge::{
        AcknowledgeEffectRequest, ActorState, AssignTargetRequest, BaselineRule,
        CommitRouteRequest, ConfigureRuleObservationsRequest, DataQuality, EffectIntent,
        EffectKind, EffectStatus, Fence, FirewallAction, GetStatusRequest,
        InferenceExecutionStatus, InferenceRoute, InstallationReadbackStatus, Ipv4Prefix,
        ObservableRule, OptionalUint32, P4WriteAtomicity, PipelineIdentity, PreflightEffectRequest,
        PrepareRouteRequest, RenewTargetRequest, ResumeRouteRequest, SourceWalRecord,
        SourceWalStage, SupplementalHint, TargetAssignment, TlsClientIdentity,
        edge_control_client::EdgeControlClient,
    },
    contract::p4::{CounterData, DigestList, PacketIn, PacketMetadata},
    digest, firewall,
    wal::{DurableWal, WalKind, WalLimits},
};
use prost::Message as _;
use serde::Deserialize;
use serde_json::json;
use support::{
    FakeControl, FakeInference, FakeP4, IdentityPaths, PIPELINE_COOKIE, TestPki,
    device_config_bytes, generate_test_pki, p4info_bytes, spawn_control, spawn_inference, spawn_p4,
    write_identity,
};
use tempfile::TempDir;
use tokio::{
    process::{Child, Command},
    task::JoinSet,
    time::{Instant, sleep},
};
use tonic::transport::{
    Certificate, Channel, ClientTlsConfig as TonicClientTlsConfig, Endpoint, Identity,
};

const FEATURE_PROFILE_BYTES: &[u8] = b"edge-feature-window-module-v1";
const TELEMETRY_PROFILE_BYTES: &[u8] = b"telemetry-source-module-v1";
static RESERVED_TEST_PORTS: OnceLock<Mutex<HashSet<u16>>> = OnceLock::new();

#[derive(Debug, Deserialize)]
struct ExternalCentralRuntime {
    schema_version: String,
    state: String,
    gateway_endpoint: String,
    tls: ExternalCentralTls,
    binding_readback: ExternalBindingReadback,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ExternalCentralTls {
    server_name: String,
    client_san: String,
    ca_path: PathBuf,
    client_cert_path: PathBuf,
    client_key_path: PathBuf,
}

#[derive(Debug, Deserialize)]
struct ExternalBindingReadback {
    logical_pool_id: String,
    pool_generation: String,
    binding_generation: String,
    model_revision_digest: String,
    feature_contract_digest: String,
    label_contract_digest: String,
    output_adapter_digest: String,
    runtime_profile: String,
    worker_id: String,
    worker_digest: String,
    schema_version: String,
    model_control_incarnation_id: String,
    operation_id: String,
    startup_envelope_digest: String,
    model_bundle_digest: String,
    wire_profile: String,
    wire_profile_digest: String,
    runtime_profile_digest: String,
    optimization_profile_digest: String,
    pool_observation_digest: String,
    binding_digest: String,
}

#[derive(Debug, Deserialize)]
struct ExternalControlRuntime {
    schema_version: String,
    state: String,
    grpc_endpoint: String,
    tls: ExternalControlTls,
    event_evidence_path: PathBuf,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ExternalControlTls {
    server_name: String,
    client_san: String,
    ca_path: PathBuf,
    client_cert_path: PathBuf,
    client_key_path: PathBuf,
}

#[derive(Debug, Deserialize)]
struct ExternalControlEvent {
    schema_version: String,
    event_id: String,
    event_idempotency_key: String,
    input_digest: String,
    output_digest: String,
    worker_id: String,
    worker_digest: String,
    commit_status: String,
    event_count: u64,
    incident_count: u64,
}

#[derive(Debug, Deserialize)]
struct ExternalP4Runtime {
    schema_version: String,
    state: String,
    p4runtime_endpoint: String,
    tls: ExternalP4Tls,
    pipeline: ExternalP4Pipeline,
    edge_ready_path: PathBuf,
    traffic_done_path: PathBuf,
    expected_traffic_packets: u64,
}

#[derive(Debug, Deserialize)]
struct ExternalTrafficReceipt {
    schema_version: String,
    packet_count: u64,
    peer_packet_count: u64,
    started_at_unix_ns: u64,
    finished_at_unix_ns: u64,
    p4runtime_credentials_present: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ExternalP4Tls {
    server_name: String,
    client_san: String,
    ca_path: PathBuf,
    client_cert_path: PathBuf,
    client_key_path: PathBuf,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ExternalP4Pipeline {
    p4runtime_api_version: String,
    p4info_digest: String,
    device_config_digest: String,
    profile_digest: String,
    cookie: u64,
    supported_write_atomicity: Vec<String>,
}

#[derive(Debug)]
struct Topology {
    root: TempDir,
    pki: TestPki,
    edge_server_paths: IdentityPaths,
    edge_client_paths: IdentityPaths,
    p4_client_paths: IdentityPaths,
    inference_client_paths: IdentityPaths,
    control_client_paths: IdentityPaths,
    p4: FakeP4,
    inference: FakeInference,
    control: FakeControl,
    p4_server: support::FakeServerHandle,
    inference_server: support::FakeServerHandle,
    control_server: support::FakeServerHandle,
    edge_address: SocketAddr,
    edge_listener_reservation: Mutex<Option<TcpListener>>,
    config_path: PathBuf,
    data_dir: PathBuf,
}

impl Topology {
    async fn new(
        target_count: usize,
        inference_failures: u32,
        control_failures: u32,
    ) -> Result<Self, Box<dyn Error>> {
        let root = tempfile::tempdir()?;
        let pki = generate_test_pki()?;
        let edge_server_paths = write_identity(root.path(), "edge-server", &pki.edge_server)?;
        let edge_client_paths = write_identity(root.path(), "edge-client", &pki.edge_client)?;
        let p4_client_paths = write_identity(root.path(), "p4-client", &pki.p4_client)?;
        let inference_client_paths =
            write_identity(root.path(), "inference-client", &pki.inference_client)?;
        let control_client_paths =
            write_identity(root.path(), "control-client", &pki.control_client)?;
        let p4 = FakeP4::new(p4info_bytes(), device_config_bytes(), PIPELINE_COOKIE);
        let p4_server = spawn_p4(p4.clone(), &pki.p4_server).await?;
        let route = route(0, &p4_server.address);
        let inference = FakeInference::new(route.clone(), inference_failures);
        let inference_server = spawn_inference(inference.clone(), &pki.inference_server).await?;
        let control = FakeControl::new(control_failures);
        let control_server = spawn_control(control.clone(), &pki.control_server).await?;
        let (edge_address, edge_listener_reservation) = reserved_address()?;
        let data_dir = root.path().join("edge-data");
        fs::create_dir(&data_dir)?;
        let config_path = root.path().join("edge-config.json");
        let config = config(ConfigInput {
            edge_address,
            data_dir: &data_dir,
            edge_server: &edge_server_paths,
            p4_client: &p4_client_paths,
            inference_client: &inference_client_paths,
            control_client: &control_client_paths,
            control_address: control_server.address,
            target_count,
            allowed_edge_client_digest: digest::sha256(&pki.edge_client.certificate_der),
        });
        fs::write(&config_path, serde_json::to_vec_pretty(&config)?)?;
        Ok(Self {
            root,
            pki,
            edge_server_paths,
            edge_client_paths,
            p4_client_paths,
            inference_client_paths,
            control_client_paths,
            p4,
            inference,
            control,
            p4_server,
            inference_server,
            control_server,
            edge_address,
            edge_listener_reservation: Mutex::new(Some(edge_listener_reservation)),
            config_path,
            data_dir,
        })
    }

    fn spawn_edge(&self) -> Result<Child, Box<dyn Error>> {
        let binary = std::env::var_os("CARGO_BIN_EXE_masi-edge")
            .map(PathBuf::from)
            .ok_or("Cargo did not expose the masi-edge binary")?;
        self.release_edge_listener_reservation()?;
        Ok(Command::new(binary)
            .arg("--config")
            .arg(&self.config_path)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()?)
    }

    fn spawn_edge_inherited_output(&self) -> Result<Child, Box<dyn Error>> {
        let binary = std::env::var_os("CARGO_BIN_EXE_masi-edge")
            .map(PathBuf::from)
            .ok_or("Cargo did not expose the masi-edge binary")?;
        self.release_edge_listener_reservation()?;
        Ok(Command::new(binary)
            .arg("--config")
            .arg(&self.config_path)
            .stdin(Stdio::null())
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit())
            .kill_on_drop(true)
            .spawn()?)
    }

    fn spawn_edge_with_nofile(&self, nofile: u64) -> Result<Child, Box<dyn Error>> {
        let binary = std::env::var_os("CARGO_BIN_EXE_masi-edge")
            .map(PathBuf::from)
            .ok_or("Cargo did not expose the masi-edge binary")?;
        self.release_edge_listener_reservation()?;
        Ok(Command::new("prlimit")
            .arg(format!("--nofile={nofile}:{nofile}"))
            .arg("--")
            .arg(binary)
            .arg("--config")
            .arg(&self.config_path)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()?)
    }

    fn release_edge_listener_reservation(&self) -> Result<(), Box<dyn Error>> {
        let reservation = self
            .edge_listener_reservation
            .lock()
            .map_err(|_| "edge listener reservation lock was poisoned")?
            .take();
        drop(reservation);
        Ok(())
    }

    async fn client(&self) -> Result<EdgeControlClient<Channel>, Box<dyn Error>> {
        self.client_as(&self.pki.edge_client).await
    }

    async fn client_as(
        &self,
        identity: &support::LeafIdentity,
    ) -> Result<EdgeControlClient<Channel>, Box<dyn Error>> {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            let tls = TonicClientTlsConfig::new()
                .domain_name("edge.test")
                .ca_certificate(Certificate::from_pem(identity.ca_pem.clone()))
                .identity(Identity::from_pem(
                    identity.certificate_pem.clone(),
                    identity.private_key_pem.clone(),
                ));
            let endpoint = Endpoint::from_shared(format!("https://{}", self.edge_address))?
                .connect_timeout(Duration::from_secs(1))
                .timeout(Duration::from_secs(2))
                .tls_config(tls)?;
            match endpoint.connect().await {
                Ok(channel) => return Ok(EdgeControlClient::new(channel)),
                Err(error) if Instant::now() < deadline => {
                    let _ignored = error;
                    sleep(Duration::from_millis(50)).await;
                }
                Err(error) => return Err(error.into()),
            }
        }
    }

    fn rewrite_config(&self, mutate: impl FnOnce(&mut EdgeConfig)) -> Result<(), Box<dyn Error>> {
        let mut config: EdgeConfig = serde_json::from_slice(&fs::read(&self.config_path)?)?;
        mutate(&mut config);
        fs::write(&self.config_path, serde_json::to_vec_pretty(&config)?)?;
        Ok(())
    }

    async fn shutdown(self) -> Result<(), Box<dyn Error>> {
        self.p4_server.shutdown().await?;
        self.inference_server.shutdown().await?;
        self.control_server.shutdown().await?;
        Ok(())
    }

    async fn shutdown_with_evidence(
        self,
        name: &str,
        mut evidence: serde_json::Value,
        edge_exit_code: i32,
        edge_process_reaped: bool,
    ) -> Result<(), Box<dyn Error>> {
        let root = self.root;
        let p4_address = self.p4_server.address;
        let inference_address = self.inference_server.address;
        let control_address = self.control_server.address;
        let p4_server_task_joined = self.p4_server.shutdown().await.is_ok();
        let inference_server_task_joined = self.inference_server.shutdown().await.is_ok();
        let control_server_task_joined = self.control_server.shutdown().await.is_ok();
        let p4_listener_released = TcpListener::bind(p4_address).is_ok();
        let inference_listener_released = TcpListener::bind(inference_address).is_ok();
        let control_listener_released = TcpListener::bind(control_address).is_ok();
        let cleanup_checks = [
            ("edge-process", edge_process_reaped),
            ("p4-server-task", p4_server_task_joined),
            ("inference-server-task", inference_server_task_joined),
            ("control-server-task", control_server_task_joined),
            ("p4-listener", p4_listener_released),
            ("inference-listener", inference_listener_released),
            ("control-listener", control_listener_released),
        ];
        let remaining_resources = cleanup_checks
            .iter()
            .filter_map(|(name, released)| (!released).then_some(*name))
            .collect::<Vec<_>>();
        let cleanup_completed = remaining_resources.is_empty();
        if !cleanup_completed {
            evidence["result"] = json!("FAIL");
            evidence["qualification"] = json!("NOT_QUALIFIED");
            evidence["interruption"] = json!("EVIDENCE_FAILURE");
            if let Some(error_count) = evidence["summary"]["error_count"].as_u64() {
                evidence["summary"]["error_count"] = json!(error_count.saturating_add(1));
            }
        }
        evidence["cleanup"] = json!({
            "attempted": true,
            "completed": cleanup_completed,
            "exit_code": edge_exit_code.clamp(0, 255),
            "remaining_resources": remaining_resources,
            "checks": {
                "edge_process_reaped": edge_process_reaped,
                "p4_server_task_joined": p4_server_task_joined,
                "inference_server_task_joined": inference_server_task_joined,
                "control_server_task_joined": control_server_task_joined,
                "p4_listener_released": p4_listener_released,
                "inference_listener_released": inference_listener_released,
                "control_listener_released": control_listener_released
            }
        });
        write_evidence(root.path(), name, &evidence)?;
        if !cleanup_completed {
            return Err("soak cleanup did not release every process, task, and listener".into());
        }
        Ok(())
    }

    fn touch_fields(&self) {
        let _ = (
            &self.root,
            &self.edge_server_paths,
            &self.edge_client_paths,
            &self.p4_client_paths,
            &self.inference_client_paths,
            &self.control_client_paths,
        );
    }
}

#[derive(Debug)]
struct TmpfsMount {
    path: PathBuf,
    mounted: bool,
}

impl TmpfsMount {
    fn mount(path: &Path, bytes: u64) -> Result<Self, Box<dyn Error>> {
        let status = StdCommand::new("mount")
            .arg("-t")
            .arg("tmpfs")
            .arg("-o")
            .arg(format!("size={bytes},nosuid,nodev,noexec"))
            .arg("tmpfs")
            .arg(path)
            .status()?;
        if !status.success() {
            return Err(format!("tmpfs mount failed with {status}").into());
        }
        Ok(Self {
            path: path.to_owned(),
            mounted: true,
        })
    }

    fn unmount(mut self) -> Result<(), Box<dyn Error>> {
        let status = StdCommand::new("umount").arg(&self.path).status()?;
        if !status.success() {
            return Err(format!("tmpfs unmount failed with {status}").into());
        }
        self.mounted = false;
        Ok(())
    }
}

impl Drop for TmpfsMount {
    fn drop(&mut self) {
        if !self.mounted {
            return;
        }
        for _ in 0..20 {
            if StdCommand::new("umount")
                .arg(&self.path)
                .status()
                .is_ok_and(|status| status.success())
            {
                self.mounted = false;
                return;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        let _ignored = StdCommand::new("umount").arg("-l").arg(&self.path).status();
        self.mounted = false;
    }
}

fn fill_until_enospc(path: &Path) -> Result<(u64, i32), Box<dyn Error>> {
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(path.join("enospc-injection.bin"))?;
    let block = vec![0xa5; 64 * 1024];
    let mut bytes = 0_u64;
    loop {
        match file.write_all(&block) {
            Ok(()) => bytes = bytes.saturating_add(block.len() as u64),
            Err(error) if error.raw_os_error() == Some(28) => return Ok((bytes, 28)),
            Err(error) => return Err(error.into()),
        }
    }
}

struct ConfigInput<'a> {
    edge_address: SocketAddr,
    data_dir: &'a Path,
    edge_server: &'a IdentityPaths,
    p4_client: &'a IdentityPaths,
    inference_client: &'a IdentityPaths,
    control_client: &'a IdentityPaths,
    control_address: SocketAddr,
    target_count: usize,
    allowed_edge_client_digest: String,
}

fn config(input: ConfigInput<'_>) -> EdgeConfig {
    let ConfigInput {
        edge_address,
        data_dir,
        edge_server,
        p4_client,
        inference_client,
        control_client,
        control_address,
        target_count,
        allowed_edge_client_digest,
    } = input;
    let identities = BTreeMap::from([
        (
            "p4-client".into(),
            client_tls(p4_client, "p4.test", "p4-client"),
        ),
        (
            "inference-client".into(),
            client_tls(inference_client, "inference.test", "inference-client"),
        ),
    ]);
    EdgeConfig {
        schema_version: "edge-config/v1".into(),
        edge_instance_id: "edge-module-instance".into(),
        listen_address: edge_address,
        deployment_tier: DeploymentTier::ModuleTest,
        data_dir: data_dir.into(),
        server_tls: ServerTlsConfig {
            certificate_path: edge_server.certificate.clone(),
            private_key_path: edge_server.private_key.clone(),
            client_ca_path: edge_server.ca.clone(),
            identity_ref: "edge-server".into(),
            allowed_client_certificate_sha256: vec![allowed_edge_client_digest],
        },
        control_sink: ControlSinkConfig {
            endpoint: format!("https://{}", control_address),
            tls: client_tls(control_client, "control.test", "control-client"),
        },
        client_identities: identities,
        endpoint_policy: EndpointPolicyConfig {
            allowed_management_cidrs: Vec::new(),
            module_test_loopback_allowlist: vec![std::net::IpAddr::V4(
                std::net::Ipv4Addr::LOCALHOST,
            )],
            resolution_deadline_ms: 1_000,
        },
        limits: RuntimeLimits {
            max_targets: target_count.max(1),
            actor_high_queue: 32,
            p4_stream_request_queue: 32,
            p4_stream_response_queue: 256,
            pending_digest_ack_queue: 256,
            p4_updates_per_write: 256,
            p4_entities_per_read: 256,
            p4_read_response_bytes: 1_048_576,
            p4_read_response_entities: 4_096,
            p4_connect_deadline_ms: 2_000,
            p4_arbitration_deadline_ms: 2_000,
            p4_rpc_deadline_ms: 2_000,
            wal_record_bytes: 4_194_304,
            wal_segment_bytes: 4_195_000,
            wal_max_age_seconds: 86_400,
            source_wal_bytes: 32_000_000,
            source_wal_records: 20_000,
            input_wal_bytes: 32_000_000,
            input_wal_records: 20_000,
            pending_input_identities: 20_000,
            result_wal_bytes: 32_000_000,
            result_wal_records: 20_000,
            journal_bytes: 32_000_000,
            journal_records: 20_000,
            route_journal_bytes: 32_000_000,
            route_journal_records: 4_096,
            telemetry_poll_interval_ms: 50,
            window_duration_ms: 100,
            allowed_lateness_ms: 0,
            idle_source_timeout_ms: 200,
            max_open_windows: 8,
            inference_batch_records: 32,
            inference_message_bytes: 1_048_576,
            inference_deadline_ms: 1_000,
            inference_max_attempts: 3,
            control_batch_records: 32,
            // A 4,096-rule exact per-entry readback manifest is slightly over
            // 1 MiB. Keep the qualified wire bound explicit and well below the
            // implementation ceiling (4 MiB).
            control_message_bytes: 2_097_152,
            control_deadline_ms: 1_000,
            observation_interval_ms: 100,
            observation_batches: 4,
            baseline_rules: 4_096,
            overlay_rules: 1_024,
            preflight_validity_ms: 30_000,
            preflight_deadline_ms: 5_000,
        },
        telemetry_source_profile_digest: digest::sha256(TELEMETRY_PROFILE_BYTES),
        feature_profile_digest: digest::sha256(FEATURE_PROFILE_BYTES),
        log_filter: "info,masi_edge=debug".into(),
    }
}

fn client_tls(paths: &IdentityPaths, server_name: &str, identity_ref: &str) -> ClientTlsConfig {
    ClientTlsConfig {
        ca_path: paths.ca.clone(),
        certificate_path: paths.certificate.clone(),
        private_key_path: paths.private_key.clone(),
        server_name: server_name.into(),
        identity_ref: identity_ref.into(),
    }
}

fn route(target_index: usize, inference_address: &SocketAddr) -> InferenceRoute {
    InferenceRoute {
        schema_version: "inference-route/v1".into(),
        shard_id: format!("target-{}", target_index),
        model_control_incarnation_id: "model-control-module-1".into(),
        logical_pool_id: "pool-cpu-module".into(),
        pool_generation: 1,
        binding_generation: 1,
        route_epoch: 1,
        model_revision_digest: digest::sha256(b"model-module-v1"),
        feature_contract_digest: digest::sha256(FEATURE_PROFILE_BYTES),
        label_contract_digest: digest::sha256(b"labels-module-v1"),
        output_adapter_digest: digest::sha256(b"output-module-v1"),
        wire_profile: "inference-central-grpc-batch/v1".into(),
        runtime_profile: "model-runtime-central-cpu/v1".into(),
        endpoint: format!("https://{}", inference_address),
        tls: Some(TlsClientIdentity {
            server_name: "inference.test".into(),
            identity_ref: "inference-client".into(),
        }),
        operation_id: "rollout-operation-module-1".into(),
        scope: format!("scope-target-{}", target_index),
        expected_binding_generation: 0,
        proposed_binding_generation: 1,
        current_binding_generation: 1,
        startup_envelope_digest: digest::sha256(b"startup-envelope-module-v1"),
        pool_observation_digest: digest::sha256(b"pool-observation-module-v1"),
        binding_digest: digest::sha256(b"binding-module-v1"),
        model_bundle_digest: digest::sha256(b"model-bundle-module-v1"),
        wire_profile_digest: digest::sha256(b"wire-profile-module-v1"),
        runtime_profile_digest: digest::sha256(b"runtime-profile-module-v1"),
        optimization_profile_digest: digest::sha256(b"optimization-profile-module-v1"),
    }
}

fn external_central_route(
    target_index: usize,
    runtime: &ExternalCentralRuntime,
) -> Result<InferenceRoute, Box<dyn Error>> {
    let readback = &runtime.binding_readback;
    if runtime.schema_version != "central-inference-oci-runtime-export/v1"
        || runtime.state != "READY"
        || readback.schema_version != "inference-committed-binding/v1"
        || runtime.tls.client_san != "masi-edge.test"
    {
        return Err("external Central runtime identity/profile mismatch".into());
    }
    Ok(InferenceRoute {
        schema_version: "inference-route/v1".into(),
        shard_id: format!("target-{target_index}"),
        model_control_incarnation_id: readback.model_control_incarnation_id.clone(),
        logical_pool_id: readback.logical_pool_id.clone(),
        pool_generation: readback.pool_generation.parse()?,
        binding_generation: readback.binding_generation.parse()?,
        route_epoch: 1,
        model_revision_digest: readback.model_revision_digest.clone(),
        feature_contract_digest: readback.feature_contract_digest.clone(),
        label_contract_digest: readback.label_contract_digest.clone(),
        output_adapter_digest: readback.output_adapter_digest.clone(),
        wire_profile: readback.wire_profile.clone(),
        runtime_profile: readback.runtime_profile.clone(),
        endpoint: runtime.gateway_endpoint.clone(),
        tls: Some(TlsClientIdentity {
            server_name: runtime.tls.server_name.clone(),
            identity_ref: "inference-client".into(),
        }),
        operation_id: readback.operation_id.clone(),
        scope: "scope-e2e".into(),
        expected_binding_generation: 0,
        proposed_binding_generation: readback.binding_generation.parse()?,
        current_binding_generation: readback.binding_generation.parse()?,
        startup_envelope_digest: readback.startup_envelope_digest.clone(),
        pool_observation_digest: readback.pool_observation_digest.clone(),
        binding_digest: readback.binding_digest.clone(),
        model_bundle_digest: readback.model_bundle_digest.clone(),
        wire_profile_digest: readback.wire_profile_digest.clone(),
        runtime_profile_digest: readback.runtime_profile_digest.clone(),
        optimization_profile_digest: readback.optimization_profile_digest.clone(),
    })
}

fn resume_request(route: &InferenceRoute, trace_id: &str) -> ResumeRouteRequest {
    ResumeRouteRequest {
        schema_version: "inference-committed-binding/v1".into(),
        shard_id: route.shard_id.clone(),
        model_control_incarnation_id: route.model_control_incarnation_id.clone(),
        route_epoch: route.route_epoch,
        trace_id: trace_id.into(),
        operation_id: route.operation_id.clone(),
        scope: route.scope.clone(),
        logical_pool_id: route.logical_pool_id.clone(),
        pool_generation: route.pool_generation,
        expected_binding_generation: route.expected_binding_generation,
        proposed_binding_generation: route.proposed_binding_generation,
        current_binding_generation: route.current_binding_generation,
        startup_envelope_digest: route.startup_envelope_digest.clone(),
        pool_observation_digest: route.pool_observation_digest.clone(),
        binding_digest: route.binding_digest.clone(),
        deadline_unix_ms: unix_ms() + 30_000,
    }
}

fn assignment(target_index: usize, p4_address: SocketAddr) -> TargetAssignment {
    let now = unix_ms();
    let election_floor = 1_000_000 + (target_index as u64 * 10_000);
    TargetAssignment {
        schema_version: "target-assignment/v1".into(),
        target_id: format!("target-{}", target_index),
        device_id: target_index as u64 + 1,
        role: "primary".into(),
        p4runtime_endpoint: format!("https://{}", p4_address),
        p4runtime_tls: Some(TlsClientIdentity {
            server_name: "p4.test".into(),
            identity_ref: "p4-client".into(),
        }),
        fence: Some(Fence {
            target_control_incarnation_id: "target-control-module-1".into(),
            target_assignment_generation: target_index as u64 + 1,
            actor_runtime_epoch: String::new(),
            application_generation: 1,
            election_id_high: 0,
            election_id_low: election_floor + 1,
        }),
        lease_id: format!("lease-target-{}", target_index),
        issued_at_unix_ms: now,
        expires_at_unix_ms: now + 300_000,
        election_floor,
        election_ceiling: election_floor + 9_999,
        expected_pipeline: Some(PipelineIdentity {
            p4runtime_api_version: "1.4.1".into(),
            p4info_digest: digest::sha256(&p4info_bytes()),
            device_config_digest: digest::sha256(&device_config_bytes()),
            profile_digest: digest::sha256(b"p4-profile-module-v1"),
            cookie: PIPELINE_COOKIE,
            supported_write_atomicity: vec![P4WriteAtomicity::ContinueOnError as i32],
        }),
        observation_epoch: 1,
        reset_epoch: 1,
        trace_id: format!("assign-target-{}", target_index),
    }
}

fn external_p4_assignment(runtime: &ExternalP4Runtime) -> Result<TargetAssignment, Box<dyn Error>> {
    if runtime.schema_version != "bmv2-edge-runtime-export/v1"
        || runtime.state != "READY"
        || runtime.tls.server_name != "masi-switch"
        || runtime.tls.client_san != "masi-p4-e2e-controller"
        || runtime.pipeline.supported_write_atomicity.as_slice() != ["CONTINUE_ON_ERROR"]
        || runtime.expected_traffic_packets == 0
        || runtime.expected_traffic_packets > 4_096
    {
        return Err("external BMv2 runtime identity/profile mismatch".into());
    }
    let now = unix_ms();
    Ok(TargetAssignment {
        schema_version: "target-assignment/v1".into(),
        target_id: "target-0".into(),
        device_id: 1,
        role: "default".into(),
        p4runtime_endpoint: runtime.p4runtime_endpoint.clone(),
        p4runtime_tls: Some(TlsClientIdentity {
            server_name: runtime.tls.server_name.clone(),
            identity_ref: "p4-client".into(),
        }),
        fence: Some(Fence {
            target_control_incarnation_id: "target-control-bmv2-rehearsal-1".into(),
            target_assignment_generation: 1,
            actor_runtime_epoch: String::new(),
            application_generation: 1,
            election_id_high: 0,
            election_id_low: 1_000_001,
        }),
        lease_id: "lease-bmv2-rehearsal-1".into(),
        issued_at_unix_ms: now,
        expires_at_unix_ms: now + 300_000,
        election_floor: 1_000_000,
        election_ceiling: 1_009_999,
        expected_pipeline: Some(PipelineIdentity {
            p4runtime_api_version: runtime.pipeline.p4runtime_api_version.clone(),
            p4info_digest: runtime.pipeline.p4info_digest.clone(),
            device_config_digest: runtime.pipeline.device_config_digest.clone(),
            profile_digest: runtime.pipeline.profile_digest.clone(),
            cookie: runtime.pipeline.cookie,
            supported_write_atomicity: vec![P4WriteAtomicity::ContinueOnError as i32],
        }),
        observation_epoch: 1,
        reset_epoch: 1,
        trace_id: "assign-real-bmv2-target-0".into(),
    })
}

fn baseline_effect(assignment: &TargetAssignment, operation: &str, rules: usize) -> EffectIntent {
    let mut intent = EffectIntent {
        schema_version: "effect-intent-edge/v1".into(),
        effect_intent_id: format!("effect-{}", operation),
        operation_id: operation.into(),
        target_id: assignment.target_id.clone(),
        fence: assignment.fence.clone(),
        effect_digest: String::new(),
        authorization_digest: digest::sha256(b"authorization-module"),
        kind: EffectKind::BaselineActivate as i32,
        policy_revision_digest: digest::sha256(operation.as_bytes()),
        default_action: FirewallAction::PermitAndContinue as i32,
        baseline_rules: (0..rules)
            .map(|index| BaselineRule {
                rule_id: format!("rule-{}-{}", operation, index),
                rule_revision: 1,
                canonical_rule_digest: digest::sha256(
                    format!("rule-{}-{}", operation, index).as_bytes(),
                ),
                priority: 10_000 - index as i32,
                ingress_port: Some(optional(Some(1))),
                source: Some(Ipv4Prefix {
                    address: 0xc000_0200 + index as u32,
                    prefix_length: 32,
                }),
                destination: Some(Ipv4Prefix {
                    address: 0xc633_640a,
                    prefix_length: 32,
                }),
                protocol: Some(optional(Some(6))),
                l4_present: Some(optional(Some(1))),
                source_port: Some(optional(None)),
                destination_port: Some(optional(Some(22))),
                fragment_class: Some(optional(Some(0))),
                action: FirewallAction::Drop as i32,
            })
            .collect(),
        overlay_rules: Vec::new(),
        bounded_capture: None,
        deadline_unix_ms: unix_ms() + 10_000,
        actor_ref: "operator-module".into(),
        reason_code: "BASELINE_ACTIVATION".into(),
        trace_id: format!("trace-{}", operation),
        required_write_atomicity: P4WriteAtomicity::ContinueOnError as i32,
    };
    let mut canonical = intent.clone();
    canonical.effect_digest.clear();
    intent.effect_digest = digest::message_sha256(&canonical);
    intent
}

fn optional(value: Option<u32>) -> OptionalUint32 {
    OptionalUint32 {
        present: value.is_some(),
        value: value.unwrap_or_default(),
    }
}

fn reserved_address() -> Result<(SocketAddr, TcpListener), Box<dyn Error>> {
    loop {
        let listener = TcpListener::bind("127.0.0.1:0")?;
        let address = listener.local_addr()?;
        let mut reserved = RESERVED_TEST_PORTS
            .get_or_init(|| Mutex::new(HashSet::new()))
            .lock()
            .map_err(|_| "test port reservation lock was poisoned")?;
        if reserved.insert(address.port()) {
            return Ok((address, listener));
        }
    }
}

fn unix_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(1, |duration| duration.as_millis() as i64)
}

fn write_evidence(
    root: &Path,
    name: &str,
    value: &serde_json::Value,
) -> Result<(), Box<dyn Error>> {
    let directory = std::env::var_os("MASI_EDGE_EVIDENCE_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| root.to_path_buf());
    fs::create_dir_all(&directory)?;
    fs::write(directory.join(name), serde_json::to_vec_pretty(value)?)?;
    Ok(())
}

fn utc_timestamp() -> Result<String, Box<dyn Error>> {
    let output = StdCommand::new("date")
        .args(["-u", "+%Y-%m-%dT%H:%M:%SZ"])
        .output()?;
    if !output.status.success() {
        return Err("date failed while producing an evidence timestamp".into());
    }
    let timestamp = String::from_utf8(output.stdout)?.trim().to_owned();
    if timestamp.is_empty() {
        return Err("date returned an empty evidence timestamp".into());
    }
    Ok(timestamp)
}

fn monotonic_ns() -> Result<u64, Box<dyn Error>> {
    let uptime = fs::read_to_string("/proc/uptime")?;
    let seconds: f64 = uptime
        .split_whitespace()
        .next()
        .ok_or("/proc/uptime did not contain monotonic time")?
        .parse()?;
    if !seconds.is_finite() || seconds < 0.0 {
        return Err("/proc/uptime contained invalid monotonic time".into());
    }
    Ok((seconds * 1_000_000_000.0) as u64)
}

#[derive(Debug, Default)]
struct ProcessCpuState {
    process_ticks: Option<u64>,
    total_ticks: Option<u64>,
}

#[derive(Debug)]
struct ProcessResources {
    cpu_pct: f64,
    rss_bytes: u64,
    fd_count: u64,
    thread_count: u64,
}

fn process_resources(
    pid: u32,
    cpu_state: &mut ProcessCpuState,
) -> Result<ProcessResources, Box<dyn Error>> {
    let process_stat = fs::read_to_string(format!("/proc/{pid}/stat"))?;
    let fields = process_stat
        .rsplit_once(") ")
        .ok_or("process stat did not contain a command terminator")?
        .1
        .split_whitespace()
        .collect::<Vec<_>>();
    let user_ticks: u64 = fields
        .get(11)
        .ok_or("process stat omitted user ticks")?
        .parse()?;
    let system_ticks: u64 = fields
        .get(12)
        .ok_or("process stat omitted system ticks")?
        .parse()?;
    let process_ticks = user_ticks.saturating_add(system_ticks);
    let system_stat = fs::read_to_string("/proc/stat")?;
    let total_ticks = system_stat
        .lines()
        .next()
        .ok_or("/proc/stat omitted aggregate CPU ticks")?
        .split_whitespace()
        .skip(1)
        .try_fold(0_u64, |sum, value| {
            value.parse::<u64>().map(|ticks| sum.saturating_add(ticks))
        })?;
    let cpu_pct = match (cpu_state.process_ticks, cpu_state.total_ticks) {
        (Some(previous_process), Some(previous_total)) if total_ticks > previous_total => {
            let process_delta = process_ticks.saturating_sub(previous_process) as f64;
            let total_delta = total_ticks.saturating_sub(previous_total) as f64;
            let cores =
                std::thread::available_parallelism().map_or(1.0, |value| value.get() as f64);
            (process_delta / total_delta * cores * 100.0).clamp(0.0, 100_000.0)
        }
        _ => 0.0,
    };
    cpu_state.process_ticks = Some(process_ticks);
    cpu_state.total_ticks = Some(total_ticks);

    let status = fs::read_to_string(format!("/proc/{pid}/status"))?;
    let rss_kib = status
        .lines()
        .find_map(|line| line.strip_prefix("VmRSS:"))
        .and_then(|value| value.split_whitespace().next())
        .ok_or("process status omitted VmRSS")?
        .parse::<u64>()?;
    let thread_count = status
        .lines()
        .find_map(|line| line.strip_prefix("Threads:"))
        .map(str::trim)
        .ok_or("process status omitted Threads")?
        .parse::<u64>()?;
    let fd_count = fs::read_dir(format!("/proc/{pid}/fd"))?.try_fold(
        0_u64,
        |count, entry| -> Result<u64, std::io::Error> {
            let _entry = entry?;
            Ok(count.saturating_add(1))
        },
    )?;
    Ok(ProcessResources {
        cpu_pct,
        rss_bytes: rss_kib.saturating_mul(1_024),
        fd_count,
        thread_count,
    })
}

async fn collect_soak_sample(
    client: &mut EdgeControlClient<Channel>,
    child: &Child,
    cpu_state: &mut ProcessCpuState,
    phase: &str,
    offset_ms: u64,
    trace_sequence: u64,
    canonical_commits: usize,
) -> Result<serde_json::Value, Box<dyn Error>> {
    let pid = child
        .id()
        .ok_or("Edge process id unavailable during soak")?;
    let resources = process_resources(pid, cpu_state)?;
    let status = client
        .get_status(GetStatusRequest {
            target_id: "target-0".into(),
            trace_id: format!("soak-sample-{phase}-{trace_sequence}"),
        })
        .await?
        .into_inner();
    let target = status.targets.first().ok_or("soak target status missing")?;
    if !status.process_live || !target.p4_connected || !target.primary {
        return Err(format!(
            "soak target was not live/connected/primary: {}",
            target.reason_code
        )
        .into());
    }
    let queue_depth = target
        .high_priority_queue_depth
        .saturating_add(target.telemetry_queue_depth)
        .saturating_add(target.observation_queue_depth);
    Ok(json!({
        "offset_ms": offset_ms,
        "phase": phase,
        "quality": "valid",
        "cpu_pct": resources.cpu_pct,
        "rss_bytes": resources.rss_bytes,
        "fd_count": resources.fd_count,
        "thread_count": resources.thread_count,
        "queue_depth": queue_depth,
        "oom_events": 0,
        "container_restarts": 0,
        "oracle_errors": 0,
        "gap_count": 0,
        "module_metrics": {
            "source_wal_bytes": target.source_wal_bytes,
            "input_wal_bytes": target.input_wal_bytes,
            "result_wal_bytes": target.result_wal_bytes,
            "canonical_commits": canonical_commits,
            "reason_code": target.reason_code
        }
    }))
}

async fn status_rpc_burst(
    client: &EdgeControlClient<Channel>,
    requests: usize,
    phase: &str,
    sequence: u64,
) -> (u64, u64) {
    let mut burst = JoinSet::new();
    for index in 0..requests {
        let mut request_client = client.clone();
        let trace_id = format!("soak-{phase}-{sequence}-{index}");
        burst.spawn(async move {
            request_client
                .get_status(GetStatusRequest {
                    target_id: "target-0".into(),
                    trace_id,
                })
                .await
        });
    }
    let mut successful = 0_u64;
    let mut errors = 0_u64;
    while let Some(result) = burst.join_next().await {
        match result {
            Ok(Ok(response)) if response.get_ref().process_live => {
                successful = successful.saturating_add(1);
            }
            Ok(Ok(_)) | Ok(Err(_)) | Err(_) => {
                errors = errors.saturating_add(1);
            }
        }
    }
    (successful, errors)
}

async fn renew_soak_lease_if_needed(
    client: &mut EdgeControlClient<Channel>,
    assigned: &mut TargetAssignment,
    trace_sequence: u64,
    run_started: &Instant,
    next_renewal: &mut Instant,
    last_observed_wall_ms: &mut i64,
) -> Result<Option<serde_json::Value>, Box<dyn Error>> {
    let monotonic_now = Instant::now();
    let now_ms = unix_ms();
    if now_ms < *last_observed_wall_ms {
        return Err("wall clock moved backwards during soak lease observation".into());
    }
    *last_observed_wall_ms = now_ms;
    if monotonic_now < *next_renewal {
        return Ok(None);
    }
    let previous_expires_at_unix_ms = assigned.expires_at_unix_ms;
    if previous_expires_at_unix_ms <= now_ms {
        return Err("soak target lease expired before its scheduled renewal".into());
    }
    let expires_at_unix_ms = now_ms
        .saturating_add(240_000)
        .max(previous_expires_at_unix_ms.saturating_add(1));
    let trace_id = format!("soak-lease-renew-{trace_sequence}");
    let reply = client
        .renew_target(RenewTargetRequest {
            schema_version: "target-lease-renew/v1".into(),
            target_id: assigned.target_id.clone(),
            fence: assigned.fence.clone(),
            lease_id: assigned.lease_id.clone(),
            expires_at_unix_ms,
            trace_id: trace_id.clone(),
        })
        .await?
        .into_inner();
    if reply.target_id != assigned.target_id || reply.state() != ActorState::Primary {
        return Err(format!(
            "soak lease renewal did not preserve PRIMARY for {}: {}",
            assigned.target_id, reply.reason_code
        )
        .into());
    }
    assigned.expires_at_unix_ms = expires_at_unix_ms;
    *next_renewal += Duration::from_secs(120);
    Ok(Some(json!({
        "offset_ms": run_started.elapsed().as_millis() as u64,
        "observed_at_unix_ms": now_ms,
        "previous_expires_at_unix_ms": previous_expires_at_unix_ms,
        "requested_expires_at_unix_ms": expires_at_unix_ms,
        "target_id": assigned.target_id.clone(),
        "lease_id": assigned.lease_id.clone(),
        "trace_id": trace_id,
        "state": "PRIMARY",
        "reason_code": reply.reason_code,
        "actor_runtime_epoch": reply.actor_runtime_epoch
    })))
}

async fn exercise_actor_saturation(
    topology: &Topology,
    client: &EdgeControlClient<Channel>,
    assigned: &TargetAssignment,
) -> (u64, u64, u64) {
    let reads_before = topology.p4.pipeline_read_attempts();
    topology.p4.delay_pipeline_reads(1, 750);
    let mut blocker_client = client.clone();
    let blocker_intent = baseline_effect(assigned, "soak-saturation-blocker", 1);
    let blocker = tokio::spawn(async move {
        blocker_client
            .preflight_effect(PreflightEffectRequest {
                intent: Some(blocker_intent),
            })
            .await
    });
    let deadline = Instant::now() + Duration::from_secs(5);
    while topology.p4.pipeline_read_attempts() <= reads_before {
        if Instant::now() >= deadline {
            blocker.abort();
            let _result = blocker.await;
            return (0, 0, 1);
        }
        sleep(Duration::from_millis(5)).await;
    }
    let mut burst = JoinSet::new();
    for index in 0..64 {
        let mut burst_client = client.clone();
        let intent = baseline_effect(assigned, &format!("soak-saturation-{index}"), 1);
        burst.spawn(async move {
            burst_client
                .preflight_effect(PreflightEffectRequest {
                    intent: Some(intent),
                })
                .await
        });
    }
    let mut accepted = 0_u64;
    let mut rejected = 0_u64;
    let mut errors = 0_u64;
    while let Some(result) = burst.join_next().await {
        match result {
            Ok(Ok(_)) => accepted = accepted.saturating_add(1),
            Ok(Err(error))
                if error.code() == tonic::Code::ResourceExhausted
                    && error.message().contains("ACTOR_QUEUE_FULL") =>
            {
                rejected = rejected.saturating_add(1);
            }
            Ok(Err(_)) | Err(_) => errors = errors.saturating_add(1),
        }
    }
    match blocker.await {
        Ok(Ok(_)) => accepted = accepted.saturating_add(1),
        Ok(Err(_)) | Err(_) => errors = errors.saturating_add(1),
    }
    if rejected == 0 {
        errors = errors.saturating_add(1);
    }
    (accepted, rejected, errors)
}

async fn graceful_terminate(child: &mut Child) -> Result<i32, Box<dyn Error>> {
    if let Some(status) = child.try_wait()? {
        return Ok(status.code().unwrap_or(1));
    }
    let pid = child
        .id()
        .ok_or("Edge process id unavailable for shutdown")?;
    let signal = StdCommand::new("kill")
        .args(["-TERM", &pid.to_string()])
        .status()?;
    if !signal.success() {
        child.start_kill()?;
        let _status = child.wait().await?;
        return Ok(1);
    }
    match tokio::time::timeout(Duration::from_secs(10), child.wait()).await {
        Ok(status) => Ok(status?.code().unwrap_or(1)),
        Err(_) => {
            child.start_kill()?;
            let _status = child.wait().await?;
            Ok(124)
        }
    }
}

fn assert_config_rejected(
    base: &EdgeConfig,
    field: &str,
    mutate: impl FnOnce(&mut EdgeConfig),
) -> Result<(), Box<dyn Error>> {
    let mut invalid = base.clone();
    mutate(&mut invalid);
    let error = invalid
        .validate()
        .err()
        .ok_or_else(|| format!("invalid {field} exceeded its frozen profile cap"))?;
    if !error.to_string().contains(field) {
        return Err(format!("validation for {field} returned {error}").into());
    }
    Ok(())
}

async fn wait_for_commits(control: &FakeControl, minimum: usize) -> Result<(), Box<dyn Error>> {
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        if control.committed_batches().len() >= minimum {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err(format!("timed out waiting for {} canonical commits", minimum).into());
        }
        sleep(Duration::from_millis(50)).await;
    }
}

async fn wait_for_external_control_event(
    runtime: &ExternalControlRuntime,
) -> Result<ExternalControlEvent, Box<dyn Error>> {
    if runtime.schema_version != "control-core-acceptance-runtime/v1"
        || runtime.state != "READY"
        || runtime.tls.client_san != "edge-e2e"
    {
        return Err("external Control runtime identity/profile mismatch".into());
    }
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        if runtime.event_evidence_path.is_file() {
            let event: ExternalControlEvent =
                serde_json::from_slice(&fs::read(&runtime.event_evidence_path)?)?;
            if event.schema_version != "go-event-commit-observation/v1"
                || event.event_id.is_empty()
                || event.event_idempotency_key.is_empty()
                || event.commit_status != "committed"
                || event.event_count != 1
                || event.incident_count > 1
                || event.worker_id.is_empty()
            {
                return Err("external Control Event evidence is invalid".into());
            }
            digest::validate_sha256(&event.input_digest, "event.input_digest")?;
            digest::validate_sha256(&event.output_digest, "event.output_digest")?;
            digest::validate_sha256(&event.worker_digest, "event.worker_digest")?;
            return Ok(event);
        }
        if Instant::now() >= deadline {
            return Err("timed out waiting for real Go/PostgreSQL Event commit".into());
        }
        sleep(Duration::from_millis(50)).await;
    }
}

async fn wait_for_target_state(
    client: &mut EdgeControlClient<Channel>,
    target_id: &str,
    expected: ActorState,
) -> Result<masi_edge::contract::edge::TargetStatus, Box<dyn Error>> {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let status = client
            .get_status(GetStatusRequest {
                target_id: target_id.into(),
                trace_id: format!("wait-state-{target_id}"),
            })
            .await?
            .into_inner();
        let target = status
            .targets
            .into_iter()
            .next()
            .ok_or("target status missing")?;
        if target.actor_state == expected as i32 {
            return Ok(target);
        }
        if Instant::now() >= deadline {
            return Err(format!("target {target_id} did not reach {expected:?}").into());
        }
        sleep(Duration::from_millis(50)).await;
    }
}

async fn wait_for_reason(
    client: &mut EdgeControlClient<Channel>,
    target_id: &str,
    expected_reason: &str,
) -> Result<masi_edge::contract::edge::TargetStatus, Box<dyn Error>> {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let target = client
            .get_status(GetStatusRequest {
                target_id: target_id.into(),
                trace_id: format!("wait-reason-{target_id}"),
            })
            .await?
            .into_inner()
            .targets
            .into_iter()
            .next()
            .ok_or("target status missing while waiting for reason")?;
        if target.reason_code.contains(expected_reason) {
            return Ok(target);
        }
        if Instant::now() >= deadline {
            return Err(format!(
                "target {target_id} did not expose reason {expected_reason}; last reason was {}",
                target.reason_code
            )
            .into());
        }
        sleep(Duration::from_millis(25)).await;
    }
}

async fn assign_and_activate_route(
    topology: &Topology,
    client: &mut EdgeControlClient<Channel>,
    target_index: usize,
    trace: &str,
) -> Result<TargetAssignment, Box<dyn Error>> {
    let mut assigned = assignment(target_index, topology.p4_server.address);
    let reply = client
        .assign_target(AssignTargetRequest {
            assignment: Some(assigned.clone()),
        })
        .await?
        .into_inner();
    if reply.state != ActorState::Primary as i32 {
        return Err(format!(
            "target assignment did not become primary: {}",
            reply.reason_code
        )
        .into());
    }
    assigned
        .fence
        .as_mut()
        .ok_or("assignment fence missing")?
        .actor_runtime_epoch = reply.actor_runtime_epoch;
    client
        .prepare_route(PrepareRouteRequest {
            schema_version: "inference-route-prepare/v1".into(),
            shard_id: assigned.target_id.clone(),
            current_model_control_incarnation_id: String::new(),
            current_route_epoch: 0,
            proposed_route_epoch: 1,
            trace_id: format!("{trace}-prepare"),
        })
        .await?;
    let exact_route = route(target_index, &topology.inference_server.address);
    client
        .commit_route(CommitRouteRequest {
            route: Some(exact_route.clone()),
            trace_id: format!("{trace}-commit"),
        })
        .await?;
    client
        .resume_route(resume_request(&exact_route, &format!("{trace}-resume")))
        .await?;
    Ok(assigned)
}

async fn wait_for_source_wal_growth(
    client: &mut EdgeControlClient<Channel>,
    target_id: &str,
    greater_than: u64,
) -> Result<u64, Box<dyn Error>> {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let target = client
            .get_status(GetStatusRequest {
                target_id: target_id.into(),
                trace_id: format!("wait-source-wal-{target_id}"),
            })
            .await?
            .into_inner()
            .targets
            .into_iter()
            .next()
            .ok_or("target status missing while waiting for source WAL")?;
        if target.source_wal_bytes > greater_than {
            return Ok(target.source_wal_bytes);
        }
        if Instant::now() >= deadline {
            return Err(format!("source WAL for {target_id} did not grow").into());
        }
        sleep(Duration::from_millis(20)).await;
    }
}

async fn terminate(child: &mut Child) -> Result<(), Box<dyn Error>> {
    child.start_kill()?;
    let _status = child.wait().await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn real_edge_routes_to_real_central_triton() -> Result<(), Box<dyn Error>> {
    let Some(runtime_path) = std::env::var_os("MASI_EDGE_REAL_CENTRAL_RUNTIME") else {
        return Ok(());
    };
    let evidence_path = std::env::var_os("MASI_EDGE_REAL_CENTRAL_EVIDENCE")
        .map(PathBuf::from)
        .ok_or("MASI_EDGE_REAL_CENTRAL_EVIDENCE is required")?;
    if evidence_path.exists() || evidence_path.is_symlink() {
        return Err("real Central evidence path must be fresh".into());
    }
    let keep_alive = std::env::var_os("MASI_EDGE_KEEP_ALIVE_TOKEN")
        .map(PathBuf::from)
        .map(|stop_token| {
            if !stop_token.is_absolute() {
                return Err("MASI_EDGE_KEEP_ALIVE_TOKEN must be absolute".into());
            }
            if stop_token.exists() || stop_token.is_symlink() {
                return Err("Edge keep-alive stop token must be fresh".into());
            }
            let seconds = std::env::var("MASI_EDGE_KEEP_ALIVE_SECONDS")
                .map_err(|_| "MASI_EDGE_KEEP_ALIVE_SECONDS is required with a stop token")?
                .parse::<u64>()?;
            if !(60..=22_800).contains(&seconds) {
                return Err("MASI_EDGE_KEEP_ALIVE_SECONDS must be in 60..22800".into());
            }
            Ok::<_, Box<dyn Error>>((stop_token, Duration::from_secs(seconds)))
        })
        .transpose()?;
    let runtime: ExternalCentralRuntime =
        serde_json::from_slice(&fs::read(PathBuf::from(runtime_path))?)?;
    let external_control = std::env::var_os("MASI_EDGE_REAL_CONTROL_RUNTIME")
        .map(PathBuf::from)
        .map(fs::read)
        .transpose()?
        .map(|raw| serde_json::from_slice::<ExternalControlRuntime>(&raw))
        .transpose()?;
    let external_p4 = std::env::var_os("MASI_EDGE_REAL_P4_RUNTIME")
        .map(PathBuf::from)
        .map(fs::read)
        .transpose()?
        .map(|raw| serde_json::from_slice::<ExternalP4Runtime>(&raw))
        .transpose()?;
    let exact_route = external_central_route(0, &runtime)?;
    let topology = Topology::new(1, 0, 0).await?;
    topology.rewrite_config(|config| {
        config.feature_profile_digest = runtime.binding_readback.feature_contract_digest.clone();
        config.client_identities.insert(
            "inference-client".into(),
            ClientTlsConfig {
                ca_path: runtime.tls.ca_path.clone(),
                certificate_path: runtime.tls.client_cert_path.clone(),
                private_key_path: runtime.tls.client_key_path.clone(),
                server_name: runtime.tls.server_name.clone(),
                identity_ref: "inference-client".into(),
            },
        );
        if let Some(control) = external_control.as_ref() {
            config.limits.telemetry_poll_interval_ms = 1_000;
            config.control_sink = ControlSinkConfig {
                endpoint: control.grpc_endpoint.clone(),
                tls: ClientTlsConfig {
                    ca_path: control.tls.ca_path.clone(),
                    certificate_path: control.tls.client_cert_path.clone(),
                    private_key_path: control.tls.client_key_path.clone(),
                    server_name: control.tls.server_name.clone(),
                    identity_ref: "control-client".into(),
                },
            };
        }
        if let Some(p4) = external_p4.as_ref() {
            config.limits.telemetry_poll_interval_ms = 5_000;
            config.client_identities.insert(
                "p4-client".into(),
                ClientTlsConfig {
                    ca_path: p4.tls.ca_path.clone(),
                    certificate_path: p4.tls.client_cert_path.clone(),
                    private_key_path: p4.tls.client_key_path.clone(),
                    server_name: p4.tls.server_name.clone(),
                    identity_ref: "p4-client".into(),
                },
            );
        }
    })?;
    let mut child = topology.spawn_edge_inherited_output()?;
    let mut client = topology.client().await?;
    let mut assigned = if let Some(p4) = external_p4.as_ref() {
        external_p4_assignment(p4)?
    } else {
        assignment(0, topology.p4_server.address)
    };
    let assignment_reply = client
        .assign_target(AssignTargetRequest {
            assignment: Some(assigned.clone()),
        })
        .await?
        .into_inner();
    if assignment_reply.state != ActorState::Primary as i32 {
        return Err(format!(
            "real Central target did not become primary: {}",
            assignment_reply.reason_code
        )
        .into());
    }
    assigned
        .fence
        .as_mut()
        .ok_or("real Central assignment fence missing")?
        .actor_runtime_epoch = assignment_reply.actor_runtime_epoch;
    client
        .prepare_route(PrepareRouteRequest {
            schema_version: "inference-route-prepare/v1".into(),
            shard_id: assigned.target_id.clone(),
            current_model_control_incarnation_id: String::new(),
            current_route_epoch: 0,
            proposed_route_epoch: 1,
            trace_id: "real-central-prepare".into(),
        })
        .await?;
    let commit = client
        .commit_route(CommitRouteRequest {
            route: Some(exact_route.clone()),
            trace_id: "real-central-commit".into(),
        })
        .await?
        .into_inner();
    if commit.state != "ready" {
        return Err(format!("real Central route commit state={}", commit.state).into());
    }
    let resume = client
        .resume_route(resume_request(&exact_route, "real-central-resume"))
        .await?
        .into_inner();
    if resume.state != "active" {
        return Err(format!("real Central route resume state={}", resume.state).into());
    }
    let external_traffic = if let Some(p4) = external_p4.as_ref() {
        let mut ready = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&p4.edge_ready_path)?;
        ready.write_all(b"READY\n")?;
        ready.sync_all()?;
        let traffic_deadline = Instant::now() + Duration::from_secs(30);
        while !p4.traffic_done_path.is_file() {
            if Instant::now() >= traffic_deadline {
                return Err("connected detection traffic sender did not complete".into());
            }
            sleep(Duration::from_millis(20)).await;
        }
        let traffic: ExternalTrafficReceipt =
            serde_json::from_slice(&fs::read(&p4.traffic_done_path)?)?;
        if traffic.schema_version != "bmv2-traffic-sender/v1"
            || traffic.packet_count != p4.expected_traffic_packets
            || traffic.peer_packet_count < p4.expected_traffic_packets
            || traffic.p4runtime_credentials_present
        {
            return Err("connected detection traffic receipt is invalid".into());
        }
        let source_wal_at_traffic_done = client
            .get_status(GetStatusRequest {
                target_id: assigned.target_id.clone(),
                trace_id: "connected-traffic-done".into(),
            })
            .await?
            .into_inner()
            .targets
            .into_iter()
            .next()
            .ok_or("connected traffic-done target status missing")?
            .source_wal_bytes;
        wait_for_source_wal_growth(&mut client, &assigned.target_id, source_wal_at_traffic_done)
            .await?;
        Some(traffic)
    } else {
        None
    };
    let (
        canonical_commit_batches,
        canonical_commit_attempts,
        worker_id,
        worker_digest,
        output_digest,
        control_sink_kind,
    ) = if let Some(control) = external_control.as_ref() {
        let event = match wait_for_external_control_event(control).await {
            Ok(event) => event,
            Err(wait_error) => {
                let status = client
                    .get_status(GetStatusRequest {
                        target_id: assigned.target_id.clone(),
                        trace_id: "real-control-timeout-status".into(),
                    })
                    .await?
                    .into_inner();
                return Err(format!("{wait_error}; Edge status at timeout: {status:?}").into());
            }
        };
        if !topology.control.committed_batches().is_empty() {
            return Err("fake Control unexpectedly received a real Go-bound result".into());
        }
        (
            event.event_count as usize,
            event.event_count as usize,
            event.worker_id,
            event.worker_digest,
            event.output_digest,
            "real-go-postgresql",
        )
    } else {
        if let Err(wait_error) = wait_for_commits(&topology.control, 1).await {
            let status = client
                .get_status(GetStatusRequest {
                    target_id: assigned.target_id.clone(),
                    trace_id: "real-central-timeout-status".into(),
                })
                .await?
                .into_inner();
            terminate(&mut child).await?;
            topology.shutdown().await?;
            return Err(format!("{wait_error}; Edge status at timeout: {status:?}").into());
        }
        let committed = topology.control.committed_batches();
        let record = committed
            .iter()
            .flat_map(|batch| batch.records.iter())
            .find(|record| record.execution_status == InferenceExecutionStatus::Ok as i32)
            .ok_or("real Central produced no canonically committed OK result")?;
        (
            committed.len(),
            topology.control.commit_attempts().len(),
            record.worker_id.clone(),
            record.worker_digest.clone(),
            record.output_digest.clone(),
            "deterministic-mtls-fake",
        )
    };
    if worker_id != runtime.binding_readback.worker_id
        || worker_digest != runtime.binding_readback.worker_digest
        || output_digest.is_empty()
    {
        return Err("real Central committed result worker/output identity mismatch".into());
    }
    if !topology.inference.calls().is_empty() {
        return Err("fake inference unexpectedly handled a real Central request".into());
    }
    let (p4runtime_kind, fake_p4_stream_opens, traffic_packets) =
        if let Some(p4) = external_p4.as_ref() {
            let status = client
                .get_status(GetStatusRequest {
                    target_id: assigned.target_id.clone(),
                    trace_id: "connected-final-p4-status".into(),
                })
                .await?
                .into_inner()
                .targets
                .into_iter()
                .next()
                .ok_or("connected final P4 status missing")?;
            if status.actor_state != ActorState::Primary as i32
                || !status.p4_connected
                || !status.primary
                || !status.pipeline_exact
                || status.p4info_digest != p4.pipeline.p4info_digest
                || status.source_wal_bytes == 0
                || topology.p4.stream_opens(1) != 0
            {
                return Err(format!("connected real P4 status mismatch: {status:?}").into());
            }
            (
                "real-bmv2",
                0,
                external_traffic
                    .as_ref()
                    .map_or(0, |traffic| traffic.packet_count),
            )
        } else {
            ("deterministic-mtls-fake", topology.p4.stream_opens(1), 0)
        };
    let evidence = json!({
        "schema_version":"edge-real-central-pairwise-rehearsal/v1",
        "target_id":assigned.target_id.clone(),
        "logical_pool_id":runtime.binding_readback.logical_pool_id,
        "binding_digest":runtime.binding_readback.binding_digest,
        "pool_observation_digest":runtime.binding_readback.pool_observation_digest,
        "worker_id":worker_id,
        "worker_digest":worker_digest,
        "output_digest":output_digest,
        "canonical_commit_batches":canonical_commit_batches,
        "canonical_commit_attempts":canonical_commit_attempts,
        "control_sink_kind":control_sink_kind,
        "p4runtime_kind":p4runtime_kind,
        "fake_p4_stream_opens":fake_p4_stream_opens,
        "traffic_packets":traffic_packets,
        "fake_inference_calls":0,
        "result":"PASS"
    });
    let mut evidence_file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&evidence_path)?;
    serde_json::to_writer_pretty(&mut evidence_file, &evidence)?;
    evidence_file.write_all(b"\n")?;
    evidence_file.sync_all()?;
    if let Some((stop_token, keep_alive_duration)) = keep_alive {
        let keep_started = Instant::now();
        let keep_deadline = keep_started + keep_alive_duration;
        let mut next_lease_renewal = Instant::now() + Duration::from_secs(120);
        let mut last_observed_wall_ms = unix_ms();
        let mut trace_sequence = 0_u64;
        while Instant::now() < keep_deadline {
            if stop_token.is_file() && !stop_token.is_symlink() {
                break;
            }
            if stop_token.exists() || stop_token.is_symlink() {
                return Err("Edge keep-alive stop token must be a regular file".into());
            }
            if child.try_wait()?.is_some() {
                return Err("Edge process exited during connected keep-alive".into());
            }
            trace_sequence = trace_sequence.saturating_add(1);
            let _renewal = renew_soak_lease_if_needed(
                &mut client,
                &mut assigned,
                trace_sequence,
                &keep_started,
                &mut next_lease_renewal,
                &mut last_observed_wall_ms,
            )
            .await?;
            let target = client
                .get_status(GetStatusRequest {
                    target_id: assigned.target_id.clone(),
                    trace_id: format!("connected-keep-alive-{trace_sequence}"),
                })
                .await?
                .into_inner()
                .targets
                .into_iter()
                .next()
                .ok_or("connected keep-alive target status missing")?;
            if target.actor_state != ActorState::Primary as i32
                || !target.lease_valid
                || external_p4.as_ref().is_some_and(|p4| {
                    !target.p4_connected
                        || !target.primary
                        || !target.pipeline_exact
                        || target.p4info_digest != p4.pipeline.p4info_digest
                })
            {
                return Err(format!(
                    "connected keep-alive target lost exact PRIMARY state: {target:?}"
                )
                .into());
            }
            sleep(Duration::from_secs(1)).await;
        }
    }
    terminate(&mut child).await?;
    topology.shutdown().await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn real_edge_reads_real_bmv2() -> Result<(), Box<dyn Error>> {
    let Some(runtime_path) = std::env::var_os("MASI_EDGE_REAL_P4_RUNTIME") else {
        return Ok(());
    };
    let evidence_path = std::env::var_os("MASI_EDGE_REAL_P4_EVIDENCE")
        .map(PathBuf::from)
        .ok_or("MASI_EDGE_REAL_P4_EVIDENCE is required")?;
    if evidence_path.exists() || evidence_path.is_symlink() {
        return Err("real BMv2 evidence path must be fresh".into());
    }
    let runtime: ExternalP4Runtime =
        serde_json::from_slice(&fs::read(PathBuf::from(runtime_path))?)?;
    let mut assigned = external_p4_assignment(&runtime)?;
    let topology = Topology::new(1, 0, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.telemetry_poll_interval_ms = 1_000;
        config.client_identities.insert(
            "p4-client".into(),
            ClientTlsConfig {
                ca_path: runtime.tls.ca_path.clone(),
                certificate_path: runtime.tls.client_cert_path.clone(),
                private_key_path: runtime.tls.client_key_path.clone(),
                server_name: runtime.tls.server_name.clone(),
                identity_ref: "p4-client".into(),
            },
        );
    })?;
    let mut child = topology.spawn_edge_inherited_output()?;
    let mut client = topology.client().await?;
    let assignment_reply = client
        .assign_target(AssignTargetRequest {
            assignment: Some(assigned.clone()),
        })
        .await?
        .into_inner();
    if assignment_reply.state != ActorState::Primary as i32 {
        return Err(format!(
            "real BMv2 target did not become primary: {}",
            assignment_reply.reason_code
        )
        .into());
    }
    assigned
        .fence
        .as_mut()
        .ok_or("real BMv2 assignment fence missing")?
        .actor_runtime_epoch = assignment_reply.actor_runtime_epoch;
    client
        .prepare_route(PrepareRouteRequest {
            schema_version: "inference-route-prepare/v1".into(),
            shard_id: assigned.target_id.clone(),
            current_model_control_incarnation_id: String::new(),
            current_route_epoch: 0,
            proposed_route_epoch: 1,
            trace_id: "real-bmv2-prepare".into(),
        })
        .await?;
    let exact_route = route(0, &topology.inference_server.address);
    client
        .commit_route(CommitRouteRequest {
            route: Some(exact_route.clone()),
            trace_id: "real-bmv2-commit".into(),
        })
        .await?;
    client
        .resume_route(resume_request(&exact_route, "real-bmv2-resume"))
        .await?;
    let mut ready = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&runtime.edge_ready_path)?;
    ready.write_all(b"READY\n")?;
    ready.sync_all()?;
    let source_wal_before_traffic = client
        .get_status(GetStatusRequest {
            target_id: assigned.target_id.clone(),
            trace_id: "real-bmv2-before-traffic".into(),
        })
        .await?
        .into_inner()
        .targets
        .into_iter()
        .next()
        .ok_or("real BMv2 pre-traffic target status missing")?
        .source_wal_bytes;
    let traffic_deadline = Instant::now() + Duration::from_secs(30);
    while !runtime.traffic_done_path.is_file() {
        if Instant::now() >= traffic_deadline {
            return Err("real BMv2 traffic sender did not complete".into());
        }
        sleep(Duration::from_millis(20)).await;
    }
    let traffic: ExternalTrafficReceipt =
        serde_json::from_slice(&fs::read(&runtime.traffic_done_path)?)?;
    if traffic.schema_version != "bmv2-traffic-sender/v1"
        || traffic.packet_count != runtime.expected_traffic_packets
        || traffic.peer_packet_count < runtime.expected_traffic_packets
        || traffic.p4runtime_credentials_present
        || traffic.started_at_unix_ns == 0
        || traffic.finished_at_unix_ns < traffic.started_at_unix_ns
    {
        return Err("real BMv2 traffic receipt is invalid".into());
    }
    let commits_at_traffic_done = topology.control.committed_batches().len();
    let source_wal_at_traffic_done = client
        .get_status(GetStatusRequest {
            target_id: assigned.target_id.clone(),
            trace_id: "real-bmv2-traffic-done".into(),
        })
        .await?
        .into_inner()
        .targets
        .into_iter()
        .next()
        .ok_or("real BMv2 traffic-done target status missing")?
        .source_wal_bytes;
    sleep(Duration::from_millis(1_200)).await;
    let source_wal_after_traffic = match wait_for_source_wal_growth(
        &mut client,
        &assigned.target_id,
        source_wal_at_traffic_done,
    )
    .await
    {
        Ok(bytes) => bytes,
        Err(wait_error) => {
            let status = client
                .get_status(GetStatusRequest {
                    target_id: assigned.target_id.clone(),
                    trace_id: "real-bmv2-source-timeout-status".into(),
                })
                .await?
                .into_inner();
            return Err(format!("{wait_error}; Edge status at timeout: {status:?}").into());
        }
    };
    if let Err(wait_error) =
        wait_for_commits(&topology.control, commits_at_traffic_done.saturating_add(1)).await
    {
        let status = client
            .get_status(GetStatusRequest {
                target_id: assigned.target_id.clone(),
                trace_id: "real-bmv2-timeout-status".into(),
            })
            .await?
            .into_inner();
        terminate(&mut child).await?;
        topology.shutdown().await?;
        return Err(format!("{wait_error}; Edge status at timeout: {status:?}").into());
    }
    let status = client
        .get_status(GetStatusRequest {
            target_id: assigned.target_id.clone(),
            trace_id: "real-bmv2-final-status".into(),
        })
        .await?
        .into_inner()
        .targets
        .into_iter()
        .next()
        .ok_or("real BMv2 final target status missing")?;
    if !status.p4_connected
        || !status.primary
        || !status.pipeline_exact
        || status.p4info_digest != runtime.pipeline.p4info_digest
        || status.actor_state != ActorState::Primary as i32
        || status.source_wal_bytes < source_wal_after_traffic
    {
        return Err(format!("real BMv2 Edge status is not exact: {status:?}").into());
    }
    if topology.p4.stream_opens(1) != 0 {
        return Err("deterministic fake P4 unexpectedly received a StreamChannel".into());
    }
    terminate(&mut child).await?;
    let traffic_started_ms = i64::try_from(traffic.started_at_unix_ns / 1_000_000)?;
    let post_traffic_inputs = topology
        .inference
        .input_batches()
        .into_iter()
        .flat_map(|batch| batch.records)
        .filter(|record| record.finalized_at_unix_ms >= traffic_started_ms)
        .collect::<Vec<_>>();
    let traffic_observed_packets = post_traffic_inputs
        .iter()
        .map(|record| {
            let bytes: [u8; 8] = record
                .feature_tensor
                .get(..8)
                .ok_or("post-traffic inference tensor is shorter than one uint64")?
                .try_into()?;
            Ok::<u64, Box<dyn Error>>(u64::from_le_bytes(bytes))
        })
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        .sum::<u64>();
    if traffic_observed_packets < runtime.expected_traffic_packets {
        let observations = post_traffic_inputs
            .iter()
            .map(|record| {
                let packets = record.feature_tensor.get(..8).map(|bytes| {
                    let mut value = [0_u8; 8];
                    value.copy_from_slice(bytes);
                    u64::from_le_bytes(value)
                });
                (
                    record.finalized_at_unix_ms,
                    record.window_id.clone(),
                    packets,
                )
            })
            .collect::<Vec<_>>();
        return Err(format!(
            "inference inputs observed {traffic_observed_packets} packets after sender start; expected at least {}; observations={observations:?}",
            runtime.expected_traffic_packets
        )
        .into());
    }
    let committed = topology.control.committed_batches();
    let evidence = json!({
        "schema_version":"edge-real-bmv2-pairwise-rehearsal/v1",
        "target_id":assigned.target_id,
        "p4runtime_api_version":runtime.pipeline.p4runtime_api_version,
        "p4info_digest":runtime.pipeline.p4info_digest,
        "device_config_digest":runtime.pipeline.device_config_digest,
        "pipeline_cookie":runtime.pipeline.cookie,
        "source_wal_bytes_before_traffic":source_wal_before_traffic,
        "source_wal_bytes_after_traffic":status.source_wal_bytes,
        "source_wal_bytes":status.source_wal_bytes,
        "post_traffic_inference_records":post_traffic_inputs.len(),
        "traffic_observed_packets":traffic_observed_packets,
        "input_wal_bytes":status.input_wal_bytes,
        "canonical_commit_batches":committed.len(),
        "fake_p4_stream_opens":0,
        "result":"PASS"
    });
    let mut evidence_file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&evidence_path)?;
    serde_json::to_writer_pretty(&mut evidence_file, &evidence)?;
    evidence_file.write_all(b"\n")?;
    topology.shutdown().await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn real_binary_runs_public_mtls_multi_target_pipeline() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(2, 1, 1).await?;
    topology.touch_fields();
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    let mut assignment0 = assignment(0, topology.p4_server.address);
    let mut assignment1 = assignment(1, topology.p4_server.address);
    let reply0 = client
        .assign_target(AssignTargetRequest {
            assignment: Some(assignment0.clone()),
        })
        .await?
        .into_inner();
    let reply1 = client
        .assign_target(AssignTargetRequest {
            assignment: Some(assignment1.clone()),
        })
        .await?
        .into_inner();
    assert_eq!(ActorState::Primary as i32, reply0.state);
    assert_eq!(ActorState::Primary as i32, reply1.state);
    assignment0
        .fence
        .as_mut()
        .ok_or("assignment 0 fence missing")?
        .actor_runtime_epoch = reply0.actor_runtime_epoch;
    assignment1
        .fence
        .as_mut()
        .ok_or("assignment 1 fence missing")?
        .actor_runtime_epoch = reply1.actor_runtime_epoch;
    assert_eq!(1, topology.p4.stream_opens(1));
    assert_eq!(1, topology.p4.stream_opens(2));
    assert_eq!(1, topology.p4.max_active_streams(1));
    assert_eq!(1, topology.p4.max_active_streams(2));

    for (index, assignment) in [assignment0.clone(), assignment1.clone()]
        .iter()
        .enumerate()
    {
        let route = route(index, &topology.inference_server.address);
        let prepare = client
            .prepare_route(PrepareRouteRequest {
                schema_version: "inference-route-prepare/v1".into(),
                shard_id: assignment.target_id.clone(),
                current_model_control_incarnation_id: String::new(),
                current_route_epoch: 0,
                proposed_route_epoch: 1,
                trace_id: format!("prepare-{}", index),
            })
            .await?
            .into_inner();
        assert_eq!("draining", prepare.state);
        let commit = client
            .commit_route(CommitRouteRequest {
                route: Some(route.clone()),
                trace_id: format!("commit-{}", index),
            })
            .await?
            .into_inner();
        assert_eq!("ready", commit.state);
        let resume = client
            .resume_route(resume_request(&route, &format!("resume-{}", index)))
            .await?
            .into_inner();
        assert_eq!("active", resume.state);
    }

    wait_for_commits(&topology.control, 2).await?;
    let calls = topology.inference.calls();
    assert!(calls.len() >= 3);
    let retry_index = calls
        .iter()
        .position(|call| call.2 == 2)
        .ok_or("same-generation retry attempt was not observed")?;
    let retry_call = &calls[retry_index];
    let initial_index = calls
        .iter()
        .position(|call| call.0 == retry_call.0 && call.1 == retry_call.1 && call.2 == 1)
        .ok_or("retry did not preserve its exact request and batch identity")?;
    assert!(initial_index < retry_index);
    assert!(
        calls
            .iter()
            .map(|call| &call.0)
            .collect::<HashSet<_>>()
            .len()
            >= 2
    );
    let equivalent_worker_deadline = Instant::now() + Duration::from_secs(15);
    loop {
        let committed = topology.control.committed_batches();
        let equivalent_worker_committed =
            committed
                .iter()
                .flat_map(|batch| &batch.records)
                .any(|record| {
                    record.worker_id == "worker-module-0002"
                        && record.worker_digest == digest::sha256(b"worker-module-0002")
                        && record.worker_attempt_id.starts_with("worker-attempt-2-")
                });
        if equivalent_worker_committed {
            break;
        }
        if Instant::now() >= equivalent_worker_deadline {
            let status0 = client
                .get_status(GetStatusRequest {
                    target_id: assignment0.target_id.clone(),
                    trace_id: "equivalent-worker-timeout-0".into(),
                })
                .await?
                .into_inner();
            let status1 = client
                .get_status(GetStatusRequest {
                    target_id: assignment1.target_id.clone(),
                    trace_id: "equivalent-worker-timeout-1".into(),
                })
                .await?
                .into_inner();
            let committed_workers = committed
                .iter()
                .flat_map(|batch| &batch.records)
                .map(|record| {
                    (
                        record.target_id.clone(),
                        record.worker_id.clone(),
                        record.worker_attempt_id.clone(),
                    )
                })
                .collect::<Vec<_>>();
            return Err(format!(
                "same-generation equivalent worker result was not canonically committed; \
                 committed_workers={committed_workers:?}; status0={status0:?}; status1={status1:?}"
            )
            .into());
        }
        sleep(Duration::from_millis(50)).await;
    }
    let commit_retry_deadline = Instant::now() + Duration::from_secs(5);
    let commit_attempts = loop {
        let attempts = topology.control.commit_attempts();
        let duplicate = attempts.iter().any(|digest| {
            attempts
                .iter()
                .filter(|candidate| *candidate == digest)
                .count()
                >= 2
        });
        if duplicate {
            break attempts;
        }
        if Instant::now() >= commit_retry_deadline {
            return Err("canonical commit ACK loss was not retried with the same digest".into());
        }
        sleep(Duration::from_millis(25)).await;
    };
    assert!(commit_attempts.len() >= 3);

    let intent = baseline_effect(&assignment0, "operation-e2e", 128);
    let preflight = client
        .preflight_effect(PreflightEffectRequest {
            intent: Some(intent.clone()),
        })
        .await?
        .into_inner();
    assert_eq!("accepted", preflight.result);
    let result = client
        .execute_effect(masi_edge::contract::edge::ExecuteEffectRequest {
            intent: Some(intent.clone()),
            preflight_token: preflight.preflight_token,
        })
        .await?
        .into_inner();
    assert_eq!(
        masi_edge::contract::edge::EffectStatus::Applied as i32,
        result.status
    );
    assert_eq!(128, result.expected_entries);
    assert_eq!(128, result.observed_entries);

    write_evidence(
        topology.root.path(),
        "module-blackbox-e2e.json",
        &json!({
            "schema_version": "edge-module-e2e-evidence/v1",
            "test_id": "TEST-EDGE-MODULE-E2E-001",
            "requirement_ids": [
                "MOD-EDGE-001", "ARCH-003", "ARCH-004", "ARCH-TELEMETRY-001",
                "ARCH-TARGET-FLEET-001", "FUNC-EFFECT-001", "FUNC-TEL-001",
                "TEST-003", "TEST-TEL-INF-001", "TEST-REAL-E2E-001"
            ],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "edge_process": "real Cargo-built masi-edge OS process",
            "neighbor_boundary": "deterministic independent mTLS fakes",
            "target_count": 2,
            "maximum_active_streams_per_device": 1,
            "central_retry_identity_stable": true,
            "same_generation_equivalent_worker_retry": true,
            "canonical_commit_retry_digest_stable": true,
            "effect_expected_entries": result.expected_entries,
            "effect_observed_entries": result.observed_entries,
            "overall_module_complete": false
        }),
    )?;

    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn committed_binding_handshake_is_exact_durable_and_idempotent() -> Result<(), Box<dyn Error>>
{
    let topology = Topology::new(1, 0, 0).await?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    let assigned = assignment(0, topology.p4_server.address);
    client
        .assign_target(AssignTargetRequest {
            assignment: Some(assigned.clone()),
        })
        .await?;
    client
        .prepare_route(PrepareRouteRequest {
            schema_version: "inference-route-prepare/v1".into(),
            shard_id: assigned.target_id.clone(),
            current_model_control_incarnation_id: String::new(),
            current_route_epoch: 0,
            proposed_route_epoch: 1,
            trace_id: "binding-handshake-prepare".into(),
        })
        .await?;
    let exact_route = route(0, &topology.inference_server.address);
    client
        .commit_route(CommitRouteRequest {
            route: Some(exact_route.clone()),
            trace_id: "binding-handshake-commit".into(),
        })
        .await?;

    let exact = resume_request(&exact_route, "binding-handshake-resume");
    let mut wrong_digest = exact.clone();
    wrong_digest.binding_digest = digest::sha256(b"wrong-binding");
    let mismatch = match client.resume_route(wrong_digest).await {
        Ok(_) => return Err("a mismatched committed binding was accepted".into()),
        Err(error) => error,
    };
    assert_eq!(tonic::Code::FailedPrecondition, mismatch.code());
    assert!(mismatch.message().contains("ROUTE_FENCE_MISMATCH"));

    let mut expired = exact.clone();
    expired.deadline_unix_ms = unix_ms().saturating_sub(1);
    let deadline = match client.resume_route(expired).await {
        Ok(_) => return Err("an expired first committed-binding admission was accepted".into()),
        Err(error) => error,
    };
    assert_eq!(tonic::Code::DeadlineExceeded, deadline.code());
    assert!(deadline.message().contains("DEADLINE_EXCEEDED"));

    let first = client.resume_route(exact.clone()).await?.into_inner();
    assert_eq!("active", first.state);
    assert_eq!(exact_route.binding_digest, first.binding_digest);
    assert!(!first.actor_runtime_epoch.is_empty());
    assert_eq!(first.actor_runtime_epoch, first.source_runtime_epoch);
    let watermark = first
        .resume_watermark
        .as_ref()
        .ok_or("resume watermark missing")?;
    assert_eq!("edge-route-resume-watermark/v1", watermark.schema_version);
    assert!(watermark.source_wal_last_sequence >= watermark.source_wal_checkpoint_sequence);
    assert!(watermark.input_wal_last_sequence >= watermark.input_wal_checkpoint_sequence);
    assert!(watermark.result_wal_last_sequence >= watermark.result_wal_checkpoint_sequence);

    let journal_segment = topology
        .data_dir
        .join("targets/target-0/route-journal/segment-00000000000000000001.wal");
    let bytes_after_first = fs::metadata(&journal_segment)?.len();
    let mut duplicate = exact.clone();
    duplicate.trace_id = "binding-handshake-retry".into();
    let duplicate_reply = client.resume_route(duplicate.clone()).await?.into_inner();
    assert_eq!("active", duplicate_reply.state);
    assert_eq!(bytes_after_first, fs::metadata(&journal_segment)?.len());

    let mut changed_deadline = duplicate.clone();
    changed_deadline.deadline_unix_ms = changed_deadline.deadline_unix_ms.saturating_add(1);
    let conflict = match client.resume_route(changed_deadline).await {
        Ok(_) => return Err("same operation with a changed durable tuple was accepted".into()),
        Err(error) => error,
    };
    assert_eq!(tonic::Code::FailedPrecondition, conflict.code());
    assert!(conflict.message().contains("ROUTE_FENCE_MISMATCH"));

    terminate(&mut child).await?;
    child = topology.spawn_edge()?;
    client = topology.client().await?;
    client
        .assign_target(AssignTargetRequest {
            assignment: Some(assigned),
        })
        .await?;
    duplicate.trace_id = "binding-handshake-retry-after-crash".into();
    let recovered = client.resume_route(duplicate).await?.into_inner();
    assert_eq!("active", recovered.state);
    assert_eq!(bytes_after_first, fs::metadata(&journal_segment)?.len());

    write_evidence(
        topology.root.path(),
        "committed-binding-handshake.json",
        &json!({
            "schema_version": "edge-module-fault-evidence/v1",
            "test_id": "TEST-EDGE-BINDING-HANDSHAKE-001",
            "requirement_ids": ["CONTRACT-MODEL-001", "CONTRACT-INFERENCE-001", "TEST-TEL-INF-001"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "exact_digest_mismatch_fenced": true,
            "expired_first_admission_rejected": true,
            "duplicate_response_idempotent": true,
            "duplicate_wal_append": false,
            "crash_recovery_idempotent": true,
            "watermark_returned": true
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn response_loss_reconciles_and_route_recovers_after_crash() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    let mut assigned = assignment(0, topology.p4_server.address);
    let reply = client
        .assign_target(AssignTargetRequest {
            assignment: Some(assigned.clone()),
        })
        .await?
        .into_inner();
    assigned
        .fence
        .as_mut()
        .ok_or("assignment fence missing")?
        .actor_runtime_epoch = reply.actor_runtime_epoch;
    client
        .prepare_route(PrepareRouteRequest {
            schema_version: "inference-route-prepare/v1".into(),
            shard_id: assigned.target_id.clone(),
            current_model_control_incarnation_id: String::new(),
            current_route_epoch: 0,
            proposed_route_epoch: 1,
            trace_id: "prepare-recovery".into(),
        })
        .await?;
    let exact_route = route(0, &topology.inference_server.address);
    client
        .commit_route(CommitRouteRequest {
            route: Some(exact_route.clone()),
            trace_id: "commit-recovery".into(),
        })
        .await?;
    client
        .resume_route(resume_request(&exact_route, "resume-recovery"))
        .await?;

    topology.p4.fail_next_effect_write_response();
    let intent = baseline_effect(&assigned, "response-loss", 1);
    let preflight = client
        .preflight_effect(PreflightEffectRequest {
            intent: Some(intent.clone()),
        })
        .await?
        .into_inner();
    let result = client
        .execute_effect(masi_edge::contract::edge::ExecuteEffectRequest {
            intent: Some(intent.clone()),
            preflight_token: preflight.preflight_token,
        })
        .await?
        .into_inner();
    assert_eq!(
        masi_edge::contract::edge::EffectStatus::Applied as i32,
        result.status
    );
    assert_eq!(1, result.observed_entries);
    assert!(topology.p4.effect_write_attempts() >= 2);
    let effect_writes_after_convergence = topology.p4.effect_write_attempts();
    let result_digest = digest::message_sha256(&result);

    let inference_calls_before_restart = topology.inference.calls().len();
    terminate(&mut child).await?;
    let opens_before = topology.p4.stream_opens(1);
    child = topology.spawn_edge()?;
    client = topology.client().await?;
    let recovered = client
        .assign_target(AssignTargetRequest {
            assignment: Some(assigned),
        })
        .await?
        .into_inner();
    assert_eq!(ActorState::Primary as i32, recovered.state);
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let status = client
            .get_status(GetStatusRequest {
                target_id: "target-0".into(),
                trace_id: "recovery-status".into(),
            })
            .await?
            .into_inner();
        let target = status
            .targets
            .first()
            .ok_or("recovered target status missing")?;
        if target.p4_connected
            && target.primary
            && topology.p4.stream_opens(1) > opens_before
            && topology.inference.calls().len() > inference_calls_before_restart
        {
            break;
        }
        if Instant::now() >= deadline {
            return Err("route/session recovery did not converge".into());
        }
        sleep(Duration::from_millis(50)).await;
    }
    assert_eq!(
        effect_writes_after_convergence,
        topology.p4.effect_write_attempts(),
        "journal recovery must reconcile without a blind duplicate write"
    );
    let ack = client
        .acknowledge_effect(AcknowledgeEffectRequest {
            schema_version: "effect-canonical-ack/v1".into(),
            target_id: intent.target_id,
            effect_intent_id: intent.effect_intent_id,
            operation_id: intent.operation_id,
            fence: result.fence,
            result_digest,
            canonical_effect_reference: "canonical-response-loss".into(),
            committed_at_unix_ms: unix_ms(),
            trace_id: "ack-response-loss-recovery".into(),
        })
        .await?
        .into_inner();
    assert_eq!("checkpointed", ack.status);
    write_evidence(
        topology.root.path(),
        "effect-journal-recovery.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-EFFECT-RECOVERY-001",
            "requirement_ids": [
                "MOD-EDGE-001", "ARCH-004", "CONTRACT-P4-FW-001",
                "FUNC-EFFECT-001", "TEST-003", "TEST-007", "TEST-P4-FW-001"
            ],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "fault": "P4 write response loss followed by Edge process crash",
            "effect_write_attempts": effect_writes_after_convergence,
            "duplicate_write_after_restart": false,
            "canonical_ack_after_recovery": true
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn corrupt_complete_wal_fails_closed_on_exact_recovery() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    let assigned = assignment(0, topology.p4_server.address);
    client
        .assign_target(AssignTargetRequest {
            assignment: Some(assigned.clone()),
        })
        .await?;
    sleep(Duration::from_millis(250)).await;
    terminate(&mut child).await?;

    let segment = topology
        .data_dir
        .join("targets/target-0/source/segment-00000000000000000001.wal");
    let mut bytes = fs::read(&segment)?;
    if bytes.len() <= 36 {
        return Err("source WAL did not contain a complete record".into());
    }
    let last = bytes.len() - 1;
    bytes[last] ^= 0xff;
    fs::write(&segment, bytes)?;

    child = topology.spawn_edge()?;
    client = topology.client().await?;
    let error = match client
        .assign_target(AssignTargetRequest {
            assignment: Some(assigned),
        })
        .await
    {
        Ok(_) => return Err("corrupt WAL exact recovery unexpectedly succeeded".into()),
        Err(error) => error,
    };
    assert_eq!(tonic::Code::FailedPrecondition, error.code());
    assert!(error.message().contains("WAL_CORRUPT"));
    write_evidence(
        topology.root.path(),
        "wal-corruption.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-WAL-CORRUPTION-001",
            "requirement_ids": ["MOD-EDGE-001", "ARCH-004", "TEST-003", "TEST-007"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "fault": "complete source WAL record checksum corruption",
            "grpc_code": "FAILED_PRECONDITION",
            "reason_code": "WAL_CORRUPT",
            "fail_closed": true,
            "overall_module_complete": false
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn security_boundaries_reject_wrong_identity_plaintext_and_metadata_endpoint()
-> Result<(), Box<dyn Error>> {
    let topology = Topology::new(2, 0, 0).await?;
    let mut child = topology.spawn_edge()?;

    let mut unauthorized = topology.client_as(&topology.pki.p4_client).await?;
    let wrong_identity = match unauthorized
        .get_status(GetStatusRequest {
            target_id: String::new(),
            trace_id: "wrong-client-identity".into(),
        })
        .await
    {
        Ok(_) => return Err("same-CA but unlisted workload identity was accepted".into()),
        Err(error) => error,
    };
    assert_eq!(tonic::Code::Unauthenticated, wrong_identity.code());
    assert!(wrong_identity.message().contains("TLS_IDENTITY_MISMATCH"));

    let plaintext = EdgeControlClient::connect(format!("http://{}", topology.edge_address)).await;
    if let Ok(mut plaintext_client) = plaintext {
        assert!(
            plaintext_client
                .get_status(GetStatusRequest {
                    target_id: String::new(),
                    trace_id: "plaintext-negative".into(),
                })
                .await
                .is_err()
        );
    }

    let mut client = topology.client().await?;
    let normal = assignment(0, topology.p4_server.address);
    client
        .assign_target(AssignTargetRequest {
            assignment: Some(normal.clone()),
        })
        .await?;
    client
        .prepare_route(PrepareRouteRequest {
            schema_version: "inference-route-prepare/v1".into(),
            shard_id: normal.target_id,
            current_model_control_incarnation_id: String::new(),
            current_route_epoch: 0,
            proposed_route_epoch: 1,
            trace_id: "identity-reuse-prepare".into(),
        })
        .await?;
    let mut reused_identity_route = route(0, &topology.inference_server.address);
    reused_identity_route.tls = Some(TlsClientIdentity {
        server_name: "p4.test".into(),
        identity_ref: "p4-client".into(),
    });
    let identity_reuse = match client
        .commit_route(CommitRouteRequest {
            route: Some(reused_identity_route),
            trace_id: "identity-reuse-commit".into(),
        })
        .await
    {
        Ok(_) => return Err("P4 and inference identity reuse was accepted".into()),
        Err(error) => error,
    };
    assert_eq!(tonic::Code::FailedPrecondition, identity_reuse.code());
    assert!(identity_reuse.message().contains("TLS_IDENTITY_REUSE"));

    let mut metadata_assignment = assignment(1, topology.p4_server.address);
    metadata_assignment.p4runtime_endpoint = "https://169.254.169.254:9559".into();
    let held = client
        .assign_target(AssignTargetRequest {
            assignment: Some(metadata_assignment),
        })
        .await?
        .into_inner();
    assert_eq!(ActorState::Hold as i32, held.state);
    assert!(held.reason_code.contains("ENDPOINT_NOT_ALLOWED"));

    write_evidence(
        topology.root.path(),
        "security-boundaries.json",
        &json!({
            "schema_version": "edge-security-evidence/v1",
            "test_id": "TEST-EDGE-SECURITY-001",
            "requirement_ids": ["MOD-EDGE-001", "TEST-003", "TEST-007"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "same_ca_unlisted_leaf_rejected": true,
            "plaintext_rejected": true,
            "cross_purpose_client_identity_reuse_rejected": true,
            "metadata_endpoint_rejected": true,
            "stable_reason_codes": ["TLS_IDENTITY_MISMATCH", "TLS_IDENTITY_REUSE", "ENDPOINT_NOT_ALLOWED"],
            "overall_module_complete": false
        }),
    )?;

    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn supplemental_digest_and_packet_are_durable_before_digest_ack() -> Result<(), Box<dyn Error>>
{
    let topology = Topology::new(1, 0, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.telemetry_poll_interval_ms = 5_000;
    })?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    let reply = client
        .assign_target(AssignTargetRequest {
            assignment: Some(assignment(0, topology.p4_server.address)),
        })
        .await?
        .into_inner();
    assert_eq!(ActorState::Primary as i32, reply.state);

    let before = client
        .get_status(GetStatusRequest {
            target_id: "target-0".into(),
            trace_id: "hint-before".into(),
        })
        .await?
        .into_inner()
        .targets
        .into_iter()
        .next()
        .ok_or("hint target status missing")?
        .source_wal_bytes;
    let digest_hint = DigestList {
        digest_id: 17,
        list_id: 42,
        data: vec![b"flow-key".to_vec(), b"sample-only".to_vec()],
        timestamp: unix_ms(),
    };
    let digest_payload = digest_hint.encode_to_vec();
    assert!(topology.p4.inject_digest(1, digest_hint));
    let ack_deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if topology
            .p4
            .digest_acks(1)
            .iter()
            .any(|ack| ack.digest_id == 17 && ack.list_id == 42)
        {
            break;
        }
        if Instant::now() >= ack_deadline {
            return Err("durable digest was not acknowledged".into());
        }
        sleep(Duration::from_millis(20)).await;
    }
    let after_digest = wait_for_source_wal_growth(&mut client, "target-0", before).await?;
    sleep(Duration::from_millis(100)).await;
    let before_packet = client
        .get_status(GetStatusRequest {
            target_id: "target-0".into(),
            trace_id: "hint-before-packet".into(),
        })
        .await?
        .into_inner()
        .targets
        .into_iter()
        .next()
        .ok_or("hint target status missing before PacketIn")?
        .source_wal_bytes
        .max(after_digest);
    let packet_hint = PacketIn {
        payload: vec![0x45, 0x00, 0x00, 0x14],
        metadata: vec![PacketMetadata {
            metadata_id: 1,
            value: 7_u32.to_be_bytes().to_vec(),
        }],
    };
    let packet_payload = packet_hint.encode_to_vec();
    assert!(topology.p4.inject_packet(1, packet_hint));
    wait_for_source_wal_growth(&mut client, "target-0", before_packet).await?;
    terminate(&mut child).await?;

    let source_dir = topology.data_dir.join("targets/target-0/source");
    let wal = DurableWal::open(
        &source_dir,
        WalKind::Source,
        WalLimits {
            max_bytes: 32_000_000,
            max_records: 20_000,
            segment_bytes: 4_195_000,
            max_record_bytes: 4_194_304,
            max_age_seconds: 86_400,
        },
    )?;
    let decoded = wal
        .replay(20_000, 32_000_000)?
        .into_iter()
        .map(|item| {
            SourceWalRecord::decode(item.payload.as_slice()).map(|record| (item.sequence, record))
        })
        .collect::<Result<Vec<_>, _>>()?;
    let (digest_sequence, digest_record) = decoded
        .iter()
        .find(|(_, record)| record.stage == SourceWalStage::DigestHintDurable as i32)
        .ok_or("durable digest source record missing")?;
    let digest_observation = digest_record
        .hint
        .as_ref()
        .ok_or("durable digest metadata missing")?;
    assert_eq!(1, digest_observation.kind);
    assert_eq!(17, digest_observation.digest_id);
    assert_eq!(42, digest_observation.list_id);
    assert_eq!(
        digest::sha256(&digest_payload),
        digest_observation.payload_digest
    );
    assert_eq!(
        digest_payload.len() as u64,
        digest_observation.payload_bytes
    );
    let digest_ack_record = decoded
        .iter()
        .find(|(_, record)| record.stage == SourceWalStage::DigestHintAcked as i32)
        .map(|(_, record)| record)
        .ok_or("durable digest ACK source record missing")?;
    assert_eq!(*digest_sequence, digest_ack_record.operation_wal_sequence);
    let packet_record = decoded
        .iter()
        .find(|(_, record)| record.stage == SourceWalStage::PacketHintDurable as i32)
        .map(|(_, record)| record)
        .ok_or("durable PacketIn source record missing")?;
    let packet_observation = packet_record
        .hint
        .as_ref()
        .ok_or("durable PacketIn metadata missing")?;
    assert_eq!(2, packet_observation.kind);
    assert_eq!(0, packet_observation.digest_id);
    assert_eq!(0, packet_observation.list_id);
    assert_eq!(
        digest::sha256(&packet_payload),
        packet_observation.payload_digest
    );
    assert_eq!(
        packet_payload.len() as u64,
        packet_observation.payload_bytes
    );
    assert_eq!(1, topology.p4.digest_acks(1).len());
    write_evidence(
        topology.root.path(),
        "supplemental-hints.json",
        &json!({
            "schema_version": "edge-telemetry-evidence/v1",
            "test_id": "TEST-EDGE-SUPPLEMENTAL-HINTS-001",
            "requirement_ids": [
                "MOD-EDGE-001", "ARCH-TELEMETRY-001", "CONTRACT-TELEMETRY-001",
                "FUNC-TEL-001", "TEST-003", "TEST-TEL-INF-001"
            ],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "digest_id": 17,
            "digest_list_id": 42,
            "digest_wal_sequence": digest_sequence,
            "digest_ack_references_durable_sequence": true,
            "packet_in_durable": true,
            "packet_in_ack_emitted": false,
            "supplemental_source_semantics": "loss-explicit hint/sample; not canonical complete flow",
            "overall_module_complete": false
        }),
    )?;
    drop(wal);
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn digest_ack_recovery_queue_bound_fails_closed() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.pending_digest_ack_queue = 1;
        config.limits.telemetry_poll_interval_ms = 5_000;
    })?;
    let source_dir = topology.data_dir.join("targets/target-0/source");
    let mut wal = DurableWal::open(
        &source_dir,
        WalKind::Source,
        WalLimits {
            max_bytes: 32_000_000,
            max_records: 20_000,
            segment_bytes: 4_195_000,
            max_record_bytes: 4_194_304,
            max_age_seconds: 86_400,
        },
    )?;
    for list_id in 1..=2_u64 {
        wal.append_message(&SourceWalRecord {
            schema_version: "edge-source-wal/v1".into(),
            stage: SourceWalStage::DigestHintDurable as i32,
            hint: Some(SupplementalHint {
                target_id: "target-0".into(),
                source_runtime_epoch: "actor-recovery-bound".into(),
                kind: 1,
                digest_id: 17,
                list_id,
                payload_digest: digest::sha256(&list_id.to_be_bytes()),
                payload_bytes: 8,
                received_at_unix_ms: unix_ms(),
            }),
            recorded_at_unix_ms: unix_ms(),
            reason_code: "DIGEST_HINT_DURABLE".into(),
            source_runtime_epoch: "actor-recovery-bound".into(),
            ..SourceWalRecord::default()
        })?;
    }
    drop(wal);

    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    let error = match client
        .assign_target(AssignTargetRequest {
            assignment: Some(assignment(0, topology.p4_server.address)),
        })
        .await
    {
        Ok(_) => return Err("digest ACK recovery exceeded its queue without rejection".into()),
        Err(error) => error,
    };
    assert_eq!(tonic::Code::ResourceExhausted, error.code());
    assert!(error.message().contains("DIGEST_ACK_RECOVERY_LIMIT"));
    assert_eq!(1, topology.p4.max_active_streams(1));
    write_evidence(
        topology.root.path(),
        "digest-ack-recovery-bound.json",
        &json!({
            "schema_version": "edge-resource-bound-evidence/v1",
            "test_id": "TEST-EDGE-DIGEST-ACK-BOUND-001",
            "requirement_ids": ["MOD-EDGE-001", "TEST-007", "TEST-TEL-INF-001"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "configured_pending_digest_ack_queue": 1,
            "durable_unacknowledged_digest_records": 2,
            "grpc_code": "RESOURCE_EXHAUSTED",
            "reason_code": "DIGEST_ACK_RECOVERY_LIMIT",
            "fail_closed_before_actor_admission": true,
            "maximum_active_streams": topology.p4.max_active_streams(1)
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn rule_observation_preserves_installation_counter_and_eligible_layers()
-> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.observation_interval_ms = 50;
        config.limits.telemetry_poll_interval_ms = 5_000;
    })?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    let mut assigned = assignment(0, topology.p4_server.address);
    let reply = client
        .assign_target(AssignTargetRequest {
            assignment: Some(assigned.clone()),
        })
        .await?
        .into_inner();
    assigned
        .fence
        .as_mut()
        .ok_or("observation assignment fence missing")?
        .actor_runtime_epoch = reply.actor_runtime_epoch;

    let intent = baseline_effect(&assigned, "rule-observation", 1);
    let expected_plan = firewall::compile(&intent, 0, 4_096, 1_024)?;
    let expected_entry = expected_plan
        .entries
        .first()
        .cloned()
        .ok_or("compiled observation entry missing")?;
    let compiled_entry = expected_plan
        .contract
        .entries
        .first()
        .cloned()
        .ok_or("compiled observation contract entry missing")?;
    let preflight = client
        .preflight_effect(PreflightEffectRequest {
            intent: Some(intent.clone()),
        })
        .await?
        .into_inner();
    let result = client
        .execute_effect(masi_edge::contract::edge::ExecuteEffectRequest {
            intent: Some(intent.clone()),
            preflight_token: preflight.preflight_token,
        })
        .await?
        .into_inner();
    assert_eq!(EffectStatus::Applied as i32, result.status);
    assert_eq!(1, result.active_bank);
    let result_digest = digest::message_sha256(&result);
    client
        .acknowledge_effect(AcknowledgeEffectRequest {
            schema_version: "effect-canonical-ack/v1".into(),
            target_id: assigned.target_id.clone(),
            effect_intent_id: intent.effect_intent_id.clone(),
            operation_id: intent.operation_id.clone(),
            fence: result.fence.clone(),
            result_digest,
            canonical_effect_reference: "canonical-rule-observation".into(),
            committed_at_unix_ms: unix_ms(),
            trace_id: "ack-rule-observation".into(),
        })
        .await?;

    assert!(topology.p4.seed_direct_counter(
        1,
        &expected_entry,
        CounterData {
            byte_count: 448,
            packet_count: 7,
        },
    ));
    topology.p4.seed_counter(
        1,
        firewall::counter_id::BASELINE_ELIGIBLE,
        1,
        CounterData {
            byte_count: 640,
            packet_count: 10,
        },
    );
    let mut configure = ConfigureRuleObservationsRequest {
        schema_version: "p4-rule-observation-config/v1".into(),
        target_id: assigned.target_id.clone(),
        fence: assigned.fence.clone(),
        observation_epoch: assigned.observation_epoch,
        reset_epoch: assigned.reset_epoch,
        replace_all: true,
        rules: vec![ObservableRule {
            effect_intent_id: intent.effect_intent_id,
            operation_id: intent.operation_id,
            rule_id: compiled_entry.logical_rule_id,
            canonical_entity: expected_entry.encode_to_vec(),
            canonical_entry_digest: compiled_entry.canonical_entry_digest,
            entity_id: compiled_entry.entity_id,
            match_priority_action_digest: compiled_entry.match_priority_action_digest,
            table_id: compiled_entry.table_id,
            direct_counter_id: compiled_entry.direct_counter_id,
            bank: compiled_entry.bank,
            expires_at_unix_ms: 0,
        }],
        configuration_digest: String::new(),
        trace_id: "configure-rule-observation".into(),
    };
    let mut canonical = configure.clone();
    canonical.configuration_digest.clear();
    canonical.trace_id.clear();
    configure.configuration_digest = digest::message_sha256(&canonical);
    let acknowledgement = client
        .configure_rule_observations(configure)
        .await?
        .into_inner();
    assert_eq!("accepted", acknowledgement.status);
    assert_eq!(
        "CANONICAL_OBSERVATION_SET_REPLACED",
        acknowledgement.reason_code
    );

    let deadline = Instant::now() + Duration::from_secs(10);
    let batch = loop {
        if let Some(batch) = topology
            .control
            .rule_observation_batches()
            .into_iter()
            .next()
        {
            break batch;
        }
        if Instant::now() >= deadline {
            return Err("rule observation sweep was not published".into());
        }
        sleep(Duration::from_millis(25)).await;
    };
    let mut canonical_batch = batch.clone();
    canonical_batch.batch_digest.clear();
    assert_eq!(digest::message_sha256(&canonical_batch), batch.batch_digest);
    let observation = batch
        .observations
        .first()
        .ok_or("published rule observation missing")?;
    assert_eq!(assigned.target_id, observation.target_id);
    assert_eq!(assigned.fence, observation.fence);
    assert_eq!(assigned.observation_epoch, observation.observation_epoch);
    assert_eq!(assigned.reset_epoch, observation.reset_epoch);
    assert_eq!(1, observation.sample_sequence);
    assert_eq!(
        Some((7, 448)),
        observation
            .cumulative
            .as_ref()
            .map(|value| (value.packets, value.bytes))
    );
    assert_eq!(
        Some((10, 640)),
        observation
            .eligible_cumulative
            .as_ref()
            .map(|value| (value.packets, value.bytes))
    );
    assert_eq!("exact", observation.installation_readback);
    assert_eq!(
        InstallationReadbackStatus::Exact as i32,
        observation.installation_status
    );
    assert_eq!("valid", observation.quality);
    assert_eq!(DataQuality::Valid as i32, observation.quality_code);
    assert_eq!(vec!["NONE"], observation.quality_reasons);
    write_evidence(
        topology.root.path(),
        "rule-observation.json",
        &json!({
            "schema_version": "edge-rule-observation-evidence/v1",
            "test_id": "TEST-EDGE-RULE-OBSERVATION-001",
            "requirement_ids": [
                "MOD-EDGE-001", "CONTRACT-RULE-001", "TEST-003", "TEST-007",
                "TEST-RULE-001"
            ],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "installation_readback": "exact",
            "direct_counter": {"packets": 7, "bytes": 448},
            "eligible_counter": {"packets": 10, "bytes": 640},
            "ratio_computed_by_edge": false,
            "packet_action_outcome_inferred_from_counter": false,
            "batch_digest_exact": true,
            "overall_module_complete": false
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn p4_stream_reconnect_is_target_isolated() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(2, 0, 0).await?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    for target_index in 0..2 {
        let reply = client
            .assign_target(AssignTargetRequest {
                assignment: Some(assignment(target_index, topology.p4_server.address)),
            })
            .await?
            .into_inner();
        assert_eq!(ActorState::Primary as i32, reply.state);
    }
    let unaffected_opens = topology.p4.stream_opens(2);
    topology.p4.close_stream(1);
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let target = client
            .get_status(GetStatusRequest {
                target_id: "target-0".into(),
                trace_id: "p4-reconnect-isolation".into(),
            })
            .await?
            .into_inner()
            .targets
            .into_iter()
            .next()
            .ok_or("target-0 status missing")?;
        if topology.p4.stream_opens(1) >= 2
            && topology.p4.active_streams(1) == 1
            && target.primary
            && target.p4_connected
        {
            break;
        }
        if Instant::now() >= deadline {
            return Err("target-scoped P4 stream did not reconnect".into());
        }
        sleep(Duration::from_millis(50)).await;
    }
    assert_eq!(unaffected_opens, topology.p4.stream_opens(2));
    assert_eq!(1, topology.p4.active_streams(2));
    assert_eq!(1, topology.p4.max_active_streams(1));
    assert_eq!(1, topology.p4.max_active_streams(2));
    write_evidence(
        topology.root.path(),
        "p4-stream-reconnect.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-P4-RECONNECT-001",
            "requirement_ids": ["MOD-EDGE-001", "ARCH-004", "TEST-003", "TEST-007"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "fault": "target-0 StreamChannel closure",
            "target_0_stream_opens": topology.p4.stream_opens(1),
            "target_1_stream_opens": topology.p4.stream_opens(2),
            "maximum_active_streams_per_device": 1,
            "unaffected_target_remained_connected": true
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn pipeline_mismatch_reconnect_does_not_leak_streamchannel() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.telemetry_poll_interval_ms = 5_000;
    })?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    let mut mismatched = assignment(0, topology.p4_server.address);
    mismatched
        .expected_pipeline
        .as_mut()
        .ok_or("pipeline mismatch assignment omitted expected pipeline")?
        .p4info_digest = digest::sha256(b"wrong-p4info");

    let reply = client
        .assign_target(AssignTargetRequest {
            assignment: Some(mismatched),
        })
        .await?
        .into_inner();
    assert_eq!(ActorState::Hold as i32, reply.state);
    assert!(reply.reason_code.contains("P4INFO_DRIFT"));

    let deadline = Instant::now() + Duration::from_secs(5);
    while topology.p4.stream_opens(1) < 3 {
        if Instant::now() >= deadline {
            return Err("pipeline mismatch did not exercise bounded reconnects".into());
        }
        sleep(Duration::from_millis(25)).await;
    }
    let quiesce_deadline = Instant::now() + Duration::from_secs(1);
    while topology.p4.active_streams(1) != 0 && Instant::now() < quiesce_deadline {
        sleep(Duration::from_millis(10)).await;
    }
    assert_eq!(0, topology.p4.active_streams(1));
    assert_eq!(1, topology.p4.max_active_streams(1));
    write_evidence(
        topology.root.path(),
        "p4-pipeline-mismatch-no-session-leak.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-P4-PIPELINE-SESSION-FENCE-001",
            "requirement_ids": [
                "MOD-EDGE-001", "ARCH-003", "ARCH-004", "TEST-003", "TEST-007"
            ],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "fault": "exact pipeline mismatch after successful arbitration",
            "reason_code": "P4INFO_DRIFT",
            "reconnect_attempts": topology.p4.stream_opens(1),
            "active_streams_after_failed_connect": topology.p4.active_streams(1),
            "maximum_active_streams": topology.p4.max_active_streams(1),
            "second_writer_or_session": false
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn p4runtime_1_3_target_is_held_before_stream_or_write() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    topology.p4.set_p4runtime_api_version("1.3.0");
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;

    let reply = client
        .assign_target(AssignTargetRequest {
            assignment: Some(assignment(0, topology.p4_server.address)),
        })
        .await?
        .into_inner();
    assert_eq!(ActorState::Hold as i32, reply.state);
    assert!(reply.reason_code.contains("CAPABILITY_DRIFT"));
    assert_eq!(0, topology.p4.stream_opens(1));
    assert_eq!(0, topology.p4.effect_write_attempts());

    write_evidence(
        topology.root.path(),
        "p4runtime-version-fence.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-P4RUNTIME-VERSION-FENCE-001",
            "requirement_ids": [
                "MOD-EDGE-001", "CONTRACT-P4-001", "TEST-003", "TEST-007"
            ],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "fault": "P4Runtime Capabilities advertised 1.3.0 while the assignment required exact 1.4.1",
            "reason": "expected=1.4.1 observed=1.3.0; rejected before StreamChannel",
            "reason_code": "CAPABILITY_DRIFT",
            "p4_stream_opens": topology.p4.stream_opens(1),
            "p4_effect_write_attempts": topology.p4.effect_write_attempts(),
            "fail_closed": true
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn lease_expiry_fences_writes_and_handoff_uses_higher_election() -> Result<(), Box<dyn Error>>
{
    let topology = Topology::new(1, 0, 0).await?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    let mut first = assignment(0, topology.p4_server.address);
    first.issued_at_unix_ms = unix_ms();
    first.expires_at_unix_ms = first.issued_at_unix_ms + 1_200;
    let reply = client
        .assign_target(AssignTargetRequest {
            assignment: Some(first.clone()),
        })
        .await?
        .into_inner();
    first
        .fence
        .as_mut()
        .ok_or("first assignment fence missing")?
        .actor_runtime_epoch = reply.actor_runtime_epoch;
    let expired = wait_for_target_state(&mut client, "target-0", ActorState::ReadOnly).await?;
    assert!(!expired.lease_valid);
    let writes_before = topology.p4.effect_write_attempts();
    let expired_intent = baseline_effect(&first, "expired-authority", 1);
    let expired_error = match client
        .preflight_effect(PreflightEffectRequest {
            intent: Some(expired_intent),
        })
        .await
    {
        Ok(_) => return Err("expired actor accepted a write preflight".into()),
        Err(error) => error,
    };
    assert_eq!(tonic::Code::FailedPrecondition, expired_error.code());
    assert!(
        expired_error.message().contains("LEASE_EXPIRED")
            || expired_error.message().contains("NOT_PRIMARY")
    );
    assert_eq!(writes_before, topology.p4.effect_write_attempts());

    let prior_ceiling = first.election_ceiling;
    let mut second = assignment(0, topology.p4_server.address);
    second.lease_id = "lease-target-0-generation-2".into();
    second.issued_at_unix_ms = unix_ms();
    second.expires_at_unix_ms = second.issued_at_unix_ms + 300_000;
    second.election_floor = prior_ceiling + 1;
    second.election_ceiling = second.election_floor + 9_999;
    let second_fence = second
        .fence
        .as_mut()
        .ok_or("second assignment fence missing")?;
    second_fence.target_assignment_generation = 2;
    second_fence.application_generation = 2;
    second_fence.election_id_low = second.election_floor + 1;
    let second_reply = client
        .assign_target(AssignTargetRequest {
            assignment: Some(second.clone()),
        })
        .await?
        .into_inner();
    assert_eq!(ActorState::Primary as i32, second_reply.state);
    assert!(second.election_floor > prior_ceiling);
    let deadline = Instant::now() + Duration::from_secs(5);
    while topology.p4.active_streams(1) != 1 {
        if Instant::now() >= deadline {
            return Err("handoff did not converge to one StreamChannel".into());
        }
        sleep(Duration::from_millis(20)).await;
    }
    assert_eq!(1, topology.p4.max_active_streams(1));
    write_evidence(
        topology.root.path(),
        "lease-expiry-handoff.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-LEASE-HANDOFF-001",
            "requirement_ids": [
                "MOD-EDGE-001", "ARCH-TARGET-FLEET-001", "REL-TARGET-FLEET-001",
                "CONTRACT-TARGET-001", "TEST-003", "TEST-007", "TEST-TARGET-FLEET-001"
            ],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "expired_actor_write_attempts": 0,
            "first_election_ceiling": prior_ceiling,
            "second_election_floor": second.election_floor,
            "maximum_active_streams": topology.p4.max_active_streams(1)
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn central_pool_outage_is_bounded_and_recovers_without_fallback() -> Result<(), Box<dyn Error>>
{
    let topology = Topology::new(1, u32::MAX, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.telemetry_poll_interval_ms = 2_000;
    })?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    let assigned = assignment(0, topology.p4_server.address);
    client
        .assign_target(AssignTargetRequest {
            assignment: Some(assigned.clone()),
        })
        .await?;
    client
        .prepare_route(PrepareRouteRequest {
            schema_version: "inference-route-prepare/v1".into(),
            shard_id: assigned.target_id.clone(),
            current_model_control_incarnation_id: String::new(),
            current_route_epoch: 0,
            proposed_route_epoch: 1,
            trace_id: "pool-outage-prepare".into(),
        })
        .await?;
    let exact_route = route(0, &topology.inference_server.address);
    client
        .commit_route(CommitRouteRequest {
            route: Some(exact_route.clone()),
            trace_id: "pool-outage-commit".into(),
        })
        .await?;
    client
        .resume_route(resume_request(&exact_route, "pool-outage-resume"))
        .await?;

    let deadline = Instant::now() + Duration::from_secs(10);
    while topology.inference.calls().len() < 3 {
        if Instant::now() >= deadline {
            return Err("central outage did not exercise the bounded attempt budget".into());
        }
        sleep(Duration::from_millis(25)).await;
    }
    let calls_after_budget = topology.inference.calls();
    assert_eq!(
        [1, 2, 3],
        [
            calls_after_budget[0].2,
            calls_after_budget[1].2,
            calls_after_budget[2].2,
        ]
    );
    assert!(topology.control.committed_batches().is_empty());
    sleep(Duration::from_millis(300)).await;
    assert_eq!(
        calls_after_budget.len(),
        topology.inference.calls().len(),
        "durable retry cycle must not spin at the actor maintenance rate"
    );

    topology.inference.set_failures(0);
    wait_for_commits(&topology.control, 1).await?;
    let recovered_calls = topology.inference.calls();
    assert!(recovered_calls.len() >= 4);
    assert_eq!(1, recovered_calls[3].2);
    assert_eq!(calls_after_budget[0].0, recovered_calls[3].0);
    assert_eq!(calls_after_budget[0].1, recovered_calls[3].1);
    write_evidence(
        topology.root.path(),
        "central-pool-outage.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-CENTRAL-OUTAGE-001",
            "requirement_ids": [
                "MOD-EDGE-001", "CONTRACT-INFERENCE-001", "TEST-003", "TEST-007",
                "TEST-TEL-INF-001"
            ],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "same_generation_attempt_budget": 3,
            "retry_cycle_cooldown_ms": 1000,
            "canonical_commits_during_outage": 0,
            "same_request_identity_after_recovery": true,
            "edge_local_fallback": false
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn source_wal_record_bound_fails_closed_without_overwrite() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.source_wal_records = 1;
        config.limits.telemetry_poll_interval_ms = 20;
    })?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    client
        .assign_target(AssignTargetRequest {
            assignment: Some(assignment(0, topology.p4_server.address)),
        })
        .await?;
    let deadline = Instant::now() + Duration::from_secs(10);
    let observed = loop {
        let status = client
            .get_status(GetStatusRequest {
                target_id: "target-0".into(),
                trace_id: "source-wal-bound".into(),
            })
            .await?
            .into_inner();
        let target = status
            .targets
            .into_iter()
            .next()
            .ok_or("target status missing")?;
        if target.reason_code.contains("WAL_FULL") {
            break target;
        }
        if Instant::now() >= deadline {
            return Err("source WAL bound did not fail closed".into());
        }
        sleep(Duration::from_millis(50)).await;
    };
    assert!(topology.control.committed_batches().is_empty());
    let source_dir = topology.data_dir.join("targets/target-0/source");
    let segment_count = fs::read_dir(&source_dir)?
        .filter_map(Result::ok)
        .filter(|entry| entry.file_name().to_string_lossy().ends_with(".wal"))
        .count();
    assert_eq!(1, segment_count);
    assert!(observed.source_wal_bytes <= 4_195_000);
    write_evidence(
        topology.root.path(),
        "source-wal-bound.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-WAL-BOUND-001",
            "requirement_ids": ["MOD-EDGE-001", "TEST-003", "TEST-007", "TEST-TEL-INF-001"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "source_wal_record_limit": 1,
            "observed_source_wal_bytes": observed.source_wal_bytes,
            "segment_count": segment_count,
            "unacknowledged_overwrite": false,
            "canonical_commits": 0,
            "reason_code": observed.reason_code
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn expired_wal_starts_real_actor_in_sticky_hold_without_external_advance()
-> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.wal_max_age_seconds = 1;
        config.limits.telemetry_poll_interval_ms = 5_000;
    })?;
    let input_dir = topology.data_dir.join("targets/target-0/input");
    let mut wal = DurableWal::open(
        &input_dir,
        WalKind::InferenceInput,
        WalLimits {
            max_bytes: 32_000_000,
            max_records: 20_000,
            segment_bytes: 4_195_000,
            max_record_bytes: 4_194_304,
            max_age_seconds: 1,
        },
    )?;
    wal.append(b"opaque-stale-input-retained-for-manual-recovery")?;
    let retained_records = wal.stats().records;
    let retained_bytes = wal.stats().bytes;
    drop(wal);
    sleep(Duration::from_millis(1_200)).await;

    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    let reply = client
        .assign_target(AssignTargetRequest {
            assignment: Some(assignment(0, topology.p4_server.address)),
        })
        .await?
        .into_inner();
    assert_eq!(ActorState::Hold as i32, reply.state);
    assert_eq!("WAL_RETENTION_EXPIRED", reply.reason_code);
    assert_eq!(0, topology.p4.stream_opens(1));
    assert!(topology.inference.calls().is_empty());
    assert!(topology.control.commit_attempts().is_empty());
    let reopened = DurableWal::open(
        &input_dir,
        WalKind::InferenceInput,
        WalLimits {
            max_bytes: 32_000_000,
            max_records: 20_000,
            segment_bytes: 4_195_000,
            max_record_bytes: 4_194_304,
            max_age_seconds: 1,
        },
    );
    assert!(
        reopened.is_err(),
        "live actor must retain the sole WAL writer lock"
    );
    write_evidence(
        topology.root.path(),
        "wal-retention-expiry.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-WAL-RETENTION-001",
            "requirement_ids": ["MOD-EDGE-001", "TEST-003", "TEST-007", "TEST-TEL-INF-001"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "configured_max_age_seconds": 1,
            "reason_code": "WAL_RETENTION_EXPIRED",
            "actor_state": "HOLD",
            "retained_records": retained_records,
            "retained_bytes": retained_bytes,
            "p4_stream_opens": 0,
            "inference_calls": 0,
            "control_commit_calls": 0,
            "automatic_delete_or_overwrite": false
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn frozen_profile_resource_ceilings_are_rejected() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    let base: EdgeConfig = serde_json::from_slice(&fs::read(&topology.config_path)?)?;
    assert_config_rejected(&base, "p4_read_response_entities", |config| {
        config.limits.p4_read_response_entities = 4_097;
    })?;
    assert_config_rejected(&base, "p4_read_response_entities", |config| {
        config.limits.p4_read_response_entities = 4_095;
    })?;
    assert_config_rejected(&base, "source_wal_bytes", |config| {
        config.limits.source_wal_bytes = 1_073_741_825;
    })?;
    assert_config_rejected(&base, "journal_bytes", |config| {
        config.limits.journal_bytes = 536_870_913;
    })?;
    assert_config_rejected(&base, "route_journal_bytes", |config| {
        config.limits.route_journal_bytes = 67_108_865;
    })?;
    assert_config_rejected(&base, "source_wal_records", |config| {
        config.limits.source_wal_records = 1_048_577;
    })?;
    assert_config_rejected(&base, "input_wal_records", |config| {
        config.limits.input_wal_records = 524_289;
    })?;
    assert_config_rejected(&base, "pending_digest_ack_queue", |config| {
        config.limits.pending_digest_ack_queue = 257;
    })?;
    assert_config_rejected(&base, "pending_input_identities", |config| {
        config.limits.pending_input_identities = 19_999;
    })?;
    assert_config_rejected(&base, "result_wal_records", |config| {
        config.limits.result_wal_records = 524_289;
    })?;
    assert_config_rejected(&base, "journal_records", |config| {
        config.limits.journal_records = 262_145;
    })?;
    assert_config_rejected(&base, "route_journal_records", |config| {
        config.limits.route_journal_records = 4_097;
    })?;
    assert_config_rejected(&base, "wal_max_age_seconds", |config| {
        config.limits.wal_max_age_seconds = 86_401;
    })?;
    write_evidence(
        topology.root.path(),
        "resource-profile-ceilings.json",
        &json!({
            "schema_version": "edge-resource-bound-evidence/v1",
            "test_id": "TEST-EDGE-RESOURCE-PROFILE-001",
            "requirement_ids": ["MOD-EDGE-001", "TEST-003", "TEST-007"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "validated_ceilings": [
                "p4_read_response_entities",
                "source_wal_bytes",
                "journal_bytes",
                "route_journal_bytes",
                "source_wal_records",
                "input_wal_records",
                "pending_digest_ack_queue",
                "pending_input_identities",
                "result_wal_records",
                "journal_records",
                "route_journal_records"
                ,"wal_max_age_seconds"
            ],
            "overall_module_complete": false
        }),
    )?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn p4_read_response_entity_amplification_fails_closed() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.telemetry_poll_interval_ms = 5_000;
    })?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    let mut assigned = assignment(0, topology.p4_server.address);
    let reply = client
        .assign_target(AssignTargetRequest {
            assignment: Some(assigned.clone()),
        })
        .await?
        .into_inner();
    assigned
        .fence
        .as_mut()
        .ok_or("read-amplification assignment fence missing")?
        .actor_runtime_epoch = reply.actor_runtime_epoch;
    sleep(Duration::from_millis(200)).await;
    topology.p4.amplify_next_read_response(4_097);
    let writes_before = topology.p4.effect_write_attempts();
    let error = match client
        .preflight_effect(PreflightEffectRequest {
            intent: Some(baseline_effect(&assigned, "read-amplification", 1)),
        })
        .await
    {
        Ok(_) => return Err("oversized P4 entity response was accepted".into()),
        Err(error) => error,
    };
    assert_eq!(tonic::Code::ResourceExhausted, error.code());
    assert!(
        error
            .message()
            .contains("P4_READ_RESPONSE_TOO_MANY_ENTITIES")
    );
    assert_eq!(writes_before, topology.p4.effect_write_attempts());
    write_evidence(
        topology.root.path(),
        "p4-read-entity-bound.json",
        &json!({
            "schema_version": "edge-resource-bound-evidence/v1",
            "test_id": "TEST-EDGE-P4-READ-BOUND-001",
            "requirement_ids": ["MOD-EDGE-001", "TEST-003", "TEST-007"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "configured_entity_limit": 4096,
            "injected_entities": 4097,
            "grpc_code": "RESOURCE_EXHAUSTED",
            "reason_code": "P4_READ_RESPONSE_TOO_MANY_ENTITIES",
            "p4_effect_write_attempts": 0,
            "overall_module_complete": false
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn inference_global_deadline_fences_attempts_and_recovers() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.inference_deadline_ms = 75;
        config.limits.telemetry_poll_interval_ms = 2_000;
    })?;
    topology.inference.delay_infer_calls(3, 250);
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    assign_and_activate_route(&topology, &mut client, 0, "inference-deadline").await?;

    let deadline = Instant::now() + Duration::from_secs(10);
    while topology.inference.calls().len() < 3 {
        if Instant::now() >= deadline {
            return Err("inference deadline did not exercise three durable retry cycles".into());
        }
        sleep(Duration::from_millis(10)).await;
    }
    let first_cycle = topology.inference.calls();
    assert_eq!(
        [1, 1, 1],
        [first_cycle[0].2, first_cycle[1].2, first_cycle[2].2],
        "a timed-out RPC must not start another attempt outside its global budget: {first_cycle:?}"
    );
    assert_eq!(first_cycle[0].0, first_cycle[1].0);
    assert_eq!(first_cycle[0].0, first_cycle[2].0);
    assert_eq!(first_cycle[0].1, first_cycle[1].1);
    assert_eq!(first_cycle[0].1, first_cycle[2].1);
    wait_for_commits(&topology.control, 1).await?;
    let recovered = topology.inference.calls();
    assert!(recovered.len() >= 4);
    assert_eq!(1, recovered[3].2);
    assert_eq!(first_cycle[0].0, recovered[3].0);
    assert_eq!(first_cycle[0].1, recovered[3].1);
    write_evidence(
        topology.root.path(),
        "inference-deadline-recovery.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-INFERENCE-DEADLINE-001",
            "requirement_ids": [
                "MOD-EDGE-001", "CONTRACT-INFERENCE-001", "TEST-007", "TEST-TEL-INF-001"
            ],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "deadline_ms": 75,
            "attempt_budget": 3,
            "timed_out_wal_retry_cycles": 3,
            "attempts_per_timed_out_cycle": 1,
            "deadline_scope": "global-across-attempts-and-backoffs",
            "retryable_grpc_codes": ["UNAVAILABLE", "RESOURCE_EXHAUSTED", "ABORTED"],
            "request_identity_stable": true,
            "batch_digest_stable": true,
            "recovered_without_fallback": true
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn duplicate_inference_result_holds_route_without_commit() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    topology.inference.duplicate_next_result();
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    assign_and_activate_route(&topology, &mut client, 0, "duplicate-result").await?;
    let status = wait_for_reason(&mut client, "target-0", "RESULT_IDENTITY_MISMATCH").await?;
    assert_eq!(ActorState::Primary as i32, status.actor_state);
    assert!(topology.control.committed_batches().is_empty());
    let calls = topology.inference.calls().len();
    sleep(Duration::from_millis(300)).await;
    assert_eq!(calls, topology.inference.calls().len());
    write_evidence(
        topology.root.path(),
        "duplicate-inference-result.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-INFERENCE-DUPLICATE-001",
            "requirement_ids": ["MOD-EDGE-001", "TEST-007", "TEST-TEL-INF-001"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "fault": "duplicate inference output record",
            "reason_code": "RESULT_IDENTITY_MISMATCH",
            "route_held": true,
            "canonical_commits": 0
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn late_or_drifted_result_fence_never_reaches_canonical_commit() -> Result<(), Box<dyn Error>>
{
    let topology = Topology::new(1, 0, 0).await?;
    topology.inference.drift_next_result_fence();
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    assign_and_activate_route(&topology, &mut client, 0, "result-fence-drift").await?;
    let status = wait_for_reason(&mut client, "target-0", "RESULT_IDENTITY_MISMATCH").await?;
    assert_eq!(ActorState::Primary as i32, status.actor_state);
    assert!(topology.control.committed_batches().is_empty());
    let calls = topology.inference.calls().len();
    sleep(Duration::from_millis(300)).await;
    assert_eq!(calls, topology.inference.calls().len());
    write_evidence(
        topology.root.path(),
        "inference-result-fence.json",
        &json!({
            "schema_version": "edge-module-fault-evidence/v1",
            "test_id": "TEST-EDGE-RESULT-FENCE-001",
            "requirement_ids": ["CONTRACT-MODEL-001", "CONTRACT-INFERENCE-001", "TEST-TEL-INF-001"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "faults": ["binding_digest_drift", "worker_attempt_identity_drift"],
            "reason_code": "RESULT_IDENTITY_MISMATCH",
            "route_held": true,
            "canonical_commits": 0,
            "no_fallback": true
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn invalid_inference_value_holds_route_without_commit() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    topology.inference.invalidate_next_result_value();
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    assign_and_activate_route(&topology, &mut client, 0, "invalid-result-value").await?;
    wait_for_reason(&mut client, "target-0", "RESULT_VALUE_INVALID").await?;
    assert!(topology.control.committed_batches().is_empty());
    let calls = topology.inference.calls().len();
    sleep(Duration::from_millis(300)).await;
    assert_eq!(calls, topology.inference.calls().len());
    write_evidence(
        topology.root.path(),
        "invalid-inference-value.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-INFERENCE-VALUE-001",
            "requirement_ids": ["MOD-EDGE-001", "TEST-007", "TEST-TEL-INF-001"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "fault": "non-finite central score with internally consistent digest",
            "reason_code": "RESULT_VALUE_INVALID",
            "route_held": true,
            "canonical_commits": 0
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn control_deadline_retries_idempotently_after_commit() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.control_deadline_ms = 75;
    })?;
    topology.control.delay_commit_calls(1, 250);
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    assign_and_activate_route(&topology, &mut client, 0, "control-deadline").await?;
    let deadline = Instant::now() + Duration::from_secs(10);
    let attempts = loop {
        let attempts = topology.control.commit_attempts();
        if attempts.len() >= 2 {
            break attempts;
        }
        if Instant::now() >= deadline {
            return Err("control deadline did not retry the durable result".into());
        }
        sleep(Duration::from_millis(10)).await;
    };
    assert_eq!(attempts[0], attempts[1]);
    assert_eq!(1, topology.control.committed_batches().len());
    write_evidence(
        topology.root.path(),
        "control-deadline-idempotency.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-CONTROL-DEADLINE-001",
            "requirement_ids": ["MOD-EDGE-001", "TEST-007", "TEST-TEL-INF-001"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "deadline_ms": 75,
            "result_batch_digest_stable": true,
            "canonical_commit_count": 1,
            "retry_ack_status": "idempotent"
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn duplicate_canonical_ack_is_rejected_then_idempotently_recovers()
-> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    topology.control.duplicate_next_ack();
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    assign_and_activate_route(&topology, &mut client, 0, "duplicate-ack").await?;
    wait_for_reason(&mut client, "target-0", "CANONICAL_ACK_MISMATCH").await?;
    let deadline = Instant::now() + Duration::from_secs(10);
    let attempts = loop {
        let attempts = topology.control.commit_attempts();
        if attempts.len() >= 2 {
            break attempts;
        }
        if Instant::now() >= deadline {
            return Err("duplicate canonical ACK did not retain and retry the result WAL".into());
        }
        sleep(Duration::from_millis(20)).await;
    };
    assert_eq!(attempts[0], attempts[1]);
    assert_eq!(1, topology.control.committed_batches().len());
    write_evidence(
        topology.root.path(),
        "duplicate-canonical-ack.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-CANONICAL-ACK-DUPLICATE-001",
            "requirement_ids": ["MOD-EDGE-001", "ARCH-003", "TEST-007", "TEST-TEL-INF-001"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "malformed_ack_rejected": true,
            "durable_result_digest_stable": true,
            "canonical_commit_count": 1,
            "recovery_ack": "idempotent"
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn p4_pipeline_read_deadline_fails_before_write_and_recovers() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.p4_rpc_deadline_ms = 50;
        config.limits.telemetry_poll_interval_ms = 5_000;
    })?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    let mut assigned = assignment(0, topology.p4_server.address);
    let reply = client
        .assign_target(AssignTargetRequest {
            assignment: Some(assigned.clone()),
        })
        .await?
        .into_inner();
    assigned
        .fence
        .as_mut()
        .ok_or("P4 deadline assignment fence missing")?
        .actor_runtime_epoch = reply.actor_runtime_epoch;
    topology.p4.delay_pipeline_reads(1, 250);
    let intent = baseline_effect(&assigned, "p4-read-deadline", 1);
    let error = match client
        .preflight_effect(PreflightEffectRequest {
            intent: Some(intent.clone()),
        })
        .await
    {
        Ok(_) => return Err("timed-out P4 pipeline readback was accepted".into()),
        Err(error) => error,
    };
    assert_eq!(tonic::Code::Unavailable, error.code());
    assert!(error.message().contains("PIPELINE_READ_FAILED"));
    assert_eq!(0, topology.p4.effect_write_attempts());
    let recovered = client
        .preflight_effect(PreflightEffectRequest {
            intent: Some(intent),
        })
        .await?
        .into_inner();
    assert_eq!("accepted", recovered.result);
    write_evidence(
        topology.root.path(),
        "p4-read-deadline.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-P4-DEADLINE-001",
            "requirement_ids": ["MOD-EDGE-001", "TEST-007"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "p4_rpc_deadline_ms": 50,
            "reason_code": "PIPELINE_READ_FAILED",
            "writes_before_exact_readback": 0,
            "subsequent_preflight_recovered": true
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn out_of_order_results_and_acks_are_accepted_by_identity() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, u32::MAX, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.telemetry_poll_interval_ms = 50;
    })?;
    topology.inference.reverse_next_result();
    topology.control.reverse_next_ack();
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    assign_and_activate_route(&topology, &mut client, 0, "out-of-order").await?;
    let deadline = Instant::now() + Duration::from_secs(10);
    while topology.inference.calls().len() < 3 {
        if Instant::now() >= deadline {
            return Err("out-of-order setup did not reach the bounded outage budget".into());
        }
        sleep(Duration::from_millis(20)).await;
    }
    sleep(Duration::from_millis(500)).await;
    topology.inference.set_failures(0);
    wait_for_commits(&topology.control, 1).await?;
    let committed = topology.control.committed_batches();
    let first = committed
        .first()
        .ok_or("out-of-order result batch missing")?;
    assert!(first.records.len() > 1);
    assert_eq!(1, topology.inference.result_reorders_applied());
    assert_eq!(1, topology.control.ack_reorders_applied());
    write_evidence(
        topology.root.path(),
        "out-of-order-result-and-ack.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-OUT-OF-ORDER-001",
            "requirement_ids": ["MOD-EDGE-001", "TEST-007", "TEST-TEL-INF-001"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "records": first.records.len(),
            "result_order_reversed": true,
            "ack_order_reversed_relative_to_result": true,
            "identity_mapped_not_position_mapped": true
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn actor_command_queue_saturation_rejects_admission() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.actor_high_queue = 1;
        config.limits.telemetry_poll_interval_ms = 5_000;
    })?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    let mut assigned = assignment(0, topology.p4_server.address);
    let reply = client
        .assign_target(AssignTargetRequest {
            assignment: Some(assigned.clone()),
        })
        .await?
        .into_inner();
    assigned
        .fence
        .as_mut()
        .ok_or("queue saturation assignment fence missing")?
        .actor_runtime_epoch = reply.actor_runtime_epoch;
    let reads_before = topology.p4.pipeline_read_attempts();
    topology.p4.delay_pipeline_reads(1, 750);
    let mut blocker_client = client.clone();
    let blocker_intent = baseline_effect(&assigned, "queue-blocker", 1);
    let blocker = tokio::spawn(async move {
        blocker_client
            .preflight_effect(PreflightEffectRequest {
                intent: Some(blocker_intent),
            })
            .await
    });
    let deadline = Instant::now() + Duration::from_secs(5);
    while topology.p4.pipeline_read_attempts() <= reads_before {
        if Instant::now() >= deadline {
            return Err("blocking P4 read did not enter the actor".into());
        }
        sleep(Duration::from_millis(5)).await;
    }
    let mut requests = JoinSet::new();
    for index in 0..16 {
        let mut burst_client = client.clone();
        let intent = baseline_effect(&assigned, &format!("queue-burst-{index}"), 1);
        requests.spawn(async move {
            burst_client
                .preflight_effect(PreflightEffectRequest {
                    intent: Some(intent),
                })
                .await
        });
    }
    let mut queue_full = 0_u64;
    while let Some(result) = requests.join_next().await {
        match result? {
            Ok(_) => {}
            Err(error)
                if error.code() == tonic::Code::ResourceExhausted
                    && error.message().contains("ACTOR_QUEUE_FULL") =>
            {
                queue_full = queue_full.saturating_add(1);
            }
            Err(error) => return Err(format!("unexpected queue burst error: {error}").into()),
        }
    }
    blocker.await??;
    assert!(queue_full > 0);
    write_evidence(
        topology.root.path(),
        "actor-queue-saturation.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-ACTOR-QUEUE-001",
            "requirement_ids": ["MOD-EDGE-001", "TEST-007"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "configured_queue_depth": 1,
            "burst_requests": 16,
            "resource_exhausted_responses": queue_full,
            "reason_code": "ACTOR_QUEUE_FULL",
            "unbounded_wait": false
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn supplemental_hint_burst_persists_explicit_gap() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.actor_high_queue = 4;
        config.limits.p4_stream_response_queue = 1;
        config.limits.telemetry_poll_interval_ms = 5_000;
    })?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    client
        .assign_target(AssignTargetRequest {
            assignment: Some(assignment(0, topology.p4_server.address)),
        })
        .await?;
    for sequence in 0..1_024_u32 {
        if !topology.p4.inject_packet(
            1,
            PacketIn {
                payload: sequence.to_be_bytes().to_vec(),
                metadata: Vec::new(),
            },
        ) {
            return Err("P4 hint burst had no active subscriber".into());
        }
    }
    wait_for_reason(&mut client, "target-0", "SUPPLEMENTAL_HINT_DROPPED").await?;
    terminate(&mut child).await?;
    let source_dir = topology.data_dir.join("targets/target-0/source");
    let wal = DurableWal::open(
        &source_dir,
        WalKind::Source,
        WalLimits {
            max_bytes: 32_000_000,
            max_records: 20_000,
            segment_bytes: 4_195_000,
            max_record_bytes: 4_194_304,
            max_age_seconds: 86_400,
        },
    )?;
    let gaps = wal
        .replay(20_000, 32_000_000)?
        .into_iter()
        .map(|item| SourceWalRecord::decode(item.payload.as_slice()))
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        .filter(|record| {
            record.stage == SourceWalStage::SourceGapDurable as i32
                && record
                    .reason_code
                    .starts_with("SUPPLEMENTAL_HINT_DROPPED:packet-in")
        })
        .count();
    assert!(gaps > 0);
    write_evidence(
        topology.root.path(),
        "supplemental-hint-burst-gap.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-HINT-QUEUE-001",
            "requirement_ids": [
                "MOD-EDGE-001", "ARCH-TELEMETRY-001", "TEST-007", "TEST-TEL-INF-001"
            ],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "configured_hint_queue_depth": 1,
            "injected_packet_hints": 1024,
            "durable_gap_records": gaps,
            "silent_hint_loss": false
        }),
    )?;
    drop(wal);
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn p4_reconnect_storm_is_backed_off_and_target_isolated() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(2, 0, 0).await?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    for target_index in 0..2 {
        client
            .assign_target(AssignTargetRequest {
                assignment: Some(assignment(target_index, topology.p4_server.address)),
            })
            .await?;
    }
    let unaffected_opens = topology.p4.stream_opens(2);
    let mut reconnect_ms = Vec::new();
    for expected_opens in 2..=4_u64 {
        let started = Instant::now();
        topology.p4.close_stream(1);
        let deadline = Instant::now() + Duration::from_secs(5);
        while topology.p4.stream_opens(1) < expected_opens || topology.p4.active_streams(1) != 1 {
            if Instant::now() >= deadline {
                return Err(
                    format!("reconnect storm iteration {expected_opens} did not recover").into(),
                );
            }
            sleep(Duration::from_millis(20)).await;
        }
        let elapsed = started.elapsed().as_millis() as u64;
        assert!(
            elapsed >= 900,
            "reconnect occurred without the one-second backoff: {elapsed}ms"
        );
        reconnect_ms.push(elapsed);
    }
    let recovered = wait_for_target_state(&mut client, "target-0", ActorState::Primary).await?;
    assert!(recovered.p4_connected && recovered.primary);
    assert_eq!(unaffected_opens, topology.p4.stream_opens(2));
    assert_eq!(1, topology.p4.max_active_streams(1));
    assert_eq!(1, topology.p4.max_active_streams(2));
    write_evidence(
        topology.root.path(),
        "p4-reconnect-storm.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-P4-RECONNECT-STORM-001",
            "requirement_ids": [
                "MOD-EDGE-001", "ARCH-TARGET-FLEET-001", "TEST-007", "TEST-TARGET-FLEET-001"
            ],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "forced_disconnects": 3,
            "reconnect_elapsed_ms": reconnect_ms,
            "minimum_backoff_ms": 900,
            "maximum_active_streams_per_device": 1,
            "unaffected_target_stream_opens": unaffected_opens
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn inference_message_limit_fails_before_central_call() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.inference_message_bytes = 2_048;
        config.limits.telemetry_poll_interval_ms = 2_000;
    })?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    assign_and_activate_route(&topology, &mut client, 0, "inference-oversize").await?;
    wait_for_reason(&mut client, "target-0", "INFERENCE_MESSAGE_TOO_LARGE").await?;
    assert!(topology.inference.calls().is_empty());
    assert!(topology.control.committed_batches().is_empty());
    write_evidence(
        topology.root.path(),
        "inference-message-limit.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-INFERENCE-MESSAGE-LIMIT-001",
            "requirement_ids": ["MOD-EDGE-001", "TEST-007", "TEST-TEL-INF-001"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "configured_bytes": 2048,
            "reason_code": "INFERENCE_MESSAGE_TOO_LARGE",
            "central_calls": 0,
            "canonical_commits": 0
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn control_message_limit_retains_result_without_publish() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.control_message_bytes = 2_048;
        config.limits.telemetry_poll_interval_ms = 2_000;
    })?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    assign_and_activate_route(&topology, &mut client, 0, "control-oversize").await?;
    let status = wait_for_reason(&mut client, "target-0", "CONTROL_MESSAGE_TOO_LARGE").await?;
    assert!(status.result_wal_bytes > 0);
    assert!(topology.control.commit_attempts().is_empty());
    assert!(topology.control.committed_batches().is_empty());
    write_evidence(
        topology.root.path(),
        "control-message-limit.json",
        &json!({
            "schema_version": "edge-fault-evidence/v1",
            "test_id": "TEST-EDGE-CONTROL-MESSAGE-LIMIT-001",
            "requirement_ids": ["MOD-EDGE-001", "TEST-007", "TEST-TEL-INF-001"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "configured_bytes": 2048,
            "reason_code": "CONTROL_MESSAGE_TOO_LARGE",
            "result_wal_bytes": status.result_wal_bytes,
            "control_calls": 0,
            "canonical_commits": 0
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "requires root/CAP_SYS_ADMIN for an isolated tmpfs mount"]
async fn os_enospc_fails_closed_without_process_loss() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.telemetry_poll_interval_ms = 250;
    })?;
    let mount = TmpfsMount::mount(&topology.data_dir, 8 * 1024 * 1024)?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    let reply = client
        .assign_target(AssignTargetRequest {
            assignment: Some(assignment(0, topology.p4_server.address)),
        })
        .await?
        .into_inner();
    assert_eq!(ActorState::Primary as i32, reply.state);
    sleep(Duration::from_millis(300)).await;
    let (fill_bytes, errno) = fill_until_enospc(&topology.data_dir)?;
    let observed = wait_for_reason(&mut client, "target-0", "LOCAL_IO").await?;
    assert!(child.try_wait()?.is_none());
    assert!(observed.p4_connected && observed.primary);
    assert_eq!(28, errno);
    terminate(&mut child).await?;
    mount.unmount()?;
    write_evidence(
        topology.root.path(),
        "os-enospc.json",
        &json!({
            "schema_version": "edge-os-fault-evidence/v1",
            "test_id": "TEST-EDGE-OS-ENOSPC-001",
            "requirement_ids": ["MOD-EDGE-001", "TEST-007", "TEST-TEL-INF-001"],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "fault": "isolated tmpfs exhausted after target activation",
            "tmpfs_bytes": 8 * 1024 * 1024,
            "filler_bytes": fill_bytes,
            "errno": errno,
            "reason_code": "LOCAL_IO",
            "process_live": true,
            "unacknowledged_overwrite": false
        }),
    )?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "requires Linux prlimit and /proc"]
async fn os_fd_exhaustion_rejects_new_target_without_overcommit() -> Result<(), Box<dyn Error>> {
    const NOFILE_LIMIT: u64 = 128;
    let topology = Topology::new(32, 0, 0).await?;
    topology.rewrite_config(|config| {
        config.limits.telemetry_poll_interval_ms = 5_000;
    })?;
    let mut child = topology.spawn_edge_with_nofile(NOFILE_LIMIT)?;
    let process_id = child.id().ok_or("prlimit Edge process ID missing")?;
    let mut client = topology.client().await?;
    let mut admitted = 0_u64;
    let rejection = loop {
        let target_index = admitted as usize;
        if target_index >= 32 {
            return Err("RLIMIT_NOFILE did not exhaust before the target profile limit".into());
        }
        match client
            .assign_target(AssignTargetRequest {
                assignment: Some(assignment(target_index, topology.p4_server.address)),
            })
            .await
        {
            Ok(_) => admitted = admitted.saturating_add(1),
            Err(error) => break error,
        }
    };
    assert!(admitted > 0);
    assert_eq!(tonic::Code::Unavailable, rejection.code());
    assert!(rejection.message().contains("LOCAL_IO"));
    assert!(child.try_wait()?.is_none());
    let limits = fs::read_to_string(format!("/proc/{process_id}/limits"))?;
    let open_files_limit = limits
        .lines()
        .find(|line| line.starts_with("Max open files"))
        .ok_or("Max open files line missing from /proc")?
        .to_owned();
    let observed_fds = fs::read_dir(format!("/proc/{process_id}/fd"))?.count();
    write_evidence(
        topology.root.path(),
        "os-fd-exhaustion.json",
        &json!({
            "schema_version": "edge-os-fault-evidence/v1",
            "test_id": "TEST-EDGE-OS-FD-EXHAUSTION-001",
            "requirement_ids": [
                "MOD-EDGE-001", "REL-TARGET-FLEET-001", "TEST-007", "TEST-TARGET-FLEET-001"
            ],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "QUALIFIED",
            "fault": "process-level RLIMIT_NOFILE",
            "configured_nofile": NOFILE_LIMIT,
            "proc_limit": open_files_limit,
            "observed_open_fds": observed_fds,
            "admitted_targets_before_exhaustion": admitted,
            "grpc_code": "UNAVAILABLE",
            "reason_code": "LOCAL_IO",
            "process_live": true,
            "target_profile_overcommitted": false
        }),
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn bounded_soak_rehearsal_or_exact_formal_duration() -> Result<(), Box<dyn Error>> {
    let formal = std::env::var("MASI_EDGE_FORMAL_SOAK").is_ok_and(|value| value == "1");
    let profile_path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../contracts/profiles/v1/qualification-soak-3600s.json");
    let schema_path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../contracts/evidence/soak/v1/schema.json");
    let edge_profile_path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../contracts/profiles/v1/rust-edge-agent.json");
    let profile_bytes = fs::read(&profile_path)?;
    let schema_bytes = fs::read(&schema_path)?;
    let profile: serde_json::Value = serde_json::from_slice(&profile_bytes)?;
    let edge_profile: serde_json::Value = serde_json::from_slice(&fs::read(edge_profile_path)?)?;
    let absolute_threshold_status = edge_profile["performance_gate"]["absolute_threshold_status"]
        .as_str()
        .ok_or("Edge absolute threshold status is missing")?;
    let process_resource_threshold_status =
        edge_profile["performance_gate"]["process_resource_threshold_status"]
            .as_str()
            .ok_or("Edge process resource threshold status is missing")?;
    let thresholds_frozen = absolute_threshold_status == "FROZEN";
    let phase_names = ["steady", "peak", "saturation", "recovery-or-activation"];
    let profile_phase_names = profile["phases"]
        .as_array()
        .ok_or("soak profile phases are missing")?
        .iter()
        .filter_map(|phase| phase["name"].as_str())
        .collect::<Vec<_>>();
    if profile["warmup_seconds"].as_u64() != Some(60)
        || profile["qualified_duration_seconds"].as_u64() != Some(3_600)
        || profile["sample_interval_seconds"].as_u64() != Some(10)
        || profile_phase_names != phase_names
        || profile["phases"].as_array().is_none_or(|phases| {
            phases
                .iter()
                .any(|phase| phase["duration_seconds"].as_u64() != Some(900))
        })
    {
        return Err("qualification soak profile no longer matches the frozen v1 schedule".into());
    }
    let warmup = if formal {
        Duration::from_secs(60)
    } else {
        Duration::ZERO
    };
    let phase_duration = if formal {
        Duration::from_secs(900)
    } else {
        Duration::from_secs(1)
    };
    let sample_interval = if formal {
        Duration::from_secs(10)
    } else {
        Duration::from_millis(250)
    };
    let topology = Topology::new(1, 0, 0).await?;
    let soak_config: EdgeConfig = serde_json::from_slice(&fs::read(&topology.config_path)?)?;
    let observed_queue_limit = soak_config
        .limits
        .actor_high_queue
        .saturating_add(soak_config.limits.pending_digest_ack_queue)
        .saturating_add(soak_config.limits.observation_batches)
        as u64;
    let resource_limits = json!({
        "aggregate_target_queue_depth": observed_queue_limit,
        "source_wal_bytes": soak_config.limits.source_wal_bytes,
        "input_wal_bytes": soak_config.limits.input_wal_bytes,
        "result_wal_bytes": soak_config.limits.result_wal_bytes,
        "rss_bytes": null,
        "fd_count": null,
        "thread_count": null,
        "process_threshold_status": process_resource_threshold_status
    });
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    let mut assigned = assign_and_activate_route(&topology, &mut client, 0, "soak").await?;
    let started_at = utc_timestamp()?;
    let monotonic_start_ns = monotonic_ns()?;
    let run_started = Instant::now();
    let mut cpu_state = ProcessCpuState::default();
    let mut samples = Vec::new();
    let mut trace_sequence = 0_u64;
    let mut successful_status_samples = 0_u64;
    let mut error_count = 0_u64;
    let mut oracle_mismatches = 0_u64;
    let mut process_crashed = false;
    let mut interruption = "NONE";
    let mut lease_renewals = Vec::new();
    let mut next_lease_renewal = Instant::now() + Duration::from_secs(120);
    let mut last_observed_wall_ms = unix_ms();

    let warmup_started = Instant::now();
    let warmup_deadline = warmup_started + warmup;
    while Instant::now() < warmup_deadline {
        if child.try_wait()?.is_some() {
            process_crashed = true;
            interruption = "CRASH";
            error_count = error_count.saturating_add(1);
            break;
        }
        trace_sequence = trace_sequence.saturating_add(1);
        match renew_soak_lease_if_needed(
            &mut client,
            &mut assigned,
            trace_sequence,
            &run_started,
            &mut next_lease_renewal,
            &mut last_observed_wall_ms,
        )
        .await
        {
            Ok(Some(renewal)) => lease_renewals.push(renewal),
            Ok(None) => {}
            Err(_) => {
                error_count = error_count.saturating_add(1);
                interruption = "EVIDENCE_FAILURE";
                break;
            }
        }
        match collect_soak_sample(
            &mut client,
            &child,
            &mut cpu_state,
            "warmup",
            run_started.elapsed().as_millis() as u64,
            trace_sequence,
            topology.control.committed_batches().len(),
        )
        .await
        {
            Ok(sample) => {
                successful_status_samples = successful_status_samples.saturating_add(1);
                samples.push(sample);
            }
            Err(_) => {
                error_count = error_count.saturating_add(1);
                samples.push(json!({
                    "offset_ms": run_started.elapsed().as_millis() as u64,
                    "phase": "warmup", "quality": "gap", "cpu_pct": 0.0,
                    "rss_bytes": 0, "fd_count": 0, "thread_count": 0,
                    "queue_depth": 0, "oom_events": 0, "container_restarts": 0,
                    "oracle_errors": 1, "gap_count": 1,
                    "module_metrics": {"reason": "resource-or-status-sample-failed"}
                }));
            }
        }
        sleep(
            warmup_deadline
                .saturating_duration_since(Instant::now())
                .min(sample_interval),
        )
        .await;
    }
    let warmup_actual_ms = warmup_started.elapsed().as_millis() as u64;
    let commits_before = topology.control.committed_batches().len();
    let measurement_started = Instant::now();
    let mut phases = Vec::new();
    let mut total_status_attempts = 0_u64;
    let mut total_status_successes = 0_u64;
    let mut saturation_accepted = 0_u64;
    let mut saturation_rejected = 0_u64;
    let mut recovery_elapsed_ms = 0_u64;

    for phase_name in phase_names {
        if process_crashed {
            phases.push(json!({
                "name": phase_name, "planned_ms": 900000, "elapsed_ms": 0,
                "result": "NOT_RUN", "requested_rate_pps": 0.0,
                "achieved_rate_pps": 0.0, "errors": 1,
                "module_metrics": {"reason": "edge-process-crashed"}
            }));
            continue;
        }
        let phase_started = Instant::now();
        let phase_start_offset_ms = run_started.elapsed().as_millis() as u64;
        let phase_deadline = phase_started + phase_duration;
        let mut phase_errors = 0_u64;
        let mut phase_attempts = 0_u64;
        let mut phase_successes = 0_u64;
        trace_sequence = trace_sequence.saturating_add(1);
        let lease_ready = match renew_soak_lease_if_needed(
            &mut client,
            &mut assigned,
            trace_sequence,
            &run_started,
            &mut next_lease_renewal,
            &mut last_observed_wall_ms,
        )
        .await
        {
            Ok(Some(renewal)) => {
                lease_renewals.push(renewal);
                true
            }
            Ok(None) => true,
            Err(_) => {
                phase_errors = phase_errors.saturating_add(1);
                interruption = "EVIDENCE_FAILURE";
                false
            }
        };

        if lease_ready && phase_name == "saturation" {
            let (accepted, rejected, errors) =
                exercise_actor_saturation(&topology, &client, &assigned).await;
            saturation_accepted = accepted;
            saturation_rejected = rejected;
            phase_errors = phase_errors.saturating_add(errors);
        } else if lease_ready && phase_name == "recovery-or-activation" {
            let opens_before = topology.p4.stream_opens(1);
            let recovery_started = Instant::now();
            topology.p4.close_stream(1);
            let recovery_deadline = Instant::now() + Duration::from_secs(10);
            let mut recovered = false;
            while Instant::now() < recovery_deadline {
                if let Ok(response) = client
                    .get_status(GetStatusRequest {
                        target_id: "target-0".into(),
                        trace_id: "soak-planned-stream-recovery".into(),
                    })
                    .await
                {
                    let status = response.into_inner();
                    if topology.p4.stream_opens(1) > opens_before
                        && status
                            .targets
                            .first()
                            .is_some_and(|target| target.p4_connected && target.primary)
                    {
                        recovered = true;
                        break;
                    }
                }
                sleep(Duration::from_millis(50)).await;
            }
            recovery_elapsed_ms = recovery_started.elapsed().as_millis() as u64;
            if !recovered {
                phase_errors = phase_errors.saturating_add(1);
            }
        }

        while lease_ready && Instant::now() < phase_deadline {
            if child.try_wait()?.is_some() {
                process_crashed = true;
                interruption = "CRASH";
                phase_errors = phase_errors.saturating_add(1);
                break;
            }
            let burst_size = match phase_name {
                "steady" => 1,
                "peak" => 16,
                "saturation" => 32,
                "recovery-or-activation" => 4,
                _ => 1,
            };
            trace_sequence = trace_sequence.saturating_add(1);
            match renew_soak_lease_if_needed(
                &mut client,
                &mut assigned,
                trace_sequence,
                &run_started,
                &mut next_lease_renewal,
                &mut last_observed_wall_ms,
            )
            .await
            {
                Ok(Some(renewal)) => lease_renewals.push(renewal),
                Ok(None) => {}
                Err(_) => {
                    phase_errors = phase_errors.saturating_add(1);
                    interruption = "EVIDENCE_FAILURE";
                    break;
                }
            }
            let (successful, errors) =
                status_rpc_burst(&client, burst_size, phase_name, trace_sequence).await;
            phase_attempts = phase_attempts.saturating_add(burst_size as u64);
            phase_successes = phase_successes.saturating_add(successful);
            phase_errors = phase_errors.saturating_add(errors);
            match collect_soak_sample(
                &mut client,
                &child,
                &mut cpu_state,
                phase_name,
                run_started.elapsed().as_millis() as u64,
                trace_sequence,
                topology.control.committed_batches().len(),
            )
            .await
            {
                Ok(sample) => {
                    successful_status_samples = successful_status_samples.saturating_add(1);
                    samples.push(sample);
                }
                Err(_) => {
                    phase_errors = phase_errors.saturating_add(1);
                    samples.push(json!({
                        "offset_ms": run_started.elapsed().as_millis() as u64,
                        "phase": phase_name, "quality": "gap", "cpu_pct": 0.0,
                        "rss_bytes": 0, "fd_count": 0, "thread_count": 0,
                        "queue_depth": 0, "oom_events": 0, "container_restarts": 0,
                        "oracle_errors": 1, "gap_count": 1,
                        "module_metrics": {"reason": "resource-or-status-sample-failed"}
                    }));
                }
            }
            if Instant::now() < phase_deadline {
                sleep(
                    phase_deadline
                        .saturating_duration_since(Instant::now())
                        .min(sample_interval),
                )
                .await;
            }
        }
        let phase_elapsed_ms = phase_started.elapsed().as_millis() as u64;
        let phase_end_offset_ms = run_started.elapsed().as_millis() as u64;
        let planned_duration_met = phase_elapsed_ms >= 900_000;
        if formal && !planned_duration_met {
            phase_errors = phase_errors.saturating_add(1);
        }
        error_count = error_count.saturating_add(phase_errors);
        total_status_attempts = total_status_attempts.saturating_add(phase_attempts);
        total_status_successes = total_status_successes.saturating_add(phase_successes);
        phases.push(json!({
            "name": phase_name,
            "planned_ms": 900000,
            "elapsed_ms": phase_elapsed_ms,
            "result": if phase_errors > 0 {
                "FAIL"
            } else if planned_duration_met {
                "PASS"
            } else {
                "HOLD"
            },
            "requested_rate_pps": 0.0,
            "achieved_rate_pps": 0.0,
            "errors": phase_errors,
            "module_metrics": {
                "status_rpc_attempts": phase_attempts,
                "status_rpc_successes": phase_successes,
                "planned_duration_met": planned_duration_met,
                "absolute_threshold_status": absolute_threshold_status,
                "phase_start_offset_ms": phase_start_offset_ms,
                "phase_end_offset_ms": phase_end_offset_ms
            }
        }));
    }
    let qualified_elapsed_ms = measurement_started.elapsed().as_millis() as u64;
    let commits_after = topology.control.committed_batches().len();
    if samples.is_empty() {
        samples.push(json!({
            "offset_ms": run_started.elapsed().as_millis() as u64,
            "phase": "warmup", "quality": "gap", "cpu_pct": 0.0,
            "rss_bytes": 0, "fd_count": 0, "thread_count": 0,
            "queue_depth": 0, "oom_events": 0, "container_restarts": 0,
            "oracle_errors": 1, "gap_count": 1,
            "module_metrics": {"reason": "no-soak-samples"}
        }));
    }
    if commits_after <= commits_before || successful_status_samples == 0 {
        oracle_mismatches = oracle_mismatches.saturating_add(1);
    }
    if topology.p4.max_active_streams(1) != 1
        || saturation_rejected == 0
        || topology.p4.stream_opens(1) < 2
    {
        oracle_mismatches = oracle_mismatches.saturating_add(1);
    }
    let max_rss_bytes = samples
        .iter()
        .filter_map(|sample| sample["rss_bytes"].as_u64())
        .max()
        .unwrap_or(0);
    let max_fd_count = samples
        .iter()
        .filter_map(|sample| sample["fd_count"].as_u64())
        .max()
        .unwrap_or(0);
    let max_thread_count = samples
        .iter()
        .filter_map(|sample| sample["thread_count"].as_u64())
        .max()
        .unwrap_or(0);
    let max_queue_depth = samples
        .iter()
        .filter_map(|sample| sample["queue_depth"].as_u64())
        .max()
        .unwrap_or(0);
    let max_source_wal_bytes = samples
        .iter()
        .filter_map(|sample| sample["module_metrics"]["source_wal_bytes"].as_u64())
        .max()
        .unwrap_or(0);
    let max_input_wal_bytes = samples
        .iter()
        .filter_map(|sample| sample["module_metrics"]["input_wal_bytes"].as_u64())
        .max()
        .unwrap_or(0);
    let max_result_wal_bytes = samples
        .iter()
        .filter_map(|sample| sample["module_metrics"]["result_wal_bytes"].as_u64())
        .max()
        .unwrap_or(0);
    let resource_limit_violations = u64::from(max_queue_depth > observed_queue_limit)
        .saturating_add(u64::from(
            max_source_wal_bytes > soak_config.limits.source_wal_bytes,
        ))
        .saturating_add(u64::from(
            max_input_wal_bytes > soak_config.limits.input_wal_bytes,
        ))
        .saturating_add(u64::from(
            max_result_wal_bytes > soak_config.limits.result_wal_bytes,
        ));
    let unclassified_gap_count = samples
        .iter()
        .filter(|sample| sample["quality"] == "gap")
        .count() as u64;
    let formal_schedule_valid = !formal
        || (warmup_actual_ms >= 60_000
            && qualified_elapsed_ms >= 3_600_000
            && phases.iter().all(|phase| {
                phase["elapsed_ms"]
                    .as_u64()
                    .is_some_and(|value| value >= 900_000)
                    && phase["result"] == "PASS"
                    && phase["errors"].as_u64() == Some(0)
            }));
    if !formal_schedule_valid {
        error_count = error_count.saturating_add(1);
        if interruption == "NONE" {
            interruption = "EVIDENCE_FAILURE";
        }
    }
    let (edge_exit_code, edge_process_reaped) = match graceful_terminate(&mut child).await {
        Ok(exit_code) => (exit_code, true),
        Err(_) => {
            error_count = error_count.saturating_add(1);
            interruption = "EVIDENCE_FAILURE";
            let _kill_result = child.start_kill();
            let reaped = tokio::time::timeout(Duration::from_secs(10), child.wait())
                .await
                .is_ok_and(|result| result.is_ok());
            (1, reaped)
        }
    };
    if edge_exit_code != 0 {
        error_count = error_count.saturating_add(1);
    }
    let monotonic_end_ns = monotonic_ns()?;
    let finished_at = utc_timestamp()?;
    if monotonic_end_ns <= monotonic_start_ns {
        error_count = error_count.saturating_add(1);
        interruption = "EVIDENCE_FAILURE";
    }
    let failed = error_count > 0
        || oracle_mismatches > 0
        || unclassified_gap_count > 0
        || resource_limit_violations > 0
        || edge_exit_code != 0;
    let result = if failed {
        "FAIL"
    } else if formal && thresholds_frozen {
        "PASS"
    } else {
        "HOLD"
    };
    let qualification = if result == "PASS" {
        "QUALIFIED"
    } else {
        "NOT_QUALIFIED"
    };
    let profile_digest = digest::sha256(&profile_bytes);
    let schema_digest = digest::sha256(&schema_bytes);
    let claim_scope = json!({
        "module": "rust-edge-agent",
        "rule_counts": [0, 128, 1024, 4096],
        "scope": "independent-module-soak",
        "target_counts": [json!(0), json!(1), json!(2), json!("N")],
        "threshold_status": absolute_threshold_status,
        "resource_limits": resource_limits
    });
    let claim_scope_bytes = serde_json::to_vec(&claim_scope)?;
    let lease_renewal_count = lease_renewals.len() as u64;
    let evidence = json!({
        "schema_version": "qualification-soak/v1",
        "run_id": format!("edge-soak-{}", unix_ms()),
        "module": "rust-edge-agent",
        "requirement_ids": ["MOD-EDGE-001", "TEST-003", "TEST-008", "TEST-TEL-INF-001"],
        "level": if formal { "MODULE" } else { "REHEARSAL" },
        "applicability": "APPLICABLE",
        "result": result,
        "qualification": qualification,
        "profile_digest": profile_digest.clone(),
        "claim_scope": claim_scope,
        "claim_scope_digest": digest::sha256(&claim_scope_bytes),
        "started_at": started_at.clone(),
        "finished_at": finished_at,
        "monotonic_start_ns": monotonic_start_ns,
        "monotonic_end_ns": monotonic_end_ns,
        "warmup_elapsed_ms": warmup_actual_ms,
        "duration_target_ms": 3600000,
        "qualified_elapsed_ms": qualified_elapsed_ms,
        "sample_interval_ms": sample_interval.as_millis() as u64,
        "phases": phases,
        "samples": samples,
        "lease_renewals": lease_renewals,
        "summary": {
            "error_count": error_count,
            "unclassified_gap_count": unclassified_gap_count,
            "oom_events": 0,
            "container_restarts": 0,
            "oracle_mismatches": oracle_mismatches,
            "resource_limit_violations": resource_limit_violations,
            "module_metrics": {
                "formal_schedule_executed": formal && formal_schedule_valid,
                "absolute_threshold_status": absolute_threshold_status,
                "warmup_actual_ms": warmup_actual_ms,
                "successful_status_samples": successful_status_samples,
                "status_rpc_attempts": total_status_attempts,
                "status_rpc_successes": total_status_successes,
                "canonical_commits_before": commits_before,
                "canonical_commits_after": commits_after,
                "p4_stream_opens": topology.p4.stream_opens(1),
                "maximum_active_streams": topology.p4.max_active_streams(1),
                "saturation_preflight_accepted": saturation_accepted,
                "saturation_admission_rejected": saturation_rejected,
                "planned_stream_recovery_elapsed_ms": recovery_elapsed_ms,
                "lease_renewals": lease_renewal_count,
                "max_rss_bytes": max_rss_bytes,
                "max_fd_count": max_fd_count,
                "max_thread_count": max_thread_count,
                "max_queue_depth": max_queue_depth,
                "max_source_wal_bytes": max_source_wal_bytes,
                "max_input_wal_bytes": max_input_wal_bytes,
                "max_result_wal_bytes": max_result_wal_bytes,
                "aggregate_target_queue_depth_limit": observed_queue_limit,
                "source_wal_bytes_limit": soak_config.limits.source_wal_bytes,
                "input_wal_bytes_limit": soak_config.limits.input_wal_bytes,
                "result_wal_bytes_limit": soak_config.limits.result_wal_bytes,
                "process_resource_threshold_status": process_resource_threshold_status,
                "resource_observation": "linux-procfs",
                "packet_rate_measurement": "not_measurable_without_owner-frozen-sender-profile"
            }
        },
        "interruption": interruption,
        "cleanup": {
            "attempted": false, "completed": false, "exit_code": 1,
            "remaining_resources": ["edge-and-fake-boundaries"],
            "checks": {
                "edge_process_reaped": false,
                "p4_server_task_joined": false,
                "inference_server_task_joined": false,
                "control_server_task_joined": false,
                "p4_listener_released": false,
                "inference_listener_released": false,
                "control_listener_released": false
            }
        },
        "artifacts": [
            {
                "name": "qualification-soak-3600s.json",
                "sha256": profile_digest.trim_start_matches("sha256:"),
                "bytes": profile_bytes.len()
            },
            {
                "name": "qualification-soak-v1-schema.json",
                "sha256": schema_digest.trim_start_matches("sha256:"),
                "bytes": schema_bytes.len()
            }
        ]
    });
    topology
        .shutdown_with_evidence(
            "soak-evidence.json",
            evidence,
            edge_exit_code,
            edge_process_reaped,
        )
        .await?;
    if failed {
        return Err("Edge soak workload produced failed evidence".into());
    }
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn implementation_bound_32_target_actor_rehearsal() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(32, 0, 0).await?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    let empty = client
        .get_status(GetStatusRequest {
            target_id: String::new(),
            trace_id: "targets-zero".into(),
        })
        .await?
        .into_inner();
    assert!(empty.targets.is_empty());

    let started = Instant::now();
    for target_index in 0..32 {
        let reply = client
            .assign_target(AssignTargetRequest {
                assignment: Some(assignment(target_index, topology.p4_server.address)),
            })
            .await?
            .into_inner();
        assert_eq!(ActorState::Primary as i32, reply.state);
    }
    let status = client
        .get_status(GetStatusRequest {
            target_id: String::new(),
            trace_id: "targets-32".into(),
        })
        .await?
        .into_inner();
    assert_eq!(32, status.targets.len());
    for device_id in 1..=32 {
        assert_eq!(1, topology.p4.max_active_streams(device_id));
    }
    let overflow = match client
        .assign_target(AssignTargetRequest {
            assignment: Some(assignment(32, topology.p4_server.address)),
        })
        .await
    {
        Ok(_) => return Err("33rd target exceeded the frozen bound without rejection".into()),
        Err(error) => error,
    };
    assert_eq!(tonic::Code::ResourceExhausted, overflow.code());
    assert!(overflow.message().contains("TARGET_LIMIT_EXCEEDED"));
    let evidence = json!({
        "schema_version": "edge-target-capacity-rehearsal/v1",
        "test_id": "TEST-EDGE-TARGET-CAPACITY-001",
        "requirement_ids": [
            "MOD-EDGE-001", "ARCH-TARGET-FLEET-001", "REL-TARGET-FLEET-001",
            "TEST-TARGET-FLEET-001"
        ],
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "HOLD",
        "qualification": "NOT_QUALIFIED",
        "reason": "DEC-001 Owner has not frozen N and its absolute workload target",
        "implementation_target_count": 32,
        "maximum_concurrent_streams_per_device": 1,
        "elapsed_ms": started.elapsed().as_millis()
    });
    write_evidence(
        topology.root.path(),
        "target-capacity-rehearsal.json",
        &evidence,
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn bounded_rule_activation_matrix_rehearsal() -> Result<(), Box<dyn Error>> {
    let topology = Topology::new(1, 0, 0).await?;
    let mut child = topology.spawn_edge()?;
    let mut client = topology.client().await?;
    let mut assigned = assignment(0, topology.p4_server.address);
    let reply = client
        .assign_target(AssignTargetRequest {
            assignment: Some(assigned.clone()),
        })
        .await?
        .into_inner();
    assigned
        .fence
        .as_mut()
        .ok_or("assignment fence missing")?
        .actor_runtime_epoch = reply.actor_runtime_epoch;
    let mut measurements = Vec::new();
    for rule_count in [0_usize, 128, 1_024, 4_096] {
        let operation = format!("capacity-{}", rule_count);
        let intent = baseline_effect(&assigned, &operation, rule_count);
        let preflight_started = Instant::now();
        let preflight = client
            .preflight_effect(PreflightEffectRequest {
                intent: Some(intent.clone()),
            })
            .await?
            .into_inner();
        let preflight_elapsed = preflight_started.elapsed();
        assert_eq!(rule_count as u32, preflight.physical_entries);
        assert!(preflight_elapsed < Duration::from_secs(10));
        let execute_started = Instant::now();
        let result = client
            .execute_effect(masi_edge::contract::edge::ExecuteEffectRequest {
                intent: Some(intent.clone()),
                preflight_token: preflight.preflight_token,
            })
            .await?
            .into_inner();
        let execute_elapsed = execute_started.elapsed();
        assert_eq!(EffectStatus::Applied as i32, result.status);
        assert_eq!(rule_count as u32, result.expected_entries);
        assert_eq!(rule_count as u32, result.observed_entries);
        let result_digest = digest::message_sha256(&result);
        let ack = client
            .acknowledge_effect(AcknowledgeEffectRequest {
                schema_version: "effect-canonical-ack/v1".into(),
                target_id: assigned.target_id.clone(),
                effect_intent_id: intent.effect_intent_id,
                operation_id: intent.operation_id,
                fence: result.fence,
                result_digest,
                canonical_effect_reference: format!("canonical-effect-{}", rule_count),
                committed_at_unix_ms: unix_ms(),
                trace_id: format!("ack-capacity-{}", rule_count),
            })
            .await?
            .into_inner();
        assert_eq!("checkpointed", ack.status);
        measurements.push(json!({
            "rule_count": rule_count,
            "preflight_elapsed_ms": preflight_elapsed.as_millis(),
            "activation_readback_elapsed_ms": execute_elapsed.as_millis()
        }));
    }
    let evidence = json!({
        "schema_version": "edge-performance-rehearsal/v1",
        "test_id": "TEST-EDGE-FW-CAPACITY-001",
        "requirement_ids": [
            "MOD-EDGE-001", "CONTRACT-P4-FW-001", "FUNC-EFFECT-001",
            "TEST-P4-FW-001", "TEST-008"
        ],
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "HOLD",
        "qualification": "NOT_QUALIFIED",
        "reason": "DEC-001 absolute hardware/rate/latency targets are not Owner-frozen",
        "rule_counts": [0, 128, 1024, 4096],
        "phase": "recovery-or-activation",
        "measurements": measurements,
        "formal_soak_seconds_required": 3600,
        "formal_soak_executed": false
    });
    write_evidence(
        topology.root.path(),
        "performance-rehearsal.json",
        &evidence,
    )?;
    terminate(&mut child).await?;
    topology.shutdown().await
}
