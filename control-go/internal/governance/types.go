// Package governance implements the Effect governance lifecycle for Go Control
// Core: immutable Effect Proposal, append-only Authorization Decision (R0-R3
// maker-checker), the single claimable Effect Intent, and the bounded effect
// dispatcher that claims/fences, calls Edge OUTSIDE any DB transaction, and
// CAS-finalizes. Proposal/Decision/fleet-parent are NEVER work queues; only
// effect_intents is claimable (ADR-0001/0004, AGENTS.md §不可破坏的架构约束).
package governance

import (
	"context"
	"encoding/json"

	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

// RiskLevel re-exports the security risk levels (R0-R3).
type RiskLevel = security.RiskLevel

// EffectKind enumerates the typed effect kinds (matches contracts/effect/v1).
type EffectKind string

const (
	KindFirewallBaselineActivate EffectKind = "firewall-baseline-activate"
	KindFirewallOverlay          EffectKind = "firewall-overlay"
	KindFirewallRollback         EffectKind = "firewall-rollback"
	KindTargetAssignment         EffectKind = "target-assignment"
	KindFleetOperation           EffectKind = "fleet-operation"
	KindModelRollout             EffectKind = "model-rollout"
	KindModelRollback            EffectKind = "model-rollback"
	KindModelRecovery            EffectKind = "model-recovery"
	KindPluginActivate           EffectKind = "plugin-activate"
	KindPluginDrain              EffectKind = "plugin-drain"
	KindPluginRevoke             EffectKind = "plugin-revoke"
	KindPluginStatisticsRun      EffectKind = "plugin-statistics-run"
	KindRuleObservationEpoch     EffectKind = "rule-observation-epoch"
	KindBoundedCapture           EffectKind = "bounded-capture"
)

// ClaimState is the effect_intent claim lifecycle (only effect_intents carry it).
type ClaimState string

const (
	ClaimUnclaimed ClaimState = "unclaimed"
	ClaimClaimed   ClaimState = "claimed"
	ClaimFenced    ClaimState = "fenced"
	ClaimExecuting ClaimState = "executing"
	ClaimUnknown   ClaimState = "unknown"
	ClaimFinalized ClaimState = "finalized"
)

// AttemptStatus is the effect attempt outcome.
type AttemptStatus string

const (
	AttemptApplied     AttemptStatus = "applied"
	AttemptHold        AttemptStatus = "hold"
	AttemptUnknown     AttemptStatus = "unknown"
	AttemptReconciling AttemptStatus = "reconciling"
)

// PreflightRejectedError proves that an Edge call failed before ExecuteEffect
// was invoked. The dispatcher may therefore finalize the claimed intent as
// HOLD; an ordinary RPC error remains ambiguous and must become unknown.
type PreflightRejectedError struct {
	Cause error
}

func (e *PreflightRejectedError) Error() string {
	if e == nil || e.Cause == nil {
		return "governance: Edge preflight rejected"
	}
	return "governance: Edge preflight rejected: " + e.Cause.Error()
}

func (e *PreflightRejectedError) Unwrap() error {
	if e == nil {
		return nil
	}
	return e.Cause
}

// Fence binds the target-control/assignment/application/P4Info/pipeline/capacity
// digests at proposal time. Distinct from P4Runtime election_id (ADR-0004).
type Fence struct {
	TargetControlIncarnationID string `json:"target_control_incarnation_id"`
	TargetAssignmentGeneration int64  `json:"target_assignment_generation"`
	EdgeWorkloadRef            string `json:"edge_workload_ref"`
	ActorRuntimeEpoch          string `json:"actor_runtime_epoch"`
	ApplicationGeneration      int64  `json:"application_generation"`
	ElectionIDHigh             uint64 `json:"election_id_high"`
	ElectionIDLow              uint64 `json:"election_id_low"`
	P4InfoDigest               string `json:"p4info_digest"`
	PipelineDigest             string `json:"pipeline_digest"`
	CapacityDigest             string `json:"capacity_digest"`
}

// AuthzContext re-exports the security authorization context.
type AuthzContext = security.AuthzContext

// Proposal is the immutable, non-executable effect proposal.
type Proposal struct {
	ProposalID      string                     `json:"proposal_id"`
	ProposalDigest  string                     `json:"proposal_digest"`
	Actor           security.Actor             `json:"actor_ref"`
	ActorLevel      security.AuthzContextLevel `json:"actor_level"`
	Scope           string                     `json:"scope"`
	RiskLevel       RiskLevel                  `json:"risk_level"`
	EffectKind      EffectKind                 `json:"effect_kind"`
	TargetSetDigest string                     `json:"target_set_digest"`
	TargetIDs       []string                   `json:"target_ids"`
	PolicyDigest    string                     `json:"policy_digest"`
	EvidenceRefs    []string                   `json:"evidence_refs"`
	ExpiresAtUnixMS int64                      `json:"expires_at_unix_ms"`
	Note            string                     `json:"note"`
	CreatedAtUnixMS int64                      `json:"created_at_unix_ms"`
	TraceID         string                     `json:"trace_id"`
	IdempotencyKey  string                     `json:"idempotency_key"`
}

// Decision is the append-only authorization decision binding the exact proposal
// digest. One terminal decision per proposal.
type Decision struct {
	DecisionID      string         `json:"decision_id"`
	ProposalID      string         `json:"proposal_id"`
	ProposalDigest  string         `json:"proposal_digest"`
	Actor           security.Actor `json:"actor_ref"`
	RiskLevel       RiskLevel      `json:"risk_level"`
	Approved        bool           `json:"approved"`
	Authz           AuthzContext   `json:"authz_context"`
	DecisionDigest  string         `json:"decision_digest"`
	ReasonCode      string         `json:"reason_code"`
	ExpiresAtUnixMS int64          `json:"expires_at_unix_ms"`
	CreatedAtUnixMS int64          `json:"created_at_unix_ms"`
	TraceID         string         `json:"trace_id"`
}

// Intent is the single claimable effect intent.
type Intent struct {
	EffectIntentID         string         `json:"effect_intent_id"`
	OperationID            string         `json:"operation_id"`
	ProposalID             string         `json:"proposal_id"`
	DecisionID             string         `json:"decision_id"`
	TargetID               string         `json:"target_id"`
	FleetOperationID       string         `json:"fleet_operation_id"`
	IsFleetParent          bool           `json:"is_fleet_parent"`
	Fence                  Fence          `json:"fence"`
	EffectDigest           string         `json:"effect_digest"`
	Payload                EffectPayload  `json:"effect_payload"`
	AuthorizationDigest    string         `json:"authorization_digest"`
	EffectKind             EffectKind     `json:"effect_kind"`
	RiskLevel              RiskLevel      `json:"risk_level"`
	RequiredWriteAtomicity string         `json:"required_write_atomicity"`
	DeadlineUnixMS         int64          `json:"deadline_unix_ms"`
	ClaimState             ClaimState     `json:"claim_state"`
	ClaimLeaseID           string         `json:"claim_lease_id"`
	ClaimExpiresAtUnixMS   int64          `json:"claim_expires_at_unix_ms"`
	Actor                  security.Actor `json:"actor_ref"`
	TraceID                string         `json:"trace_id"`
}

// EffectPayload is the immutable normalized P4 effect body stored on the sole
// claimable intent. Rule bodies remain canonical contract JSON; the gRPC
// adapter performs the explicit typed map to edge.proto.
type EffectPayload struct {
	SchemaVersion        string              `json:"schema_version"`
	Operation            string              `json:"operation"` // baseline-activate|overlay-upsert|overlay-delete
	PolicyRevisionDigest string              `json:"policy_revision_digest"`
	DefaultAction        string              `json:"default_action"`
	BaselineRules        json.RawMessage     `json:"baseline_rules"`
	OverlayRules         json.RawMessage     `json:"overlay_rules"`
	BoundedCapture       *BoundedCaptureSpec `json:"bounded_capture,omitempty"`
}

// BoundedCaptureSpec is the immutable, exact resource envelope used by the
// R0 capture effect. The first-release profile is metadata-only: raw payload is
// never returned over the Effect RPC or persisted in the core database.
type BoundedCaptureSpec struct {
	SchemaVersion         string        `json:"schema_version"`
	CaptureID             string        `json:"capture_id"`
	CaptureDigest         string        `json:"capture_digest"`
	CaptureAdapterID      string        `json:"capture_adapter_id"`
	DurationMS            int           `json:"duration_ms"`
	SampleLimit           int           `json:"sample_limit"`
	ByteLimit             int64         `json:"byte_limit"`
	ExpiresAtUnixMS       int64         `json:"expires_at_unix_ms"`
	MaxConcurrentOnTarget int           `json:"max_concurrent_on_target"`
	PayloadMode           string        `json:"payload_mode"`
	Filter                CaptureFilter `json:"filter"`
}

// CaptureFilter is deliberately closed and expression-free. Empty fields are
// wildcards; addresses, when present, are normalized IPv4 prefixes.
type CaptureFilter struct {
	SourceIPv4      string  `json:"source_ipv4,omitempty"`
	DestinationIPv4 string  `json:"destination_ipv4,omitempty"`
	Protocol        *uint32 `json:"protocol,omitempty"`
	SourcePort      *uint32 `json:"source_port,omitempty"`
	DestinationPort *uint32 `json:"destination_port,omitempty"`
}

// Attempt is the claim/fence/Edge/readback/finalize record.
type Attempt struct {
	AttemptID         string        `json:"attempt_id"`
	IntentID          string        `json:"intent_id"`
	OperationID       string        `json:"operation_id"`
	AttemptNumber     int           `json:"attempt_number"`
	Status            AttemptStatus `json:"status"`
	PlanDigest        string        `json:"plan_digest"`
	ReadbackDigest    string        `json:"readback_digest"`
	ExpectedEntries   int           `json:"expected_entries"`
	ObservedEntries   int           `json:"observed_entries"`
	MismatchedEntries int           `json:"mismatched_entries"`
	ActiveBank        int           `json:"active_bank"`
	StartedAtUnixMS   int64         `json:"started_at_unix_ms"`
	FinishedAtUnixMS  int64         `json:"finished_at_unix_ms"`
	TraceID           string        `json:"trace_id"`
}

// EdgeEffectClient is the boundary the dispatcher calls OUTSIDE any DB
// transaction. Rust Edge implements this (edge.proto EdgeControl.ExecuteEffect);
// tests use a fake. The dispatcher never holds a DB connection across this call.
type EdgeEffectClient interface {
	// ExecuteEffect performs the journal/write/readback on Edge. It must NOT be
	// called inside a DB transaction. The returned result carries the readback
	// digest and entry counts for CAS finalize.
	ExecuteEffect(ctx context.Context, intent Intent) (EdgeEffectResult, error)
	AcknowledgeEffect(ctx context.Context, intent Intent, result EdgeEffectResult, canonicalReference string, committedAtUnixMS int64) error
}

// EdgeEffectPreflightClient is the read-only compiler/admission boundary used
// by the Firewall revision inspection API. It cannot execute or acknowledge an
// effect and never exposes the Edge preflight token to browser callers.
type EdgeEffectPreflightClient interface {
	PreflightEffect(ctx context.Context, intent Intent) (EdgePreflightResult, error)
}

type EdgePreflightResult struct {
	TargetID        string `json:"target_id"`
	EffectIntentID  string `json:"effect_intent_id"`
	PlanDigest      string `json:"plan_digest"`
	PhysicalEntries int    `json:"physical_entries"`
	ExpiresAtUnixMS int64  `json:"expires_at_unix_ms"`
	Result          string `json:"result"`
	ReasonCode      string `json:"reason_code"`
	TraceID         string `json:"trace_id"`
	PreflightToken  string `json:"-"`
}

// EdgeEffectReadbackClient is deliberately separate from the mutation client.
// It submits the complete original identity to Edge's idempotent journal path:
// Edge may return an already-pending result, but without a preflight token it
// cannot begin a new P4 mutation.
type EdgeEffectReadbackClient interface {
	QueryEffectReadback(ctx context.Context, intent Intent) (EdgeEffectResult, error)
	AcknowledgeEffect(ctx context.Context, intent Intent, result EdgeEffectResult, canonicalReference string, committedAtUnixMS int64) error
}

// EdgeEffectResult is the Edge-side journal/write/readback outcome.
type EdgeEffectResult struct {
	OperationID            string                  `json:"operation_id"`
	TargetID               string                  `json:"target_id"`
	EffectDigest           string                  `json:"effect_digest"`
	Outcome                string                  `json:"outcome"` // applied|absent|unknown
	ReadbackDigest         string                  `json:"readback_digest"`
	ResultDigest           string                  `json:"result_digest"`
	ExpectedEntries        int                     `json:"expected_entries"`
	ObservedEntries        int                     `json:"observed_entries"`
	MismatchedEntries      int                     `json:"mismatched_entries"`
	ActiveBank             int                     `json:"active_bank"`
	TimedOut               bool                    `json:"timed_out"`
	ReadbackManifestDigest string                  `json:"readback_manifest_digest"`
	AppliedEntries         []AppliedRuleReadback   `json:"applied_entries"`
	BoundedCapture         *BoundedCaptureReadback `json:"bounded_capture,omitempty"`
}

type BoundedCaptureReadback struct {
	SchemaVersion       string `json:"schema_version"`
	CaptureID           string `json:"capture_id"`
	CaptureDigest       string `json:"capture_digest"`
	CaptureSessionID    string `json:"capture_session_id"`
	StartedAtUnixMS     int64  `json:"started_at_unix_ms"`
	FinishedAtUnixMS    int64  `json:"finished_at_unix_ms"`
	ObservedSamples     int    `json:"observed_samples"`
	ObservedBytes       int64  `json:"observed_bytes"`
	ContentDigest       string `json:"content_digest"`
	ObservedFlowDigest  string `json:"observed_flow_digest"`
	CaptureWindowDigest string `json:"capture_window_digest"`
	Truncated           bool   `json:"truncated"`
	Gap                 bool   `json:"gap"`
}

type AppliedRuleReadback struct {
	EntityID                  string `json:"entity_id"`
	RuleID                    string `json:"rule_id"`
	CanonicalEntryDigest      string `json:"canonical_entry_digest"`
	MatchPriorityActionDigest string `json:"match_priority_action_digest"`
	TableID                   uint32 `json:"table_id"`
	DirectCounterID           uint32 `json:"direct_counter_id"`
	Bank                      uint32 `json:"bank"`
}

// IntentStateProjector updates a derived durable projection in the SAME short
// transaction that changes the canonical effect intent. Fleet parent/child is
// the first implementation. This is not a queue and cannot execute an effect.
type IntentStateProjector interface {
	ProjectIntentState(ctx context.Context, tx *db.Tx, intentID string, state ClaimState, outcome AttemptStatus, reasonCode string) error
}
