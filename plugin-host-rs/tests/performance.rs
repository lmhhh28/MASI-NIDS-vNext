#![allow(missing_docs)]

mod support;

use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use masi_plugin_host::HostState;
use masi_plugin_host::admission::sha256_bytes;
use masi_plugin_host::contract::host::{BindingEnvelope, ExecuteRequest};
use support::TestEnvironment;
use tokio::sync::Mutex;

#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
#[ignore = "formal bounded performance gate"]
async fn bounded_concurrency_batch_and_saturation_matrix() -> Result<(), Box<dyn std::error::Error>>
{
    let environment = TestEnvironment::new(true)?;
    let wasm_binding = environment.wasm_binding(1, "active")?;
    let service_binding = environment.service_binding("active")?;
    let service_config =
        environment.write_service_fixture_config(&service_binding, "bounded-echo")?;
    let mut service_process = ProcessGuard::spawn(
        &service_binary(),
        &["--config", path_text(&service_config)?],
        &environment.temporary.path().join("performance-service.log"),
    )?;
    wait_for_socket(&environment.service_socket).await?;
    let state = HostState::new(environment.config.clone());
    let cold_started = Instant::now();
    state.apply_binding(wasm_binding.clone()).await?;
    let compile_admission_micros = cold_started.elapsed().as_micros();
    state.apply_binding(service_binding.clone()).await?;
    let sequence = Arc::new(AtomicU64::new(1));
    let cold_warm = measure_wasm_cold_warm(
        state.clone(),
        &wasm_binding,
        sequence.clone(),
        compile_admission_micros,
    )
    .await?;
    let wasm_scenarios = measure_matrix(state.clone(), wasm_binding, sequence.clone()).await?;
    let service_scenarios =
        measure_matrix(state.clone(), service_binding.clone(), sequence.clone()).await?;
    let host_resources = process_resources_for(std::process::id())?;
    let service_resources = process_resources_for(service_process.child.id())?;
    state
        .drain_binding(
            &service_binding.plugin_id,
            service_binding.binding_generation,
            5000,
            "performance-drain",
        )
        .await?;
    service_process.terminate()?;
    let saturation = measure_real_saturation().await?;
    let scenarios: Vec<_> = wasm_scenarios
        .iter()
        .chain(&service_scenarios)
        .cloned()
        .collect();
    let rejected_total: u64 = scenarios
        .iter()
        .filter_map(|scenario| scenario["rejected"].as_u64())
        .sum();
    let maximum_p99 = scenarios
        .iter()
        .filter_map(|scenario| scenario["admission_queue_execute_e2e_micros"]["p99"].as_u64())
        .max()
        .unwrap_or_default();
    assert_eq!(rejected_total, 0);
    assert!(maximum_p99 <= 10_000_000);
    assert!(host_resources.rss_bytes <= 384 * 1024 * 1024);
    assert!(host_resources.fd_count <= 128);
    assert!(host_resources.threads <= 64);
    assert!(saturation["classified_rejections"].as_u64().unwrap_or(0) > 0);
    assert_eq!(saturation["unclassified_failures"].as_u64().unwrap_or(1), 0);
    let environment_profile_digest = sha256_bytes(&serde_json::to_vec(&environment.config)?);
    let result_set_digest = sha256_bytes(&serde_json::to_vec(&serde_json::json!({
        "wasm_matrix": wasm_scenarios,
        "service_matrix": service_scenarios,
        "cold_warm": cold_warm,
        "saturation": saturation,
        "host_resources": resource_json(&host_resources),
        "service_resources": resource_json(&service_resources)
    }))?);
    let report = serde_json::json!({
        "schema_version": "plugin-host-performance-evidence/v1",
        "module_id": "MOD-PLUGIN-001",
        "runtime_profiles": ["wasm-component/v1", "grpc-service/v1"],
        "wasmtime_version": "47.0.3",
        "wasm_matrix": wasm_scenarios,
        "service_matrix": service_scenarios,
        "wasm_cold_warm": cold_warm,
        "saturation": saturation,
        "environment_profile_digest": environment_profile_digest,
        "result_set_digest": result_set_digest,
        "resource_peak_observation": {
            "host_process": resource_json(&host_resources),
            "service_process": resource_json(&service_resources),
            "request_bytes_measured": scenarios.iter().map(|scenario| scenario["input_bytes"].as_u64().unwrap_or(0).saturating_mul(scenario["calls"].as_u64().unwrap_or(0))).sum::<u64>(),
            "copy_model": {"public_request_owned_copies": 1, "typed_runtime_owned_copies": 1},
            "wasm_fuel_budget_per_call": environment.config.limits.wasm_fuel,
            "instances_in_flight_limit": 2,
            "queue_depth_limit": 32,
            "global_queue_limit": 64,
            "restart_limit_per_10_minutes": 5
        },
        "steady_rejected_total": rejected_total,
        "maximum_p99_micros": maximum_p99,
        "absolute_deadline_micros": 10000000,
        "result": "PASS",
        "qualification": "NOT_QUALIFIED",
        "reason_code": "HOST_BOUNDARY_PERFORMANCE_WITHIN_PROFILE",
        "core_peak_relative_degradation": {
            "applicability": "NOT_APPLICABLE",
            "reason_code": "REQUIRES_FORMAL_CORE_PLUGIN_PAIRWISE_NOT_STARTED"
        }
    });
    if let Some(path) = std::env::var_os("MASI_PLUGIN_HOST_PERFORMANCE_OUTPUT") {
        let path = std::path::PathBuf::from(path);
        if path.exists() || std::fs::symlink_metadata(&path).is_ok() {
            return Err("performance evidence output already exists".into());
        }
        std::fs::write(path, serde_json::to_vec_pretty(&report)?)?;
    }
    Ok(())
}

async fn measure_wasm_cold_warm(
    state: Arc<HostState>,
    binding: &BindingEnvelope,
    sequence: Arc<AtomicU64>,
    compile_admission_micros: u128,
) -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    let input = vec![b'w'; 128];
    let identity = sequence.fetch_add(1, Ordering::Relaxed);
    let started = Instant::now();
    state
        .execute(performance_request(
            binding,
            input.clone(),
            identity,
            10_000,
        ))
        .await?;
    let cold_instantiate_execute_micros = started.elapsed().as_micros();
    let mut warm = Vec::with_capacity(64);
    for _ in 0..64 {
        let identity = sequence.fetch_add(1, Ordering::Relaxed);
        let started = Instant::now();
        state
            .execute(performance_request(
                binding,
                input.clone(),
                identity,
                10_000,
            ))
            .await?;
        warm.push(started.elapsed().as_micros());
    }
    warm.sort_unstable();
    Ok(serde_json::json!({
        "component_compile_and_admission_micros": u64::try_from(compile_admission_micros).unwrap_or(u64::MAX),
        "cold_first_instantiate_execute_micros": u64::try_from(cold_instantiate_execute_micros).unwrap_or(u64::MAX),
        "warm_calls": warm.len(),
        "warm_execute_micros": {
            "p50": percentile(&warm, 50),
            "p95": percentile(&warm, 95),
            "p99": percentile(&warm, 99),
            "max": warm.last().copied().and_then(|value| u64::try_from(value).ok()).unwrap_or(u64::MAX)
        },
        "component_cache_state": "fresh-host-state-no-precompiled-entry"
    }))
}

async fn measure_real_saturation() -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    let environment = TestEnvironment::new(true)?;
    let binding = environment.service_binding("active")?;
    let service_config = environment.write_service_fixture_config(&binding, "slow")?;
    let mut service = ProcessGuard::spawn(
        &service_binary(),
        &["--config", path_text(&service_config)?],
        &environment.temporary.path().join("saturation-service.log"),
    )?;
    wait_for_socket(&environment.service_socket).await?;
    let state = HostState::new(environment.config.clone());
    state.apply_binding(binding.clone()).await?;
    let task_count = 128usize;
    let barrier = Arc::new(tokio::sync::Barrier::new(task_count + 1));
    let mut tasks = tokio::task::JoinSet::new();
    for identity in 0..task_count {
        let state = state.clone();
        let binding = binding.clone();
        let barrier = barrier.clone();
        tasks.spawn(async move {
            barrier.wait().await;
            state
                .execute(performance_request(
                    &binding,
                    vec![b's'; 128],
                    10_000_000 + identity as u64,
                    1000,
                ))
                .await
        });
    }
    barrier.wait().await;
    tokio::time::sleep(Duration::from_millis(20)).await;
    let (peak_queued_observed, peak_in_flight_observed) = state.runtime_gauges().await;
    let mut succeeded = 0u64;
    let mut classified_rejections = 0u64;
    let mut unclassified_failures = 0u64;
    while let Some(result) = tasks.join_next().await {
        match result? {
            Ok(_) => succeeded = succeeded.saturating_add(1),
            Err(error)
                if matches!(
                    error.reason,
                    masi_plugin_host::ReasonCode::ResourceExhausted
                        | masi_plugin_host::ReasonCode::DeadlineExceeded
                ) =>
            {
                classified_rejections = classified_rejections.saturating_add(1);
            }
            Err(_) => unclassified_failures = unclassified_failures.saturating_add(1),
        }
    }
    let (final_queued, final_in_flight) = state.runtime_gauges().await;
    service.terminate()?;
    Ok(serde_json::json!({
        "submitted": task_count,
        "succeeded": succeeded,
        "classified_rejections": classified_rejections,
        "unclassified_failures": unclassified_failures,
        "stable_rejection_reasons": ["RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED"],
        "peak_queued_observed": peak_queued_observed,
        "peak_in_flight_observed": peak_in_flight_observed,
        "per_binding_queue_limit": environment.config.limits.per_binding_queue_depth,
        "global_queue_limit": environment.config.limits.global_queue_depth,
        "per_binding_in_flight_limit": environment.config.limits.per_binding_in_flight,
        "final_queued": final_queued,
        "final_in_flight": final_in_flight
    }))
}

fn performance_request(
    binding: &BindingEnvelope,
    input: Vec<u8>,
    identity: u64,
    deadline_ms: u32,
) -> ExecuteRequest {
    ExecuteRequest {
        schema_version: "plugin-host-execute/v1".to_owned(),
        invocation_id: format!("perf-invocation-{identity}"),
        plugin_id: binding.plugin_id.clone(),
        binding_generation: binding.binding_generation,
        binding_epoch: binding.binding_epoch.clone(),
        capability_id: "plugin.transform.execute".to_owned(),
        input_digest: sha256_bytes(&input),
        input,
        deadline_ms,
        result_fence: format!("perf-fence-{identity}"),
        trace_id: format!("perf-trace-{identity}"),
        execution_mode: "active".to_owned(),
    }
}

async fn measure_matrix(
    state: Arc<HostState>,
    binding: BindingEnvelope,
    sequence: Arc<AtomicU64>,
) -> Result<Vec<serde_json::Value>, Box<dyn std::error::Error>> {
    let mut scenarios = Vec::new();
    for concurrency in [1usize, 2, 4, 8, 32] {
        for (batch_class, input_bytes, calls_per_trial) in [
            ("minimum", 128usize, 256usize),
            ("typical", 65_536usize, 128usize),
            ("maximum", 2_097_152usize, 16usize),
        ] {
            let input = Arc::new(vec![b'x'; input_bytes]);
            let trials = 3usize;
            let warmup_calls = 8usize;
            let calls = calls_per_trial.saturating_mul(trials);
            let latencies = Arc::new(Mutex::new(Vec::<u128>::with_capacity(calls)));
            let rejected = Arc::new(AtomicU64::new(0));
            for _ in 0..warmup_calls {
                let identity = sequence.fetch_add(1, Ordering::Relaxed);
                state
                    .execute(performance_request(
                        &binding,
                        input.as_ref().clone(),
                        identity,
                        10_000,
                    ))
                    .await?;
            }
            let mut elapsed = Duration::ZERO;
            for _ in 0..trials {
                let started = Instant::now();
                let mut tasks = tokio::task::JoinSet::new();
                for worker in 0..concurrency {
                    let state = state.clone();
                    let binding = binding.clone();
                    let input = input.clone();
                    let latencies = latencies.clone();
                    let rejected = rejected.clone();
                    let sequence = sequence.clone();
                    tasks.spawn(async move {
                        let worker_calls = calls_per_trial / concurrency
                            + usize::from(worker < calls_per_trial % concurrency);
                        for _ in 0..worker_calls {
                            let identity = sequence.fetch_add(1, Ordering::Relaxed);
                            let request = performance_request(
                                &binding,
                                input.as_ref().clone(),
                                identity,
                                10_000,
                            );
                            let call_started = Instant::now();
                            if state.execute(request).await.is_err() {
                                rejected.fetch_add(1, Ordering::Relaxed);
                            }
                            latencies
                                .lock()
                                .await
                                .push(call_started.elapsed().as_micros());
                        }
                    });
                }
                while let Some(result) = tasks.join_next().await {
                    result?;
                }
                elapsed = elapsed.saturating_add(started.elapsed());
            }
            let mut observed = latencies.lock().await.clone();
            observed.sort_unstable();
            scenarios.push(serde_json::json!({
                "runtime_profile": binding.runtime_profile.clone(),
                "concurrency": concurrency,
                "batch_class": batch_class,
                "input_bytes": input_bytes,
                "calls": calls,
                "trials": trials,
                "warmup_calls": warmup_calls,
                "admission_queue_execute_e2e_micros": {
                    "p50": percentile(&observed, 50),
                    "p95": percentile(&observed, 95),
                    "p99": percentile(&observed, 99),
                    "max": observed.last().copied().unwrap_or_default()
                },
                "throughput_per_second": calls as f64 / elapsed.as_secs_f64(),
                "rejected": rejected.load(Ordering::Relaxed),
                "timeouts": 0,
                "retries": 0,
                "elapsed_millis": elapsed.as_millis()
            }));
        }
    }
    Ok(scenarios)
}

fn percentile(values: &[u128], percentile: usize) -> u64 {
    if values.is_empty() {
        return 0;
    }
    let index = ((values.len() - 1) * percentile) / 100;
    u64::try_from(values[index]).unwrap_or(u64::MAX)
}

struct ProcessResources {
    rss_bytes: u64,
    fd_count: u64,
    threads: u64,
}

fn process_resources_for(pid: u32) -> Result<ProcessResources, Box<dyn std::error::Error>> {
    let status = std::fs::read_to_string(format!("/proc/{pid}/status"))?;
    let mut rss_bytes = 0;
    let mut threads = 0;
    for line in status.lines() {
        if let Some(value) = line.strip_prefix("VmRSS:") {
            rss_bytes = value
                .split_whitespace()
                .next()
                .ok_or("VmRSS value missing")?
                .parse::<u64>()?
                .saturating_mul(1024);
        } else if let Some(value) = line.strip_prefix("Threads:") {
            threads = value.trim().parse()?;
        }
    }
    let fd_count = u64::try_from(std::fs::read_dir(format!("/proc/{pid}/fd"))?.count())?;
    Ok(ProcessResources {
        rss_bytes,
        fd_count,
        threads,
    })
}

fn resource_json(resources: &ProcessResources) -> serde_json::Value {
    serde_json::json!({
        "rss_bytes": resources.rss_bytes,
        "fd_count": resources.fd_count,
        "threads": resources.threads
    })
}

async fn wait_for_socket(path: &Path) -> Result<(), Box<dyn std::error::Error>> {
    for _ in 0..100 {
        if path.exists() {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    Err("performance service UDS did not become ready".into())
}

fn service_binary() -> PathBuf {
    std::env::var_os("MASI_PLUGIN_SERVICE_FIXTURE_BINARY")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_BIN_EXE_masi-plugin-service-fixture")))
}

fn path_text(path: &Path) -> Result<&str, Box<dyn std::error::Error>> {
    path.to_str().ok_or_else(|| "non-UTF8 test path".into())
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

    fn terminate(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        let status = Command::new("kill")
            .arg("-TERM")
            .arg(self.child.id().to_string())
            .status()?;
        if !status.success() {
            return Err("failed to stop performance service".into());
        }
        for _ in 0..100 {
            if self.child.try_wait()?.is_some() {
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(25));
        }
        Err("performance service did not stop".into())
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
