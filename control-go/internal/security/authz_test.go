package security

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestCanApproveR2MakerChecker(t *testing.T) {
	proposer := Actor{Issuer: "https://idp.example", Subject: "sub-proposer"}
	checker := Actor{Issuer: "https://idp.example", Subject: "sub-checker"}
	freshCtx := AuthzContext{StepUpType: StepUpWebAuthn, StepUpAgeMS: 120000, PhishingResistant: true, Level: LevelScopedOperator}

	if err := CanApprove(R2, proposer, LevelAnalyst, checker, freshCtx); err != nil {
		t.Fatalf("R2 different-actor fresh step-up should approve: %v", err)
	}
	// R2 self-approval rejected.
	if err := CanApprove(R2, proposer, LevelAnalyst, proposer, freshCtx); err == nil {
		t.Fatal("R2 self-approval must be rejected")
	}
	// R2 stale step-up rejected.
	stale := freshCtx
	stale.StepUpAgeMS = 400000
	if err := CanApprove(R2, proposer, LevelAnalyst, checker, stale); err == nil {
		t.Fatal("R2 stale step-up (>5min) must be rejected")
	}
	// R2 non-phishing-resistant rejected.
	weak := freshCtx
	weak.StepUpType = StepUpNone
	weak.PhishingResistant = false
	if err := CanApprove(R2, proposer, LevelAnalyst, checker, weak); err == nil {
		t.Fatal("R2 non-phishing-resistant step-up must be rejected")
	}
}

func TestCanApproveR1SameOperator(t *testing.T) {
	op := Actor{Issuer: "https://idp.example", Subject: "sub-op"}
	ctx := AuthzContext{Level: LevelOperator}
	if err := CanApprove(R1, op, LevelOperator, op, ctx); err != nil {
		t.Fatalf("R1 same operator should approve: %v", err)
	}
	other := Actor{Issuer: "https://idp.example", Subject: "sub-other"}
	if err := CanApprove(R1, op, LevelOperator, other, ctx); err == nil {
		t.Fatal("R1 different actor must be rejected")
	}
}

func TestCanApproveR3PlatformAdmin(t *testing.T) {
	maker := Actor{Issuer: "https://idp.example", Subject: "sub-admin"}
	checker := Actor{Issuer: "https://idp.example", Subject: "sub-op"}
	checkerCtx := AuthzContext{StepUpType: StepUpWebAuthn, StepUpAgeMS: 60000, PhishingResistant: true, Level: LevelOperator}
	if err := CanApprove(R3, maker, LevelPlatformAdmin, checker, checkerCtx); err != nil {
		t.Fatalf("R3 admin-maker + operator-checker should approve: %v", err)
	}
	if err := CanApprove(R3, maker, LevelPlatformAdmin, maker, checkerCtx); err == nil {
		t.Fatal("R3 self-approval must be rejected")
	}
	nonOperator := checkerCtx
	nonOperator.Level = LevelAnalyst
	if err := CanApprove(R3, maker, LevelPlatformAdmin, checker, nonOperator); err == nil {
		t.Fatal("R3 non-admin/non-scoped-operator checker must be rejected")
	}
}

func TestActorStableKey(t *testing.T) {
	a := Actor{Issuer: "https://idp.example", Subject: "sub-1"}
	key := a.String()
	// actor_ref MUST match the contracts identity pattern (no "/").
	if !identityRE.MatchString(key) {
		t.Fatalf("actor_ref %q does not match identity pattern", key)
	}
	if !strings.HasPrefix(key, "actor:") || len(key) != len("actor:")+64 {
		t.Fatalf("actor_ref = %q, want opaque actor:sha256", key)
	}
	// deterministic
	if a.String() != key {
		t.Fatal("actor_ref not deterministic")
	}
	// distinct actors produce distinct keys
	b := Actor{Issuer: "https://idp.example", Subject: "sub-2"}
	if a.String() == b.String() {
		t.Fatal("distinct subjects produced same actor_ref")
	}
	// Equal compares the full structured identity.
	if !a.Equal(Actor{Issuer: "https://idp.example", Subject: "sub-1"}) {
		t.Fatal("Equal mismatch")
	}
	if _, err := ParseActor("no-colon"); err == nil {
		t.Fatal("malformed actor_ref must be rejected")
	}
}

func TestRoleScopeMappingDefaultDeny(t *testing.T) {
	m := &RoleScopeMapping{Version: "v1", DefaultDeny: false, Digest: "sha256:" + strings.Repeat("a", 64)}
	if err := m.Validate(); err == nil {
		t.Fatal("default-deny=false must be rejected")
	}
	m.DefaultDeny = true
	if err := m.Validate(); err != nil {
		t.Fatalf("valid mapping: %v", err)
	}
	actor := Actor{Issuer: "https://idp.example", Subject: "sub-x"}
	if _, err := m.Authorize(actor, LevelOperator); err == nil {
		t.Fatal("absent actor must be denied (default-deny)")
	}
}

func TestRoleScopeMappingRejectsAllZeroDigest(t *testing.T) {
	m := &RoleScopeMapping{Version: "v1", DefaultDeny: true, Digest: zeroDigest, ActorScopes: map[string][]Scope{}}
	if err := m.Validate(); err == nil {
		t.Fatal("all-zero role mapping digest must be rejected")
	}
}

func TestSharedDigestAndRevisionValidatorsRejectSentinels(t *testing.T) {
	if ValidDigest("sha256:" + strings.Repeat("0", 64)) {
		t.Fatal("all-zero SHA-256 sentinel must be rejected")
	}
	if !ValidDigest("sha256:" + strings.Repeat("a", 64)) {
		t.Fatal("canonical non-zero SHA-256 digest rejected")
	}
	if ValidRevision(strings.Repeat("0", 40)) {
		t.Fatal("all-zero revision sentinel must be rejected")
	}
	if !ValidRevision(strings.Repeat("0", 39) + "3") {
		t.Fatal("canonical non-zero 40-hex revision rejected")
	}
}

func TestRoleMappingContentDigestRejectsTamper(t *testing.T) {
	actor := Actor{Issuer: "https://idp.example", Subject: "u1"}
	m := RoleScopeMapping{Version: "v1", DefaultDeny: true, ActorScopes: map[string][]Scope{
		actor.String(): []Scope{{ScopeID: "scope-1", TargetSetDigest: "sha256:" + strings.Repeat("a", 64), EffectKinds: []string{"source-read"}, Levels: []AuthzContextLevel{LevelAnalyst}}},
	}}
	digest, err := RoleMappingContentDigest(m)
	if err != nil {
		t.Fatal(err)
	}
	m.Digest = digest
	path := filepath.Join(t.TempDir(), "mapping.json")
	raw, _ := json.Marshal(m)
	if err := os.WriteFile(path, raw, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadRoleScopeMapping(path, digest); err != nil {
		t.Fatal(err)
	}
	m.ActorScopes[actor.String()][0].EffectKinds = []string{"plugin.statistics.run"}
	raw, _ = json.Marshal(m)
	if err := os.WriteFile(path, raw, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadRoleScopeMapping(path, digest); err == nil {
		t.Fatal("tampered mapping with stale declared digest must fail")
	}
}
