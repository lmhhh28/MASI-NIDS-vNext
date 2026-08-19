package firewall

import (
	"context"
	"strings"
	"testing"
	"time"
)

func validRule(id string, priority int, action string) Rule {
	r := Rule{
		RuleID: id, RuleRevision: 1, Priority: priority,
		SourceIPv4:      IPv4Prefix{Address: "0.0.0.0", PrefixLength: 0},
		DestinationIPv4: IPv4Prefix{Address: "0.0.0.0", PrefixLength: 0},
		FragmentClass:   FragmentWildcard, Action: action, Enabled: true,
		ActorRef: "issuer:actor", ReasonCode: "TEST_RULE",
	}
	r.CanonicalRuleDigest = ComputeRuleDigest(r)
	return r
}

func TestValidateRevision(t *testing.T) {
	good := Revision{
		RevisionID:    "rev-1",
		TargetID:      "tgt-1",
		DefaultAction: DefaultDrop,
		Scope:         "fleet-1",
		Rules: []Rule{
			validRule("r-1", 10, "permit-and-continue"),
		},
	}
	if err := validateRevision(good); err != nil {
		t.Fatalf("good revision rejected: %v", err)
	}
	// bad default action
	bad := good
	bad.DefaultAction = "allow"
	if err := validateRevision(bad); err == nil {
		t.Fatal("bad default_action must be rejected")
	}
	// too many rules
	tooMany := good
	tooMany.Rules = make([]Rule, 4097)
	for i := range tooMany.Rules {
		tooMany.Rules[i] = validRule("r", 1, "drop")
	}
	if err := validateRevision(tooMany); err == nil {
		t.Fatal(">4096 rules must be rejected")
	}
	// bad action
	badAction := good
	badRule := validRule("r-1", 10, "drop")
	badRule.Action = "reject"
	badAction.Rules = []Rule{badRule}
	if err := validateRevision(badAction); err == nil {
		t.Fatal("bad rule action must be rejected")
	}
	// missing canonical digest
	noDigest := good
	missingDigestRule := validRule("r-1", 10, "drop")
	missingDigestRule.CanonicalRuleDigest = ""
	noDigest.Rules = []Rule{missingDigestRule}
	if err := validateRevision(noDigest); err == nil {
		t.Fatal("missing canonical_rule_digest must be rejected")
	}
	badHex := good
	badHexRule := validRule("r-1", 10, "drop")
	badHexRule.CanonicalRuleDigest = "sha256:" + strings.Repeat("z", 64)
	badHex.Rules = []Rule{badHexRule}
	if err := validateRevision(badHex); err == nil {
		t.Fatal("non-hex canonical_rule_digest must be rejected")
	}
	// priority out of bounds
	badPri := good
	badPriRule := validRule("r-1", 1, "drop")
	badPriRule.Priority = 0
	badPri.Rules = []Rule{badPriRule}
	if err := validateRevision(badPri); err == nil {
		t.Fatal("priority < 1 must be rejected")
	}
}

func TestOverlayRuleUsesExactPublicContract(t *testing.T) {
	s := &OverlayService{}
	s.now = func() time.Time { return time.UnixMilli(1000) }
	rule := OverlayRule{RuleID: "rule-1", RuleRevision: 1, StagePrecedence: "before-baseline",
		SourceIPv4: "192.0.2.1", DestinationIPv4: "198.51.100.2", Protocol: 6,
		SourcePort: 12345, DestinationPort: 443, Action: "drop", Enabled: true,
		ExpiresAt: time.UnixMilli(2000).UTC().Format(time.RFC3339Nano), ActorRef: "issuer:actor", ReasonCode: "TEST_OVERLAY"}
	rule.CanonicalRuleDigest = ComputeOverlayRuleDigest(rule)
	if err := validateOverlayRule(rule, 2000, time.UnixMilli(1000)); err != nil {
		t.Fatalf("valid overlay contract rejected: %v", err)
	}
	bad := Overlay{OverlayRuleID: "ov-1", TargetID: "target-1", ExpiresAtUnixMS: 2000,
		Rule: rule}
	bad.Rule.CanonicalRuleDigest = "sha256:" + strings.Repeat("z", 64)
	if err := s.Create(context.Background(), bad); err == nil {
		t.Fatal("overlay must reject malformed rule before touching PostgreSQL")
	}
}

func TestRevisionDigestDeterministic(t *testing.T) {
	rev := Revision{
		RevisionID: "rev-1", TargetID: "tgt-1", DefaultAction: DefaultDrop, Scope: "fleet-1",
		Rules: []Rule{
			validRule("r-2", 20, "drop"),
			validRule("r-1", 10, "permit-and-continue"),
		},
	}
	d1 := computeRevisionDigest(rev)
	d2 := computeRevisionDigest(rev)
	if d1 != d2 {
		t.Fatal("revision digest not deterministic")
	}
	if !strings.HasPrefix(d1, "sha256:") {
		t.Fatalf("digest %q must be sha256-prefixed", d1)
	}
	// Rule order in the input must NOT affect the digest (sorted by priority).
	reordered := rev
	reordered.Rules = []Rule{rev.Rules[1], rev.Rules[0]}
	if computeRevisionDigest(reordered) != d1 {
		t.Fatal("revision digest must be order-independent (sorted by priority)")
	}
	otherTarget := rev
	otherTarget.TargetID = "tgt-2"
	if computeRevisionDigest(otherTarget) != d1 {
		t.Fatal("normalized policy digest must be fleet-portable across target identity")
	}
	// A field change produces a different digest.
	changed := rev
	changed.DefaultAction = DefaultPermitAndContinue
	if computeRevisionDigest(changed) == d1 {
		t.Fatal("digest must change when default_action changes")
	}
}

func TestSamePriorityOnlyRejectsOverlappingMatches(t *testing.T) {
	left := validRule("left", 100, "drop")
	left.SourceIPv4 = IPv4Prefix{Address: "192.0.2.0", PrefixLength: 25}
	left.CanonicalRuleDigest = ComputeRuleDigest(left)
	right := validRule("right", 100, "permit-and-continue")
	right.SourceIPv4 = IPv4Prefix{Address: "192.0.2.128", PrefixLength: 25}
	right.CanonicalRuleDigest = ComputeRuleDigest(right)
	revision := Revision{RevisionID: "rev-disjoint", TargetID: "target-1", Scope: "scope-1",
		DefaultAction: DefaultDrop, Rules: []Rule{left, right}}
	if err := validateRevision(revision); err != nil {
		t.Fatalf("disjoint same-priority rules rejected: %v", err)
	}
	right.SourceIPv4 = IPv4Prefix{Address: "192.0.2.0", PrefixLength: 24}
	right.CanonicalRuleDigest = ComputeRuleDigest(right)
	revision.Rules = []Rule{left, right}
	if err := validateRevision(revision); err == nil {
		t.Fatal("overlapping same-priority rules must be rejected")
	}
}

func TestActivationStageOrder(t *testing.T) {
	// The canonical order has 8 stages, starting prepared, ending cleanup.
	if len(ActivationStagesInOrder) != 8 {
		t.Fatalf("expected 8 stages, got %d", len(ActivationStagesInOrder))
	}
	if ActivationStagesInOrder[0] != StagePrepared {
		t.Fatal("first stage must be prepared")
	}
	if ActivationStagesInOrder[7] != StageCleanup {
		t.Fatal("last stage must be cleanup")
	}
	// current_committed comes after selector_verified.
	if StageIndex(StageCurrentCommitted) <= StageIndex(StageSelectorVerified) {
		t.Fatal("current_committed must come after selector_verified")
	}
}

func TestReachedStage(t *testing.T) {
	completed := []ActivationStage{StagePrepared, StageInactiveWriting, StageInactiveVerified}
	if !ReachedStage(completed, StagePrepared) {
		t.Fatal("should have reached prepared")
	}
	if !ReachedStage(completed, StageInactiveVerified) {
		t.Fatal("should have reached inactive_verified")
	}
	if ReachedStage(completed, StageSelectorSwitching) {
		t.Fatal("should NOT have reached selector_switching")
	}
	if ReachedStage(completed, ActivationStage("bogus")) {
		t.Fatal("unknown stage should not be reached")
	}
}
