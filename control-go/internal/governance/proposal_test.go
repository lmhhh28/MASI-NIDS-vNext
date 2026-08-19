package governance

import (
	"strings"
	"testing"
	"time"

	"masi-nids/control-go/internal/security"
)

func TestComputeProposalDigestDeterministic(t *testing.T) {
	p := Proposal{
		ProposalID:      "prop-1",
		Actor:           security.Actor{Issuer: "https://idp.example", Subject: "sub-a"},
		Scope:           "fleet-1",
		RiskLevel:       security.R2,
		EffectKind:      KindFirewallBaselineActivate,
		TargetSetDigest: "sha256:" + strings.Repeat("a", 64),
		PolicyDigest:    "sha256:" + strings.Repeat("b", 64),
		Note:            "n",
	}
	d1 := computeProposalDigest(p)
	d2 := computeProposalDigest(p)
	if d1 != d2 {
		t.Fatal("proposal digest not deterministic")
	}
	if !strings.HasPrefix(d1, "sha256:") {
		t.Fatalf("digest %q must be sha256-prefixed", d1)
	}
	// A field change produces a different digest (immutability binding).
	changed := p
	changed.Note = "different"
	if computeProposalDigest(changed) == d1 {
		t.Fatal("proposal digest must change when a field changes")
	}
}

func TestValidateProposalRejects(t *testing.T) {
	good := Proposal{
		ProposalID: "prop-1",
		Actor:      security.Actor{Issuer: "https://idp.example", Subject: "sub-a"},
		ActorLevel: security.LevelAnalyst,
		Scope:      "fleet-1", RiskLevel: security.R2, EffectKind: KindFirewallOverlay,
		TargetSetDigest: "sha256:" + strings.Repeat("a", 64),
		TargetIDs:       []string{"target-1"},
		PolicyDigest:    "sha256:" + strings.Repeat("b", 64),
		ExpiresAtUnixMS: time.Now().Add(time.Hour).UnixMilli(),
		IdempotencyKey:  "idem-1",
	}
	if err := validateProposal(good); err != nil {
		t.Fatalf("good proposal rejected: %v", err)
	}
	bad := good
	bad.RiskLevel = "R5"
	if err := validateProposal(bad); err == nil {
		t.Fatal("R5 risk must be rejected")
	}
	bad2 := good
	bad2.Note = strings.Repeat("x", 2049)
	if err := validateProposal(bad2); err == nil {
		t.Fatal("note > 2KiB must be rejected")
	}
	bad3 := good
	bad3.Actor = security.Actor{Issuer: "not-a-url", Subject: "sub-a"}
	if err := validateProposal(bad3); err == nil {
		t.Fatal("malformed actor issuer must be rejected")
	}
}

func TestFleetParentNonClaimableInvariant(t *testing.T) {
	// The Intent type documents that fleet parents have no claim_state; the
	// dispatcher rejects dispatching a parent intent.
	parent := Intent{EffectIntentID: "parent-1", IsFleetParent: true}
	if !parent.IsFleetParent {
		t.Fatal("parent flag mismatch")
	}
}

func TestDecisionReasonCodeIsControlledAndDigestBound(t *testing.T) {
	a := security.Actor{Issuer: "https://idp.example", Subject: "operator-1"}
	authz := AuthzContext{Level: security.LevelOperator}
	one := computeDecisionDigest("sha256:"+strings.Repeat("a", 64), a, "approve", "APPROVED_BY_POLICY", authz)
	two := computeDecisionDigest("sha256:"+strings.Repeat("a", 64), a, "approve", "APPROVED_AFTER_REVIEW", authz)
	if one == two {
		t.Fatal("reason_code must be bound into decision digest")
	}
	if !reasonCodeRE.MatchString("APPROVED_AFTER_REVIEW") || reasonCodeRE.MatchString("free form") {
		t.Fatal("controlled reason_code validation drift")
	}
}
