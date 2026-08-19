// Package model implements the Model Manager for Go Control Core. It is the
// sole writer of model revision/qualification, model-control incarnation,
// logical pool/pool generation, per-shard desired/current/previous binding,
// rollout/recovery/rollback operations, and worker/pool observation.
//
// Rollout order (go-control-core-design §9, ADR-0017, MIG-INF-001):
//
//	durable operation -> exact pool envelope/deployment action -> real Central
//	startup/warmup/readback/capacity -> Edge per-shard route-withdraw/drain/WAL
//	-> short PG CAS current/previous -> committed-binding handshake/resume ->
//	old generation drain.
//
// ALL external waits (deployment adapter, Central, Edge) occur OUTSIDE any DB
// transaction; only the CAS finalize and operation append are short txns.
// loaded/Ready != current; same-generation replica does not change route;
// CPU<->CUDA/model/backend change uses a new generation. Rollback is a NEW
// operation to exact previous (never auto on inference error). PITR/clone/
// rewind rotates a never-used incarnation before reopening writer.
package model

import (
	"context"

	"masi-nids/control-go/internal/security"
)

// QualificationStatus of a model revision.
type QualificationStatus string

const (
	Qualified   QualificationStatus = "qualified"
	Unqualified QualificationStatus = "unqualified"
	Hold        QualificationStatus = "hold"
	Revoked     QualificationStatus = "revoked"
)

// IncarnationSource records why a model-control incarnation was created.
type IncarnationSource string

const (
	IncarnationInitial IncarnationSource = "initial"
	IncarnationPITR    IncarnationSource = "pitr"
	IncarnationClone   IncarnationSource = "clone"
	IncarnationRewind  IncarnationSource = "rewind"
)

// PoolGenerationStatus is the lifecycle of a pool generation.
type PoolGenerationStatus string

const (
	PoolWarming    PoolGenerationStatus = "warming"
	PoolActive     PoolGenerationStatus = "active"
	PoolDraining   PoolGenerationStatus = "draining"
	PoolRetired    PoolGenerationStatus = "retired"
	PoolQuarantine PoolGenerationStatus = "quarantined"
)

// ResumeState records whether a shard binding is current, resuming (handshake
// failed AFTER CAS), or unavailable. resume_pending keeps the new current and
// reconciles along the original identity — no silent rollback.
type ResumeState string

const (
	ResumeCurrent     ResumeState = "current"
	ResumePending     ResumeState = "resume_pending"
	ResumeUnavailable ResumeState = "unavailable"
)

// OperationKind and OperationStatus for rollout operations.
type OperationKind string

const (
	OpRollout      OperationKind = "rollout"
	OpRollback     OperationKind = "rollback"
	OpRecovery     OperationKind = "recovery"
	OpPITRRecovery OperationKind = "pitr-recovery"
)

type OperationStatus string

const (
	OpPlanned          OperationStatus = "planned"
	OpStaging          OperationStatus = "staging"
	OpWarming          OperationStatus = "warming"
	OpRouteWithdrawing OperationStatus = "route-withdrawing"
	OpCASCommitting    OperationStatus = "cas-committing"
	OpHandshakePending OperationStatus = "handshake-pending"
	OpApplied          OperationStatus = "applied"
	OpResumePending    OperationStatus = "resume_pending"
	OpFailed           OperationStatus = "failed"
	OpAborted          OperationStatus = "aborted"
)

// ModelRevision is the immutable, digest-pinned model revision (catalog +
// qualification). Offline ML produces the bundle; Go registers qualification.
type ModelRevision struct {
	ModelRevisionID       string              `json:"model_revision_id"`
	ModelRevisionDigest   string              `json:"model_revision_digest"`
	ModelBundleDigest     string              `json:"model_bundle_digest"`
	FeatureContractDigest string              `json:"feature_contract_digest"`
	LabelContractDigest   string              `json:"label_contract_digest"`
	OutputAdapterDigest   string              `json:"output_adapter_digest"`
	QualificationStatus   QualificationStatus `json:"qualification_status"`
	QualifiedAtUnixMS     int64               `json:"qualified_at_unix_ms,omitempty"`
	ReaderRuntimeProfile  string              `json:"reader_runtime_profile"`
	Scope                 string              `json:"scope"`
	Actor                 security.Actor      `json:"actor_ref"`
	TraceID               string              `json:"trace_id"`
}

// ModelControlIncarnation is the never-reused incarnation rotated on
// PITR/clone/rewind before a writer is reopened.
type ModelControlIncarnation struct {
	IncarnationID         string            `json:"incarnation_id"`
	Source                IncarnationSource `json:"source"`
	RotatedAtUnixMS       int64             `json:"rotated_at_unix_ms"`
	ReplacedIncarnationID string            `json:"replaced_incarnation_id,omitempty"`
	ActorRef              string            `json:"actor_ref"`
	TraceID               string            `json:"trace_id"`
}

// LogicalPool is the named logical inference pool.
type LogicalPool struct {
	LogicalPoolID       string `json:"logical_pool_id"`
	CurrentGeneration   int64  `json:"current_generation"`
	AvailabilityProfile string `json:"availability_profile"`
	RuntimeProfile      string `json:"runtime_profile"`
	ActorRef            string `json:"actor_ref"`
	TraceID             string `json:"trace_id"`
}

// PoolGeneration is one generation of a logical pool. A CPU<->CUDA / model /
// backend change creates a NEW generation (same-generation replicas do not
// change route).
type PoolGeneration struct {
	LogicalPoolID             string               `json:"logical_pool_id"`
	PoolGeneration            int64                `json:"pool_generation"`
	ModelControlIncarnationID string               `json:"model_control_incarnation_id"`
	OperationID               string               `json:"operation_id"`
	Scope                     string               `json:"scope"`
	BindingGeneration         int64                `json:"binding_generation"`
	ModelRevisionID           string               `json:"model_revision_id"`
	StartupEnvelopeDigest     string               `json:"startup_envelope_digest"`
	PoolObservationDigest     string               `json:"pool_observation_digest"`
	BindingDigest             string               `json:"binding_digest"`
	Status                    PoolGenerationStatus `json:"status"`
	MinReadyReplicas          int                  `json:"min_ready_replicas"`
	CapacityQualified         bool                 `json:"capacity_qualified"`
	ModelRevisionDigest       string               `json:"model_revision_digest"`
	ModelBundleDigest         string               `json:"model_bundle_digest"`
	FeatureContractDigest     string               `json:"feature_contract_digest"`
	LabelContractDigest       string               `json:"label_contract_digest"`
	OutputAdapterDigest       string               `json:"output_adapter_digest"`
	WireProfileDigest         string               `json:"wire_profile_digest"`
	RuntimeProfileDigest      string               `json:"runtime_profile_digest"`
	OptimizationProfileDigest string               `json:"optimization_profile_digest"`
	WireProfile               string               `json:"wire_profile"`
	RuntimeProfile            string               `json:"runtime_profile"`
	AvailabilityProfile       string               `json:"availability_profile"`
}

// ShardBinding is the per-shard desired/current/previous binding. loaded/ready
// are observation flags that are NOT equal to current.
type ShardBinding struct {
	ShardID                   string      `json:"shard_id"`
	LogicalPoolID             string      `json:"logical_pool_id"`
	ModelControlIncarnationID string      `json:"model_control_incarnation_id"`
	CurrentGeneration         int64       `json:"current_generation"`
	PreviousGeneration        int64       `json:"previous_generation,omitempty"`
	CurrentBindingGeneration  int64       `json:"current_binding_generation"`
	PreviousBindingGeneration int64       `json:"previous_binding_generation,omitempty"`
	CurrentRevisionID         string      `json:"current_revision_id"`
	PreviousRevisionID        string      `json:"previous_revision_id,omitempty"`
	RouteEpoch                int64       `json:"route_epoch"`
	ResumeState               ResumeState `json:"resume_state"`
	Loaded                    bool        `json:"loaded"`
	Ready                     bool        `json:"ready"`
	CASDigest                 string      `json:"cas_digest"`
	Scope                     string      `json:"scope"`
}

// RolloutOperation is the durable rollout/rollback/recovery operation.
type RolloutOperation struct {
	OperationID               string          `json:"operation_id"`
	ShardID                   string          `json:"shard_id"`
	LogicalPoolID             string          `json:"logical_pool_id"`
	TargetGeneration          int64           `json:"target_generation"`
	PreviousGeneration        int64           `json:"previous_generation,omitempty"`
	OperationKind             OperationKind   `json:"operation_kind"`
	Status                    OperationStatus `json:"status"`
	StartedAtUnixMS           int64           `json:"started_at_unix_ms"`
	FinishedAtUnixMS          int64           `json:"finished_at_unix_ms,omitempty"`
	ActorRef                  string          `json:"actor_ref"`
	ReasonCode                string          `json:"reason_code"`
	TraceID                   string          `json:"trace_id"`
	RequestDigest             string          `json:"request_digest"`
	ModelControlIncarnationID string          `json:"model_control_incarnation_id"`
	Scope                     string          `json:"scope"`
	TargetRevisionID          string          `json:"target_revision_id"`
	EdgeWorkloadRef           string          `json:"edge_workload_ref"`
}

// PoolStartupEnvelope is the exact envelope Central consumes at startup
// (from contracts/inference/v1 + model/v1 pool_startup_envelope).
type PoolStartupEnvelope struct {
	LogicalPoolID         string `json:"logical_pool_id"`
	PoolGeneration        int64  `json:"pool_generation"`
	ModelRevisionDigest   string `json:"model_revision_digest"`
	StartupEnvelopeDigest string `json:"startup_envelope_digest"`
	PoolObservationDigest string `json:"pool_observation_digest"`
	BindingDigest         string `json:"binding_digest"`
	AvailabilityProfile   string `json:"availability_profile"`
	RuntimeProfile        string `json:"runtime_profile"`
}

// CommittedBindingHandshake carries the exact fields of the committed-binding
// handshake (contracts/inference/v1 profile.committed_binding_handshake).
// On failure AFTER the PG CAS, the new current is kept with resume_pending/
// unavailable and reconciled along the original identity — no silent rollback.
type CommittedBindingHandshake struct {
	ModelControlIncarnationID string `json:"model_control_incarnation_id"`
	OperationID               string `json:"operation_id"`
	Scope                     string `json:"scope"`
	ShardID                   string `json:"shard_id"`
	RouteEpoch                int64  `json:"route_epoch"`
	LogicalPoolID             string `json:"logical_pool_id"`
	PoolGeneration            int64  `json:"pool_generation"`
	ExpectedBindingGeneration int64  `json:"expected_binding_generation"`
	ProposedBindingGeneration int64  `json:"proposed_binding_generation"`
	CurrentBindingGeneration  int64  `json:"current_binding_generation"`
	StartupEnvelopeDigest     string `json:"startup_envelope_digest"`
	PoolObservationDigest     string `json:"pool_observation_digest"`
	BindingDigest             string `json:"binding_digest"`
	DeadlineUnixMS            int64  `json:"deadline_unix_ms"`
	EdgeWorkloadRef           string `json:"edge_workload_ref"`
}

// Readback is the Central startup/warmup readback result.
type Readback struct {
	LogicalPoolID             string           `json:"logical_pool_id"`
	PoolGeneration            int64            `json:"pool_generation"`
	BindingGeneration         int64            `json:"binding_generation"`
	ModelControlIncarnationID string           `json:"model_control_incarnation_id"`
	OperationID               string           `json:"operation_id"`
	ModelRevisionDigest       string           `json:"model_revision_digest"`
	ModelBundleDigest         string           `json:"model_bundle_digest"`
	FeatureContractDigest     string           `json:"feature_contract_digest"`
	LabelContractDigest       string           `json:"label_contract_digest"`
	OutputAdapterDigest       string           `json:"output_adapter_digest"`
	WireProfileDigest         string           `json:"wire_profile_digest"`
	RuntimeProfileDigest      string           `json:"runtime_profile_digest"`
	OptimizationProfileDigest string           `json:"optimization_profile_digest"`
	WireProfile               string           `json:"wire_profile"`
	RuntimeProfile            string           `json:"runtime_profile"`
	AvailabilityProfile       string           `json:"availability_profile"`
	ReadyReplicas             int              `json:"ready_replicas"`
	ReadbackAttemptID         string           `json:"readback_attempt_id"`
	ObservedAtUnixMS          int64            `json:"observed_at_unix_ms"`
	StartupEnvelopeDigest     string           `json:"startup_envelope_digest"`
	PoolObservationDigest     string           `json:"pool_observation_digest"`
	BindingDigest             string           `json:"binding_digest"`
	Endpoint                  string           `json:"endpoint"`
	TLSServerName             string           `json:"tls_server_name"`
	TLSIdentityRef            string           `json:"tls_identity_ref"`
	EligibleWorkers           []WorkerIdentity `json:"eligible_workers"`
	Loaded                    bool             `json:"loaded"`
}

// WorkerIdentity is an exact eligible Central replica identity returned by the
// selected deployment/readback adapter. It is observation, never current fact.
type WorkerIdentity struct {
	WorkerID     string `json:"worker_id"`
	WorkerDigest string `json:"worker_digest"`
}

// PoolObservation is the append-only, exact startup/readback/capacity fact
// persisted before any shard can CAS to the pool generation.
type PoolObservation struct {
	ObservationID             string         `json:"observation_id"`
	ObservationDigest         string         `json:"observation_digest"`
	LogicalPoolID             string         `json:"logical_pool_id"`
	PoolGeneration            int64          `json:"pool_generation"`
	ModelControlIncarnationID string         `json:"model_control_incarnation_id"`
	OperationID               string         `json:"operation_id"`
	Scope                     string         `json:"scope"`
	Readback                  Readback       `json:"readback"`
	Capacity                  CapacityResult `json:"capacity"`
	TraceID                   string         `json:"trace_id"`
}

// CapacityResult is the capacity qualification for the chosen availability
// profile (HA additionally requires failure-domain/N+1/min-ready).
type CapacityResult struct {
	LogicalPoolID  string `json:"logical_pool_id"`
	PoolGeneration int64  `json:"pool_generation"`
	Qualified      bool   `json:"qualified"`
	MinReadyMet    bool   `json:"min_ready_met"`
	FailureDomain  string `json:"failure_domain,omitempty"`
	ReasonCode     string `json:"reason_code"`
}

// DeploymentAdapterClient is the boundary the RolloutService calls OUTSIDE any
// DB transaction (repository pre-stage, Gateway/Triton replica start,
// warmup/readback/capacity). systemd/Kubernetes/MLflow/KServe/Triton
// repository/OCI tag must NEVER become the canonical current binding.
type DeploymentAdapterClient interface {
	StagePool(ctx context.Context, pool PoolGeneration) error
	StartReplicas(ctx context.Context, pool PoolGeneration) (Readback, error)
	WarmupAndCapacity(ctx context.Context, pool PoolGeneration) (CapacityResult, error)
}

// RoutePrepare is the exact Edge route-withdraw/drain fence. The proposed route
// epoch is persisted only after Edge has installed/read back the proposed route.
type RoutePrepare struct {
	SchemaVersion                    string `json:"schema_version"`
	ShardID                          string `json:"shard_id"`
	CurrentModelControlIncarnationID string `json:"current_model_control_incarnation_id"`
	CurrentRouteEpoch                int64  `json:"current_route_epoch"`
	ProposedRouteEpoch               int64  `json:"proposed_route_epoch"`
	TraceID                          string `json:"trace_id"`
	EdgeWorkloadRef                  string `json:"edge_workload_ref"`
}

// TLSClientIdentity references an Edge-local, preconfigured inference client
// identity. No certificate or private-key bytes cross the public contract.
type TLSClientIdentity struct {
	ServerName  string `json:"server_name"`
	IdentityRef string `json:"identity_ref"`
}

// InferenceRoute is the exact immutable route Edge installs and reads back
// while admission remains withdrawn. It mirrors contracts/edge/v1 InferenceRoute.
type InferenceRoute struct {
	SchemaVersion             string            `json:"schema_version"`
	ShardID                   string            `json:"shard_id"`
	ModelControlIncarnationID string            `json:"model_control_incarnation_id"`
	LogicalPoolID             string            `json:"logical_pool_id"`
	PoolGeneration            int64             `json:"pool_generation"`
	BindingGeneration         int64             `json:"binding_generation"`
	RouteEpoch                int64             `json:"route_epoch"`
	ModelRevisionDigest       string            `json:"model_revision_digest"`
	FeatureContractDigest     string            `json:"feature_contract_digest"`
	LabelContractDigest       string            `json:"label_contract_digest"`
	OutputAdapterDigest       string            `json:"output_adapter_digest"`
	WireProfile               string            `json:"wire_profile"`
	RuntimeProfile            string            `json:"runtime_profile"`
	Endpoint                  string            `json:"endpoint"`
	TLS                       TLSClientIdentity `json:"tls"`
	OperationID               string            `json:"operation_id"`
	Scope                     string            `json:"scope"`
	ExpectedBindingGeneration int64             `json:"expected_binding_generation"`
	ProposedBindingGeneration int64             `json:"proposed_binding_generation"`
	CurrentBindingGeneration  int64             `json:"current_binding_generation"`
	StartupEnvelopeDigest     string            `json:"startup_envelope_digest"`
	PoolObservationDigest     string            `json:"pool_observation_digest"`
	BindingDigest             string            `json:"binding_digest"`
	ModelBundleDigest         string            `json:"model_bundle_digest"`
	WireProfileDigest         string            `json:"wire_profile_digest"`
	RuntimeProfileDigest      string            `json:"runtime_profile_digest"`
	OptimizationProfileDigest string            `json:"optimization_profile_digest"`
	EdgeWorkloadRef           string            `json:"edge_workload_ref"`
}

// RouteReadback is the bounded Edge reply used as the installation/resume
// oracle. A successful RPC without the exact state/epoch/digest is rejected.
type RouteReadback struct {
	ShardID        string `json:"shard_id"`
	State          string `json:"state"`
	RouteEpoch     int64  `json:"route_epoch"`
	PendingInputs  int64  `json:"pending_inputs"`
	PendingResults int64  `json:"pending_results"`
	ReasonCode     string `json:"reason_code"`
	BindingDigest  string `json:"binding_digest"`
}

// EdgeRouteClient is the exact public EdgeControl prepare/commit/resume
// boundary, called OUTSIDE any DB transaction.
type EdgeRouteClient interface {
	PrepareRoute(ctx context.Context, prepare RoutePrepare) (RouteReadback, error)
	CommitRoute(ctx context.Context, route InferenceRoute, traceID string) (RouteReadback, error)
	ResumeRoute(ctx context.Context, hs CommittedBindingHandshake, traceID string) (RouteReadback, error)
}
