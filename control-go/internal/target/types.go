// Package target implements the Target Registry, Assignment handoff, and Fleet
// Coordinator for Go Control Core (go-control-core-design §7, ADR-0015).
//
// The Registry owns never-reused stable target_id, lifecycle, desired profile,
// endpoint/credential reference, scope, and capability observation. External
// NetBox/CMDB imports are provenance/digest candidates only; an Admin confirms
// the diff before any canonical write. Assignment binds target-control
// incarnation, generation, Edge workload, a non-reusable lease, and an election
// floor/range. The Fleet Coordinator freezes the target set, ordered waves, and
// failure policy; the non-claimable parent + bounded per-target child intents
// are formed in ONE short transaction. The parent projects its aggregate status
// from the FULL child vector (any unknown -> reconciling); there is no
// cross-target atomic commit and no majority-success = applied.
package target

import (
	"masi-nids/control-go/internal/governance"
	"masi-nids/control-go/internal/security"
)

// TargetStatus is the target lifecycle state.
type TargetStatus string

const (
	StatusCandidate   TargetStatus = "candidate"
	StatusVerified    TargetStatus = "verified"
	StatusActive      TargetStatus = "active"
	StatusDraining    TargetStatus = "draining"
	StatusDisabled    TargetStatus = "disabled"
	StatusQuarantined TargetStatus = "quarantined"
	StatusRetired     TargetStatus = "retired"
)

// FailurePolicy is the per-wave failure policy.
type FailurePolicy string

const (
	FailFast         FailurePolicy = "fail-fast"
	ContinueIsolated FailurePolicy = "continue-isolated"
	ManualGate       FailurePolicy = "manual-gate"
)

// AggregateStatus is the fleet parent projection from the full child vector.
type AggregateStatus string

const (
	AggregatePlanned     AggregateStatus = "planned"
	AggregateRunning     AggregateStatus = "running"
	AggregatePartial     AggregateStatus = "partial"
	AggregateReconciling AggregateStatus = "reconciling"
	AggregateApplied     AggregateStatus = "applied"
	AggregateFailed      AggregateStatus = "failed"
	AggregateAborted     AggregateStatus = "aborted"
)

// ChildStatus is the per-target child intent status.
type ChildStatus string

const (
	ChildPending     ChildStatus = "pending"
	ChildClaimed     ChildStatus = "claimed"
	ChildExecuting   ChildStatus = "executing"
	ChildApplied     ChildStatus = "applied"
	ChildHold        ChildStatus = "hold"
	ChildUnknown     ChildStatus = "unknown"
	ChildReconciling ChildStatus = "reconciling"
	ChildFailed      ChildStatus = "failed"
	ChildSkipped     ChildStatus = "skipped"
	ChildBlocked     ChildStatus = "blocked"
)

// GateState is the per-wave gate state (future-wave children are not claimable
// until their gate opens).
type GateState string

const (
	GateClosed GateState = "closed"
	GateOpen   GateState = "open"
)

// TLSIdentity is the P4Runtime server TLS identity (matches contracts/target/v1).
type TLSIdentity struct {
	ServerName  string `json:"server_name"`
	IdentityRef string `json:"identity_ref"`
}

// Provenance records the external inventory candidate provenance (never an
// auto-active; Admin confirms the diff before canonical write).
type Provenance struct {
	SourceID          string `json:"source_id"`
	SourceRevision    string `json:"source_revision"`
	RetrievedAtUnixMS int64  `json:"retrieved_at_unix_ms"`
	Digest            string `json:"digest"`
}

// Target is the stable target identity + attributes. target_id is never reused.
type Target struct {
	TargetID             string         `json:"target_id"`
	DisplayName          string         `json:"display_name"`
	P4RuntimeEndpoint    string         `json:"p4runtime_endpoint"`
	DeviceID             int64          `json:"device_id"`
	Role                 string         `json:"role"`
	Status               TargetStatus   `json:"status"`
	DesiredProfileDigest string         `json:"desired_profile_digest"`
	CredentialRef        string         `json:"credential_ref"`
	Scope                string         `json:"scope"`
	TLS                  TLSIdentity    `json:"p4runtime_tls"`
	Provenance           *Provenance    `json:"provenance,omitempty"`
	Actor                security.Actor `json:"actor_ref"`
	TraceID              string         `json:"trace_id"`
	IdempotencyKey       string         `json:"idempotency_key"`
}

type LifecycleAuthorization struct {
	Scope         string
	PlatformAdmin bool
	StepUpFresh   bool
	CSRFVerified  bool
}

// Assignment binds the target-control incarnation, assignment generation, Edge
// workload, non-reusable lease, and election floor/range.
type Assignment struct {
	TargetID              string         `json:"target_id"`
	AssignmentGeneration  int64          `json:"assignment_generation"`
	IncarnationID         string         `json:"incarnation_id"`
	EdgeWorkloadRef       string         `json:"edge_workload_ref"`
	LeaseID               string         `json:"lease_id"`
	IssuedAtUnixMS        int64          `json:"issued_at_unix_ms"`
	ExpiresAtUnixMS       int64          `json:"expires_at_unix_ms"`
	ElectionFloor         int64          `json:"election_floor"`
	ElectionCeiling       int64          `json:"election_ceiling"`
	ActorRuntimeEpoch     string         `json:"actor_runtime_epoch"`
	ApplicationGeneration int64          `json:"application_generation"`
	Actor                 security.Actor `json:"actor_ref"`
	TraceID               string         `json:"trace_id"`
}

// CapabilityObservation is the authenticated, bounded Edge observation for one
// current target assignment. It is append-only in history; the current table is
// only a CAS projection and never replaces the Target Registry fact.
type CapabilityObservation struct {
	ObservationID              string `json:"observation_id"`
	ObservationDigest          string `json:"observation_digest"`
	TargetID                   string `json:"target_id"`
	TargetControlIncarnationID string `json:"target_control_incarnation_id"`
	AssignmentGeneration       int64  `json:"assignment_generation"`
	ActorRuntimeEpoch          string `json:"actor_runtime_epoch"`
	ApplicationGeneration      int64  `json:"application_generation"`
	P4InfoDigest               string `json:"p4info_digest"`
	PipelineDigest             string `json:"pipeline_digest"`
	ProfileDigest              string `json:"profile_digest"`
	CapacityDigest             string `json:"capacity_digest"`
	CapacityAvailable          bool   `json:"capacity_available"`
	LeaseValid                 bool   `json:"lease_valid"`
	P4Connected                bool   `json:"p4_connected"`
	Primary                    bool   `json:"primary"`
	PipelineExact              bool   `json:"pipeline_exact"`
	HighPriorityQueueDepth     uint64 `json:"high_priority_queue_depth"`
	TelemetryQueueDepth        uint64 `json:"telemetry_queue_depth"`
	ObservationQueueDepth      uint64 `json:"observation_queue_depth"`
	SourceWALBytes             uint64 `json:"source_wal_bytes"`
	InputWALBytes              uint64 `json:"input_wal_bytes"`
	ResultWALBytes             uint64 `json:"result_wal_bytes"`
	LastSuccessfulReadUnixMS   int64  `json:"last_successful_read_unix_ms"`
	Freshness                  string `json:"freshness"`
	ReasonCode                 string `json:"reason_code"`
	ObservedAtUnixMS           int64  `json:"observed_at_unix_ms"`
	ExpiresAtUnixMS            int64  `json:"expires_at_unix_ms"`
	TraceID                    string `json:"trace_id"`
}

type CapabilityObservationResult struct {
	TargetID   string `json:"target_id"`
	Accepted   bool   `json:"accepted"`
	Idempotent bool   `json:"idempotent"`
	ReasonCode string `json:"reason_code"`
}

// Wave is one ordered wave of a fleet operation.
type Wave struct {
	WaveIndex     int           `json:"wave_index"`
	TargetIDs     []string      `json:"target_ids"`
	ParallelLimit int           `json:"parallel_limit"`
	FailurePolicy FailurePolicy `json:"failure_policy"`
	GateState     GateState     `json:"gate_state"`
}

// ChildIntent is the per-target child intent reference within a fleet operation.
type ChildIntent struct {
	TargetID   string            `json:"target_id"`
	IntentID   string            `json:"intent_id"`
	WaveIndex  int               `json:"wave_index"`
	Status     ChildStatus       `json:"status"`
	ReasonCode string            `json:"reason_code"`
	Intent     governance.Intent `json:"intent"`
}

// FleetOperation is the frozen target set, waves, non-claimable parent, and
// per-target child intents.
type FleetOperation struct {
	FleetOperationID string            `json:"fleet_operation_id"`
	TargetSetDigest  string            `json:"target_set_digest"`
	WaveCount        int               `json:"wave_count"`
	Waves            []Wave            `json:"waves"`
	ParentIntentID   string            `json:"parent_intent_id"`
	ParentIntent     governance.Intent `json:"parent_intent"`
	ChildIntents     []ChildIntent     `json:"child_intents"`
	AggregateStatus  AggregateStatus   `json:"aggregate_status"`
	Actor            security.Actor    `json:"actor_ref"`
	Scope            string            `json:"scope"`
	TraceID          string            `json:"trace_id"`
}

// IsValidFailurePolicy reports whether the policy is a closed-enum value.
func IsValidFailurePolicy(p FailurePolicy) bool {
	switch p {
	case FailFast, ContinueIsolated, ManualGate:
		return true
	}
	return false
}

// isTerminalChild reports whether a child status is terminal for gate purposes.
func isTerminalChild(s ChildStatus) bool {
	switch s {
	case ChildApplied, ChildFailed, ChildSkipped, ChildBlocked, ChildHold:
		return true
	}
	return false
}
