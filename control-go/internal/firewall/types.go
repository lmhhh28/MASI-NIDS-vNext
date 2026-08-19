// Package firewall implements the Firewall Policy Manager for Go Control Core.
// It owns normalized baseline revisions, default action, current/previous
// binding, the baseline activation operation (an explicit phase sequence on the
// existing effect operation, NOT a new workflow engine), response overlay with
// durable TTL expiry, and readback references.
//
// Go receives only normalized business fields (contracts/p4/firewall-policy/v1);
// Edge compiles to P4 entities. All Edge compiler calls happen OUTSIDE any DB
// transaction; the PG CAS to set current happens in a NEW short transaction
// after selector readback converges (ADR-0014).
package firewall

import (
	"encoding/json"
	"fmt"

	"masi-nids/control-go/internal/governance"
	"masi-nids/control-go/internal/security"
)

// DefaultAction is the baseline default action (matches the contract).
type DefaultAction string

const (
	DefaultPermitAndContinue DefaultAction = "permit-and-continue"
	DefaultDrop              DefaultAction = "drop"
)

// ActivationStage is the baseline activation phase sequence (ADR-0014). One
// selector per target; no double-current; no blind retry.
type ActivationStage string

const (
	StagePrepared          ActivationStage = "prepared"
	StageInactiveWriting   ActivationStage = "inactive_writing"
	StageInactiveVerified  ActivationStage = "inactive_verified"
	StageSelectorSwitching ActivationStage = "selector_switching"
	StageSelectorVerified  ActivationStage = "selector_verified"
	StageCurrentCommitted  ActivationStage = "current_committed"
	StagePreviousRetained  ActivationStage = "previous_retained"
	StageCleanup           ActivationStage = "cleanup"
)

// ActivationStagesInOrder is the canonical phase order. A stage may only be
// reached after all prior stages are completed.
var ActivationStagesInOrder = []ActivationStage{
	StagePrepared, StageInactiveWriting, StageInactiveVerified,
	StageSelectorSwitching, StageSelectorVerified, StageCurrentCommitted,
	StagePreviousRetained, StageCleanup,
}

// ActivationResult is the activation outcome.
type ActivationResult string

const (
	ActivationPrepared    ActivationResult = "prepared"
	ActivationApplied     ActivationResult = "applied"
	ActivationReconciling ActivationResult = "reconciling"
	ActivationHold        ActivationResult = "hold"
)

// Rule is a normalized baseline rule (subset of contracts/p4/firewall-policy/v1
// baseline_rule — the full match fields are carried by the compiled plan).
type Rule struct {
	RuleID              string         `json:"rule_id"`
	RuleRevision        int64          `json:"rule_revision"`
	Priority            int            `json:"priority"`
	IngressPort         OptionalUint32 `json:"ingress_port"`
	SourceIPv4          IPv4Prefix     `json:"src_ipv4"`
	DestinationIPv4     IPv4Prefix     `json:"dst_ipv4"`
	Protocol            OptionalUint32 `json:"protocol"`
	L4Present           OptionalBool   `json:"l4_present"`
	SourcePort          OptionalUint32 `json:"src_port"`
	DestinationPort     OptionalUint32 `json:"dst_port"`
	FragmentClass       FragmentClass  `json:"fragment_class"`
	Action              string         `json:"action"`
	Enabled             bool           `json:"enabled"`
	CanonicalRuleDigest string         `json:"canonical_rule_digest"`
	ActorRef            string         `json:"actor_ref"`
	ReasonCode          string         `json:"reason_code"`
}

type IPv4Prefix struct {
	Address      string `json:"address"`
	PrefixLength uint32 `json:"prefix_length"`
}

// OptionalUint32 and OptionalBool encode the contract's closed wildcard union
// as either the literal "wildcard" or the scalar value.
type OptionalUint32 struct {
	Present bool
	Value   uint32
}

func (m OptionalUint32) MarshalJSON() ([]byte, error) {
	if !m.Present {
		return []byte(`"wildcard"`), nil
	}
	return json.Marshal(m.Value)
}

func (m *OptionalUint32) UnmarshalJSON(raw []byte) error {
	if string(raw) == `"wildcard"` {
		*m = OptionalUint32{}
		return nil
	}
	var value uint32
	if err := json.Unmarshal(raw, &value); err != nil {
		return fmt.Errorf("firewall: optional uint32 must be wildcard or uint32: %w", err)
	}
	*m = OptionalUint32{Present: true, Value: value}
	return nil
}

type OptionalBool struct {
	Present bool
	Value   bool
}

func (m OptionalBool) MarshalJSON() ([]byte, error) {
	if !m.Present {
		return []byte(`"wildcard"`), nil
	}
	return json.Marshal(m.Value)
}

func (m *OptionalBool) UnmarshalJSON(raw []byte) error {
	if string(raw) == `"wildcard"` {
		*m = OptionalBool{}
		return nil
	}
	var value bool
	if err := json.Unmarshal(raw, &value); err != nil {
		return fmt.Errorf("firewall: optional bool must be wildcard or boolean: %w", err)
	}
	*m = OptionalBool{Present: true, Value: value}
	return nil
}

type FragmentClass string

const (
	FragmentWildcard   FragmentClass = "wildcard"
	FragmentNone       FragmentClass = "unfragmented"
	FragmentFirst      FragmentClass = "first-fragment"
	FragmentNonInitial FragmentClass = "non-initial-fragment"
)

// Revision is the immutable normalized baseline revision.
type Revision struct {
	RevisionID     string        `json:"revision_id"`
	RevisionDigest string        `json:"revision_digest"`
	TargetID       string        `json:"target_id"`
	DefaultAction  DefaultAction `json:"default_action"`
	Rules          []Rule        `json:"rules"`
	Scope          string        `json:"scope"`
	ActorRef       string        `json:"actor_ref"`
}

// Binding is the per-target current/previous binding + selector state.
type Binding struct {
	TargetID           string `json:"target_id"`
	CurrentRevisionID  string `json:"current_revision_id"`
	PreviousRevisionID string `json:"previous_revision_id"`
	ActiveBank         int    `json:"active_bank"`
	SelectorState      string `json:"selector_state"`
	OperationID        string `json:"operation_id"`
	CASDigest          string `json:"cas_digest"`
}

// Overlay is a response overlay rule with durable TTL expiry.
type Overlay struct {
	OverlayRuleID   string            `json:"overlay_rule_id"`
	TargetID        string            `json:"target_id"`
	Rule            OverlayRule       `json:"rule"`
	ExpiresAtUnixMS int64             `json:"expires_at_unix_ms"`
	Deleted         bool              `json:"deleted"`
	DeleteIntentID  string            `json:"delete_intent_id,omitempty"`
	OperationID     string            `json:"operation_id"`
	Scope           string            `json:"scope"`
	Actor           security.Actor    `json:"actor_ref"`
	TraceID         string            `json:"trace_id"`
	UpsertIntent    governance.Intent `json:"upsert_intent"`
	DeleteIntent    governance.Intent `json:"delete_intent"`
}

// OverlayRule is the exact public response-overlay contract. Overlay matching
// is deliberately narrower than baseline matching: exact IPv4 endpoints,
// required TCP/UDP protocol and ports, fixed before-baseline precedence, and a
// durable RFC3339 expiry.
type OverlayRule struct {
	RuleID              string `json:"rule_id"`
	RuleRevision        int64  `json:"rule_revision"`
	CanonicalRuleDigest string `json:"canonical_rule_digest"`
	StagePrecedence     string `json:"stage_precedence"`
	SourceIPv4          string `json:"src_ipv4"`
	DestinationIPv4     string `json:"dst_ipv4"`
	Protocol            uint32 `json:"protocol"`
	SourcePort          uint32 `json:"src_port"`
	DestinationPort     uint32 `json:"dst_port"`
	Action              string `json:"action"`
	Enabled             bool   `json:"enabled"`
	ExpiresAt           string `json:"expires_at"`
	ActorRef            string `json:"actor_ref"`
	ReasonCode          string `json:"reason_code"`
}

// CompiledPlan is the Edge-compiled plan for a revision (contracts/p4/firewall-
// policy/v1 compiled_plan). Go does NOT generate this; the FirewallEdgeCompiler
// boundary returns it.
type CompiledPlan struct {
	PlanDigest      string `json:"plan_digest"`
	ActiveBank      int    `json:"active_bank"`
	InactiveBank    int    `json:"inactive_bank"`
	ExpectedEntries int    `json:"expected_entries"`
	Atomicity       string `json:"atomicity"`
}

// Readback is the inactive-bank write/readback result from Edge.
type Readback struct {
	TargetID          string `json:"target_id"`
	PlanDigest        string `json:"plan_digest"`
	ExpectedEntries   int    `json:"expected_entries"`
	ReadbackDigest    string `json:"readback_digest"`
	ObservedEntries   int    `json:"observed_entries"`
	MismatchedEntries int    `json:"mismatched_entries"`
	DefaultReadback   string `json:"default_readback"` // exact|missing|mismatch
}

// SelectorReadback is the selector flip/readback result from Edge.
type SelectorReadback struct {
	TargetID       string `json:"target_id"`
	PlanDigest     string `json:"plan_digest"`
	ActiveBank     int    `json:"active_bank"`
	Bank           string `json:"bank"` // old-bank|new-bank|missing|mismatch
	ReadbackDigest string `json:"readback_digest"`
}
