//! Event-time final-window engine for qualified P4 aggregate snapshots.

use std::collections::{BTreeMap, BTreeSet};

use crate::{
    EdgeError, EdgeResult,
    contract::edge::{
        ClearAdvanceCondition, DataQuality, InferenceRecord, SnapshotConsistency,
        TelemetryFlowDirection, TelemetryIpVersion, TelemetrySnapshot,
    },
    digest,
};

/// Frozen first-release feature layout.
pub const FEATURE_DTYPE: &str = "uint64-le";
/// Packet/byte/nonzero/max/snapshot-count fields, in this exact order.
pub const FEATURE_WIDTH: usize = 6;

#[derive(Clone, Debug)]
struct OpenWindow {
    start_ms: i64,
    end_ms: i64,
    source_runtime_epoch: String,
    target_id: String,
    source_sequence_start: u64,
    source_sequence_end: u64,
    latest_source_wal_sequence: u64,
    packets: u64,
    bytes: u64,
    nonzero_cells: u64,
    max_cell_packets: u64,
    max_cell_bytes: u64,
    snapshots: u64,
    sampling_coverage_ppm: u32,
    valid: bool,
    reasons: BTreeSet<String>,
}

/// Finalization output and explicit non-inference evidence.
#[derive(Clone, Debug, Default)]
pub struct WindowAdvance {
    /// Final and valid windows admitted to inference.
    pub inference_records: Vec<InferenceRecord>,
    /// Number of records arriving after a final boundary.
    pub late_after_final: u64,
    /// Final windows withheld due to invalid/partial quality.
    pub invalid_final_windows: u64,
}

/// Per-source bounded event-time window state.
#[derive(Debug)]
pub struct WindowEngine {
    duration_ms: i64,
    allowed_lateness_ms: i64,
    idle_source_timeout_ms: i64,
    max_open: usize,
    max_event_time_ms: Option<i64>,
    last_ingest_time_ms: Option<i64>,
    finalized_through_ms: i64,
    open: BTreeMap<i64, OpenWindow>,
    late_after_final: u64,
    feature_contract_digest: String,
}

impl WindowEngine {
    /// Create a bounded `[start,end)` engine.
    pub fn new(
        duration_ms: u64,
        allowed_lateness_ms: u64,
        idle_source_timeout_ms: u64,
        max_open: usize,
        feature_contract_digest: String,
    ) -> EdgeResult<Self> {
        let duration_ms = i64::try_from(duration_ms)
            .map_err(|_| EdgeError::invalid("window_duration_ms", "exceeds i64"))?;
        let allowed_lateness_ms = i64::try_from(allowed_lateness_ms)
            .map_err(|_| EdgeError::invalid("allowed_lateness_ms", "exceeds i64"))?;
        let idle_source_timeout_ms = i64::try_from(idle_source_timeout_ms)
            .map_err(|_| EdgeError::invalid("idle_source_timeout_ms", "exceeds i64"))?;
        if duration_ms <= 0 || max_open == 0 || max_open > 64 {
            return Err(EdgeError::invalid(
                "window",
                "duration and max_open must be within frozen bounds",
            ));
        }
        if idle_source_timeout_ms <= 0 {
            return Err(EdgeError::invalid(
                "idle_source_timeout_ms",
                "must be positive",
            ));
        }
        digest::validate_sha256(&feature_contract_digest, "feature_contract_digest")?;
        Ok(Self {
            duration_ms,
            allowed_lateness_ms,
            idle_source_timeout_ms,
            max_open,
            max_event_time_ms: None,
            last_ingest_time_ms: None,
            finalized_through_ms: i64::MIN,
            open: BTreeMap::new(),
            late_after_final: 0,
            feature_contract_digest,
        })
    }

    /// Admit one source-WAL-durable snapshot and finalize by watermark.
    pub fn push(&mut self, snapshot: &TelemetrySnapshot, now_ms: i64) -> EdgeResult<WindowAdvance> {
        validate_snapshot(snapshot)?;
        if snapshot.allowed_lateness_ms
            != u64::try_from(self.allowed_lateness_ms).map_err(|_| {
                EdgeError::precondition("WINDOW_CONFIG_INVALID", "negative allowed lateness")
            })?
        {
            return Err(EdgeError::precondition(
                "WINDOW_PROFILE_MISMATCH",
                "snapshot allowed lateness differs from the frozen window profile",
            ));
        }
        let event_time = snapshot.export_time_unix_ms;
        let start = event_time.div_euclid(self.duration_ms) * self.duration_ms;
        let end = start
            .checked_add(self.duration_ms)
            .ok_or_else(|| EdgeError::invalid("window_end", "timestamp overflow"))?;
        if end <= self.finalized_through_ms {
            self.late_after_final = self.late_after_final.saturating_add(1);
            return Ok(WindowAdvance {
                late_after_final: 1,
                ..WindowAdvance::default()
            });
        }
        if !self.open.contains_key(&start) && self.open.len() == self.max_open {
            return Err(EdgeError::exhausted(
                "WINDOW_LIMIT_EXCEEDED",
                "maximum open event-time windows reached",
            ));
        }
        let quality_valid = snapshot.quality == "valid"
            && snapshot.quality_code == DataQuality::Valid as i32
            && (snapshot.quality_reasons.is_empty()
                || snapshot.quality_reasons.as_slice() == ["NONE"]);
        let window = self.open.entry(start).or_insert_with(|| OpenWindow {
            start_ms: start,
            end_ms: end,
            source_runtime_epoch: snapshot.source_runtime_epoch.clone(),
            target_id: snapshot.target_id.clone(),
            source_sequence_start: snapshot.source_sequence_start,
            source_sequence_end: snapshot.source_sequence_end,
            latest_source_wal_sequence: snapshot.source_wal_sequence,
            packets: 0,
            bytes: 0,
            nonzero_cells: 0,
            max_cell_packets: 0,
            max_cell_bytes: 0,
            snapshots: 0,
            sampling_coverage_ppm: 1_000_000,
            valid: true,
            reasons: BTreeSet::new(),
        });
        if window.target_id != snapshot.target_id
            || window.source_runtime_epoch != snapshot.source_runtime_epoch
        {
            return Err(EdgeError::precondition(
                "SOURCE_IDENTITY_DRIFT",
                "target or source runtime epoch changed inside a window",
            ));
        }
        window.source_sequence_start = window
            .source_sequence_start
            .min(snapshot.source_sequence_start);
        window.source_sequence_end = window.source_sequence_end.max(snapshot.source_sequence_end);
        window.latest_source_wal_sequence = window
            .latest_source_wal_sequence
            .max(snapshot.source_wal_sequence);
        window.packets = window.packets.saturating_add(snapshot.aggregate_packets);
        window.bytes = window.bytes.saturating_add(snapshot.aggregate_bytes);
        window.nonzero_cells = window
            .nonzero_cells
            .saturating_add(snapshot.cells.len() as u64);
        for cell in &snapshot.cells {
            window.max_cell_packets = window.max_cell_packets.max(cell.packets);
            window.max_cell_bytes = window.max_cell_bytes.max(cell.bytes);
        }
        window.snapshots = window.snapshots.saturating_add(1);
        let sampling_coverage = snapshot
            .sampling
            .as_ref()
            .map_or(0, |sampling| sampling.coverage_ppm);
        window.sampling_coverage_ppm = window.sampling_coverage_ppm.min(sampling_coverage);
        window.valid &= quality_valid;
        if !quality_valid {
            window
                .reasons
                .extend(snapshot.quality_reasons.iter().cloned());
        }
        self.max_event_time_ms = Some(
            self.max_event_time_ms
                .map_or(event_time, |current| current.max(event_time)),
        );
        self.last_ingest_time_ms = Some(now_ms);
        self.finalize(now_ms, false)
    }

    /// Advance idle-source time without synthesizing or zero-filling data.
    pub fn tick(&mut self, now_ms: i64) -> EdgeResult<WindowAdvance> {
        self.finalize(now_ms, true)
    }

    /// Total observed late-after-final records.
    #[must_use]
    pub fn late_after_final(&self) -> u64 {
        self.late_after_final
    }

    fn finalize(&mut self, now_ms: i64, allow_idle_advance: bool) -> EdgeResult<WindowAdvance> {
        let Some(max_event) = self.max_event_time_ms else {
            return Ok(WindowAdvance::default());
        };
        // Event-time watermark; wall time only stamps finalization and never
        // makes absent source records appear as zero-valued windows.
        let source_idle = allow_idle_advance
            && self.last_ingest_time_ms.is_some_and(|last_ingest| {
                now_ms.saturating_sub(last_ingest) >= self.idle_source_timeout_ms
            });
        let watermark_basis = if source_idle { now_ms } else { max_event };
        let watermark = watermark_basis.saturating_sub(self.allowed_lateness_ms);
        let final_starts: Vec<i64> = self
            .open
            .iter()
            .filter_map(|(start, window)| (window.end_ms <= watermark).then_some(*start))
            .collect();
        let mut advance = WindowAdvance::default();
        for start in final_starts {
            let window = self.open.remove(&start).ok_or_else(|| {
                EdgeError::precondition("WINDOW_STATE_LOST", "open window missing")
            })?;
            self.finalized_through_ms = self.finalized_through_ms.max(window.end_ms);
            if window.valid && window.snapshots > 0 {
                advance.inference_records.push(to_inference_record(
                    &window,
                    now_ms,
                    watermark,
                    &self.feature_contract_digest,
                )?);
            } else {
                advance.invalid_final_windows = advance.invalid_final_windows.saturating_add(1);
            }
        }
        Ok(advance)
    }
}

fn validate_snapshot(snapshot: &TelemetrySnapshot) -> EdgeResult<()> {
    if snapshot.schema_version != "telemetry-p4-window/v1" {
        return Err(EdgeError::UnknownMajor(snapshot.schema_version.clone()));
    }
    if snapshot.source_profile != "p4-bounded-aggregate-dual-bank/v1" {
        return Err(EdgeError::precondition(
            "SOURCE_PROFILE_MISMATCH",
            "unexpected telemetry source profile",
        ));
    }
    crate::digest::validate_identity(&snapshot.target_id, "target_id")?;
    crate::digest::validate_identity(&snapshot.source_runtime_epoch, "source_runtime_epoch")?;
    crate::digest::validate_sha256(&snapshot.source_profile_digest, "source_profile_digest")?;
    crate::digest::validate_identity(&snapshot.telemetry_source_id, "telemetry_source_id")?;
    crate::digest::validate_identity(&snapshot.window_id, "window_id")?;
    let fence = snapshot
        .fence
        .as_ref()
        .ok_or_else(|| EdgeError::invalid("telemetry_snapshot.fence", "fence is required"))?;
    let sampling = snapshot.sampling.as_ref().ok_or_else(|| {
        EdgeError::invalid(
            "telemetry_snapshot.sampling",
            "sampling evidence is required",
        )
    })?;
    let flow_profile = snapshot.flow_identity_profile.as_ref().ok_or_else(|| {
        EdgeError::invalid(
            "telemetry_snapshot.flow_identity_profile",
            "flow identity profile is required",
        )
    })?;
    let drops = snapshot.drops.as_ref().ok_or_else(|| {
        EdgeError::invalid("telemetry_snapshot.drops", "drop evidence is required")
    })?;
    let expected_hints = sampling
        .eligible_population
        .saturating_add(sampling.period_packets.saturating_sub(1))
        .checked_div(sampling.period_packets.max(1))
        .unwrap_or_default();
    let allowed_lateness_ms = i64::try_from(snapshot.allowed_lateness_ms).map_err(|_| {
        EdgeError::invalid(
            "telemetry_snapshot.allowed_lateness_ms",
            "exceeds signed timestamp range",
        )
    })?;
    let expected_watermark = snapshot
        .export_time_unix_ms
        .saturating_sub(allowed_lateness_ms);
    if snapshot.cells.len() > 256
        || snapshot.source_sequence_start > snapshot.source_sequence_end
        || snapshot.export_time_unix_ms <= 0
        || snapshot.ingest_time_unix_ms < snapshot.export_time_unix_ms
        || !snapshot.final_snapshot
        || snapshot.aggregation_end_unix_ms != snapshot.export_time_unix_ms
        || snapshot.watermark_unix_ms != expected_watermark
        || snapshot.role != "primary"
        || snapshot.observation_domain != format!("target:{}", snapshot.target_id)
        || snapshot.observation_point != "p4-ingress-pre-firewall"
        || snapshot.shard_id != snapshot.target_id
        || snapshot.capture_adapter_id != "p4runtime-bounded-aggregate"
        || snapshot.endpoint_identity != format!("p4runtime-device:{}", snapshot.device_id)
        || flow_profile.schema_version != "telemetry-flow-identity-profile/v1"
        || flow_profile.shard_id != snapshot.shard_id
        || flow_profile.capture_adapter_id != snapshot.capture_adapter_id
        || flow_profile.endpoint_identity != snapshot.endpoint_identity
        || flow_profile.direction != TelemetryFlowDirection::Ingress as i32
        || flow_profile.supported_ip_versions.as_slice() != [TelemetryIpVersion::Ipv4 as i32]
        || flow_profile.endpoint_ordering != "unidirectional-observation-order"
        || flow_profile.vlan_id_included
        || flow_profile.tunnel_id_included
        || flow_profile.fragment_semantics != "fragment-class-preserved"
        || flow_profile.l4_unavailable_semantics != "port-unavailable-with-absent-ports"
        || flow_profile.selector_algorithm != "p4-qualified-cell-selector/v1"
        || flow_profile.selector_seed_digest != snapshot.source_profile_digest
        || flow_profile.exposure != "aggregate-cell-selector-digest-only"
        || snapshot.application_generation == 0
        || snapshot.application_generation != fence.application_generation
        || snapshot.event_time_basis != "p4-aggregate-export-time"
        || snapshot.finalized_at_unix_ms < snapshot.export_time_unix_ms
        || snapshot.produced_at_unix_ms < snapshot.finalized_at_unix_ms
        || snapshot.expected_sequence_start != snapshot.source_sequence_start
        || snapshot.expected_sequence_end != snapshot.source_sequence_end
        || snapshot.observed_sequence_start != snapshot.source_sequence_start
        || snapshot.observed_sequence_end != snapshot.source_sequence_end
        || sampling.algorithm != "sequence-modulo"
        || sampling.period_packets != 1024
        || sampling.expected_hints != expected_hints
        || sampling.observed_hints > sampling.eligible_population
        || sampling.coverage_ppm > 1_000_000
        || sampling.selector != "source-sequence-modulo-period"
        || sampling.seed_digest != snapshot.source_profile_digest
        || sampling.weighting != "none-hint-only"
        || sampling.estimation_error_ppm != 1_000_000
        || sampling.complete_population_claim
        || !drops.edge_client_drop_measurable
        || (!drops.p4_source_drop_measurable && drops.p4_source_drops != 0)
        || (!drops.p4_server_drop_measurable && drops.p4_server_drops != 0)
        || (!drops.grpc_channel_drop_measurable && drops.grpc_channel_drops != 0)
        || snapshot.cell_count != 256
        || snapshot.reported_nonzero_cells != snapshot.cells.len() as u64
        || snapshot.snapshot_strategy != "freeze-flip-read-readback-clear"
        || snapshot.active_bank_after_flip > 1
        || snapshot.active_bank_after_flip == snapshot.frozen_bank
        || snapshot.sequence_before != snapshot.sequence_after
        || snapshot.expected_entries != 256
        || snapshot.observed_entries != 256
        || snapshot.snapshot_consistency != SnapshotConsistency::Stable as i32
        || snapshot.clear_advance_condition != ClearAdvanceCondition::ReadbackStable as i32
        || snapshot.event_time_min_unix_ms != snapshot.aggregation_start_unix_ms
        || snapshot.event_time_max_unix_ms != snapshot.aggregation_end_unix_ms
        || snapshot.packet_time_observed
        || snapshot.packet_time_semantics != "not-observed-by-p4-bounded-aggregate"
        || snapshot.monotonic_queue_age_ms > snapshot.monotonic_deadline_budget_ms
        || snapshot.monotonic_deadline_budget_ms == 0
    {
        return Err(EdgeError::invalid(
            "telemetry_snapshot",
            "cell, sequence, or timestamp bounds are invalid",
        ));
    }
    let quality = DataQuality::try_from(snapshot.quality_code).map_err(|_| {
        EdgeError::invalid("telemetry_snapshot.quality_code", "unknown data quality")
    })?;
    let quality_exact = matches!(
        (snapshot.quality.as_str(), quality),
        ("valid", DataQuality::Valid)
            | ("partial", DataQuality::Partial)
            | ("gap", DataQuality::Gap)
            | ("stale", DataQuality::Stale)
            | ("invalid", DataQuality::Invalid)
            | ("reset", DataQuality::Reset)
            | ("not-covered", DataQuality::NotCovered)
            | ("not-measurable", DataQuality::NotMeasurable)
    );
    if !quality_exact
        || (quality == DataQuality::Valid
            && (snapshot.aggregation_start_unix_ms <= 0
                || snapshot.aggregation_start_unix_ms > snapshot.aggregation_end_unix_ms
                || !snapshot.gaps.is_empty()))
        || (quality != DataQuality::Valid && snapshot.quality_reasons.is_empty())
    {
        return Err(EdgeError::precondition(
            "SNAPSHOT_QUALITY_MISMATCH",
            "typed/display quality, aggregation bounds, or gap evidence conflict",
        ));
    }
    let mut indices = BTreeSet::new();
    if snapshot.cells.iter().any(|cell| {
        cell.index > 255
            || !indices.insert(cell.index)
            || crate::digest::validate_sha256(&cell.selector_digest, "selector_digest").is_err()
    }) {
        return Err(EdgeError::invalid(
            "telemetry_snapshot.cells",
            "cell indices must be unique and in [0,255]",
        ));
    }
    Ok(())
}

fn to_inference_record(
    window: &OpenWindow,
    finalized_at_ms: i64,
    watermark_ms: i64,
    feature_contract_digest: &str,
) -> EdgeResult<InferenceRecord> {
    let features = [
        window.packets,
        window.bytes,
        window.nonzero_cells,
        window.max_cell_packets,
        window.max_cell_bytes,
        window.snapshots,
    ];
    let mut tensor = Vec::with_capacity(FEATURE_WIDTH * std::mem::size_of::<u64>());
    for value in features {
        tensor.extend_from_slice(&value.to_le_bytes());
    }
    let identity_material = format!(
        "{}\0{}\0{}\0{}\0{}\0{}",
        window.target_id,
        window.source_runtime_epoch,
        window.source_sequence_start,
        window.source_sequence_end,
        window.start_ms,
        window.end_ms
    );
    let input_id = format!(
        "input:{}",
        digest::sha256(identity_material.as_bytes())
            .strip_prefix("sha256:")
            .ok_or_else(|| EdgeError::precondition("DIGEST_INTERNAL", "sha256 prefix missing"))?
    );
    let window_id = format!(
        "window:{}",
        digest::sha256(format!("window\0{identity_material}").as_bytes())
            .strip_prefix("sha256:")
            .ok_or_else(|| EdgeError::precondition("DIGEST_INTERNAL", "sha256 prefix missing"))?
    );
    let mut record = InferenceRecord {
        input_id,
        event_idempotency_key: String::new(),
        target_id: window.target_id.clone(),
        source_runtime_epoch: window.source_runtime_epoch.clone(),
        source_sequence_start: window.source_sequence_start,
        source_sequence_end: window.source_sequence_end,
        window_start_unix_ms: window.start_ms,
        window_end_unix_ms: window.end_ms,
        finalized_at_unix_ms: finalized_at_ms,
        quality: "valid".into(),
        feature_tensor: tensor,
        shape: vec![1, FEATURE_WIDTH as u32],
        dtype: FEATURE_DTYPE.into(),
        input_digest: String::new(),
        source_wal_sequence: window.latest_source_wal_sequence,
        input_wal_sequence: 0,
        schema_version: "edge-inference-record/v1".into(),
        feature_contract_digest: feature_contract_digest.into(),
        final_window: true,
        watermark_unix_ms: watermark_ms,
        quality_code: DataQuality::Valid as i32,
        window_id,
        model_control_incarnation_id: String::new(),
        logical_pool_id: String::new(),
        pool_generation: 0,
        binding_generation: 0,
        route_epoch: 0,
        model_revision_digest: String::new(),
        label_contract_digest: String::new(),
        output_adapter_digest: String::new(),
        wire_profile: String::new(),
        runtime_profile: String::new(),
        quality_reasons: vec!["NONE".into()],
        sampling_coverage_ppm: window.sampling_coverage_ppm,
        enqueued_at_unix_ms: finalized_at_ms,
        operation_id: String::new(),
        scope: String::new(),
        expected_binding_generation: 0,
        proposed_binding_generation: 0,
        current_binding_generation: 0,
        startup_envelope_digest: String::new(),
        pool_observation_digest: String::new(),
        binding_digest: String::new(),
        model_bundle_digest: String::new(),
        wire_profile_digest: String::new(),
        runtime_profile_digest: String::new(),
        optimization_profile_digest: String::new(),
    };
    record.input_digest = canonical_input_record_digest(&record);
    Ok(record)
}

/// Canonical identity-and-feature digest used at the central inference fence.
#[must_use]
pub fn canonical_input_record_digest(record: &InferenceRecord) -> String {
    let identity_material = [
        record.schema_version.clone(),
        record.input_id.clone(),
        record.event_idempotency_key.clone(),
        record.window_id.clone(),
        record.target_id.clone(),
        record.source_runtime_epoch.clone(),
        record.source_sequence_start.to_string(),
        record.source_sequence_end.to_string(),
        record.window_start_unix_ms.to_string(),
        record.window_end_unix_ms.to_string(),
        record.feature_contract_digest.clone(),
        record.model_control_incarnation_id.clone(),
        record.logical_pool_id.clone(),
        record.pool_generation.to_string(),
        record.binding_generation.to_string(),
        record.route_epoch.to_string(),
        record.model_revision_digest.clone(),
        record.label_contract_digest.clone(),
        record.output_adapter_digest.clone(),
        record.wire_profile.clone(),
        record.runtime_profile.clone(),
        record.operation_id.clone(),
        record.scope.clone(),
        record.expected_binding_generation.to_string(),
        record.proposed_binding_generation.to_string(),
        record.current_binding_generation.to_string(),
        record.startup_envelope_digest.clone(),
        record.pool_observation_digest.clone(),
        record.binding_digest.clone(),
        record.model_bundle_digest.clone(),
        record.wire_profile_digest.clone(),
        record.runtime_profile_digest.clone(),
        record.optimization_profile_digest.clone(),
        record.dtype.clone(),
        record.sampling_coverage_ppm.to_string(),
    ]
    .join("\0");
    let mut material = identity_material.into_bytes();
    material.extend_from_slice(&record.feature_tensor);
    digest::sha256(&material)
}

#[cfg(test)]
mod tests {
    use crate::contract::edge::{
        DropEvidence, Fence, SamplingEvidence, TelemetryCell, TelemetryFlowIdentityProfile,
    };

    use super::*;

    fn snapshot(event_ms: i64, sequence: u64, quality: &str) -> TelemetrySnapshot {
        TelemetrySnapshot {
            schema_version: "telemetry-p4-window/v1".into(),
            source_profile: "p4-bounded-aggregate-dual-bank/v1".into(),
            source_profile_digest: format!("sha256:{}", "a".repeat(64)),
            telemetry_source_id: "source-1".into(),
            target_id: "target-1".into(),
            source_runtime_epoch: "source-epoch-1".into(),
            fence: Some(Fence {
                target_control_incarnation_id: "target-control-1".into(),
                target_assignment_generation: 1,
                actor_runtime_epoch: "source-epoch-1".into(),
                application_generation: 1,
                ..Fence::default()
            }),
            source_sequence_start: sequence,
            source_sequence_end: sequence,
            export_time_unix_ms: event_ms,
            ingest_time_unix_ms: event_ms + 1,
            cells: vec![TelemetryCell {
                index: 1,
                packets: 2,
                bytes: 100,
                selector_digest: format!("sha256:{}", "b".repeat(64)),
            }],
            aggregate_packets: 2,
            aggregate_bytes: 100,
            quality: quality.into(),
            quality_code: if quality == "valid" {
                DataQuality::Valid
            } else {
                DataQuality::Gap
            } as i32,
            aggregation_start_unix_ms: event_ms - 1,
            aggregation_end_unix_ms: event_ms,
            watermark_unix_ms: event_ms,
            final_snapshot: true,
            quality_reasons: if quality == "valid" {
                vec!["NONE".into()]
            } else {
                vec!["SEQUENCE_GAP".into()]
            },
            source_wal_sequence: sequence,
            role: "primary".into(),
            observation_domain: "target:target-1".into(),
            observation_point: "p4-ingress-pre-firewall".into(),
            application_generation: 1,
            window_id: format!("window-{sequence}"),
            event_time_basis: "p4-aggregate-export-time".into(),
            allowed_lateness_ms: 0,
            finalized_at_unix_ms: event_ms + 1,
            produced_at_unix_ms: event_ms + 1,
            expected_sequence_start: sequence,
            expected_sequence_end: sequence,
            observed_sequence_start: sequence,
            observed_sequence_end: sequence,
            sampling: Some(SamplingEvidence {
                algorithm: "sequence-modulo".into(),
                period_packets: 1024,
                eligible_population: 2,
                expected_hints: 1,
                observed_hints: 1,
                coverage_ppm: 1_000_000,
                selector: "source-sequence-modulo-period".into(),
                seed_digest: format!("sha256:{}", "a".repeat(64)),
                weighting: "none-hint-only".into(),
                estimation_error_ppm: 1_000_000,
                complete_population_claim: false,
            }),
            drops: Some(DropEvidence {
                edge_client_drop_measurable: true,
                ..DropEvidence::default()
            }),
            cell_count: 256,
            reported_nonzero_cells: 1,
            snapshot_strategy: "freeze-flip-read-readback-clear".into(),
            frozen_bank: 0,
            active_bank_after_flip: 1,
            sequence_before: 2,
            sequence_after: 2,
            expected_entries: 256,
            observed_entries: 256,
            snapshot_consistency: SnapshotConsistency::Stable as i32,
            clear_advance_condition: ClearAdvanceCondition::ReadbackStable as i32,
            shard_id: "target-1".into(),
            capture_adapter_id: "p4runtime-bounded-aggregate".into(),
            endpoint_identity: "p4runtime-device:0".into(),
            flow_identity_profile: Some(TelemetryFlowIdentityProfile {
                schema_version: "telemetry-flow-identity-profile/v1".into(),
                shard_id: "target-1".into(),
                capture_adapter_id: "p4runtime-bounded-aggregate".into(),
                endpoint_identity: "p4runtime-device:0".into(),
                direction: TelemetryFlowDirection::Ingress as i32,
                supported_ip_versions: vec![TelemetryIpVersion::Ipv4 as i32],
                endpoint_ordering: "unidirectional-observation-order".into(),
                fragment_semantics: "fragment-class-preserved".into(),
                l4_unavailable_semantics: "port-unavailable-with-absent-ports".into(),
                selector_algorithm: "p4-qualified-cell-selector/v1".into(),
                selector_seed_digest: format!("sha256:{}", "a".repeat(64)),
                exposure: "aggregate-cell-selector-digest-only".into(),
                ..TelemetryFlowIdentityProfile::default()
            }),
            event_time_min_unix_ms: event_ms - 1,
            event_time_max_unix_ms: event_ms,
            packet_time_observed: false,
            packet_time_semantics: "not-observed-by-p4-bounded-aggregate".into(),
            monotonic_queue_age_ms: 0,
            monotonic_deadline_budget_ms: 2_000,
            ..TelemetrySnapshot::default()
        }
    }

    #[test]
    fn only_final_valid_window_is_inferred() -> Result<(), Box<dyn std::error::Error>> {
        let mut engine = WindowEngine::new(100, 0, 1_000, 4, format!("sha256:{}", "f".repeat(64)))?;
        assert!(
            engine
                .push(&snapshot(10, 1, "valid"), 11)?
                .inference_records
                .is_empty()
        );
        let advance = engine.push(&snapshot(110, 2, "valid"), 111)?;
        assert_eq!(1, advance.inference_records.len());
        assert_eq!(FEATURE_DTYPE, advance.inference_records[0].dtype);
        assert_eq!(
            FEATURE_WIDTH * 8,
            advance.inference_records[0].feature_tensor.len()
        );
        Ok(())
    }

    #[test]
    fn invalid_window_is_withheld_without_zero_fill() -> Result<(), Box<dyn std::error::Error>> {
        let mut engine = WindowEngine::new(100, 0, 1_000, 4, format!("sha256:{}", "f".repeat(64)))?;
        engine.push(&snapshot(10, 1, "gap"), 11)?;
        let advance = engine.push(&snapshot(110, 2, "valid"), 111)?;
        assert!(advance.inference_records.is_empty());
        assert_eq!(1, advance.invalid_final_windows);
        Ok(())
    }

    #[test]
    fn late_after_final_never_reopens() -> Result<(), Box<dyn std::error::Error>> {
        let mut engine = WindowEngine::new(100, 0, 1_000, 4, format!("sha256:{}", "f".repeat(64)))?;
        engine.push(&snapshot(10, 1, "valid"), 11)?;
        engine.push(&snapshot(110, 2, "valid"), 111)?;
        let late = engine.push(&snapshot(20, 3, "valid"), 120)?;
        assert_eq!(1, late.late_after_final);
        assert!(late.inference_records.is_empty());
        Ok(())
    }

    #[test]
    fn idle_source_finalizes_observed_window_without_zero_fill()
    -> Result<(), Box<dyn std::error::Error>> {
        let mut engine = WindowEngine::new(100, 0, 1_000, 4, format!("sha256:{}", "f".repeat(64)))?;
        assert!(
            engine
                .push(&snapshot(10, 1, "valid"), 11)?
                .inference_records
                .is_empty()
        );
        assert!(engine.tick(1_010)?.inference_records.is_empty());
        let advance = engine.tick(1_011)?;
        assert_eq!(1, advance.inference_records.len());
        assert_eq!(2, advance.inference_records[0].feature_tensor[0]);
        assert!(engine.tick(2_011)?.inference_records.is_empty());
        Ok(())
    }
}
