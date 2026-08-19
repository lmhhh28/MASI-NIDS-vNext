package governance

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"time"

	"google.golang.org/protobuf/proto"

	edgev1 "masi-nids/control-go/internal/grpc/edgev1"
)

type normalizedPrefix struct {
	Address      string `json:"address"`
	PrefixLength uint32 `json:"prefix_length"`
}

type normalizedBaselineRule struct {
	RuleID              string           `json:"rule_id"`
	RuleRevision        int64            `json:"rule_revision"`
	CanonicalRuleDigest string           `json:"canonical_rule_digest"`
	Priority            int32            `json:"priority"`
	IngressPort         json.RawMessage  `json:"ingress_port"`
	Source              normalizedPrefix `json:"src_ipv4"`
	Destination         normalizedPrefix `json:"dst_ipv4"`
	Protocol            json.RawMessage  `json:"protocol"`
	L4Present           json.RawMessage  `json:"l4_present"`
	SourcePort          json.RawMessage  `json:"src_port"`
	DestinationPort     json.RawMessage  `json:"dst_port"`
	FragmentClass       string           `json:"fragment_class"`
	Action              string           `json:"action"`
	Enabled             bool             `json:"enabled"`
}

type normalizedOverlayRule struct {
	RuleID              string `json:"rule_id"`
	RuleRevision        int64  `json:"rule_revision"`
	CanonicalRuleDigest string `json:"canonical_rule_digest"`
	StagePrecedence     string `json:"stage_precedence"`
	Source              string `json:"src_ipv4"`
	Destination         string `json:"dst_ipv4"`
	Protocol            uint32 `json:"protocol"`
	SourcePort          uint32 `json:"src_port"`
	DestinationPort     uint32 `json:"dst_port"`
	Action              string `json:"action"`
	Enabled             bool   `json:"enabled"`
	ExpiresAt           string `json:"expires_at"`
	ExpiresAtUnixMS     int64  `json:"expires_at_unix_ms"`
}

// ToEdgeEffectIntent is the tested explicit adapter from Go's persisted
// normalized payload to contracts/edge/v1. It contains no network behavior.
func ToEdgeEffectIntent(intent Intent) (*edgev1.EffectIntent, error) {
	if err := ValidateFence(intent.Fence); err != nil {
		return nil, err
	}
	if err := ValidateEffectPayload(intent.Payload); err != nil {
		return nil, err
	}
	fence := &edgev1.Fence{
		TargetControlIncarnationId: intent.Fence.TargetControlIncarnationID,
		TargetAssignmentGeneration: uint64(intent.Fence.TargetAssignmentGeneration),
		ActorRuntimeEpoch:          intent.Fence.ActorRuntimeEpoch,
		ApplicationGeneration:      uint64(intent.Fence.ApplicationGeneration),
		ElectionIdHigh:             intent.Fence.ElectionIDHigh, ElectionIdLow: intent.Fence.ElectionIDLow,
	}
	out := &edgev1.EffectIntent{
		SchemaVersion: "effect-intent-edge/v1", EffectIntentId: intent.EffectIntentID,
		OperationId: intent.OperationID, TargetId: intent.TargetID, Fence: fence,
		AuthorizationDigest:  intent.AuthorizationDigest,
		PolicyRevisionDigest: intent.Payload.PolicyRevisionDigest,
		DeadlineUnixMs:       intent.DeadlineUnixMS, ActorRef: intent.Actor.String(),
		TraceId: intent.TraceID,
	}
	switch intent.RequiredWriteAtomicity {
	case "CONTINUE_ON_ERROR":
		out.RequiredWriteAtomicity = edgev1.P4WriteAtomicity_P4_WRITE_ATOMICITY_CONTINUE_ON_ERROR
	case "DATAPLANE_ATOMIC":
		out.RequiredWriteAtomicity = edgev1.P4WriteAtomicity_P4_WRITE_ATOMICITY_DATAPLANE_ATOMIC
	default:
		return nil, fmt.Errorf("governance: unsupported write atomicity %q", intent.RequiredWriteAtomicity)
	}
	switch intent.Payload.Operation {
	case "baseline-activate":
		out.Kind = edgev1.EffectKind_EFFECT_KIND_BASELINE_ACTIVATE
		out.ReasonCode = "BASELINE_ACTIVATION"
		action, err := edgeFirewallAction(intent.Payload.DefaultAction)
		if err != nil {
			return nil, err
		}
		out.DefaultAction = action
		var rules []normalizedBaselineRule
		if err := json.Unmarshal(intent.Payload.BaselineRules, &rules); err != nil {
			return nil, fmt.Errorf("governance: decode baseline rules: %w", err)
		}
		for _, rule := range rules {
			if !rule.Enabled {
				continue
			}
			mapped, err := edgeBaselineRule(rule)
			if err != nil {
				return nil, err
			}
			out.BaselineRules = append(out.BaselineRules, mapped)
		}
	case "overlay-upsert", "overlay-delete":
		if intent.Payload.Operation == "overlay-delete" {
			out.Kind = edgev1.EffectKind_EFFECT_KIND_OVERLAY_DELETE
			out.ReasonCode = "OVERLAY_DELETE"
		} else {
			out.Kind = edgev1.EffectKind_EFFECT_KIND_OVERLAY_UPSERT
			out.ReasonCode = "OVERLAY_UPSERT"
		}
		var rules []normalizedOverlayRule
		if err := json.Unmarshal(intent.Payload.OverlayRules, &rules); err != nil {
			return nil, fmt.Errorf("governance: decode overlay rules: %w", err)
		}
		for _, rule := range rules {
			if !rule.Enabled {
				return nil, errors.New("governance: disabled overlay cannot become an effect")
			}
			mapped, err := edgeOverlayRule(rule)
			if err != nil {
				return nil, err
			}
			out.OverlayRules = append(out.OverlayRules, mapped)
		}
	default:
		return nil, errors.New("governance: unsupported effect payload operation")
	}
	out.EffectDigest = edgeEffectDigest(out)
	return out, nil
}

func edgeBaselineRule(rule normalizedBaselineRule) (*edgev1.BaselineRule, error) {
	if rule.RuleRevision < 1 {
		return nil, errors.New("governance: baseline rule revision invalid")
	}
	source, err := edgePrefix(rule.Source)
	if err != nil {
		return nil, err
	}
	destination, err := edgePrefix(rule.Destination)
	if err != nil {
		return nil, err
	}
	ingress, err := edgeOptionalUint32(rule.IngressPort, 510)
	if err != nil {
		return nil, err
	}
	protocol, err := edgeOptionalUint32(rule.Protocol, 255)
	if err != nil {
		return nil, err
	}
	sourcePort, err := edgeOptionalUint32(rule.SourcePort, 65535)
	if err != nil {
		return nil, err
	}
	destinationPort, err := edgeOptionalUint32(rule.DestinationPort, 65535)
	if err != nil {
		return nil, err
	}
	l4, err := edgeOptionalBool(rule.L4Present)
	if err != nil {
		return nil, err
	}
	fragmentValue := uint32(0)
	fragmentPresent := true
	switch rule.FragmentClass {
	case "wildcard":
		fragmentPresent = false
	case "unfragmented":
		fragmentValue = 0
	case "first-fragment":
		fragmentValue = 1
	case "non-initial-fragment":
		fragmentValue = 2
	default:
		return nil, errors.New("governance: unknown fragment class")
	}
	action, err := edgeFirewallAction(rule.Action)
	if err != nil {
		return nil, err
	}
	return &edgev1.BaselineRule{
		RuleId: rule.RuleID, RuleRevision: uint64(rule.RuleRevision),
		CanonicalRuleDigest: rule.CanonicalRuleDigest, Priority: rule.Priority,
		IngressPort: ingress, Source: source, Destination: destination, Protocol: protocol,
		L4Present: l4, SourcePort: sourcePort, DestinationPort: destinationPort,
		FragmentClass: &edgev1.OptionalUint32{Present: fragmentPresent, Value: fragmentValue},
		Action:        action,
	}, nil
}

func edgeOverlayRule(rule normalizedOverlayRule) (*edgev1.OverlayRule, error) {
	if rule.RuleRevision < 1 || rule.ExpiresAtUnixMS < 1 || rule.StagePrecedence != "before-baseline" {
		return nil, errors.New("governance: overlay requires revision, expiry, and before-baseline precedence")
	}
	expires, err := time.Parse(time.RFC3339, rule.ExpiresAt)
	if err != nil || expires.UnixMilli() != rule.ExpiresAtUnixMS {
		return nil, errors.New("governance: overlay expiry identity mismatch")
	}
	sourceAddr, err := netip.ParseAddr(rule.Source)
	if err != nil || !sourceAddr.Is4() {
		return nil, errors.New("governance: overlay source IPv4 malformed")
	}
	destinationAddr, err := netip.ParseAddr(rule.Destination)
	if err != nil || !destinationAddr.Is4() {
		return nil, errors.New("governance: overlay destination IPv4 malformed")
	}
	if rule.Protocol != 6 && rule.Protocol != 17 {
		return nil, errors.New("governance: overlay protocol must be TCP or UDP")
	}
	source, err := edgePrefix(normalizedPrefix{Address: sourceAddr.String(), PrefixLength: 32})
	if err != nil {
		return nil, err
	}
	destination, err := edgePrefix(normalizedPrefix{Address: destinationAddr.String(), PrefixLength: 32})
	if err != nil {
		return nil, err
	}
	action, err := edgeFirewallAction(rule.Action)
	if err != nil {
		return nil, err
	}
	return &edgev1.OverlayRule{
		RuleId: rule.RuleID, RuleRevision: uint64(rule.RuleRevision),
		CanonicalRuleDigest: rule.CanonicalRuleDigest, SourceIpv4: source.Address,
		DestinationIpv4: destination.Address,
		Protocol:        rule.Protocol,
		SourcePort:      rule.SourcePort,
		DestinationPort: rule.DestinationPort,
		Action:          action, ExpiresAtUnixMs: rule.ExpiresAtUnixMS,
	}, nil
}

func edgePrefix(prefix normalizedPrefix) (*edgev1.Ipv4Prefix, error) {
	p, err := netip.ParsePrefix(fmt.Sprintf("%s/%d", prefix.Address, prefix.PrefixLength))
	if err != nil || !p.Addr().Is4() || p != p.Masked() {
		return nil, errors.New("governance: IPv4 prefix malformed or not normalized")
	}
	octets := p.Addr().As4()
	return &edgev1.Ipv4Prefix{Address: binary.BigEndian.Uint32(octets[:]), PrefixLength: prefix.PrefixLength}, nil
}

func edgeOptionalUint32(raw json.RawMessage, maximum uint32) (*edgev1.OptionalUint32, error) {
	if string(raw) == `"wildcard"` {
		return &edgev1.OptionalUint32{}, nil
	}
	var value uint32
	if err := json.Unmarshal(raw, &value); err != nil || value > maximum {
		return nil, errors.New("governance: optional uint32 malformed or out of range")
	}
	return &edgev1.OptionalUint32{Present: true, Value: value}, nil
}

func edgeOptionalBool(raw json.RawMessage) (*edgev1.OptionalUint32, error) {
	if string(raw) == `"wildcard"` {
		return &edgev1.OptionalUint32{}, nil
	}
	var value bool
	if err := json.Unmarshal(raw, &value); err != nil {
		return nil, errors.New("governance: optional bool malformed")
	}
	var number uint32
	if value {
		number = 1
	}
	return &edgev1.OptionalUint32{Present: true, Value: number}, nil
}

func edgeFirewallAction(action string) (edgev1.FirewallAction, error) {
	switch action {
	case "permit-and-continue":
		return edgev1.FirewallAction_FIREWALL_ACTION_PERMIT_AND_CONTINUE, nil
	case "drop":
		return edgev1.FirewallAction_FIREWALL_ACTION_DROP, nil
	default:
		return edgev1.FirewallAction_FIREWALL_ACTION_UNSPECIFIED, fmt.Errorf("governance: firewall action %q unsupported", action)
	}
}

func edgeEffectDigest(intent *edgev1.EffectIntent) string {
	canonical := proto.Clone(intent).(*edgev1.EffectIntent)
	canonical.EffectDigest = ""
	raw, _ := proto.MarshalOptions{Deterministic: true}.Marshal(canonical)
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}
