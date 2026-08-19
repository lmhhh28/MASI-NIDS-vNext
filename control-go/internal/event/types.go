// Package event implements Event Ingest and Canonical ACK for Go Control Core.
// Edge submits bounded batches of InferenceResults; Go validates source/window/
// input/result, model-control incarnation, shard/route/pool/binding generation,
// feature/label/adapter/profile and payload digest, then in ONE short PG
// transaction creates or recalls the canonical Event + idempotency record.
//
// Only AFTER the transaction durably commits does Go return the canonical ACK/
// cursor (go-control-core-design §4). Duplicate replay returns the original
// Event; same key + different digest is a stable conflict; wrong/late
// generation is rejected (no canonical Event). Abstain/OOD/low-quality are
// projected explicitly, never as normal.
package event

// SourceWindowIdentity is the telemetry source window identity (from
// contracts/telemetry/v1 + edge.proto InferenceRecord).
type SourceWindowIdentity struct {
	SourceID          string `json:"source_id"`
	WindowID          string `json:"window_id"`
	WindowStartUnixMS int64  `json:"window_start_unix_ms"`
	WindowEndUnixMS   int64  `json:"window_end_unix_ms"`
	FinalizedAtUnixMS int64  `json:"finalized_at_unix_ms"`
	WatermarkUnixMS   int64  `json:"watermark_unix_ms"`
	EventTimeUnixMS   int64  `json:"event_time_unix_ms"`
}

// InferenceResult is the validated fence dimension set Go receives from Edge
// (CommitResults). The digest fields bind the exact input/output/model/wire.
type InferenceResult struct {
	EventIDempotencyKey        string                   `json:"event_idempotency_key"`
	InputDigest                string                   `json:"input_digest"`
	OutputDigest               string                   `json:"output_digest"`
	ModelControlIncarnationID  string                   `json:"model_control_incarnation_id"`
	ShardID                    string                   `json:"shard_id"`
	RouteEpoch                 int64                    `json:"route_epoch"`
	LogicalPoolID              string                   `json:"logical_pool_id"`
	PoolGeneration             int64                    `json:"pool_generation"`
	BindingGeneration          int64                    `json:"binding_generation"`
	ModelRevisionDigest        string                   `json:"model_revision_digest"`
	ModelBundleDigest          string                   `json:"model_bundle_digest"`
	FeatureContractDigest      string                   `json:"feature_contract_digest"`
	LabelContractDigest        string                   `json:"label_contract_digest"`
	OutputAdapterDigest        string                   `json:"output_adapter_digest"`
	WireProfileDigest          string                   `json:"wire_profile_digest"`
	RuntimeProfileDigest       string                   `json:"runtime_profile_digest"`
	OptimizationProfileDigest  string                   `json:"optimization_profile_digest"`
	StartupEnvelopeDigest      string                   `json:"startup_envelope_digest"`
	PoolObservationDigest      string                   `json:"pool_observation_digest"`
	BindingDigest              string                   `json:"binding_digest"`
	Scores                     []float64                `json:"scores"`
	PredictedLabel             uint32                   `json:"predicted_label"`
	Decision                   InferenceDecision        `json:"decision"`
	OutOfDistribution          bool                     `json:"out_of_distribution"`
	Abstain                    bool                     `json:"abstain"`
	ExecutionStatus            InferenceExecutionStatus `json:"execution_status"`
	ErrorCode                  string                   `json:"error_code,omitempty"`
	WorkerID                   string                   `json:"worker_id"`
	WorkerDigest               string                   `json:"worker_digest"`
	WorkerAttemptID            string                   `json:"worker_attempt_id"`
	InferenceStartedAtUnixMS   int64                    `json:"inference_started_at_unix_ms"`
	InferenceCompletedAtUnixMS int64                    `json:"inference_completed_at_unix_ms"`
	Scope                      string                   `json:"scope"`
	SourceWindow               SourceWindowIdentity     `json:"source_window_identity"`
	Quality                    string                   `json:"quality"`
	EventTimeUnixMS            int64                    `json:"event_time_unix_ms"`
	IngestBatchDigest          string                   `json:"ingest_batch_digest"`
	TraceID                    string                   `json:"trace_id"`
}

type InferenceDecision string

const (
	DecisionBenign  InferenceDecision = "benign"
	DecisionAlert   InferenceDecision = "alert"
	DecisionAbstain InferenceDecision = "abstain"
)

type InferenceExecutionStatus string

const (
	ExecutionOK    InferenceExecutionStatus = "ok"
	ExecutionHold  InferenceExecutionStatus = "hold"
	ExecutionError InferenceExecutionStatus = "error"
)

// CommitStatus is the canonical ACK commit status (matches edge.proto
// CanonicalCommitStatus + the event/v1 reject outcomes).
type CommitStatus string

const (
	StatusCommitted  CommitStatus = "committed"
	StatusIdempotent CommitStatus = "idempotent"
	StatusConflict   CommitStatus = "conflict"
	StatusRejected   CommitStatus = "rejected"
)

// CanonicalACK is the per-record ACK returned after durable commit. An ACK is
// only returned for committed or idempotent records; conflict/rejected carry a
// reason_code and no canonical_event_id (edge.proto CanonicalAck shape).
type CanonicalACK struct {
	EventIDempotencyKey string       `json:"event_idempotency_key"`
	InputDigest         string       `json:"input_digest"`
	OutputDigest        string       `json:"output_digest"`
	CanonicalEventID    string       `json:"canonical_event_id,omitempty"`
	CommitStatus        CommitStatus `json:"commit_status"`
	CommittedAtUnixMS   int64        `json:"committed_at_unix_ms,omitempty"`
	ReasonCode          string       `json:"reason_code"`
}

// Quality values (matches contracts/event/v1 + DataQuality enum).
const (
	QualityValid         = "valid"
	QualityPartial       = "partial"
	QualityGap           = "gap"
	QualityStale         = "stale"
	QualityInvalid       = "invalid"
	QualityReset         = "reset"
	QualityNotCovered    = "not-covered"
	QualityNotMeasurable = "not-measurable"
)

// isLowQuality reports whether the quality must NOT be projected as a normal
// committed event (abstain/OOD/low-quality projected explicitly — §4).
func isLowQuality(q string) bool {
	switch q {
	case QualityGap, QualityStale, QualityInvalid, QualityReset,
		QualityNotCovered, QualityNotMeasurable:
		return true
	}
	return false
}
