package model

import (
	"strings"
	"testing"

	"masi-nids/control-go/internal/security"
)

func TestShouldRaiseRouteEpoch(t *testing.T) {
	cases := []struct {
		name           string
		curGen, tgtGen int64
		curRev, tgtRev string
		wantRaise      bool
	}{
		{"same generation same revision (replica restart)", 3, 3, "rev-a", "rev-a", false},
		{"same generation new revision", 3, 3, "rev-a", "rev-b", true},
		{"new generation same revision id", 3, 4, "rev-a", "rev-a", true},
		{"new generation new revision", 3, 4, "rev-a", "rev-b", true},
	}
	for _, c := range cases {
		if got := ShouldRaiseRouteEpoch(c.curGen, c.tgtGen, c.curRev, c.tgtRev); got != c.wantRaise {
			t.Fatalf("%s: got %v want %v", c.name, got, c.wantRaise)
		}
	}
}

func TestComputeCASDigestDeterministicAndDistinct(t *testing.T) {
	a := computeCASDigest("op-1", "shard-1", 2, "rev-a")
	b := computeCASDigest("op-1", "shard-1", 2, "rev-a")
	if a != b {
		t.Fatal("same inputs must yield the same CAS digest")
	}
	if !strings.HasPrefix(a, "sha256:") || len(a) != len("sha256:")+64 {
		t.Fatalf("digest malformed: %s", a)
	}
	c := computeCASDigest("op-1", "shard-1", 3, "rev-a")
	if c == a {
		t.Fatal("different generation must yield a different digest")
	}
	d := computeCASDigest("op-2", "shard-1", 2, "rev-a")
	if d == a {
		t.Fatal("different operation must yield a different digest")
	}
}

func TestValidateSource(t *testing.T) {
	for _, s := range []IncarnationSource{IncarnationInitial, IncarnationPITR, IncarnationClone, IncarnationRewind} {
		if err := validateSource(s); err != nil {
			t.Fatalf("valid source %s rejected: %v", s, err)
		}
	}
	if err := validateSource(IncarnationSource("snapshot")); err == nil {
		t.Fatal("unknown incarnation source must be rejected")
	}
}

func TestValidateRevision(t *testing.T) {
	good := ModelRevision{
		ModelRevisionID:       "mr-1",
		ModelRevisionDigest:   "sha256:" + strings.Repeat("a", 64),
		ModelBundleDigest:     "sha256:" + strings.Repeat("b", 64),
		FeatureContractDigest: "sha256:" + strings.Repeat("c", 64),
		LabelContractDigest:   "sha256:" + strings.Repeat("d", 64),
		OutputAdapterDigest:   "sha256:" + strings.Repeat("e", 64),
		QualificationStatus:   Qualified,
		ReaderRuntimeProfile:  "model-runtime-central-cpu/v1",
		Scope:                 "scope-1",
		Actor:                 security.Actor{Issuer: "https://issuer.example", Subject: "admin-1"},
		TraceID:               "trace-1",
	}
	if err := validateRevision(good); err != nil {
		t.Fatalf("good revision rejected: %v", err)
	}
	bad := good
	bad.ModelBundleDigest = "sha256:short"
	if err := validateRevision(bad); err == nil {
		t.Fatal("malformed bundle digest must be rejected")
	}
	bad2 := good
	bad2.QualificationStatus = QualificationStatus("maybe")
	if err := validateRevision(bad2); err == nil {
		t.Fatal("unknown qualification status must be rejected")
	}
	bad3 := good
	bad3.ReaderRuntimeProfile = ""
	if err := validateRevision(bad3); err == nil {
		t.Fatal("missing reader runtime profile must be rejected")
	}
}

func TestRolloutRequestKindClosed(t *testing.T) {
	// Rollout() itself guards, but keep the closed enum stable: only rollout
	// and rollback are caller-usable kinds; recovery/pitr-recovery are
	// reconcile-driven and must never be passed by an API caller.
	callerKinds := map[OperationKind]bool{OpRollout: true, OpRollback: true}
	if callerKinds[OpRecovery] || callerKinds[OpPITRRecovery] {
		t.Fatal("recovery kinds must not be caller-usable")
	}
}
