//! Low-cardinality Host metrics with no plugin/run/artifact identity labels.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use crate::error::ReasonCode;

const LATENCY_BUCKETS_MS: [u64; 10] = [1, 5, 10, 25, 50, 100, 250, 500, 1000, 10_000];
const REASON_COUNT: usize = 25;

/// Process-local bounded metrics. Canonical plugin/statistics facts remain in Go/PostgreSQL.
pub struct HostMetrics {
    wasm_success: AtomicU64,
    wasm_failure: AtomicU64,
    service_success: AtomicU64,
    service_failure: AtomicU64,
    deduplicated: AtomicU64,
    cancellations: AtomicU64,
    circuit_opens: AtomicU64,
    revocations: AtomicU64,
    activation_failures: AtomicU64,
    wasm_bindings: AtomicU64,
    service_bindings: AtomicU64,
    active_connections: AtomicU64,
    peak_connections: AtomicU64,
    latency_buckets: [AtomicU64; LATENCY_BUCKETS_MS.len()],
    latency_count: AtomicU64,
    latency_sum_micros: AtomicU64,
    reasons: [AtomicU64; REASON_COUNT],
}

impl HostMetrics {
    /// Create empty metrics.
    #[must_use]
    pub fn new() -> Self {
        Self {
            wasm_success: AtomicU64::new(0),
            wasm_failure: AtomicU64::new(0),
            service_success: AtomicU64::new(0),
            service_failure: AtomicU64::new(0),
            deduplicated: AtomicU64::new(0),
            cancellations: AtomicU64::new(0),
            circuit_opens: AtomicU64::new(0),
            revocations: AtomicU64::new(0),
            activation_failures: AtomicU64::new(0),
            wasm_bindings: AtomicU64::new(0),
            service_bindings: AtomicU64::new(0),
            active_connections: AtomicU64::new(0),
            peak_connections: AtomicU64::new(0),
            latency_buckets: std::array::from_fn(|_| AtomicU64::new(0)),
            latency_count: AtomicU64::new(0),
            latency_sum_micros: AtomicU64::new(0),
            reasons: std::array::from_fn(|_| AtomicU64::new(0)),
        }
    }

    /// Count a completed invocation and bounded latency.
    pub fn record_invocation(
        &self,
        runtime: &str,
        outcome: &str,
        reason: ReasonCode,
        elapsed: Duration,
    ) {
        match (runtime, outcome) {
            ("wasm-component/v1", "succeeded") => &self.wasm_success,
            ("wasm-component/v1", _) => &self.wasm_failure,
            ("grpc-service/v1", "succeeded") => &self.service_success,
            _ => &self.service_failure,
        }
        .fetch_add(1, Ordering::Relaxed);
        self.reasons[reason_index(reason)].fetch_add(1, Ordering::Relaxed);
        let millis = u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX);
        for (index, bound) in LATENCY_BUCKETS_MS.iter().enumerate() {
            if millis <= *bound {
                self.latency_buckets[index].fetch_add(1, Ordering::Relaxed);
            }
        }
        self.latency_count.fetch_add(1, Ordering::Relaxed);
        self.latency_sum_micros.fetch_add(
            u64::try_from(elapsed.as_micros()).unwrap_or(u64::MAX),
            Ordering::Relaxed,
        );
    }

    /// Count an exact idempotent replay.
    pub fn record_deduplicated(&self) {
        self.deduplicated.fetch_add(1, Ordering::Relaxed);
    }

    /// Count a propagated cancellation.
    pub fn record_cancellation(&self) {
        self.cancellations.fetch_add(1, Ordering::Relaxed);
    }

    /// Count a bounded circuit opening.
    pub fn record_circuit_open(&self) {
        self.circuit_opens.fetch_add(1, Ordering::Relaxed);
    }

    /// Count a generation revocation.
    pub fn record_revocation(&self) {
        self.revocations.fetch_add(1, Ordering::Relaxed);
    }

    /// Count an admission/instantiation failure.
    pub fn record_activation_failure(&self, reason: ReasonCode) {
        self.activation_failures.fetch_add(1, Ordering::Relaxed);
        self.reasons[reason_index(reason)].fetch_add(1, Ordering::Relaxed);
    }

    /// Count a newly observed runtime by closed profile.
    pub fn binding_added(&self, runtime: &str) {
        match runtime {
            "wasm-component/v1" => &self.wasm_bindings,
            _ => &self.service_bindings,
        }
        .fetch_add(1, Ordering::Relaxed);
    }

    /// Observe one accepted Manager mTLS connection.
    pub fn connection_opened(&self) {
        let active = self.active_connections.fetch_add(1, Ordering::AcqRel) + 1;
        let _ = self
            .peak_connections
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |peak| {
                (active > peak).then_some(active)
            });
    }

    /// Observe one closed Manager connection.
    pub fn connection_closed(&self) {
        let _ =
            self.active_connections
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |active| {
                    active.checked_sub(1)
                });
    }

    /// Render bounded Prometheus text. No high-cardinality identity is accepted.
    #[must_use]
    pub fn render(&self, binding_count: usize, queued: u64, in_flight: u64, ready: bool) -> String {
        let mut output = String::with_capacity(8192);
        output.push_str("# TYPE masi_plugin_host_ready gauge\n");
        output.push_str(&format!("masi_plugin_host_ready {}\n", u8::from(ready)));
        output.push_str("# TYPE masi_plugin_host_bindings gauge\n");
        output.push_str(&format!("masi_plugin_host_bindings {}\n", binding_count));
        output.push_str("# TYPE masi_plugin_host_queued gauge\n");
        output.push_str(&format!("masi_plugin_host_queued {queued}\n"));
        output.push_str("# TYPE masi_plugin_host_in_flight gauge\n");
        output.push_str(&format!("masi_plugin_host_in_flight {in_flight}\n"));
        output.push_str("# TYPE masi_plugin_host_manager_connections gauge\n");
        output.push_str(&format!(
            "masi_plugin_host_manager_connections {}\n",
            self.active_connections.load(Ordering::Acquire)
        ));
        output.push_str("# TYPE masi_plugin_host_manager_connections_peak gauge\n");
        output.push_str(&format!(
            "masi_plugin_host_manager_connections_peak {}\n",
            self.peak_connections.load(Ordering::Acquire)
        ));
        output.push_str("# TYPE masi_plugin_host_invocations_total counter\n");
        for (runtime, outcome, counter) in [
            ("wasm-component/v1", "succeeded", &self.wasm_success),
            ("wasm-component/v1", "failed", &self.wasm_failure),
            ("grpc-service/v1", "succeeded", &self.service_success),
            ("grpc-service/v1", "failed", &self.service_failure),
        ] {
            output.push_str(&format!(
                "masi_plugin_host_invocations_total{{runtime=\"{runtime}\",outcome=\"{outcome}\"}} {}\n",
                counter.load(Ordering::Relaxed)
            ));
        }
        output.push_str("# TYPE masi_plugin_host_invocation_duration_seconds histogram\n");
        for (index, bound) in LATENCY_BUCKETS_MS.iter().enumerate() {
            output.push_str(&format!(
                "masi_plugin_host_invocation_duration_seconds_bucket{{le=\"{}\"}} {}\n",
                (*bound as f64) / 1000.0,
                self.latency_buckets[index].load(Ordering::Relaxed)
            ));
        }
        output.push_str(&format!(
            "masi_plugin_host_invocation_duration_seconds_count {}\nmasi_plugin_host_invocation_duration_seconds_sum {:.6}\n",
            self.latency_count.load(Ordering::Relaxed),
            self.latency_sum_micros.load(Ordering::Relaxed) as f64 / 1_000_000.0
        ));
        for (name, counter) in [
            ("deduplicated", &self.deduplicated),
            ("cancellations", &self.cancellations),
            ("circuit_opens", &self.circuit_opens),
            ("revocations", &self.revocations),
            ("activation_failures", &self.activation_failures),
        ] {
            output.push_str(&format!(
                "# TYPE masi_plugin_host_{name}_total counter\nmasi_plugin_host_{name}_total {}\n",
                counter.load(Ordering::Relaxed)
            ));
        }
        output.push_str(&process_metrics());
        output
    }
}

impl Default for HostMetrics {
    fn default() -> Self {
        Self::new()
    }
}

fn reason_index(reason: ReasonCode) -> usize {
    match reason {
        ReasonCode::Ok => 0,
        ReasonCode::UnknownVersion => 1,
        ReasonCode::UnknownKind => 2,
        ReasonCode::UnknownRuntimeProfile => 3,
        ReasonCode::DigestMismatch => 4,
        ReasonCode::PublisherUntrusted => 5,
        ReasonCode::Revoked => 6,
        ReasonCode::TrustStale => 7,
        ReasonCode::Fenced => 8,
        ReasonCode::CapabilityDenied => 9,
        ReasonCode::ResourceExhausted => 10,
        ReasonCode::WasmFuelExhausted => 11,
        ReasonCode::WasmEpochInterrupted => 12,
        ReasonCode::DeadlineExceeded => 13,
        ReasonCode::Cancelled => 14,
        ReasonCode::Draining => 15,
        ReasonCode::CircuitOpen => 16,
        ReasonCode::ServiceIdentityMismatch => 17,
        ReasonCode::PluginTrap => 18,
        ReasonCode::IdempotencyConflict => 19,
        ReasonCode::Unavailable => 20,
        ReasonCode::InvalidArgument => 21,
        ReasonCode::Internal => 22,
    }
}

fn process_metrics() -> String {
    let mut rss_bytes = 0u64;
    let mut threads = 0u64;
    if let Ok(status) = std::fs::read_to_string("/proc/self/status") {
        for line in status.lines() {
            if let Some(value) = line.strip_prefix("VmRSS:") {
                rss_bytes = value
                    .split_whitespace()
                    .next()
                    .and_then(|value| value.parse::<u64>().ok())
                    .unwrap_or(0)
                    .saturating_mul(1024);
            } else if let Some(value) = line.strip_prefix("Threads:") {
                threads = value.trim().parse::<u64>().unwrap_or(0);
            }
        }
    }
    let fd_count = std::fs::read_dir("/proc/self/fd")
        .ok()
        .map(|entries| entries.filter_map(Result::ok).count())
        .unwrap_or(0);
    format!(
        "# TYPE masi_plugin_host_process_rss_bytes gauge\nmasi_plugin_host_process_rss_bytes {rss_bytes}\n\
# TYPE masi_plugin_host_process_threads gauge\nmasi_plugin_host_process_threads {threads}\n\
# TYPE masi_plugin_host_process_fds gauge\nmasi_plugin_host_process_fds {fd_count}\n"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn labels_are_closed_and_low_cardinality() {
        let metrics = HostMetrics::new();
        metrics.record_invocation(
            "wasm-component/v1",
            "succeeded",
            ReasonCode::Ok,
            Duration::from_millis(2),
        );
        let text = metrics.render(1, 0, 0, true);
        assert!(text.contains("runtime=\"wasm-component/v1\""));
        assert!(!text.contains("plugin_id"));
        assert!(!text.contains("artifact_digest"));
    }
}
