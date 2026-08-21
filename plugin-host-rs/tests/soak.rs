#![allow(missing_docs)]

mod support;

use std::io::{Read as _, Write as _};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use masi_plugin_host::admission::sha256_bytes;
use masi_plugin_host::contract::host::plugin_host_control_client::PluginHostControlClient;
use masi_plugin_host::contract::host::{ApplyBindingRequest, BindingEnvelope, ExecuteRequest};
use support::{TestEnvironment, refresh_binding};
use tokio::sync::{Mutex, RwLock, watch};
use tonic::Code;
use tonic::transport::{Certificate, ClientTlsConfig, Endpoint, Identity};

#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
#[ignore = "formal 60-second warmup plus four 900-second stages"]
async fn formal_four_stage_soak() -> Result<(), Box<dyn std::error::Error>> {
    let quick = std::env::var_os("MASI_PLUGIN_HOST_SOAK_QUICK").is_some();
    let durations = if quick {
        StageDurations {
            warmup: 1,
            steady: 1,
            peak: 1,
            saturation: 1,
            recovery: 1,
        }
    } else {
        StageDurations {
            warmup: 60,
            steady: 900,
            peak: 900,
            saturation: 900,
            recovery: 900,
        }
    };
    let environment = Arc::new(TestEnvironment::new(true)?);
    let wasm = Arc::new(RwLock::new(environment.wasm_binding(1, "active")?));
    let service_binding = environment.service_binding("active")?;
    let service = Arc::new(RwLock::new(service_binding.clone()));
    let service_config = environment.write_service_fixture_config(&service_binding, "slow")?;
    let mut service_process = ProcessGuard::spawn(
        &service_binary(),
        &["--config", path_text(&service_config)?],
        &environment.temporary.path().join("soak-service.log"),
    )?;
    wait_for_path(&environment.service_socket).await?;
    let mut host_process = ProcessGuard::spawn(
        &host_binary(),
        &["--config", path_text(&environment.config_path)?],
        &environment.temporary.path().join("soak-host.log"),
    )?;
    wait_for_http(environment.health_address, "/readyz").await?;
    let channel = manager_channel(&environment).await?;
    let mut manager = PluginHostControlClient::new(channel.clone());
    manager
        .apply_binding(ApplyBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            binding: Some(wasm.read().await.clone()),
        })
        .await?;
    manager
        .apply_binding(ApplyBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            binding: Some(service.read().await.clone()),
        })
        .await?;

    let sequence = Arc::new(AtomicU64::new(1));
    let samples = Arc::new(Mutex::new(Vec::<serde_json::Value>::new()));
    let refresh_failures = Arc::new(AtomicU64::new(0));
    let (stop_tx, stop_rx) = watch::channel(false);
    let sampler = tokio::spawn(sample_resources(
        environment.clone(),
        samples.clone(),
        stop_rx.clone(),
    ));
    let refresher = tokio::spawn(refresh_bindings(
        channel.clone(),
        wasm.clone(),
        service.clone(),
        refresh_failures.clone(),
        stop_rx,
        quick,
    ));

    let mut stages = Vec::new();
    stages.push(
        run_serial_stage(
            "warmup",
            durations.warmup,
            Duration::from_millis(100),
            channel.clone(),
            wasm.clone(),
            service.clone(),
            sequence.clone(),
        )
        .await?,
    );
    stages.push(
        run_serial_stage(
            "steady",
            durations.steady,
            Duration::from_millis(100),
            channel.clone(),
            wasm.clone(),
            service.clone(),
            sequence.clone(),
        )
        .await?,
    );
    stages.push(
        run_serial_stage(
            "peak",
            durations.peak,
            Duration::from_millis(20),
            channel.clone(),
            wasm.clone(),
            service.clone(),
            sequence.clone(),
        )
        .await?,
    );
    stages.push(
        run_saturation_stage(
            durations.saturation,
            channel.clone(),
            wasm.clone(),
            service.clone(),
            sequence.clone(),
        )
        .await?,
    );
    stages.push(
        run_recovery_activation_stage(
            durations.recovery,
            &environment,
            channel,
            wasm.clone(),
            service.clone(),
            sequence,
            &service_config,
            &mut service_process,
        )
        .await?,
    );

    let _ = stop_tx.send(true);
    sampler.await?.map_err(std::io::Error::other)?;
    refresher.await?.map_err(std::io::Error::other)?;
    samples.lock().await.push(
        collect_resource_sample(&environment)
            .await
            .map_err(std::io::Error::other)?,
    );
    let process_running = host_process.is_running()? && service_process.is_running()?;
    service_process.terminate()?;
    host_process.terminate()?;
    let samples = samples.lock().await.clone();
    let max_rss = samples
        .iter()
        .filter_map(|sample| sample["rss_bytes"].as_u64())
        .max()
        .unwrap_or(0);
    let max_threads = samples
        .iter()
        .filter_map(|sample| sample["threads"].as_u64())
        .max()
        .unwrap_or(0);
    let max_fds = samples
        .iter()
        .filter_map(|sample| sample["fds"].as_u64())
        .max()
        .unwrap_or(0);
    let max_queued = samples
        .iter()
        .filter_map(|sample| sample["queued"].as_u64())
        .max()
        .unwrap_or(0);
    let max_in_flight = samples
        .iter()
        .filter_map(|sample| sample["in_flight"].as_u64())
        .max()
        .unwrap_or(0);
    let max_manager_connections = samples
        .iter()
        .filter_map(|sample| sample["manager_connections_peak"].as_u64())
        .max()
        .unwrap_or(0);
    let first_sample = samples.first().cloned().unwrap_or_default();
    let final_sample = samples.last().cloned().unwrap_or_default();
    let delta = |field: &str| {
        let first = first_sample[field].as_u64().unwrap_or(0);
        let last = final_sample[field].as_u64().unwrap_or(0);
        i64::try_from(last)
            .unwrap_or(i64::MAX)
            .saturating_sub(i64::try_from(first).unwrap_or(i64::MAX))
    };
    let unclassified_failures: u64 = stages
        .iter()
        .filter_map(|stage| stage["unclassified_failures"].as_u64())
        .sum();
    let requested_formal_seconds =
        durations.steady + durations.peak + durations.saturation + durations.recovery;
    let formal = !quick && durations.warmup == 60 && requested_formal_seconds == 3600;
    let passed = formal
        && process_running
        && unclassified_failures == 0
        && refresh_failures.load(Ordering::Relaxed) == 0
        && max_rss <= 384 * 1024 * 1024
        && max_threads <= 64
        && max_fds <= 128
        && max_queued <= 64
        && max_in_flight <= 4
        && max_manager_connections <= 64
        && stages[3]["classified_rejections"].as_u64().unwrap_or(0) > 0
        && samples.len() >= 300
        && delta("rss_bytes") <= 64 * 1024 * 1024
        && delta("threads") <= 8
        && delta("fds") <= 8
        && final_sample["queued"].as_u64().unwrap_or(1) == 0
        && final_sample["in_flight"].as_u64().unwrap_or(1) == 0;
    let evidence = serde_json::json!({
        "schema_version": "plugin-host-soak-evidence/v1",
        "module_id": "MOD-PLUGIN-001",
        "runtime_profiles": ["wasm-component/v1", "grpc-service/v1"],
        "wasmtime_version": "47.0.3",
        "warmup_seconds": durations.warmup,
        "formal_seconds": requested_formal_seconds,
        "stages": stages,
        "resource_samples": samples,
        "max_rss_bytes": max_rss,
        "max_threads": max_threads,
        "max_fds": max_fds,
        "max_queued": max_queued,
        "max_in_flight": max_in_flight,
        "max_manager_connections": max_manager_connections,
        "resource_growth": {
            "rss_bytes_delta": delta("rss_bytes"),
            "threads_delta": delta("threads"),
            "fds_delta": delta("fds"),
            "queued_final": final_sample["queued"],
            "in_flight_final": final_sample["in_flight"],
            "sample_count": samples.len(),
            "leak_thresholds_passed": formal && delta("rss_bytes") <= 64 * 1024 * 1024 && delta("threads") <= 8 && delta("fds") <= 8
        },
        "revocation_refresh_failures": refresh_failures.load(Ordering::Relaxed),
        "unclassified_failures": unclassified_failures,
        "candidate_processes_remained_live": process_running,
        "cleanup_succeeded": true,
        "formal_soak_executed": formal,
        "result": if passed { "PASS" } else if quick { "NOT_RUN" } else { "FAIL" },
        "qualification": "NOT_QUALIFIED",
        "reason_code": if passed {
            "FORMAL_PLUGIN_HOST_SOAK_COMPLETE"
        } else if quick {
            "QUICK_REHEARSAL_IS_NOT_FORMAL_SOAK"
        } else {
            "FORMAL_PLUGIN_HOST_SOAK_FAILED"
        }
    });
    if let Some(output) = std::env::var_os("MASI_PLUGIN_HOST_SOAK_OUTPUT") {
        let output = PathBuf::from(output);
        if output.exists() || std::fs::symlink_metadata(&output).is_ok() {
            return Err("soak evidence output already exists".into());
        }
        std::fs::write(output, serde_json::to_vec_pretty(&evidence)?)?;
    }
    if !quick {
        assert!(passed);
    }
    Ok(())
}

struct StageDurations {
    warmup: u64,
    steady: u64,
    peak: u64,
    saturation: u64,
    recovery: u64,
}

async fn run_serial_stage(
    name: &str,
    seconds: u64,
    interval: Duration,
    channel: tonic::transport::Channel,
    wasm: Arc<RwLock<BindingEnvelope>>,
    service: Arc<RwLock<BindingEnvelope>>,
    sequence: Arc<AtomicU64>,
) -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    let started = Instant::now();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(seconds);
    let mut successes = 0u64;
    let mut classified_rejections = 0u64;
    let mut unclassified_failures = 0u64;
    while tokio::time::Instant::now() < deadline {
        let identity = sequence.fetch_add(1, Ordering::Relaxed);
        let binding = if identity.is_multiple_of(10) {
            service.read().await.clone()
        } else {
            wasm.read().await.clone()
        };
        match invoke(channel.clone(), &binding, identity).await {
            Ok(()) => successes = successes.saturating_add(1),
            Err(status)
                if matches!(
                    status.code(),
                    Code::ResourceExhausted | Code::DeadlineExceeded
                ) =>
            {
                classified_rejections = classified_rejections.saturating_add(1);
            }
            Err(_) => unclassified_failures = unclassified_failures.saturating_add(1),
        }
        tokio::time::sleep(interval).await;
    }
    Ok(stage_evidence(
        name,
        seconds,
        started.elapsed(),
        successes,
        classified_rejections,
        unclassified_failures,
    ))
}

async fn run_saturation_stage(
    seconds: u64,
    channel: tonic::transport::Channel,
    wasm: Arc<RwLock<BindingEnvelope>>,
    service: Arc<RwLock<BindingEnvelope>>,
    sequence: Arc<AtomicU64>,
) -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    let started = Instant::now();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(seconds);
    let counters = Arc::new(StageCounters::default());
    let mut tasks = tokio::task::JoinSet::new();
    for _ in 0..96 {
        let channel = channel.clone();
        let wasm = wasm.clone();
        let service = service.clone();
        let sequence = sequence.clone();
        let counters = counters.clone();
        tasks.spawn(async move {
            while tokio::time::Instant::now() < deadline {
                let identity = sequence.fetch_add(1, Ordering::Relaxed);
                let binding = if identity.is_multiple_of(2) {
                    service.read().await.clone()
                } else {
                    wasm.read().await.clone()
                };
                match invoke(channel.clone(), &binding, identity).await {
                    Ok(()) => {
                        counters.successes.fetch_add(1, Ordering::Relaxed);
                    }
                    Err(status)
                        if matches!(
                            status.code(),
                            Code::ResourceExhausted | Code::DeadlineExceeded
                        ) =>
                    {
                        counters.classified.fetch_add(1, Ordering::Relaxed);
                    }
                    Err(_) => {
                        counters.unclassified.fetch_add(1, Ordering::Relaxed);
                    }
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        });
    }
    while let Some(result) = tasks.join_next().await {
        result?;
    }
    Ok(stage_evidence(
        "saturation",
        seconds,
        started.elapsed(),
        counters.successes.load(Ordering::Relaxed),
        counters.classified.load(Ordering::Relaxed),
        counters.unclassified.load(Ordering::Relaxed),
    ))
}

#[allow(clippy::too_many_arguments)]
async fn run_recovery_activation_stage(
    seconds: u64,
    environment: &TestEnvironment,
    channel: tonic::transport::Channel,
    wasm: Arc<RwLock<BindingEnvelope>>,
    service: Arc<RwLock<BindingEnvelope>>,
    sequence: Arc<AtomicU64>,
    service_config: &Path,
    service_process: &mut ProcessGuard,
) -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    let started = Instant::now();
    let half = seconds / 2;
    let before = run_serial_stage(
        "recovery-before-activation",
        half,
        Duration::from_millis(50),
        channel.clone(),
        wasm.clone(),
        service.clone(),
        sequence.clone(),
    )
    .await?;
    service_process.crash()?;
    let service_binding = service.read().await.clone();
    let service_crash = invoke(
        channel.clone(),
        &service_binding,
        sequence.fetch_add(1, Ordering::Relaxed),
    )
    .await;
    let service_crash_classified = service_crash
        .as_ref()
        .err()
        .is_some_and(|status| status.code() == Code::Unavailable);
    *service_process = ProcessGuard::spawn(
        &service_binary(),
        &["--config", path_text(service_config)?],
        &environment
            .temporary
            .path()
            .join("soak-service-restarted.log"),
    )?;
    wait_for_path(&environment.service_socket).await?;
    let recovery_deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    let mut service_restart_recovered = false;
    while tokio::time::Instant::now() < recovery_deadline {
        let identity = sequence.fetch_add(1, Ordering::Relaxed);
        if invoke(channel.clone(), &service_binding, identity)
            .await
            .is_ok()
        {
            service_restart_recovered = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    if !service_crash_classified || !service_restart_recovered {
        return Err("service crash/restart recovery oracle failed".into());
    }
    // Serialize the shared desired binding with the periodic freshness refresher.
    // This models one Manager-owned binding operation stream and prevents the
    // test driver itself from emitting an obsolete refresh concurrently.
    let mut desired_wasm = wasm.write().await;
    let staged = environment.wasm_binding(2, "staged")?;
    let mut manager = PluginHostControlClient::new(channel.clone());
    manager
        .apply_binding(ApplyBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            binding: Some(staged.clone()),
        })
        .await?;
    let shadow = refresh_binding(&staged, "shadow")?;
    manager
        .apply_binding(ApplyBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            binding: Some(shadow.clone()),
        })
        .await?;
    invoke_mode(channel.clone(), &shadow, 9_000_000_001, "shadow").await?;
    let active = refresh_binding(&shadow, "active")?;
    manager
        .apply_binding(ApplyBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            binding: Some(active.clone()),
        })
        .await?;
    *desired_wasm = active;
    drop(desired_wasm);
    let after_seconds = seconds.saturating_sub(half);
    let after = run_serial_stage(
        "recovery-after-activation",
        after_seconds,
        Duration::from_millis(50),
        channel,
        wasm,
        service,
        sequence,
    )
    .await?;
    Ok(serde_json::json!({
        "name": "recovery_activation",
        "requested_seconds": seconds,
        "actual_millis": started.elapsed().as_millis(),
        "successes": before["successes"].as_u64().unwrap_or(0)
            + after["successes"].as_u64().unwrap_or(0),
        "classified_rejections": before["classified_rejections"].as_u64().unwrap_or(0)
            + after["classified_rejections"].as_u64().unwrap_or(0),
        "unclassified_failures": before["unclassified_failures"].as_u64().unwrap_or(0)
            + after["unclassified_failures"].as_u64().unwrap_or(0),
        "activation_sequence": ["service-crash", "service-restart", "staged", "shadow", "active", "old-generation-draining"],
        "service_crash_classified": service_crash_classified,
        "service_restart_recovered": service_restart_recovered,
        "children": [before, after]
    }))
}

#[derive(Default)]
struct StageCounters {
    successes: AtomicU64,
    classified: AtomicU64,
    unclassified: AtomicU64,
}

fn stage_evidence(
    name: &str,
    requested_seconds: u64,
    actual: Duration,
    successes: u64,
    classified_rejections: u64,
    unclassified_failures: u64,
) -> serde_json::Value {
    serde_json::json!({
        "name": name,
        "requested_seconds": requested_seconds,
        "actual_millis": actual.as_millis(),
        "successes": successes,
        "classified_rejections": classified_rejections,
        "unclassified_failures": unclassified_failures,
        "completed": actual >= Duration::from_secs(requested_seconds)
    })
}

async fn invoke(
    channel: tonic::transport::Channel,
    binding: &BindingEnvelope,
    identity: u64,
) -> Result<(), tonic::Status> {
    invoke_mode(channel, binding, identity, "active").await
}

async fn invoke_mode(
    channel: tonic::transport::Channel,
    binding: &BindingEnvelope,
    identity: u64,
    execution_mode: &str,
) -> Result<(), tonic::Status> {
    let input = format!("soak-input-{identity}").into_bytes();
    PluginHostControlClient::new(channel)
        .execute(ExecuteRequest {
            schema_version: "plugin-host-execute/v1".to_owned(),
            invocation_id: format!("soak-invocation-{identity}"),
            plugin_id: binding.plugin_id.clone(),
            binding_generation: binding.binding_generation,
            binding_epoch: binding.binding_epoch.clone(),
            capability_id: "plugin.transform.execute".to_owned(),
            input_digest: sha256_bytes(&input),
            input,
            deadline_ms: 10_000,
            result_fence: format!("soak-fence-{identity}"),
            trace_id: format!("soak-trace-{identity}"),
            execution_mode: execution_mode.to_owned(),
        })
        .await
        .map(|_| ())
}

async fn refresh_bindings(
    channel: tonic::transport::Channel,
    wasm: Arc<RwLock<BindingEnvelope>>,
    service: Arc<RwLock<BindingEnvelope>>,
    failures: Arc<AtomicU64>,
    mut stop: watch::Receiver<bool>,
    quick: bool,
) -> Result<(), String> {
    let refresh_period = if quick {
        Duration::from_millis(250)
    } else {
        Duration::from_secs(30)
    };
    let mut interval = tokio::time::interval(refresh_period);
    interval.tick().await;
    loop {
        tokio::select! {
            changed = stop.changed() => {
                if changed.is_err() || *stop.borrow() {
                    return Ok(());
                }
            }
            _ = interval.tick() => {
                for binding in [&wasm, &service] {
                    let mut desired = binding.write().await;
                    let current = desired.clone();
                    let refreshed = match refresh_binding(&current, "active") {
                        Ok(refreshed) => refreshed,
                        Err(error) => {
                            failures.fetch_add(1, Ordering::Relaxed);
                            tracing::warn!(error = %error, "soak binding refresh construction failed");
                            continue;
                        }
                    };
                    let result = PluginHostControlClient::new(channel.clone())
                        .apply_binding(ApplyBindingRequest {
                            schema_version: "plugin-host-control/v1".to_owned(),
                            binding: Some(refreshed.clone()),
                        })
                        .await;
                    if result.is_ok() {
                        *desired = refreshed;
                    } else {
                        failures.fetch_add(1, Ordering::Relaxed);
                    }
                }
            }
        }
    }
}

async fn sample_resources(
    environment: Arc<TestEnvironment>,
    samples: Arc<Mutex<Vec<serde_json::Value>>>,
    mut stop: watch::Receiver<bool>,
) -> Result<(), String> {
    let mut interval = tokio::time::interval(Duration::from_secs(10));
    loop {
        tokio::select! {
            changed = stop.changed() => {
                if changed.is_err() || *stop.borrow() {
                    return Ok(());
                }
            }
            _ = interval.tick() => {
                samples.lock().await.push(collect_resource_sample(&environment).await?);
            }
        }
    }
}

async fn collect_resource_sample(
    environment: &TestEnvironment,
) -> Result<serde_json::Value, String> {
    let address = environment.health_address;
    let response = tokio::task::spawn_blocking(move || read_http(address, "/metrics"))
        .await
        .map_err(|error| error.to_string())?
        .map_err(|error| error.to_string())?;
    let body = response
        .split_once("\r\n\r\n")
        .map(|(_, body)| body)
        .unwrap_or("");
    Ok(serde_json::json!({
        "elapsed_unix_ms": support::unix_ms().map_err(|error| error.to_string())?,
        "rss_bytes": metric_value(body, "masi_plugin_host_process_rss_bytes"),
        "threads": metric_value(body, "masi_plugin_host_process_threads"),
        "fds": metric_value(body, "masi_plugin_host_process_fds"),
        "queued": metric_value(body, "masi_plugin_host_queued"),
        "in_flight": metric_value(body, "masi_plugin_host_in_flight"),
        "manager_connections": metric_value(body, "masi_plugin_host_manager_connections"),
        "manager_connections_peak": metric_value(body, "masi_plugin_host_manager_connections_peak")
    }))
}

fn metric_value(body: &str, name: &str) -> u64 {
    body.lines()
        .find_map(|line| line.strip_prefix(&format!("{name} ")))
        .and_then(|value| value.parse().ok())
        .unwrap_or(0)
}

async fn manager_channel(
    environment: &TestEnvironment,
) -> Result<tonic::transport::Channel, Box<dyn std::error::Error>> {
    let tls = ClientTlsConfig::new()
        .ca_certificate(Certificate::from_pem(&environment.ca_pem))
        .identity(Identity::from_pem(
            &environment.manager_certificate_pem,
            &environment.manager_private_key_pem,
        ))
        .domain_name("plugin-host.test");
    Ok(
        Endpoint::from_shared(format!("https://{}", environment.listen_address))?
            .tls_config(tls)?
            .connect_timeout(Duration::from_secs(2))
            .timeout(Duration::from_secs(10))
            .connect()
            .await?,
    )
}

fn read_http(
    address: std::net::SocketAddr,
    path: &str,
) -> Result<String, Box<dyn std::error::Error + Send + Sync>> {
    let mut stream = std::net::TcpStream::connect_timeout(&address, Duration::from_secs(2))?;
    stream.set_read_timeout(Some(Duration::from_secs(2)))?;
    write!(
        stream,
        "GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
    )?;
    let mut response = String::new();
    stream.read_to_string(&mut response)?;
    Ok(response)
}

async fn wait_for_http(
    address: std::net::SocketAddr,
    path: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    for _ in 0..100 {
        if read_http(address, path).is_ok_and(|value| value.starts_with("HTTP/1.1 200")) {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    Err("Host did not become ready".into())
}

async fn wait_for_path(path: &Path) -> Result<(), Box<dyn std::error::Error>> {
    for _ in 0..100 {
        if path.exists() {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    Err("service socket did not become ready".into())
}

fn host_binary() -> PathBuf {
    std::env::var_os("MASI_PLUGIN_HOST_BINARY")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_BIN_EXE_masi-plugin-host")))
}

fn service_binary() -> PathBuf {
    std::env::var_os("MASI_PLUGIN_SERVICE_FIXTURE_BINARY")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_BIN_EXE_masi-plugin-service-fixture")))
}

fn path_text(path: &Path) -> Result<&str, Box<dyn std::error::Error>> {
    path.to_str().ok_or_else(|| "non-UTF8 path".into())
}

struct ProcessGuard {
    child: Child,
}

impl ProcessGuard {
    fn spawn(
        binary: &Path,
        arguments: &[&str],
        log_path: &Path,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let stdout = std::fs::File::create(log_path)?;
        let stderr = stdout.try_clone()?;
        Ok(Self {
            child: Command::new(binary)
                .args(arguments)
                .stdout(Stdio::from(stdout))
                .stderr(Stdio::from(stderr))
                .spawn()?,
        })
    }

    fn is_running(&mut self) -> Result<bool, Box<dyn std::error::Error>> {
        Ok(self.child.try_wait()?.is_none())
    }

    fn terminate(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        let status = Command::new("kill")
            .arg("-TERM")
            .arg(self.child.id().to_string())
            .status()?;
        if !status.success() {
            return Err("failed to send SIGTERM".into());
        }
        for _ in 0..200 {
            if self.child.try_wait()?.is_some() {
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(25));
        }
        Err("process drain exceeded deadline".into())
    }

    fn crash(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        self.child.kill()?;
        let _ = self.child.wait()?;
        Ok(())
    }
}

impl Drop for ProcessGuard {
    fn drop(&mut self) {
        if self.child.try_wait().ok().flatten().is_none() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}
