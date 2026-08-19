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
	SchemaVersion        string          `json:"schema_version"`
	Operation            string          `json:"operation"` // baseline-activate|overlay-upsert|overlay-delete
	PolicyRevisionDigest string          `json:"policy_revision_digest"`
	DefaultAction        string          `json:"default_action"`
	BaselineRules        json.RawMessage `json:"baseline_rules"`
	OverlayRules         json.RawMessage `json:"overlay_rules"`
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
	OperationID       string `json:"operation_id"`
	TargetID          string `json:"target_id"`
	EffectDigest      string `json:"effect_digest"`
	Outcome           string `json:"outcome"` // applied|absent|unknown
	ReadbackDigest    string `json:"readback_digest"`
	ResultDigest      string `json:"result_digest"`
	ExpectedEntries   int    `json:"expected_entries"`
	ObservedEntries   int    `json:"observed_entries"`
	MismatchedEntries int    `json:"mismatched_entries"`
	ActiveBank        int    `json:"active_bank"`
	TimedOut          bool   `json:"timed_out"`
}

// IntentStateProjector updates a derived durable projection in the SAME short
// transaction that changes the canonical effect intent. Fleet parent/child is
// the first implementation. This is not a queue and cannot execute an effect.
type IntentStateProjector interface {
	ProjectIntentState(ctx context.Context, tx *db.Tx, intentID string, state ClaimState, outcome AttemptStatus, reasonCode string) error
}
