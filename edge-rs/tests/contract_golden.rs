//! Executable protobuf-byte golden vectors for every first-release Edge boundary.

use std::{
    collections::BTreeSet,
    error::Error,
    fs,
    path::{Path, PathBuf},
};

use masi_edge::{
    EdgeError,
    contract::edge::{
        BaselineRule, CanonicalAck, CanonicalAckBatch, CanonicalCommitStatus,
        ClearAdvanceCondition, DataQuality, DropEvidence, EffectIntent, EffectKind, Fence,
        FirewallAction, InferenceDecision, InferenceExecutionStatus, InferenceInputBatch,
        InferenceRecord, InferenceResultBatch, InferenceResultRecord, InferenceRoute, Ipv4Prefix,
        OptionalUint32, P4WriteAtomicity, PipelineIdentity, ResumeRouteRequest, RouteWalRecord,
        RouteWalStage, SamplingEvidence, SnapshotConsistency, SourceWalRecord, SourceWalStage,
        SupplementalHint, TargetAssignment, TelemetryCell, TelemetryEndpoint,
        TelemetryFlowDirection, TelemetryFlowIdentity, TelemetryFlowIdentityProfile,
        TelemetryIpVersion, TelemetrySnapshot, TlsClientIdentity,
    },
    contract::p4::{
        DigestList, DigestListAck, PacketIn, PacketMetadata, StreamMessageRequest,
        StreamMessageResponse, stream_message_request, stream_message_response,
    },
    digest,
    error::{STABLE_REASON_CODES, STABLE_STATUS_REASON_CODES},
    inference::{
        canonical_ack_batch_digest, canonical_input_batch_digest, canonical_output_record_digest,
        canonical_result_batch_digest,
    },
    window::canonical_input_record_digest,
};
use prost::Message;
use serde_json::Value;
use tonic::Code;

fn hash(character: char) -> String {
    format!("sha256:{}", character.to_string().repeat(64))
}

fn fence() -> Fence {
    Fence {
        target_control_incarnation_id: "target-control-incarnation-golden-0001".into(),
        target_assignment_generation: 7,
        actor_runtime_epoch: "actor-runtime-golden-0001".into(),
        application_generation: 11,
        election_id_high: 0,
        election_id_low: 7_000_001,
    }
}

fn pipeline() -> PipelineIdentity {
    PipelineIdentity {
        p4runtime_api_version: "1.3.0".into(),
        p4info_digest: hash('a'),
        device_config_digest: hash('b'),
        profile_digest: hash('c'),
        cookie: 731_047_109,
        supported_write_atomicity: vec![P4WriteAtomicity::ContinueOnError as i32],
    }
}

fn route() -> InferenceRoute {
    InferenceRoute {
        schema_version: "inference-route/v1".into(),
        shard_id: "target-golden-0001".into(),
        model_control_incarnation_id: "model-control-incarnation-golden-0001".into(),
        logical_pool_id: "pool-cpu-golden".into(),
        pool_generation: 4,
        binding_generation: 9,
        route_epoch: 12,
        model_revision_digest: hash('d'),
        feature_contract_digest: hash('e'),
        label_contract_digest: hash('f'),
        output_adapter_digest: hash('1'),
        wire_profile: "inference-central-grpc-batch/v1".into(),
        runtime_profile: "model-runtime-central-cpu/v1".into(),
        endpoint: "https://127.0.0.1:7443".into(),
        tls: Some(TlsClientIdentity {
            server_name: "masi-central-inference.test".into(),
            identity_ref: "central-inference-golden".into(),
        }),
        operation_id: "rollout-operation-golden-0001".into(),
        scope: "scope-target-golden-0001".into(),
        expected_binding_generation: 8,
        proposed_binding_generation: 9,
        current_binding_generation: 9,
        startup_envelope_digest: hash('9'),
        pool_observation_digest: hash('0'),
        binding_digest: hash('a'),
        model_bundle_digest: hash('b'),
        wire_profile_digest: hash('c'),
        runtime_profile_digest: hash('d'),
        optimization_profile_digest: hash('e'),
    }
}

fn committed_binding_handshake() -> ResumeRouteRequest {
    let exact_route = route();
    ResumeRouteRequest {
        schema_version: "inference-committed-binding/v1".into(),
        shard_id: exact_route.shard_id,
        model_control_incarnation_id: exact_route.model_control_incarnation_id,
        route_epoch: exact_route.route_epoch,
        trace_id: "trace-edge-route-golden-0001".into(),
        operation_id: exact_route.operation_id,
        scope: exact_route.scope,
        logical_pool_id: exact_route.logical_pool_id,
        pool_generation: exact_route.pool_generation,
        expected_binding_generation: exact_route.expected_binding_generation,
        proposed_binding_generation: exact_route.proposed_binding_generation,
        current_binding_generation: exact_route.current_binding_generation,
        startup_envelope_digest: exact_route.startup_envelope_digest,
        pool_observation_digest: exact_route.pool_observation_digest,
        binding_digest: exact_route.binding_digest,
        deadline_unix_ms: 1_893_456_030_000,
    }
}

fn assignment() -> TargetAssignment {
    TargetAssignment {
        schema_version: "target-assignment/v1".into(),
        target_id: "target-golden-0001".into(),
        device_id: 1,
        role: "primary".into(),
        p4runtime_endpoint: "https://127.0.0.1:9559".into(),
        p4runtime_tls: Some(TlsClientIdentity {
            server_name: "masi-switch.test".into(),
            identity_ref: "p4runtime-golden".into(),
        }),
        fence: Some(fence()),
        lease_id: "target-lease-golden-0007".into(),
        issued_at_unix_ms: 1_893_456_000_000,
        expires_at_unix_ms: 1_893_456_060_000,
        election_floor: 7_000_000,
        election_ceiling: 7_999_999,
        expected_pipeline: Some(pipeline()),
        observation_epoch: 3,
        reset_epoch: 2,
        trace_id: "trace-edge-assignment-golden-0001".into(),
    }
}

fn effect() -> EffectIntent {
    EffectIntent {
        schema_version: "effect-intent-edge/v1".into(),
        effect_intent_id: "effect-golden-0001".into(),
        operation_id: "operation-golden-0001".into(),
        target_id: "target-golden-0001".into(),
        fence: Some(fence()),
        effect_digest: hash('2'),
        authorization_digest: hash('3'),
        kind: EffectKind::BaselineActivate as i32,
        policy_revision_digest: hash('4'),
        default_action: FirewallAction::PermitAndContinue as i32,
        baseline_rules: vec![BaselineRule {
            rule_id: "drop-ssh-from-test-net".into(),
            rule_revision: 1,
            canonical_rule_digest: hash('5'),
            priority: 100,
            ingress_port: Some(OptionalUint32 {
                present: true,
                value: 1,
            }),
            source: Some(Ipv4Prefix {
                address: 0xc000_0200,
                prefix_length: 24,
            }),
            destination: Some(Ipv4Prefix {
                address: 0xc633_640a,
                prefix_length: 32,
            }),
            protocol: Some(OptionalUint32 {
                present: true,
                value: 6,
            }),
            l4_present: Some(OptionalUint32 {
                present: true,
                value: 1,
            }),
            source_port: Some(OptionalUint32 {
                present: false,
                value: 0,
            }),
            destination_port: Some(OptionalUint32 {
                present: true,
                value: 22,
            }),
            fragment_class: Some(OptionalUint32 {
                present: true,
                value: 0,
            }),
            action: FirewallAction::Drop as i32,
        }],
        overlay_rules: Vec::new(),
        bounded_capture: None,
        deadline_unix_ms: 1_893_456_030_000,
        actor_ref: "oidc-subject-operator-golden-0002".into(),
        reason_code: "BASELINE_ACTIVATION".into(),
        trace_id: "trace-edge-effect-golden-0001".into(),
        required_write_atomicity: P4WriteAtomicity::ContinueOnError as i32,
    }
}

fn telemetry() -> TelemetrySnapshot {
    TelemetrySnapshot {
        schema_version: "telemetry-p4-window/v1".into(),
        source_profile: "p4-bounded-aggregate-dual-bank/v1".into(),
        source_profile_digest: hash('6'),
        telemetry_source_id: "p4-source-target-golden-0001".into(),
        target_id: "target-golden-0001".into(),
        device_id: 1,
        fence: Some(fence()),
        pipeline: Some(pipeline()),
        source_runtime_epoch: "actor-runtime-golden-0001".into(),
        frozen_bank: 0,
        epoch: 20,
        source_sequence_start: 101,
        source_sequence_end: 101,
        export_time_unix_ms: 1_893_456_010_000,
        ingest_time_unix_ms: 1_893_456_010_001,
        cells: vec![TelemetryCell {
            index: 3,
            packets: 10,
            bytes: 640,
            selector_digest: hash('7'),
        }],
        aggregate_packets: 10,
        aggregate_bytes: 640,
        quality: "valid".into(),
        quality_reasons: vec!["NONE".into()],
        source_wal_sequence: 33,
        quality_code: DataQuality::Valid as i32,
        aggregation_start_unix_ms: 1_893_456_000_000,
        aggregation_end_unix_ms: 1_893_456_010_000,
        watermark_unix_ms: 1_893_456_009_000,
        final_snapshot: true,
        gaps: Vec::new(),
        observation_epoch: 3,
        reset_epoch: 2,
        role: "primary".into(),
        observation_domain: "target:target-golden-0001".into(),
        observation_point: "p4-ingress-pre-firewall".into(),
        application_generation: 11,
        window_id: "window-golden-0001".into(),
        event_time_basis: "p4-aggregate-export-time".into(),
        allowed_lateness_ms: 1_000,
        finalized_at_unix_ms: 1_893_456_010_001,
        produced_at_unix_ms: 1_893_456_010_002,
        expected_sequence_start: 101,
        expected_sequence_end: 101,
        observed_sequence_start: 101,
        observed_sequence_end: 101,
        sampling: Some(SamplingEvidence {
            algorithm: "sequence-modulo".into(),
            period_packets: 1024,
            eligible_population: 10,
            expected_hints: 1,
            observed_hints: 1,
            coverage_ppm: 1_000_000,
            selector: "source-sequence-modulo-period".into(),
            seed_digest: hash('6'),
            weighting: "none-hint-only".into(),
            estimation_error_ppm: 1_000_000,
            complete_population_claim: false,
        }),
        drops: Some(DropEvidence {
            edge_client_drop_measurable: true,
            digest_hints_observed: 1,
            ..DropEvidence::default()
        }),
        cell_count: 256,
        reported_nonzero_cells: 1,
        snapshot_strategy: "freeze-flip-read-readback-clear".into(),
        active_bank_after_flip: 1,
        sequence_before: 10,
        sequence_after: 10,
        expected_entries: 256,
        observed_entries: 256,
        snapshot_consistency: SnapshotConsistency::Stable as i32,
        clear_advance_condition: ClearAdvanceCondition::ReadbackStable as i32,
        shard_id: "target-golden-0001".into(),
        capture_adapter_id: "p4runtime-bounded-aggregate".into(),
        endpoint_identity: "p4runtime-device:1".into(),
        flow_identity_profile: Some(TelemetryFlowIdentityProfile {
            schema_version: "telemetry-flow-identity-profile/v1".into(),
            shard_id: "target-golden-0001".into(),
            capture_adapter_id: "p4runtime-bounded-aggregate".into(),
            endpoint_identity: "p4runtime-device:1".into(),
            direction: TelemetryFlowDirection::Ingress as i32,
            supported_ip_versions: vec![TelemetryIpVersion::Ipv4 as i32],
            endpoint_ordering: "unidirectional-observation-order".into(),
            vlan_id_included: false,
            tunnel_id_included: false,
            fragment_semantics: "fragment-class-preserved".into(),
            l4_unavailable_semantics: "port-unavailable-with-absent-ports".into(),
            selector_algorithm: "p4-qualified-cell-selector/v1".into(),
            selector_seed_digest: hash('6'),
            exposure: "aggregate-cell-selector-digest-only".into(),
        }),
        event_time_min_unix_ms: 1_893_456_000_000,
        event_time_max_unix_ms: 1_893_456_010_000,
        packet_time_observed: false,
        packet_time_semantics: "not-observed-by-p4-bounded-aggregate".into(),
        monotonic_queue_age_ms: 0,
        monotonic_deadline_budget_ms: 2_000,
    }
}

fn inference_record() -> InferenceRecord {
    let exact_route = route();
    let mut feature_tensor = Vec::new();
    for value in [10_u64, 640, 1, 10, 640, 1] {
        feature_tensor.extend_from_slice(&value.to_le_bytes());
    }
    let mut record = InferenceRecord {
        input_id: "input-golden-0001".into(),
        event_idempotency_key: "event-golden-0001".into(),
        target_id: exact_route.shard_id.clone(),
        source_runtime_epoch: "actor-runtime-golden-0001".into(),
        source_sequence_start: 101,
        source_sequence_end: 101,
        window_start_unix_ms: 1_893_456_000_000,
        window_end_unix_ms: 1_893_456_010_000,
        finalized_at_unix_ms: 1_893_456_011_000,
        quality: "valid".into(),
        feature_tensor,
        shape: vec![1, 6],
        dtype: "uint64-le".into(),
        input_digest: String::new(),
        source_wal_sequence: 33,
        input_wal_sequence: 21,
        schema_version: "edge-inference-record/v1".into(),
        feature_contract_digest: exact_route.feature_contract_digest.clone(),
        final_window: true,
        watermark_unix_ms: 1_893_456_010_000,
        quality_code: DataQuality::Valid as i32,
        window_id: "window-golden-0001".into(),
        model_control_incarnation_id: exact_route.model_control_incarnation_id,
        logical_pool_id: exact_route.logical_pool_id,
        pool_generation: exact_route.pool_generation,
        binding_generation: exact_route.binding_generation,
        route_epoch: exact_route.route_epoch,
        model_revision_digest: exact_route.model_revision_digest,
        label_contract_digest: exact_route.label_contract_digest,
        output_adapter_digest: exact_route.output_adapter_digest,
        wire_profile: exact_route.wire_profile,
        runtime_profile: exact_route.runtime_profile,
        quality_reasons: vec!["NONE".into()],
        sampling_coverage_ppm: 1_000_000,
        enqueued_at_unix_ms: 1_893_456_011_000,
        operation_id: exact_route.operation_id,
        scope: exact_route.scope,
        expected_binding_generation: exact_route.expected_binding_generation,
        proposed_binding_generation: exact_route.proposed_binding_generation,
        current_binding_generation: exact_route.current_binding_generation,
        startup_envelope_digest: exact_route.startup_envelope_digest,
        pool_observation_digest: exact_route.pool_observation_digest,
        binding_digest: exact_route.binding_digest,
        model_bundle_digest: exact_route.model_bundle_digest,
        wire_profile_digest: exact_route.wire_profile_digest,
        runtime_profile_digest: exact_route.runtime_profile_digest,
        optimization_profile_digest: exact_route.optimization_profile_digest,
    };
    record.input_digest = canonical_input_record_digest(&record);
    record
}

fn inference_input_batch() -> InferenceInputBatch {
    let mut batch = InferenceInputBatch {
        schema_version: "inference-central-grpc-batch/v1".into(),
        request_id: "request-golden-0001".into(),
        route: Some(route()),
        records: vec![inference_record()],
        deadline_unix_ms: 1_893_456_015_000,
        attempt: 1,
        batch_digest: String::new(),
        trace_id: "trace-inference-request-golden-0001".into(),
    };
    batch.batch_digest = canonical_input_batch_digest(&batch);
    batch
}

fn inference_result_record() -> InferenceResultRecord {
    let source = inference_record();
    let mut record = InferenceResultRecord {
        input_id: source.input_id.clone(),
        event_idempotency_key: source.event_idempotency_key.clone(),
        input_digest: source.input_digest.clone(),
        output_digest: String::new(),
        scores: vec![0.1, 0.9],
        predicted_label: 1,
        decision: "alert".into(),
        out_of_distribution: false,
        abstain: false,
        quality: "valid".into(),
        status: "ok".into(),
        error_code: "NONE".into(),
        worker_id: "worker-cpu-golden-0001".into(),
        worker_digest: hash('8'),
        source_wal_sequence: source.source_wal_sequence,
        input_wal_sequence: source.input_wal_sequence,
        result_wal_sequence: 41,
        decision_code: InferenceDecision::Alert as i32,
        quality_code: DataQuality::Valid as i32,
        execution_status: InferenceExecutionStatus::Ok as i32,
        window_id: source.window_id.clone(),
        model_control_incarnation_id: source.model_control_incarnation_id.clone(),
        logical_pool_id: source.logical_pool_id.clone(),
        pool_generation: source.pool_generation,
        binding_generation: source.binding_generation,
        route_epoch: source.route_epoch,
        model_revision_digest: source.model_revision_digest.clone(),
        feature_contract_digest: source.feature_contract_digest.clone(),
        label_contract_digest: source.label_contract_digest.clone(),
        output_adapter_digest: source.output_adapter_digest.clone(),
        wire_profile: source.wire_profile.clone(),
        runtime_profile: source.runtime_profile.clone(),
        source_runtime_epoch: source.source_runtime_epoch.clone(),
        source_sequence_start: source.source_sequence_start,
        source_sequence_end: source.source_sequence_end,
        window_start_unix_ms: source.window_start_unix_ms,
        window_end_unix_ms: source.window_end_unix_ms,
        finalized_at_unix_ms: source.finalized_at_unix_ms,
        target_id: source.target_id.clone(),
        sampling_coverage_ppm: source.sampling_coverage_ppm,
        operation_id: source.operation_id.clone(),
        scope: source.scope.clone(),
        expected_binding_generation: source.expected_binding_generation,
        proposed_binding_generation: source.proposed_binding_generation,
        current_binding_generation: source.current_binding_generation,
        startup_envelope_digest: source.startup_envelope_digest.clone(),
        pool_observation_digest: source.pool_observation_digest.clone(),
        binding_digest: source.binding_digest.clone(),
        model_bundle_digest: source.model_bundle_digest.clone(),
        wire_profile_digest: source.wire_profile_digest.clone(),
        runtime_profile_digest: source.runtime_profile_digest.clone(),
        optimization_profile_digest: source.optimization_profile_digest.clone(),
        worker_attempt_id: "worker-attempt-golden-0001".into(),
        inference_started_at_unix_ms: 1_893_456_011_001,
        inference_completed_at_unix_ms: 1_893_456_011_002,
        trace_id: "trace-inference-request-golden-0001".into(),
    };
    record.output_digest = canonical_output_record_digest(&record);
    record
}

fn inference_result_batch() -> InferenceResultBatch {
    let input = inference_input_batch();
    let mut batch = InferenceResultBatch {
        schema_version: "inference-central-grpc-batch/v1".into(),
        request_id: input.request_id,
        route: input.route,
        records: vec![inference_result_record()],
        batch_digest: String::new(),
        trace_id: "trace-inference-result-golden-0001".into(),
    };
    batch.batch_digest = canonical_result_batch_digest(&batch);
    batch
}

fn canonical_ack_batch() -> CanonicalAckBatch {
    let result = inference_result_batch();
    let record = inference_result_record();
    let mut batch = CanonicalAckBatch {
        schema_version: "canonical-event-ack/v1".into(),
        acknowledgements: vec![CanonicalAck {
            event_idempotency_key: record.event_idempotency_key,
            input_digest: record.input_digest,
            output_digest: record.output_digest,
            canonical_event_id: "canonical-event-golden-0001".into(),
            committed_at_unix_ms: 1_893_456_020_000,
            status: "committed".into(),
            reason_code: "POSTGRESQL_EVENT_COMMITTED".into(),
            commit_status: CanonicalCommitStatus::Committed as i32,
        }],
        trace_id: "trace-canonical-ack-golden-0001".into(),
        result_batch_digest: result.batch_digest,
        ack_batch_digest: String::new(),
    };
    batch.ack_batch_digest = canonical_ack_batch_digest(&batch);
    batch
}

fn telemetry_flow_ipv4() -> TelemetryFlowIdentity {
    TelemetryFlowIdentity {
        direction: TelemetryFlowDirection::Ingress as i32,
        ip_version: TelemetryIpVersion::Ipv4 as i32,
        first_endpoint: Some(TelemetryEndpoint {
            address: vec![192, 0, 2, 1],
            port: Some(OptionalUint32 {
                present: true,
                value: 12_345,
            }),
        }),
        second_endpoint: Some(TelemetryEndpoint {
            address: vec![198, 51, 100, 2],
            port: Some(OptionalUint32 {
                present: true,
                value: 443,
            }),
        }),
        protocol: 6,
        vlan_id: Some(OptionalUint32 {
            present: true,
            value: 100,
        }),
        tunnel_id: Vec::new(),
        fragment_class: 0,
        port_unavailable: false,
    }
}

fn telemetry_flow_ipv6() -> TelemetryFlowIdentity {
    TelemetryFlowIdentity {
        direction: TelemetryFlowDirection::Egress as i32,
        ip_version: TelemetryIpVersion::Ipv6 as i32,
        first_endpoint: Some(TelemetryEndpoint {
            address: vec![0x20, 0x01, 0x0d, 0xb8, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],
            port: Some(OptionalUint32 {
                present: true,
                value: 53,
            }),
        }),
        second_endpoint: Some(TelemetryEndpoint {
            address: vec![0x20, 0x01, 0x0d, 0xb8, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2],
            port: Some(OptionalUint32 {
                present: true,
                value: 53_000,
            }),
        }),
        protocol: 17,
        vlan_id: Some(OptionalUint32 {
            present: false,
            value: 0,
        }),
        tunnel_id: vec![0, 0, 0, 42],
        fragment_class: 0,
        port_unavailable: false,
    }
}

fn telemetry_flow_fragment_without_l4() -> TelemetryFlowIdentity {
    TelemetryFlowIdentity {
        direction: TelemetryFlowDirection::Ingress as i32,
        ip_version: TelemetryIpVersion::Ipv4 as i32,
        first_endpoint: Some(TelemetryEndpoint {
            address: vec![203, 0, 113, 10],
            port: Some(OptionalUint32 {
                present: false,
                value: 0,
            }),
        }),
        second_endpoint: Some(TelemetryEndpoint {
            address: vec![203, 0, 113, 20],
            port: Some(OptionalUint32 {
                present: false,
                value: 0,
            }),
        }),
        protocol: 6,
        vlan_id: Some(OptionalUint32 {
            present: false,
            value: 0,
        }),
        tunnel_id: Vec::new(),
        fragment_class: 2,
        port_unavailable: true,
    }
}

fn route_wal_record() -> RouteWalRecord {
    RouteWalRecord {
        schema_version: "edge-route-wal/v1".into(),
        stage: RouteWalStage::CanonicalActive as i32,
        route: Some(route()),
        proposed_route_epoch: 12,
        recorded_at_unix_ms: 1_893_456_012_000,
        reason_code: "GO_CANONICAL_BINDING_COMMITTED".into(),
        trace_id: "trace-edge-route-golden-0001".into(),
        committed_binding_handshake: Some(committed_binding_handshake()),
    }
}

fn source_hint_record() -> SourceWalRecord {
    SourceWalRecord {
        schema_version: "edge-source-wal/v1".into(),
        stage: SourceWalStage::DigestHintDurable as i32,
        hint: Some(SupplementalHint {
            target_id: "target-golden-0001".into(),
            source_runtime_epoch: "actor-runtime-golden-0001".into(),
            kind: 1,
            digest_id: 17,
            list_id: 42,
            payload_digest: hash('7'),
            payload_bytes: 31,
            received_at_unix_ms: 1_893_456_013_000,
        }),
        recorded_at_unix_ms: 1_893_456_013_001,
        reason_code: "DIGEST_HINT_DURABLE".into(),
        source_runtime_epoch: "actor-runtime-golden-0001".into(),
        ..SourceWalRecord::default()
    }
}

fn p4_digest_response() -> StreamMessageResponse {
    StreamMessageResponse {
        update: Some(stream_message_response::Update::Digest(DigestList {
            digest_id: 17,
            list_id: 42,
            data: vec![b"flow-key".to_vec(), b"sample-only".to_vec()],
            timestamp: 1_893_456_013_000,
        })),
    }
}

fn p4_digest_ack_request() -> StreamMessageRequest {
    StreamMessageRequest {
        update: Some(stream_message_request::Update::DigestAck(DigestListAck {
            digest_id: 17,
            list_id: 42,
        })),
    }
}

fn p4_packet_response() -> StreamMessageResponse {
    StreamMessageResponse {
        update: Some(stream_message_response::Update::Packet(PacketIn {
            payload: vec![0x45, 0x00, 0x00, 0x14],
            metadata: vec![PacketMetadata {
                metadata_id: 1,
                value: 7_u32.to_be_bytes().to_vec(),
            }],
        })),
    }
}

fn assert_vector<M: Message>(
    file_name: &str,
    message_type: &str,
    message: &M,
) -> Result<(), Box<dyn Error>> {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../contracts/golden/edge")
        .join(file_name);
    let document: Value = serde_json::from_str(&fs::read_to_string(path)?)?;
    let schema = document
        .get("schema_version")
        .and_then(Value::as_str)
        .ok_or("golden schema_version missing")?;
    let actual_type = document
        .get("protobuf_message")
        .and_then(Value::as_str)
        .ok_or("golden protobuf_message missing")?;
    let expected_hex = document
        .get("protobuf_hex")
        .and_then(Value::as_str)
        .ok_or("golden protobuf_hex missing")?;
    let expected_sha256 = document
        .get("protobuf_sha256")
        .and_then(Value::as_str)
        .ok_or("golden protobuf_sha256 missing")?;
    assert_eq!("edge-contract-golden/v1", schema);
    assert_eq!(message_type, actual_type);
    let bytes = message.encode_to_vec();
    assert_eq!(
        (expected_hex.to_owned(), expected_sha256.to_owned()),
        (hex::encode(&bytes), digest::sha256(&bytes))
    );
    Ok(())
}

fn grpc_name(code: Code) -> &'static str {
    match code {
        Code::InvalidArgument => "INVALID_ARGUMENT",
        Code::PermissionDenied => "PERMISSION_DENIED",
        Code::Unauthenticated => "UNAUTHENTICATED",
        Code::ResourceExhausted => "RESOURCE_EXHAUSTED",
        Code::FailedPrecondition => "FAILED_PRECONDITION",
        Code::DeadlineExceeded => "DEADLINE_EXCEEDED",
        Code::Unavailable => "UNAVAILABLE",
        Code::Aborted => "ABORTED",
        _ => "UNEXPECTED",
    }
}

fn first_quoted_literal(source: &str) -> Option<&str> {
    let start = source.find('"')?.saturating_add(1);
    let end = source.get(start..)?.find('"')?.saturating_add(start);
    source.get(start..end)
}

fn quoted_literals(source: &str) -> Vec<&str> {
    let mut remaining = source;
    let mut result = Vec::new();
    while let Some(value) = first_quoted_literal(remaining) {
        result.push(value);
        let Some(position) = remaining.find(value) else {
            break;
        };
        let next = position.saturating_add(value.len()).saturating_add(1);
        let Some(tail) = remaining.get(next..) else {
            break;
        };
        remaining = tail;
    }
    result
}

fn stable_code_literal(value: &str) -> bool {
    !value.is_empty()
        && value.bytes().any(|byte| byte.is_ascii_uppercase())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_')
}

fn production_sources() -> [&'static str; 11] {
    [
        include_str!("../src/actor.rs"),
        include_str!("../src/config.rs"),
        include_str!("../src/endpoint.rs"),
        include_str!("../src/firewall.rs"),
        include_str!("../src/inference.rs"),
        include_str!("../src/main.rs"),
        include_str!("../src/p4runtime.rs"),
        include_str!("../src/server.rs"),
        include_str!("../src/supervisor.rs"),
        include_str!("../src/wal.rs"),
        include_str!("../src/window.rs"),
    ]
}

fn source_literal_reason_codes() -> BTreeSet<&'static str> {
    let mut result = BTreeSet::new();
    for source in production_sources() {
        for marker in ["EdgeError::precondition(", "EdgeError::exhausted("] {
            for tail in source.split(marker).skip(1) {
                if let Some(value) = first_quoted_literal(tail.trim_start())
                    && stable_code_literal(value)
                {
                    result.insert(value);
                }
            }
        }
        for tail in source.split("EdgeError::Remote {").skip(1) {
            let Some(code_body) = tail
                .split_once("code:")
                .and_then(|(_, value)| value.split_once("message:"))
                .map(|(value, _)| value)
            else {
                continue;
            };
            for value in quoted_literals(code_body) {
                if stable_code_literal(value) {
                    result.insert(value);
                }
            }
        }
    }
    result
}

fn source_literal_status_codes() -> BTreeSet<&'static str> {
    let mut result = BTreeSet::new();
    for source in production_sources() {
        for tail in source.split("reason_code:").skip(1) {
            let bounded = tail.get(..tail.len().min(512)).unwrap_or(tail);
            for value in quoted_literals(bounded) {
                if stable_code_literal(value) {
                    result.insert(value);
                }
            }
        }
    }
    result
}

fn source_uppercase_literals() -> BTreeSet<&'static str> {
    production_sources()
        .into_iter()
        .flat_map(quoted_literals)
        .filter(|value| stable_code_literal(value))
        .collect()
}

#[test]
fn assignment_bytes_are_frozen() -> Result<(), Box<dyn Error>> {
    assert_vector(
        "assignment-v1.json",
        "masi.edge.v1.TargetAssignment",
        &assignment(),
    )
}

#[test]
fn target_assignment_json_adapter_is_exact() -> Result<(), Box<dyn Error>> {
    let assignment = assignment();
    let tls = assignment
        .p4runtime_tls
        .as_ref()
        .ok_or("golden assignment TLS identity is missing")?;
    let fence = assignment
        .fence
        .as_ref()
        .ok_or("golden assignment fence is missing")?;
    let pipeline = assignment
        .expected_pipeline
        .as_ref()
        .ok_or("golden assignment pipeline is missing")?;
    let supported_atomicity = pipeline
        .supported_write_atomicity
        .iter()
        .map(|value| match P4WriteAtomicity::try_from(*value) {
            Ok(P4WriteAtomicity::ContinueOnError) => Ok("CONTINUE_ON_ERROR"),
            Ok(other) => Err(format!("unsupported golden atomicity {other:?}")),
            Err(error) => Err(format!("invalid golden atomicity: {error}")),
        })
        .collect::<Result<Vec<_>, _>>()?;
    let adapted = serde_json::json!({
        "schema_version": assignment.schema_version,
        "target_id": assignment.target_id,
        "device_id": assignment.device_id,
        "role": assignment.role,
        "p4runtime_endpoint": assignment.p4runtime_endpoint,
        "p4runtime_tls": {
            "server_name": tls.server_name,
            "identity_ref": tls.identity_ref
        },
        "fence": {
            "target_control_incarnation_id": fence.target_control_incarnation_id,
            "target_assignment_generation": fence.target_assignment_generation,
            "actor_runtime_epoch": fence.actor_runtime_epoch,
            "application_generation": fence.application_generation,
            "election_id_high": fence.election_id_high,
            "election_id_low": fence.election_id_low
        },
        "lease_id": assignment.lease_id,
        "issued_at_unix_ms": assignment.issued_at_unix_ms,
        "expires_at_unix_ms": assignment.expires_at_unix_ms,
        "election_floor": assignment.election_floor,
        "election_ceiling": assignment.election_ceiling,
        "expected_pipeline": {
            "p4runtime_api_version": pipeline.p4runtime_api_version,
            "p4info_digest": pipeline.p4info_digest,
            "device_config_digest": pipeline.device_config_digest,
            "profile_digest": pipeline.profile_digest,
            "cookie": pipeline.cookie,
            "supported_write_atomicity": supported_atomicity
        },
        "observation_epoch": assignment.observation_epoch,
        "reset_epoch": assignment.reset_epoch,
        "trace_id": assignment.trace_id
    });
    let golden_path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../contracts/golden/target/assignment-v1.json");
    let expected: Value = serde_json::from_slice(&fs::read(golden_path)?)?;
    assert_eq!(expected, adapted);
    Ok(())
}

#[test]
fn effect_bytes_are_frozen() -> Result<(), Box<dyn Error>> {
    assert_vector("effect-v1.json", "masi.edge.v1.EffectIntent", &effect())
}

#[test]
fn telemetry_bytes_are_frozen() -> Result<(), Box<dyn Error>> {
    assert_vector(
        "telemetry-v1.json",
        "masi.edge.v1.TelemetrySnapshot",
        &telemetry(),
    )
}

#[test]
fn inference_record_bytes_are_frozen() -> Result<(), Box<dyn Error>> {
    assert_vector(
        "inference-v1.json",
        "masi.edge.v1.InferenceRecord",
        &inference_record(),
    )
}

#[test]
fn inference_input_batch_bytes_are_frozen() -> Result<(), Box<dyn Error>> {
    assert_vector(
        "inference-input-batch-v1.json",
        "masi.edge.v1.InferenceInputBatch",
        &inference_input_batch(),
    )
}

#[test]
fn inference_result_batch_bytes_are_frozen() -> Result<(), Box<dyn Error>> {
    assert_vector(
        "inference-result-batch-v1.json",
        "masi.edge.v1.InferenceResultBatch",
        &inference_result_batch(),
    )
}

#[test]
fn canonical_ack_batch_bytes_are_frozen() -> Result<(), Box<dyn Error>> {
    assert_vector(
        "canonical-ack-batch-v1.json",
        "masi.edge.v1.CanonicalAckBatch",
        &canonical_ack_batch(),
    )
}

#[test]
fn telemetry_flow_ipv4_bytes_are_frozen() -> Result<(), Box<dyn Error>> {
    assert_vector(
        "telemetry-flow-ipv4-v1.json",
        "masi.edge.v1.TelemetryFlowIdentity",
        &telemetry_flow_ipv4(),
    )
}

#[test]
fn telemetry_flow_ipv6_bytes_are_frozen() -> Result<(), Box<dyn Error>> {
    assert_vector(
        "telemetry-flow-ipv6-v1.json",
        "masi.edge.v1.TelemetryFlowIdentity",
        &telemetry_flow_ipv6(),
    )
}

#[test]
fn telemetry_fragment_without_l4_bytes_are_frozen() -> Result<(), Box<dyn Error>> {
    let fragment = telemetry_flow_fragment_without_l4();
    assert!(fragment.port_unavailable);
    assert!(fragment.fragment_class > 0);
    assert!(
        fragment
            .first_endpoint
            .as_ref()
            .and_then(|endpoint| endpoint.port.as_ref())
            .is_some_and(|port| !port.present)
    );
    assert!(
        fragment
            .second_endpoint
            .as_ref()
            .and_then(|endpoint| endpoint.port.as_ref())
            .is_some_and(|port| !port.present)
    );
    assert_vector(
        "telemetry-flow-fragment-no-l4-v1.json",
        "masi.edge.v1.TelemetryFlowIdentity",
        &fragment,
    )
}

#[test]
fn telemetry_schema_freezes_identity_time_and_sampling_semantics() -> Result<(), Box<dyn Error>> {
    let path =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../contracts/telemetry/v1/schema.json");
    let document: Value = serde_json::from_str(&fs::read_to_string(path)?)?;
    let required: BTreeSet<&str> = document
        .get("required")
        .and_then(Value::as_array)
        .ok_or("telemetry required fields missing")?
        .iter()
        .map(|value| value.as_str().ok_or("required field must be a string"))
        .collect::<Result<_, _>>()?;
    for field in [
        "shard_id",
        "capture_adapter_id",
        "endpoint_identity",
        "flow_identity_profile",
        "time_semantics",
        "sampling",
    ] {
        assert!(required.contains(field), "missing required field: {field}");
    }
    let definitions = document
        .get("$defs")
        .and_then(Value::as_object)
        .ok_or("telemetry definitions missing")?;
    for definition in [
        "flow_identity",
        "flow_identity_profile",
        "time_semantics",
        "sampling",
    ] {
        assert!(
            definitions.contains_key(definition),
            "missing telemetry definition: {definition}"
        );
    }
    Ok(())
}

#[test]
fn route_wal_bytes_are_frozen() -> Result<(), Box<dyn Error>> {
    assert_vector(
        "route-wal-v1.json",
        "masi.edge.v1.RouteWalRecord",
        &route_wal_record(),
    )
}

#[test]
fn source_hint_wal_bytes_are_frozen() -> Result<(), Box<dyn Error>> {
    assert_vector(
        "source-hint-wal-v1.json",
        "masi.edge.v1.SourceWalRecord",
        &source_hint_record(),
    )
}

#[test]
fn p4_digest_response_bytes_are_frozen() -> Result<(), Box<dyn Error>> {
    assert_vector(
        "p4-digest-response-v1.json",
        "p4.v1.StreamMessageResponse",
        &p4_digest_response(),
    )
}

#[test]
fn p4_digest_ack_request_bytes_are_frozen() -> Result<(), Box<dyn Error>> {
    assert_vector(
        "p4-digest-ack-request-v1.json",
        "p4.v1.StreamMessageRequest",
        &p4_digest_ack_request(),
    )
}

#[test]
fn p4_packet_response_bytes_are_frozen() -> Result<(), Box<dyn Error>> {
    assert_vector(
        "p4-packet-response-v1.json",
        "p4.v1.StreamMessageResponse",
        &p4_packet_response(),
    )
}

#[test]
fn error_categories_and_grpc_mapping_are_frozen() -> Result<(), Box<dyn Error>> {
    let path =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../contracts/edge/v1/error-semantics.json");
    let document: Value = serde_json::from_str(&fs::read_to_string(path)?)?;
    let categories = document
        .get("categories")
        .and_then(Value::as_array)
        .ok_or("error categories missing")?;
    let errors = vec![
        (
            "INVALID_ARGUMENT",
            EdgeError::invalid("field", "invalid golden"),
        ),
        ("UNKNOWN_MAJOR", EdgeError::UnknownMajor("v2".into())),
        (
            "ENDPOINT_NOT_ALLOWED",
            EdgeError::EndpointNotAllowed("blocked golden".into()),
        ),
        (
            "TLS_IDENTITY_MISMATCH",
            EdgeError::TlsIdentityMismatch("identity golden".into()),
        ),
        (
            "RESOURCE_EXHAUSTED",
            EdgeError::exhausted("WAL_FULL", "full golden"),
        ),
        (
            "PRECONDITION_FAILED",
            EdgeError::precondition("ROUTE_FENCE_MISMATCH", "fence golden"),
        ),
        (
            "DEADLINE_EXCEEDED",
            EdgeError::Deadline("deadline golden".into()),
        ),
        (
            "WAL_CORRUPT",
            EdgeError::WalCorrupt("corrupt golden".into()),
        ),
        (
            "LOCAL_IO",
            EdgeError::io("golden", std::io::Error::other("I/O golden")),
        ),
        (
            "REMOTE_FAILURE",
            EdgeError::Remote {
                code: "POOL_UNAVAILABLE",
                message: "remote golden".into(),
            },
        ),
        (
            "WRITE_OUTCOME_UNCONFIRMED",
            EdgeError::UnknownOutcome("unknown golden".into()),
        ),
        (
            "ACTOR_UNAVAILABLE",
            EdgeError::ActorUnavailable("actor golden".into()),
        ),
    ];
    assert_eq!(errors.len(), categories.len());
    for ((expected_category, error), category) in errors.into_iter().zip(categories) {
        assert_eq!(
            expected_category,
            category
                .get("category")
                .and_then(Value::as_str)
                .ok_or("error category name missing")?
        );
        let status = tonic::Status::from(error);
        assert_eq!(
            grpc_name(status.code()),
            category
                .get("grpc_code")
                .and_then(Value::as_str)
                .ok_or("error grpc_code missing")?
        );
    }
    let contract_reason_codes: BTreeSet<&str> = document
        .get("stable_reason_codes")
        .and_then(Value::as_array)
        .ok_or("stable_reason_codes missing")?
        .iter()
        .map(|value| value.as_str().ok_or("reason code must be a string"))
        .collect::<Result<_, _>>()?;
    let implementation_reason_codes: BTreeSet<&str> = STABLE_REASON_CODES.iter().copied().collect();
    assert_eq!(implementation_reason_codes, contract_reason_codes);
    assert_eq!(implementation_reason_codes.len(), STABLE_REASON_CODES.len());

    let contract_status_codes: BTreeSet<&str> = document
        .get("stable_status_reason_codes")
        .and_then(Value::as_array)
        .ok_or("stable_status_reason_codes missing")?
        .iter()
        .map(|value| value.as_str().ok_or("status code must be a string"))
        .collect::<Result<_, _>>()?;
    let implementation_status_codes: BTreeSet<&str> =
        STABLE_STATUS_REASON_CODES.iter().copied().collect();
    assert_eq!(implementation_status_codes, contract_status_codes);
    assert_eq!(
        implementation_status_codes.len(),
        STABLE_STATUS_REASON_CODES.len()
    );
    Ok(())
}

#[test]
fn production_literal_reason_codes_are_registered() {
    let registered: BTreeSet<&str> = STABLE_REASON_CODES
        .iter()
        .chain(STABLE_STATUS_REASON_CODES)
        .copied()
        .collect();
    let non_reason_literals = BTreeSet::from(["CARGO_PKG_VERSION", "MSWA", "MSWAL001", "MSWCP001"]);
    let emitted: BTreeSet<&str> = source_literal_reason_codes()
        .into_iter()
        .chain(source_literal_status_codes())
        .chain(source_uppercase_literals())
        .filter(|value| !non_reason_literals.contains(value))
        .collect();
    assert!(!emitted.is_empty());
    let missing: Vec<&str> = emitted.difference(&registered).copied().collect();
    assert!(
        missing.is_empty(),
        "unregistered production reason codes: {missing:?}"
    );
}
