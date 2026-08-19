package event

import (
	"strings"
	"testing"
)

func TestCanonicalEventIDDeterministic(t *testing.T) {
	r := InferenceResult{
		EventIDempotencyKey:       "idk-1",
		InputDigest:               "sha256:" + strings.Repeat("a", 64),
		OutputDigest:              "sha256:" + strings.Repeat("b", 64),
		ModelControlIncarnationID: "inc-1",
		ShardID:                   "sh-1",
		RouteEpoch:                1,
		LogicalPoolID:             "pool-1",
		PoolGeneration:            1,
		BindingGeneration:         1,
		ModelRevisionDigest:       "sha256:" + strings.Repeat("c", 64), ModelBundleDigest: "sha256:" + strings.Repeat("d", 64),
		FeatureContractDigest: "sha256:" + strings.Repeat("e", 64), LabelContractDigest: "sha256:" + strings.Repeat("f", 64),
		OutputAdapterDigest: "sha256:" + strings.Repeat("1", 64), WireProfileDigest: "sha256:" + strings.Repeat("2", 64),
		RuntimeProfileDigest: "sha256:" + strings.Repeat("3", 64), OptimizationProfileDigest: "sha256:" + strings.Repeat("4", 64), StartupEnvelopeDigest: "sha256:" + strings.Repeat("5", 64), PoolObservationDigest: "sha256:" + strings.Repeat("6", 64), BindingDigest: "sha256:" + strings.Repeat("7", 64), Scope: "scope-1",
		Scores: []float64{0.1, 0.9}, PredictedLabel: 1, Decision: DecisionAlert, ExecutionStatus: ExecutionOK,
		WorkerID: "worker-1", WorkerDigest: "sha256:" + strings.Repeat("9", 64), WorkerAttemptID: "attempt-1", InferenceStartedAtUnixMS: 1, InferenceCompletedAtUnixMS: 2,
		SourceWindow: SourceWindowIdentity{SourceID: "s", WindowID: "w", WindowStartUnixMS: 1, WindowEndUnixMS: 2, FinalizedAtUnixMS: 3, WatermarkUnixMS: 2, EventTimeUnixMS: 2},
		Quality:      "valid", EventTimeUnixMS: 2, IngestBatchDigest: "sha256:" + strings.Repeat("8", 64), TraceID: "trace-1",
	}
	id1, err := CanonicalEventID(r)
	if err != nil {
		t.Fatalf("CanonicalEventID: %v", err)
	}
	id2, err := CanonicalEventID(r)
	if err != nil {
		t.Fatalf("CanonicalEventID: %v", err)
	}
	if id1 != id2 {
		t.Fatal("canonical event id not deterministic")
	}
	if !strings.HasPrefix(id1, "evt-") {
		t.Fatalf("canonical id %q must start with evt-", id1)
	}
}

func TestCanonicalEventIDDiffersOnDigest(t *testing.T) {
	base := InferenceResult{
		EventIDempotencyKey:       "idk-1",
		InputDigest:               "sha256:" + strings.Repeat("a", 64),
		OutputDigest:              "sha256:" + strings.Repeat("b", 64),
		ModelControlIncarnationID: "inc-1", ShardID: "sh-1", RouteEpoch: 1,
		LogicalPoolID: "pool-1", PoolGeneration: 1, BindingGeneration: 1,
		ModelRevisionDigest: "sha256:" + strings.Repeat("c", 64), ModelBundleDigest: "sha256:" + strings.Repeat("d", 64),
		FeatureContractDigest: "sha256:" + strings.Repeat("e", 64), LabelContractDigest: "sha256:" + strings.Repeat("f", 64),
		OutputAdapterDigest: "sha256:" + strings.Repeat("1", 64), WireProfileDigest: "sha256:" + strings.Repeat("2", 64),
		RuntimeProfileDigest: "sha256:" + strings.Repeat("3", 64), OptimizationProfileDigest: "sha256:" + strings.Repeat("4", 64), StartupEnvelopeDigest: "sha256:" + strings.Repeat("5", 64), PoolObservationDigest: "sha256:" + strings.Repeat("6", 64), BindingDigest: "sha256:" + strings.Repeat("7", 64), Scope: "scope-1",
		Scores: []float64{0.1, 0.9}, PredictedLabel: 1, Decision: DecisionAlert, ExecutionStatus: ExecutionOK,
		WorkerID: "worker-1", WorkerDigest: "sha256:" + strings.Repeat("9", 64), WorkerAttemptID: "attempt-1", InferenceStartedAtUnixMS: 1, InferenceCompletedAtUnixMS: 2,
		SourceWindow: SourceWindowIdentity{SourceID: "s", WindowID: "w", WindowStartUnixMS: 1, WindowEndUnixMS: 2, FinalizedAtUnixMS: 3, WatermarkUnixMS: 2, EventTimeUnixMS: 2},
		Quality:      "valid", EventTimeUnixMS: 2, IngestBatchDigest: "sha256:" + strings.Repeat("8", 64), TraceID: "trace-1",
	}
	idA, _ := CanonicalEventID(base)
	changed := base
	changed.OutputDigest = "sha256:" + strings.Repeat("c", 64)
	idB, _ := CanonicalEventID(changed)
	if idA == idB {
		t.Fatal("canonical id must differ when output_digest differs")
	}
}

func TestValidateFenceRejects(t *testing.T) {
	good := InferenceResult{
		EventIDempotencyKey: "idk-1", InputDigest: "sha256:" + strings.Repeat("a", 64),
		OutputDigest:              "sha256:" + strings.Repeat("b", 64),
		ModelControlIncarnationID: "inc-1", ShardID: "sh-1", RouteEpoch: 1,
		LogicalPoolID: "pool-1", PoolGeneration: 1, BindingGeneration: 1,
		ModelRevisionDigest: "sha256:" + strings.Repeat("c", 64), ModelBundleDigest: "sha256:" + strings.Repeat("d", 64),
		FeatureContractDigest: "sha256:" + strings.Repeat("e", 64), LabelContractDigest: "sha256:" + strings.Repeat("f", 64),
		OutputAdapterDigest: "sha256:" + strings.Repeat("1", 64), WireProfileDigest: "sha256:" + strings.Repeat("2", 64),
		RuntimeProfileDigest: "sha256:" + strings.Repeat("3", 64), OptimizationProfileDigest: "sha256:" + strings.Repeat("4", 64), StartupEnvelopeDigest: "sha256:" + strings.Repeat("5", 64), PoolObservationDigest: "sha256:" + strings.Repeat("6", 64), BindingDigest: "sha256:" + strings.Repeat("7", 64), Scope: "scope-1",
		Scores: []float64{0.1, 0.9}, PredictedLabel: 1, Decision: DecisionAlert, ExecutionStatus: ExecutionOK,
		WorkerID: "worker-1", WorkerDigest: "sha256:" + strings.Repeat("9", 64), WorkerAttemptID: "attempt-1", InferenceStartedAtUnixMS: 1, InferenceCompletedAtUnixMS: 2,
		SourceWindow: SourceWindowIdentity{SourceID: "s", WindowID: "w", WindowStartUnixMS: 1, WindowEndUnixMS: 2, FinalizedAtUnixMS: 3, WatermarkUnixMS: 2, EventTimeUnixMS: 2},
		Quality:      "valid", EventTimeUnixMS: 2, IngestBatchDigest: "sha256:" + strings.Repeat("8", 64), TraceID: "trace-1",
	}
	if err := validateFence(good); err != nil {
		t.Fatalf("good fence rejected: %v", err)
	}
	bad := good
	bad.RouteEpoch = 0
	if err := validateFence(bad); err == nil {
		t.Fatal("route_epoch=0 must be rejected")
	}
	bad2 := good
	bad2.InputDigest = "not-a-digest"
	if err := validateFence(bad2); err == nil {
		t.Fatal("malformed input_digest must be rejected")
	}
	bad3 := good
	bad3.Quality = "normal"
	if err := validateFence(bad3); err == nil {
		t.Fatal("quality=normal must be rejected (not a valid quality enum)")
	}
	bad4 := good
	bad4.TraceID = strings.Repeat("x", 129)
	if err := validateFence(bad4); err == nil {
		t.Fatal("identity fields over 128 bytes must be rejected before persistence")
	}
}

func TestIngestBatchBounds(t *testing.T) {
	svc := &IngestService{maxBatchRecords: 2, maxBatchBytes: 1024}
	if err := svc.validateBatchBounds(make([]InferenceResult, 3)); err == nil {
		t.Fatal("record bound must be enforced inside the ingest service")
	}
	large := InferenceResult{Scope: strings.Repeat("x", 2048)}
	if err := svc.validateBatchBounds([]InferenceResult{large}); err == nil {
		t.Fatal("byte bound must be enforced inside the ingest service")
	}
}

func TestIsConflict(t *testing.T) {
	dA := "sha256:" + strings.Repeat("a", 64)
	dB := "sha256:" + strings.Repeat("b", 64)
	if IsConflict(dA, dB, dA, dB) {
		t.Fatal("same digests must not be a conflict")
	}
	if !IsConflict(dA, dB, dA, dA) {
		t.Fatal("different output digest must be a conflict")
	}
}

func TestReasonForQuality(t *testing.T) {
	if ReasonForQuality("valid") != "COMMITTED" {
		t.Fatal("valid quality reason must be COMMITTED")
	}
	if ReasonForQuality("not-measurable") != "NOT_MEASURABLE" {
		t.Fatal("not-measurable reason must be NOT_MEASURABLE")
	}
	if ReasonForQuality("not-covered") != "NOT_COVERED" {
		t.Fatal("not-covered reason must be NOT_COVERED")
	}
}
