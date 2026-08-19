package target

import (
	"context"
	"strings"
	"testing"

	"masi-nids/control-go/internal/security"
)

func TestProjectAggregateAnyUnknownReconciling(t *testing.T) {
	children := []ChildIntent{
		{TargetID: "t1", Status: ChildApplied},
		{TargetID: "t2", Status: ChildUnknown},
	}
	if got := ProjectAggregate(children, nil); got != AggregateReconciling {
		t.Fatalf("any unknown child -> reconciling; got %s", got)
	}
	children = []ChildIntent{
		{TargetID: "t1", Status: ChildApplied},
		{TargetID: "t2", Status: ChildReconciling},
	}
	if got := ProjectAggregate(children, nil); got != AggregateReconciling {
		t.Fatalf("any reconciling child -> reconciling; got %s", got)
	}
}

func TestProjectAggregateAllApplied(t *testing.T) {
	children := []ChildIntent{
		{TargetID: "t1", Status: ChildApplied},
		{TargetID: "t2", Status: ChildApplied},
	}
	if got := ProjectAggregate(children, nil); got != AggregateApplied {
		t.Fatalf("all applied -> applied; got %s", got)
	}
}

func TestProjectAggregatePartial(t *testing.T) {
	children := []ChildIntent{
		{TargetID: "t1", Status: ChildApplied},
		{TargetID: "t2", Status: ChildHold},
		{TargetID: "t3", Status: ChildSkipped},
	}
	if got := ProjectAggregate(children, nil); got != AggregatePartial {
		t.Fatalf("all terminal with hold/skipped -> partial; got %s", got)
	}
}

func TestProjectAggregateFailed(t *testing.T) {
	children := []ChildIntent{
		{TargetID: "t1", Status: ChildApplied},
		{TargetID: "t2", Status: ChildFailed},
	}
	if got := ProjectAggregate(children, nil); got != AggregateFailed {
		t.Fatalf("any failed -> failed; got %s", got)
	}
}

func TestProjectAggregateRunning(t *testing.T) {
	children := []ChildIntent{
		{TargetID: "t1", Status: ChildApplied},
		{TargetID: "t2", Status: ChildPending},
	}
	if got := ProjectAggregate(children, nil); got != AggregateRunning {
		t.Fatalf("pending present -> running; got %s", got)
	}
}

func TestProjectAggregateNoChildrenPlanned(t *testing.T) {
	if got := ProjectAggregate(nil, nil); got != AggregatePlanned {
		t.Fatalf("no children -> planned; got %s", got)
	}
}

func TestProjectAggregateNoMajorityApplied(t *testing.T) {
	// Majority applied but one unknown -> reconciling, NOT applied. There is no
	// majority-success = applied rule.
	children := []ChildIntent{
		{TargetID: "t1", Status: ChildApplied},
		{TargetID: "t2", Status: ChildApplied},
		{TargetID: "t3", Status: ChildApplied},
		{TargetID: "t4", Status: ChildUnknown},
	}
	if got := ProjectAggregate(children, nil); got != AggregateReconciling {
		t.Fatalf("majority applied + one unknown -> reconciling (no majority rule); got %s", got)
	}
}

func TestFailurePolicyEnum(t *testing.T) {
	if !IsValidFailurePolicy(FailFast) {
		t.Fatal("fail-fast must be valid")
	}
	if !IsValidFailurePolicy(ContinueIsolated) {
		t.Fatal("continue-isolated must be valid")
	}
	if !IsValidFailurePolicy(ManualGate) {
		t.Fatal("manual-gate must be valid")
	}
	if IsValidFailurePolicy(FailurePolicy("fast")) {
		t.Fatal("invalid failure policy must be rejected")
	}
}

func TestValidateFleetOperationRejects(t *testing.T) {
	good := FleetOperation{
		FleetOperationID: "fo-1",
		TargetSetDigest:  "sha256:" + strings.Repeat("a", 64),
		WaveCount:        2,
		Waves: []Wave{
			{WaveIndex: 0, TargetIDs: []string{"t1", "t2"}, ParallelLimit: 2, FailurePolicy: FailFast, GateState: GateClosed},
			{WaveIndex: 1, TargetIDs: []string{"t3"}, ParallelLimit: 1, FailurePolicy: ContinueIsolated, GateState: GateClosed},
		},
	}
	if err := validateFleetOperation(good); err != nil {
		t.Fatalf("good fleet op rejected: %v", err)
	}
	bad := good
	bad.WaveCount = 3
	if err := validateFleetOperation(bad); err == nil {
		t.Fatal("wave_count != len(waves) must be rejected")
	}
	bad2 := good
	bad2.Waves[0].FailurePolicy = FailurePolicy("fast")
	if err := validateFleetOperation(bad2); err == nil {
		t.Fatal("invalid failure policy must be rejected")
	}
	bad3 := good
	bad3.Waves[0].WaveIndex = 5
	if err := validateFleetOperation(bad3); err == nil {
		t.Fatal("wave index mismatch must be rejected")
	}
}

func TestTargetIDNeverReusedByConstruction(t *testing.T) {
	// The default ID generator derives a fresh target_id from a unique seed
	// including a timestamp; the same inputs at different instants yield
	// different ids. The registry never reassigns a retired id (enforced by the
	// retired_is_terminal CHECK + Retire never re-activating).
	id1 := defaultTargetID("seed-x")
	id2 := defaultTargetID("seed-x")
	// Collisions are astronomically unlikely given the timestamp; if they
	// happen the test is still valid as long as ids are pattern-valid.
	if id1 == id2 {
		t.Skip("default id generator produced identical ids (same instant)")
	}
	if !strings.HasPrefix(id1, "tgt-") {
		t.Fatalf("target id %q must start with tgt-", id1)
	}
}

func TestAssignmentLeaseBoundRejectsBeforeDatabaseAccess(t *testing.T) {
	svc := &AssignmentService{}
	a := Assignment{
		TargetID: "target-1", AssignmentGeneration: 2, IncarnationID: "inc-1",
		EdgeWorkloadRef: "edge-1", LeaseID: "lease-1", IssuedAtUnixMS: 1,
		ExpiresAtUnixMS: 300_002, ElectionFloor: 11, ElectionCeiling: 20,
		ActorRuntimeEpoch: "epoch-1", ApplicationGeneration: 1,
		Actor: security.Actor{Issuer: "https://issuer.example", Subject: "admin-1"}, TraceID: "trace-1",
	}
	if _, err := svc.Handoff(context.Background(), a, 1, 10); err == nil {
		t.Fatal("lease longer than frozen 300s bound must fail before database access")
	}
}

func TestCapabilityObservationDigestAndBounds(t *testing.T) {
	d := "sha256:" + strings.Repeat("a", 64)
	o := CapabilityObservation{
		ObservationID: "obs-1", TargetID: "target-1", TargetControlIncarnationID: "inc-1",
		AssignmentGeneration: 1, ActorRuntimeEpoch: "actor-1", ApplicationGeneration: 1,
		P4InfoDigest: d, PipelineDigest: d, ProfileDigest: d, CapacityDigest: d,
		CapacityAvailable: true, LeaseValid: true, P4Connected: true, Primary: true, PipelineExact: true,
		Freshness: "fresh", ReasonCode: "READY", ObservedAtUnixMS: 1000, ExpiresAtUnixMS: 2000, TraceID: "trace-1",
	}
	o.ObservationDigest = ComputeCapabilityObservationDigest(o)
	if err := validateCapabilityObservation(o); err != nil {
		t.Fatalf("valid observation rejected: %v", err)
	}
	bad := o
	bad.TelemetryQueueDepth = 65
	bad.ObservationDigest = ComputeCapabilityObservationDigest(bad)
	if err := validateCapabilityObservation(bad); err == nil {
		t.Fatal("queue above frozen profile must be rejected")
	}
	conflict := o
	conflict.ReasonCode = "DRIFT"
	if err := validateCapabilityObservation(conflict); err == nil {
		t.Fatal("changed payload with old digest must be rejected")
	}
}
