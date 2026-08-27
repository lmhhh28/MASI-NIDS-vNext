// Package grpcapi implements Go Control Core's gRPC surfaces: the ControlSink
// server (Edge → Go: inference results / rule observations / target status)
// and the EdgeControl client wrapper (Go → Edge: assignments, effects,
// routes, rule observation configuration).
//
// Invariants (AGENTS.md, ADR-0004/0005):
//   - ControlSink.CommitResults returns canonical ACKs only AFTER the
//     PostgreSQL Event transaction commits (never on RPC/Gateway success);
//   - every RPC is bounded (deadline propagated, no unbounded buffering);
//   - schema_version is validated fail-closed (unknown major rejected);
//   - no gRPC handler ever holds a DB transaction across the call — handlers
//     call subdomain services that own the short transaction boundaries.
package grpcapi

import (
	"context"
	"crypto/sha256"
	"fmt"
	"math"
	"strings"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	"masi-nids/control-go/internal/event"
	edgev1 "masi-nids/control-go/internal/grpc/edgev1"
	"masi-nids/control-go/internal/ruleobs"
	"masi-nids/control-go/internal/target"
)

// ControlSinkServer implements the Edge → Go sink. It is registered on the
// production mTLS gRPC server; Edge is the only intended client.
type ControlSinkServer struct {
	edgev1.UnimplementedControlSinkServer
	Ingest          *event.IngestService
	RuleObs         *ruleobs.Projector
	TargetRegistry  *target.RegistryService
	MaxBatchRecords int
	MaxBatchBytes   int
	Accepting       func() bool
	AuthorizeTarget func(context.Context, string) error
}

func (s *ControlSinkServer) authorizeTarget(ctx context.Context, targetID string) error {
	if s.AuthorizeTarget == nil {
		return nil
	}
	if err := s.AuthorizeTarget(ctx, targetID); err != nil {
		return status.Error(codes.PermissionDenied, "mTLS workload/target assignment mismatch")
	}
	return nil
}

const (
	defaultMaxBatchRecords = 256
	defaultMaxBatchBytes   = 4 * 1024 * 1024
)

func (s *ControlSinkServer) maxBatch() int {
	if s.MaxBatchRecords <= 0 {
		return defaultMaxBatchRecords
	}
	return s.MaxBatchRecords
}

func (s *ControlSinkServer) maxBytes() int {
	if s.MaxBatchBytes <= 0 {
		return defaultMaxBatchBytes
	}
	return s.MaxBatchBytes
}

func (s *ControlSinkServer) validateWireSize(message proto.Message) error {
	if message == nil || proto.Size(message) > s.maxBytes() {
		return status.Error(codes.ResourceExhausted, "message exceeds byte bound")
	}
	return nil
}

// CommitResults ingests a bounded result batch and returns canonical ACKs
// only after durable PostgreSQL commit.
func (s *ControlSinkServer) CommitResults(ctx context.Context, batch *edgev1.InferenceResultBatch) (*edgev1.CanonicalAckBatch, error) {
	if s.Accepting != nil && !s.Accepting() {
		return nil, status.Error(codes.Unavailable, "control core is draining")
	}
	if batch == nil || batch.GetSchemaVersion() != "inference-central-grpc-batch/v1" {
		return nil, status.Error(codes.InvalidArgument, "unknown schema major")
	}
	if err := s.validateWireSize(batch); err != nil {
		return nil, err
	}
	if len(batch.GetRecords()) == 0 || len(batch.GetRecords()) > s.maxBatch() {
		return nil, status.Error(codes.ResourceExhausted, "batch exceeds bound")
	}
	if !wireDigest(batch.GetBatchDigest()) || batch.GetRequestId() == "" || batch.GetTraceId() == "" {
		return nil, status.Error(codes.InvalidArgument, "batch identity/digest malformed")
	}
	results := make([]event.InferenceResult, 0, len(batch.GetRecords()))
	route := batch.GetRoute()
	if route == nil || route.GetSchemaVersion() != "inference-route/v1" ||
		route.GetShardId() == "" || route.GetLogicalPoolId() == "" || route.GetScope() == "" ||
		route.GetRouteEpoch() == 0 || route.GetPoolGeneration() == 0 || route.GetBindingGeneration() == 0 {
		return nil, status.Error(codes.InvalidArgument, "route fence malformed")
	}
	for _, digest := range []string{route.GetModelRevisionDigest(), route.GetModelBundleDigest(), route.GetFeatureContractDigest(),
		route.GetLabelContractDigest(), route.GetOutputAdapterDigest(), route.GetWireProfileDigest(), route.GetRuntimeProfileDigest(),
		route.GetOptimizationProfileDigest(), route.GetStartupEnvelopeDigest(), route.GetPoolObservationDigest(), route.GetBindingDigest()} {
		if !wireDigest(digest) {
			return nil, status.Error(codes.InvalidArgument, "route digest malformed")
		}
	}
	for _, rec := range batch.GetRecords() {
		if rec == nil || rec.GetEventIdempotencyKey() == "" ||
			!wireDigest(rec.GetInputDigest()) || !wireDigest(rec.GetOutputDigest()) || rec.GetWorkerId() == "" ||
			!wireDigest(rec.GetWorkerDigest()) || rec.GetWorkerAttemptId() == "" || rec.GetTargetId() == "" ||
			rec.GetWindowId() == "" || rec.GetWindowStartUnixMs() <= 0 || rec.GetWindowEndUnixMs() <= rec.GetWindowStartUnixMs() ||
			rec.GetFinalizedAtUnixMs() < rec.GetWindowEndUnixMs() || rec.GetTraceId() == "" ||
			rec.GetInferenceStartedAtUnixMs() <= 0 || rec.GetInferenceCompletedAtUnixMs() < rec.GetInferenceStartedAtUnixMs() {
			// A nil/empty record (nil normalizes to empty on the wire) lacks
			// the required identity/digest fields: reject fail-closed.
			return nil, status.Error(codes.InvalidArgument, "record missing identity/digest")
		}
		if err := s.authorizeTarget(ctx, rec.GetTargetId()); err != nil {
			return nil, err
		}
		if rec.GetModelControlIncarnationId() != route.GetModelControlIncarnationId() ||
			rec.GetLogicalPoolId() != route.GetLogicalPoolId() || rec.GetPoolGeneration() != route.GetPoolGeneration() ||
			rec.GetBindingGeneration() != route.GetBindingGeneration() || rec.GetRouteEpoch() != route.GetRouteEpoch() ||
			rec.GetModelRevisionDigest() != route.GetModelRevisionDigest() ||
			rec.GetFeatureContractDigest() != route.GetFeatureContractDigest() ||
			rec.GetLabelContractDigest() != route.GetLabelContractDigest() ||
			rec.GetOutputAdapterDigest() != route.GetOutputAdapterDigest() ||
			rec.GetModelBundleDigest() != route.GetModelBundleDigest() ||
			rec.GetWireProfileDigest() != route.GetWireProfileDigest() ||
			rec.GetRuntimeProfileDigest() != route.GetRuntimeProfileDigest() ||
			rec.GetOptimizationProfileDigest() != route.GetOptimizationProfileDigest() ||
			rec.GetStartupEnvelopeDigest() != route.GetStartupEnvelopeDigest() ||
			rec.GetPoolObservationDigest() != route.GetPoolObservationDigest() ||
			rec.GetBindingDigest() != route.GetBindingDigest() || rec.GetScope() != route.GetScope() {
			return nil, status.Error(codes.InvalidArgument, "record/route fence conflict")
		}
		decision, decisionOK := inferenceDecision(rec.GetDecisionCode())
		execution, executionOK := inferenceExecutionStatus(rec.GetExecutionStatus())
		quality, qualityOK := inferenceQuality(rec.GetQualityCode())
		wireStatusExact := (execution == event.ExecutionOK && rec.GetStatus() == "OK") ||
			(execution != event.ExecutionOK && rec.GetStatus() == string(execution))
		if !decisionOK || !executionOK || !qualityOK || rec.GetDecision() != string(decision) ||
			!wireStatusExact || rec.GetQuality() != quality ||
			rec.GetAbstain() != (decision == event.DecisionAbstain) ||
			(rec.GetOutOfDistribution() && !rec.GetAbstain()) ||
			(execution != event.ExecutionOK && !rec.GetAbstain()) ||
			(execution == event.ExecutionOK && rec.GetErrorCode() != "NONE") ||
			(execution != event.ExecutionOK && (rec.GetErrorCode() == "" || len(rec.GetErrorCode()) > 64)) ||
			len(rec.GetScores()) > 4096 || (execution == event.ExecutionOK && len(rec.GetScores()) == 0) {
			return nil, status.Error(codes.InvalidArgument, "record inference outcome malformed")
		}
		eventErrorCode := rec.GetErrorCode()
		if execution == event.ExecutionOK {
			eventErrorCode = ""
		}
		scores := make([]float64, len(rec.GetScores()))
		for i, score := range rec.GetScores() {
			scores[i] = float64(score)
			if math.IsNaN(scores[i]) || math.IsInf(scores[i], 0) {
				return nil, status.Error(codes.InvalidArgument, "record score contains NaN/Inf")
			}
		}
		results = append(results, event.InferenceResult{
			EventIDempotencyKey:       rec.GetEventIdempotencyKey(),
			InputDigest:               rec.GetInputDigest(),
			OutputDigest:              rec.GetOutputDigest(),
			ModelControlIncarnationID: route.GetModelControlIncarnationId(),
			ShardID:                   route.GetShardId(),
			RouteEpoch:                int64(route.GetRouteEpoch()),
			LogicalPoolID:             route.GetLogicalPoolId(),
			PoolGeneration:            int64(route.GetPoolGeneration()),
			BindingGeneration:         int64(route.GetBindingGeneration()),
			ModelRevisionDigest:       route.GetModelRevisionDigest(),
			ModelBundleDigest:         route.GetModelBundleDigest(),
			FeatureContractDigest:     route.GetFeatureContractDigest(),
			LabelContractDigest:       route.GetLabelContractDigest(),
			OutputAdapterDigest:       route.GetOutputAdapterDigest(),
			WireProfileDigest:         route.GetWireProfileDigest(),
			RuntimeProfileDigest:      route.GetRuntimeProfileDigest(),
			OptimizationProfileDigest: route.GetOptimizationProfileDigest(),
			StartupEnvelopeDigest:     route.GetStartupEnvelopeDigest(), PoolObservationDigest: route.GetPoolObservationDigest(), BindingDigest: route.GetBindingDigest(),
			Scores: scores, PredictedLabel: rec.GetPredictedLabel(), Decision: decision,
			OutOfDistribution: rec.GetOutOfDistribution(), Abstain: rec.GetAbstain(),
			ExecutionStatus: execution, ErrorCode: eventErrorCode, WorkerID: rec.GetWorkerId(),
			WorkerDigest: rec.GetWorkerDigest(), WorkerAttemptID: rec.GetWorkerAttemptId(),
			InferenceStartedAtUnixMS:   rec.GetInferenceStartedAtUnixMs(),
			InferenceCompletedAtUnixMS: rec.GetInferenceCompletedAtUnixMs(),
			Scope:                      route.GetScope(),
			SourceWindow: event.SourceWindowIdentity{
				SourceID:          rec.GetTargetId(),
				WindowID:          rec.GetWindowId(),
				WindowStartUnixMS: rec.GetWindowStartUnixMs(),
				WindowEndUnixMS:   rec.GetWindowEndUnixMs(),
				FinalizedAtUnixMS: rec.GetFinalizedAtUnixMs(),
				WatermarkUnixMS:   rec.GetFinalizedAtUnixMs(),
				// The proto record has no separate event-time field; the window
				// end is the event-time basis for this record (validateFence
				// requires SourceWindow.EventTimeUnixMS > 0).
				EventTimeUnixMS: rec.GetWindowEndUnixMs(),
			},
			Quality:           quality,
			EventTimeUnixMS:   rec.GetWindowEndUnixMs(),
			IngestBatchDigest: batch.GetBatchDigest(),
			TraceID:           rec.GetTraceId(),
		})
	}
	if s.Ingest == nil {
		return nil, status.Error(codes.Unavailable, "ingest service not wired")
	}
	acks, err := s.Ingest.IngestBatch(ctx, results)
	if err != nil {
		// Ingest errors are structural (no canonical commit happened).
		return nil, err
	}
	out := &edgev1.CanonicalAckBatch{
		SchemaVersion:     "canonical-event-ack/v1",
		TraceId:           batch.GetTraceId(),
		ResultBatchDigest: batch.GetBatchDigest(),
	}
	for _, a := range acks {
		out.Acknowledgements = append(out.Acknowledgements, &edgev1.CanonicalAck{
			EventIdempotencyKey: a.EventIDempotencyKey,
			InputDigest:         a.InputDigest,
			OutputDigest:        a.OutputDigest,
			CanonicalEventId:    a.CanonicalEventID,
			CommittedAtUnixMs:   a.CommittedAtUnixMS,
			Status:              string(a.CommitStatus),
			ReasonCode:          a.ReasonCode,
			CommitStatus:        canonicalCommitStatus(a.CommitStatus),
		})
	}
	digest, err := canonicalAckBatchDigest(out)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "canonical ACK digest: %v", err)
	}
	out.AckBatchDigest = digest
	return out, nil
}

func canonicalAckBatchDigest(batch *edgev1.CanonicalAckBatch) (string, error) {
	canonical := proto.Clone(batch).(*edgev1.CanonicalAckBatch)
	canonical.AckBatchDigest = ""
	raw, err := proto.MarshalOptions{Deterministic: true}.Marshal(canonical)
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("sha256:%x", sha256.Sum256(raw)), nil
}

func inferenceDecision(code edgev1.InferenceDecision) (event.InferenceDecision, bool) {
	switch code {
	case edgev1.InferenceDecision_INFERENCE_DECISION_BENIGN:
		return event.DecisionBenign, true
	case edgev1.InferenceDecision_INFERENCE_DECISION_ALERT:
		return event.DecisionAlert, true
	case edgev1.InferenceDecision_INFERENCE_DECISION_ABSTAIN:
		return event.DecisionAbstain, true
	default:
		return "", false
	}
}

func inferenceExecutionStatus(code edgev1.InferenceExecutionStatus) (event.InferenceExecutionStatus, bool) {
	switch code {
	case edgev1.InferenceExecutionStatus_INFERENCE_EXECUTION_STATUS_OK:
		return event.ExecutionOK, true
	case edgev1.InferenceExecutionStatus_INFERENCE_EXECUTION_STATUS_HOLD:
		return event.ExecutionHold, true
	case edgev1.InferenceExecutionStatus_INFERENCE_EXECUTION_STATUS_ERROR:
		return event.ExecutionError, true
	default:
		return "", false
	}
}

func inferenceQuality(code edgev1.DataQuality) (string, bool) {
	switch code {
	case edgev1.DataQuality_DATA_QUALITY_VALID:
		return event.QualityValid, true
	case edgev1.DataQuality_DATA_QUALITY_PARTIAL:
		return event.QualityPartial, true
	case edgev1.DataQuality_DATA_QUALITY_GAP:
		return event.QualityGap, true
	case edgev1.DataQuality_DATA_QUALITY_STALE:
		return event.QualityStale, true
	case edgev1.DataQuality_DATA_QUALITY_INVALID:
		return event.QualityInvalid, true
	case edgev1.DataQuality_DATA_QUALITY_RESET:
		return event.QualityReset, true
	case edgev1.DataQuality_DATA_QUALITY_NOT_COVERED:
		return event.QualityNotCovered, true
	case edgev1.DataQuality_DATA_QUALITY_NOT_MEASURABLE:
		return event.QualityNotMeasurable, true
	default:
		return "", false
	}
}

func wireDigest(s string) bool {
	if len(s) != 71 || !strings.HasPrefix(s, "sha256:") {
		return false
	}
	for _, c := range strings.TrimPrefix(s, "sha256:") {
		if !strings.ContainsRune("0123456789abcdef", c) {
			return false
		}
	}
	return true
}

func canonicalCommitStatus(s event.CommitStatus) edgev1.CanonicalCommitStatus {
	switch s {
	case event.StatusCommitted:
		return edgev1.CanonicalCommitStatus_CANONICAL_COMMIT_STATUS_COMMITTED
	case event.StatusIdempotent:
		return edgev1.CanonicalCommitStatus_CANONICAL_COMMIT_STATUS_IDEMPOTENT
	default:
		return edgev1.CanonicalCommitStatus_CANONICAL_COMMIT_STATUS_UNSPECIFIED
	}
}

// PublishRuleObservations folds a bounded rule observation batch into the
// Go-owned rule observation reference. Late samples (older epoch) are
// rejected per-record with a reason; the batch is acknowledged.
func (s *ControlSinkServer) PublishRuleObservations(ctx context.Context, batch *edgev1.RuleObservationBatch) (*edgev1.PublishAck, error) {
	if s.Accepting != nil && !s.Accepting() {
		return nil, status.Error(codes.Unavailable, "control core is draining")
	}
	if batch == nil || batch.GetSchemaVersion() != "p4-rule-observation-batch/v1" {
		return nil, status.Error(codes.InvalidArgument, "unknown schema major")
	}
	if err := s.validateWireSize(batch); err != nil {
		return nil, err
	}
	if len(batch.GetObservations()) > s.maxBatch() {
		return nil, status.Error(codes.ResourceExhausted, "batch exceeds bound")
	}
	if batch.GetBatchId() == "" || !wireDigest(batch.GetBatchDigest()) {
		return nil, status.Error(codes.InvalidArgument, "rule observation batch identity/digest malformed")
	}
	if s.RuleObs == nil {
		return nil, status.Error(codes.Unavailable, "rule observation projector not wired")
	}
	accepted, rejected := 0, 0
	var lastReason string
	for _, o := range batch.GetObservations() {
		if o == nil || o.GetTargetId() == "" || o.GetEntityId() == "" || o.GetRuleId() == "" ||
			o.GetEffectIntentId() == "" || o.GetOperationId() == "" || !wireDigest(o.GetCanonicalEntryDigest()) ||
			!wireDigest(o.GetMatchPriorityActionDigest()) ||
			o.GetObservationEpoch() == 0 || o.GetResetEpoch() == 0 || o.GetSampleSequence() == 0 ||
			o.GetReadCompletedAtUnixMs() <= 0 || o.GetInstallationReadback() != "exact" || o.GetSamplingCoveragePpm() == 0 || o.GetSamplingCoveragePpm() > 1_000_000 {
			rejected++
			lastReason = "MALFORMED_OBSERVATION"
			continue
		}
		if err := s.authorizeTarget(ctx, o.GetTargetId()); err != nil {
			return nil, err
		}
		sample := ruleobs.CounterSample{
			Sequence:        int64(o.GetSampleSequence()),
			ReadCompletedAt: time.UnixMilli(o.GetReadCompletedAtUnixMs()).UTC(),
			Packets:         o.GetCumulative().GetPackets(),
			Bytes:           o.GetCumulative().GetBytes(),
			EligiblePackets: o.GetEligibleCumulative().GetPackets(),
			CoveragePPM:     o.GetSamplingCoveragePpm(),
		}
		// Order/gap statuses are carried by Edge; without explicit flags a
		// in-order/none sample is assumed only when installation is exact.
		sample.OrderStatus = "in-order"
		switch o.GetQuality() {
		case "gap":
			sample.GapStatus = "gap"
		case "reset":
			sample.GapStatus = "reset-or-wrap"
		case "valid", "partial":
			sample.GapStatus = "none"
		default:
			rejected++
			lastReason = "QUALITY_UNSUPPORTED"
			continue
		}
		key := ruleobs.EpochKey{
			ObservationEpoch: int64(o.GetObservationEpoch()),
			ResetEpoch:       int64(o.GetResetEpoch()),
		}
		if _, err := s.RuleObs.IngestSample(ctx, o.GetTargetId(), o.GetEntityId(), o.GetRuleId(),
			o.GetEffectIntentId(), o.GetOperationId(), o.GetCanonicalEntryDigest(),
			o.GetMatchPriorityActionDigest(), key, sample); err != nil {
			rejected++
			lastReason = reasonOf(err)
			continue
		}
		accepted++
	}
	rc := "ACCEPTED"
	if rejected > 0 {
		rc = fmt.Sprintf("PARTIAL_ACCEPTED_%d_REJECTED_%d_%s", accepted, rejected, lastReason)
	}
	return &edgev1.PublishAck{Status: "accepted", Identity: batch.GetBatchId(), Digest: batch.GetBatchDigest(), ReasonCode: rc}, nil
}

// PublishTargetStatus records the Edge target status observation. Status is
// an observation, never the canonical target fact (Go's Target Registry is).
func (s *ControlSinkServer) PublishTargetStatus(ctx context.Context, batch *edgev1.TargetStatusBatch) (*edgev1.PublishAck, error) {
	if s.Accepting != nil && !s.Accepting() {
		return nil, status.Error(codes.Unavailable, "control core is draining")
	}
	if batch == nil || batch.GetSchemaVersion() != "masi-target-status/v1" {
		return nil, status.Error(codes.InvalidArgument, "unknown schema major")
	}
	if err := s.validateWireSize(batch); err != nil {
		return nil, err
	}
	if len(batch.GetTargets()) == 0 || len(batch.GetTargets()) > s.maxBatch() {
		return nil, status.Error(codes.ResourceExhausted, "batch exceeds bound")
	}
	if batch.GetTraceId() == "" {
		return nil, status.Error(codes.InvalidArgument, "target status trace identity required")
	}
	if batch.GetBatchId() == "" || !wireDigest(batch.GetBatchDigest()) {
		return nil, status.Error(codes.InvalidArgument, "target status batch identity/digest malformed")
	}
	observations := make([]target.CapabilityObservation, 0, len(batch.GetTargets()))
	for _, wire := range batch.GetTargets() {
		fence := wire.GetFence()
		if wire == nil || wire.GetTargetId() == "" || fence == nil ||
			fence.GetTargetControlIncarnationId() == "" || fence.GetTargetAssignmentGeneration() == 0 ||
			fence.GetActorRuntimeEpoch() == "" || fence.GetApplicationGeneration() == 0 ||
			wire.GetFreshnessCode() == edgev1.FreshnessStatus_FRESHNESS_STATUS_UNSPECIFIED {
			return nil, status.Error(codes.InvalidArgument, "target status identity/fence malformed")
		}
		if err := s.authorizeTarget(ctx, wire.GetTargetId()); err != nil {
			return nil, err
		}
		observation := target.CapabilityObservation{
			ObservationID: wire.GetObservationId(), ObservationDigest: wire.GetObservationDigest(),
			TargetID: wire.GetTargetId(), TargetControlIncarnationID: fence.GetTargetControlIncarnationId(),
			AssignmentGeneration: int64(fence.GetTargetAssignmentGeneration()), ActorRuntimeEpoch: fence.GetActorRuntimeEpoch(),
			ApplicationGeneration: int64(fence.GetApplicationGeneration()), P4InfoDigest: wire.GetP4InfoDigest(),
			PipelineDigest: wire.GetPipelineDigest(), ProfileDigest: wire.GetProfileDigest(),
			CapacityDigest: wire.GetCapacityDigest(), CapacityAvailable: wire.GetCapacityAvailable(),
			LeaseValid: wire.GetLeaseValid(), P4Connected: wire.GetP4Connected(), Primary: wire.GetPrimary(),
			PipelineExact: wire.GetPipelineExact(), HighPriorityQueueDepth: wire.GetHighPriorityQueueDepth(),
			TelemetryQueueDepth: wire.GetTelemetryQueueDepth(), ObservationQueueDepth: wire.GetObservationQueueDepth(),
			SourceWALBytes: wire.GetSourceWalBytes(), InputWALBytes: wire.GetInputWalBytes(), ResultWALBytes: wire.GetResultWalBytes(),
			LastSuccessfulReadUnixMS: wire.GetLastSuccessfulReadUnixMs(), Freshness: freshnessString(wire.GetFreshnessCode()),
			ReasonCode: wire.GetReasonCode(), ObservedAtUnixMS: wire.GetObservedAtUnixMs(),
			ExpiresAtUnixMS: wire.GetExpiresAtUnixMs(), TraceID: batch.GetTraceId(),
		}
		if observation.ObservationDigest != target.ComputeCapabilityObservationDigest(observation) {
			return nil, status.Error(codes.InvalidArgument, "target observation digest mismatch")
		}
		observations = append(observations, observation)
	}
	if s.TargetRegistry == nil {
		return nil, status.Error(codes.Unavailable, "target registry observation sink not wired")
	}
	results, err := s.TargetRegistry.RecordCapabilityObservations(ctx, observations)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "record target observations: %v", err)
	}
	accepted, rejected := 0, 0
	lastReason := "ACCEPTED"
	for _, result := range results {
		if result.Accepted {
			accepted++
		} else {
			rejected++
			lastReason = result.ReasonCode
		}
	}
	reason := "ACCEPTED"
	if rejected > 0 {
		reason = fmt.Sprintf("PARTIAL_ACCEPTED_%d_REJECTED_%d_%s", accepted, rejected, lastReason)
	}
	return &edgev1.PublishAck{
		Status: "accepted", Identity: batch.GetBatchId(), Digest: batch.GetBatchDigest(),
		ReasonCode: reason, StatusCode: edgev1.PublishStatus_PUBLISH_STATUS_ACCEPTED,
	}, nil
}

func freshnessString(status edgev1.FreshnessStatus) string {
	switch status {
	case edgev1.FreshnessStatus_FRESHNESS_STATUS_NOT_OBSERVED:
		return "not-observed"
	case edgev1.FreshnessStatus_FRESHNESS_STATUS_FRESH:
		return "fresh"
	case edgev1.FreshnessStatus_FRESHNESS_STATUS_STALE:
		return "stale"
	case edgev1.FreshnessStatus_FRESHNESS_STATUS_GAP:
		return "gap"
	default:
		return ""
	}
}

func reasonOf(err error) string {
	if err == nil {
		return ""
	}
	msg := err.Error()
	if i := strings.LastIndex(msg, ": "); i >= 0 && i+2 < len(msg) {
		msg = msg[i+2:]
	}
	msg = strings.ToUpper(strings.ReplaceAll(msg, " ", "_"))
	if len(msg) > 48 {
		msg = msg[:48]
	}
	return msg
}
