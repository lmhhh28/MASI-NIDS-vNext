package grpcapi

import (
	"context"
	"errors"
	"net"
	"strings"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"

	edgev1 "masi-nids/control-go/internal/grpc/edgev1"
	targetdomain "masi-nids/control-go/internal/target"
)

// fakeSinkDeps wires the ControlSink server against a stub ingest service by
// exercising the schema/bounds validation surface, which is contract-level
// and does not require PostgreSQL (integration tests cover the DB path).
func newTestServer(t *testing.T) (*grpc.Server, edgev1.ControlSinkClient) {
	t.Helper()
	srv := grpc.NewServer()
	sink := &ControlSinkServer{MaxBatchRecords: 4}
	edgev1.RegisterControlSinkServer(srv, sink)
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(srv.Stop)
	cc, err := grpc.NewClient(ln.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatalf("client: %v", err)
	}
	t.Cleanup(func() { _ = cc.Close() })
	return srv, edgev1.NewControlSinkClient(cc)
}

func validResultBatch() *edgev1.InferenceResultBatch {
	d := "sha256:" + strings.Repeat("a", 64)
	route := &edgev1.InferenceRoute{
		SchemaVersion: "inference-route/v1", ShardId: "shard-1",
		ModelControlIncarnationId: "inc-1", LogicalPoolId: "pool-1", PoolGeneration: 1,
		BindingGeneration: 1, RouteEpoch: 1, ModelRevisionDigest: d, ModelBundleDigest: d,
		FeatureContractDigest: d, LabelContractDigest: d, OutputAdapterDigest: d,
		WireProfileDigest: d, RuntimeProfileDigest: d, OptimizationProfileDigest: d,
		StartupEnvelopeDigest: d, PoolObservationDigest: d, BindingDigest: d, Scope: "scope-1",
	}
	record := &edgev1.InferenceResultRecord{
		EventIdempotencyKey: "idk-1", InputDigest: d, OutputDigest: d,
		ModelControlIncarnationId: route.ModelControlIncarnationId, LogicalPoolId: route.LogicalPoolId,
		PoolGeneration: 1, BindingGeneration: 1, RouteEpoch: 1,
		ModelRevisionDigest: d, ModelBundleDigest: d, FeatureContractDigest: d,
		LabelContractDigest: d, OutputAdapterDigest: d, WireProfileDigest: d,
		RuntimeProfileDigest: d, OptimizationProfileDigest: d, StartupEnvelopeDigest: d,
		PoolObservationDigest: d, BindingDigest: d, Scope: route.Scope, TargetId: "target-1", WindowId: "window-1",
		WorkerId: "worker-1", WorkerDigest: d, WorkerAttemptId: "attempt-1",
		WindowStartUnixMs: 1, WindowEndUnixMs: 2, FinalizedAtUnixMs: 3, Quality: "valid", TraceId: "trace-1",
		Scores: []float32{0.1, 0.9}, PredictedLabel: 1, Decision: "alert",
		DecisionCode: edgev1.InferenceDecision_INFERENCE_DECISION_ALERT,
		QualityCode:  edgev1.DataQuality_DATA_QUALITY_VALID, Status: "OK", ErrorCode: "NONE",
		ExecutionStatus:          edgev1.InferenceExecutionStatus_INFERENCE_EXECUTION_STATUS_OK,
		InferenceStartedAtUnixMs: 1, InferenceCompletedAtUnixMs: 2,
	}
	return &edgev1.InferenceResultBatch{SchemaVersion: "inference-central-grpc-batch/v1",
		RequestId: "request-1", Route: route, Records: []*edgev1.InferenceResultRecord{record},
		BatchDigest: d, TraceId: "trace-1"}
}

func TestCommitResultsRejectsUnknownSchemaMajor(t *testing.T) {
	_, client := newTestServer(t)
	_, err := client.CommitResults(context.Background(), &edgev1.InferenceResultBatch{
		SchemaVersion: "inference-central-grpc-batch/v999",
	})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("unknown major must be InvalidArgument, got %v", status.Code(err))
	}
}

func TestCommitResultsRejectsOversizeBatch(t *testing.T) {
	_, client := newTestServer(t)
	batch := &edgev1.InferenceResultBatch{
		SchemaVersion: "inference-central-grpc-batch/v1",
		Records:       make([]*edgev1.InferenceResultRecord, 5),
	}
	for i := range batch.Records {
		batch.Records[i] = &edgev1.InferenceResultRecord{}
	}
	_, err := client.CommitResults(context.Background(), batch)
	if status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("oversize batch must be ResourceExhausted, got %v", status.Code(err))
	}
}

func TestCommitResultsRejectsOversizeBytesBeforeParsingRecords(t *testing.T) {
	s := &ControlSinkServer{MaxBatchRecords: 4, MaxBatchBytes: 64}
	_, err := s.CommitResults(context.Background(), validResultBatch())
	if status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("oversize protobuf must be ResourceExhausted, got %v", status.Code(err))
	}
}

func TestCommitResultsRejectsEmptyRecord(t *testing.T) {
	// A nil record normalizes to an empty message on the wire; the missing
	// identity/digest fields must be rejected fail-closed.
	_, client := newTestServer(t)
	batch := &edgev1.InferenceResultBatch{
		SchemaVersion: "inference-central-grpc-batch/v1",
		Records: []*edgev1.InferenceResultRecord{
			{EventIdempotencyKey: "k1", InputDigest: "d1", OutputDigest: "d2"},
			nil,
		},
	}
	_, err := client.CommitResults(context.Background(), batch)
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("empty record must be InvalidArgument, got %v", status.Code(err))
	}
}

func TestPublishRuleObservationsRejectsUnknownMajor(t *testing.T) {
	_, client := newTestServer(t)
	_, err := client.PublishRuleObservations(context.Background(), &edgev1.RuleObservationBatch{
		SchemaVersion: "p4-rule-observation/v2",
	})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("unknown major must be InvalidArgument, got %v", status.Code(err))
	}
}

func TestPublishTargetStatusRejectsUnknownMajor(t *testing.T) {
	_, client := newTestServer(t)
	_, err := client.PublishTargetStatus(context.Background(), &edgev1.TargetStatusBatch{
		SchemaVersion: "bogus/v1",
	})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("unknown major must be InvalidArgument, got %v", status.Code(err))
	}
}

func TestPublishTargetStatusRejectsNilAndMalformedFence(t *testing.T) {
	s := &ControlSinkServer{MaxBatchRecords: 4}
	if _, err := s.PublishTargetStatus(context.Background(), nil); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("nil batch must be InvalidArgument, got %v", status.Code(err))
	}
	_, err := s.PublishTargetStatus(context.Background(), &edgev1.TargetStatusBatch{
		SchemaVersion: "masi-target-status/v1", TraceId: "trace-1", BatchId: "batch-1",
		BatchDigest: "sha256:" + strings.Repeat("a", 64),
		Targets:     []*edgev1.TargetStatus{{TargetId: "target-1"}},
	})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("missing fence must be InvalidArgument, got %v", status.Code(err))
	}
}

func validTargetStatusBatch() *edgev1.TargetStatusBatch {
	d := "sha256:" + strings.Repeat("a", 64)
	observation := targetdomain.CapabilityObservation{
		ObservationID: "obs-1", TargetID: "target-1", TargetControlIncarnationID: "inc-1",
		AssignmentGeneration: 1, ActorRuntimeEpoch: "actor-1", ApplicationGeneration: 1,
		P4InfoDigest: d, PipelineDigest: d, ProfileDigest: d, CapacityDigest: d,
		CapacityAvailable: true, LeaseValid: true, P4Connected: true, Primary: true, PipelineExact: true,
		Freshness: "fresh", ReasonCode: "READY", ObservedAtUnixMS: 1000, ExpiresAtUnixMS: 2000, TraceID: "trace-1",
	}
	observation.ObservationDigest = targetdomain.ComputeCapabilityObservationDigest(observation)
	return &edgev1.TargetStatusBatch{
		SchemaVersion: "masi-target-status/v1", TraceId: observation.TraceID,
		BatchId: "batch-1", BatchDigest: d,
		Targets: []*edgev1.TargetStatus{{
			TargetId: observation.TargetID, Fence: &edgev1.Fence{
				TargetControlIncarnationId: observation.TargetControlIncarnationID,
				TargetAssignmentGeneration: uint64(observation.AssignmentGeneration),
				ActorRuntimeEpoch:          observation.ActorRuntimeEpoch,
				ApplicationGeneration:      uint64(observation.ApplicationGeneration),
			},
			LeaseValid: true, P4Connected: true, Primary: true, PipelineExact: true,
			PipelineDigest: d, P4InfoDigest: d, ProfileDigest: d, CapacityDigest: d,
			CapacityAvailable: true, FreshnessCode: edgev1.FreshnessStatus_FRESHNESS_STATUS_FRESH,
			ReasonCode: "READY", ObservedAtUnixMs: 1000, ExpiresAtUnixMs: 2000,
			ObservationId: observation.ObservationID, ObservationDigest: observation.ObservationDigest,
		}},
	}
}

func TestPublishTargetStatusNotWiredFailsClosed(t *testing.T) {
	s := &ControlSinkServer{MaxBatchRecords: 4}
	_, err := s.PublishTargetStatus(context.Background(), validTargetStatusBatch())
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("unwired target observation sink must be Unavailable, got %v", status.Code(err))
	}
}

func TestCommitResultsNotWiredFailsClosed(t *testing.T) {
	_, client := newTestServer(t)
	_, err := client.CommitResults(context.Background(), validResultBatch())
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("unwired ingest must fail closed Unavailable, got %v", status.Code(err))
	}
}

func TestReasonOfBoundedAndStable(t *testing.T) {
	got := reasonOf(errors.New(strings.Repeat("x", 200)))
	if len(got) > 48 {
		t.Fatalf("reason must be bounded: %d", len(got))
	}
	if reasonOf(nil) != "" {
		t.Fatal("nil error yields empty reason")
	}
	if got := reasonOf(errors.New("ruleobs: late sample epoch rejected")); !strings.Contains(got, "LATE") {
		t.Fatalf("reason must be upper-cased: %s", got)
	}
}

func TestEdgeControlClientConstruction(t *testing.T) {
	// The wrapper only needs a ClientConnInterface; verify it builds and its
	// methods fail gracefully with a dead connection (bounded by ctx).
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	cc, err := grpc.NewClient(ln.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer cc.Close()
	ec := NewEdgeControlClient(cc)
	ctx, cancel := context.WithTimeout(context.Background(), 300*1e6)
	defer cancel()
	_, err = ec.GetStatus(ctx, "tgt-1")
	if err == nil {
		t.Fatal("dead connection must fail")
	}
	if !strings.Contains(err.Error(), "grpcapi") {
		t.Fatalf("wrapper must annotate errors: %v", err)
	}
}
