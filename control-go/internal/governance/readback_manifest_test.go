package governance

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestOverlayDeleteManifestBindsExactEntryButProvesAbsence(t *testing.T) {
	digest := "sha256:" + strings.Repeat("a", 64)
	entry := AppliedRuleReadback{EntityID: "entity-delete-1", RuleID: "overlay-rule-1",
		CanonicalEntryDigest: digest, MatchPriorityActionDigest: digest,
		TableID: 1, DirectCounterID: 2, Bank: 0}
	overlay, _ := json.Marshal([]map[string]any{{"rule_id": "overlay-rule-1", "enabled": true}})
	intent := Intent{Payload: EffectPayload{Operation: "overlay-delete", OverlayRules: overlay}}
	result := EdgeEffectResult{ExpectedEntries: 0, ObservedEntries: 0,
		AppliedEntries: []AppliedRuleReadback{entry}}
	result.ReadbackManifestDigest = ComputeReadbackManifestDigest(result.AppliedEntries)
	if err := validateAppliedReadbackManifest(intent, result); err != nil {
		t.Fatalf("exact delete proof rejected: %v", err)
	}
	result.ObservedEntries = 1
	result.ExpectedEntries = 1
	if err := validateAppliedReadbackManifest(intent, result); err == nil {
		t.Fatal("overlay delete must prove zero observed entries")
	}
}
