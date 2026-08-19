package governance

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"masi-nids/control-go/internal/security"
)

func validBaselineIntent() Intent {
	d := "sha256:" + strings.Repeat("a", 64)
	rules := json.RawMessage(`[{
		"rule_id":"rule-1","rule_revision":1,"canonical_rule_digest":"` + d + `",
		"priority":100,"ingress_port":"wildcard","src_ipv4":{"address":"192.0.2.0","prefix_length":24},
		"dst_ipv4":{"address":"198.51.100.10","prefix_length":32},"protocol":6,
		"l4_present":true,"src_port":"wildcard","dst_port":22,"fragment_class":"unfragmented",
		"action":"drop","enabled":true,"actor_ref":"issuer:admin","reason_code":"TEST_RULE"
	}]`)
	intent := Intent{
		EffectIntentID: "intent-1", OperationID: "operation-1", TargetID: "target-1",
		Fence: Fence{TargetControlIncarnationID: "incarnation-1", TargetAssignmentGeneration: 1,
			EdgeWorkloadRef: "edge-1", ActorRuntimeEpoch: "actor-epoch-1", ApplicationGeneration: 1, ElectionIDLow: 10,
			P4InfoDigest: d, PipelineDigest: d, CapacityDigest: d},
		AuthorizationDigest: d, EffectKind: KindFirewallBaselineActivate, RiskLevel: security.R3,
		RequiredWriteAtomicity: "CONTINUE_ON_ERROR", DeadlineUnixMS: time.Now().Add(time.Minute).UnixMilli(),
		Actor: security.Actor{Issuer: "https://issuer.example", Subject: "operator-1"}, TraceID: "trace-1",
		Payload: EffectPayload{SchemaVersion: "p4-effect-payload/v1", Operation: "baseline-activate",
			PolicyRevisionDigest: d, DefaultAction: "drop", BaselineRules: rules, OverlayRules: json.RawMessage(`[]`)},
	}
	intent.EffectDigest = ComputeEffectDigest(intent)
	return intent
}

func TestToEdgeEffectIntentBindsDeterministicProtobuf(t *testing.T) {
	intent := validBaselineIntent()
	wire, err := ToEdgeEffectIntent(intent)
	if err != nil {
		t.Fatal(err)
	}
	if wire.EffectDigest != intent.EffectDigest || len(wire.BaselineRules) != 1 ||
		wire.BaselineRules[0].DestinationPort == nil || !wire.BaselineRules[0].DestinationPort.Present ||
		wire.BaselineRules[0].DestinationPort.Value != 22 {
		t.Fatalf("wire mapping mismatch: %+v", wire)
	}
	if edgeEffectDigest(wire) != wire.EffectDigest {
		t.Fatal("effect digest does not match deterministic protobuf payload")
	}
	again, err := ToEdgeEffectIntent(intent)
	if err != nil || again.EffectDigest != wire.EffectDigest {
		t.Fatalf("mapping digest is not deterministic: %v", err)
	}
	changed := intent
	changed.Payload.DefaultAction = "permit-and-continue"
	changed.EffectDigest = ComputeEffectDigest(changed)
	if changed.EffectDigest == intent.EffectDigest {
		t.Fatal("payload change must change deterministic protobuf effect digest")
	}
}

func TestEffectPayloadRejectsUnknownOperation(t *testing.T) {
	intent := validBaselineIntent()
	intent.Payload.Operation = "shell-command"
	if err := ValidateEffectPayload(intent.Payload); err == nil {
		t.Fatal("unknown effect payload operation must fail closed")
	}
}

func TestToEdgeOverlayUsesExactPublicShape(t *testing.T) {
	intent := validBaselineIntent()
	d := "sha256:" + strings.Repeat("b", 64)
	expires := time.Now().UTC().Add(time.Minute).Truncate(time.Second)
	raw, err := json.Marshal([]map[string]any{{
		"rule_id": "overlay-1", "rule_revision": 1, "canonical_rule_digest": d,
		"stage_precedence": "before-baseline", "src_ipv4": "192.0.2.1",
		"dst_ipv4": "198.51.100.2", "protocol": 6, "src_port": 12345,
		"dst_port": 443, "action": "drop", "enabled": true,
		"expires_at": expires.Format(time.RFC3339), "expires_at_unix_ms": expires.UnixMilli(),
		"actor_ref": "issuer:operator", "reason_code": "TEST_OVERLAY",
	}})
	if err != nil {
		t.Fatal(err)
	}
	intent.Payload = EffectPayload{SchemaVersion: "p4-effect-payload/v1", Operation: "overlay-upsert",
		PolicyRevisionDigest: d, BaselineRules: json.RawMessage(`[]`), OverlayRules: raw}
	intent.EffectKind = KindFirewallOverlay
	intent.RiskLevel = security.R2
	intent.EffectDigest = ComputeEffectDigest(intent)
	wire, err := ToEdgeEffectIntent(intent)
	if err != nil || len(wire.OverlayRules) != 1 {
		t.Fatalf("exact overlay mapping rejected: wire=%+v err=%v", wire, err)
	}
	overlay := wire.OverlayRules[0]
	if overlay.Protocol != 6 || overlay.SourcePort != 12345 || overlay.DestinationPort != 443 ||
		overlay.ExpiresAtUnixMs != expires.UnixMilli() {
		t.Fatalf("overlay mapping drift: %+v", overlay)
	}
	var malformed []map[string]any
	_ = json.Unmarshal(raw, &malformed)
	malformed[0]["stage_precedence"] = "after-baseline"
	badRaw, _ := json.Marshal(malformed)
	intent.Payload.OverlayRules = badRaw
	if _, err := ToEdgeEffectIntent(intent); err == nil {
		t.Fatal("wrong overlay precedence must fail closed")
	}
}
