package event

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"strings"
)

var (
	digestRE   = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	identityRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]*$`)
)

// CanonicalEventID derives the stable, never-reused canonical event id from the
// fence dimensions. The id is deterministic over (event_idempotency_key,
// input_digest, output_digest, model_control_incarnation_id, shard_id,
// route_epoch, logical_pool_id, pool_generation, binding_generation): the same
// result replayed with the same digests yields the SAME canonical_event_id
// (idempotent). A different output_digest produces a different id (conflict is
// detected via the idempotency unique index, not the id itself).
func CanonicalEventID(r InferenceResult) (string, error) {
	if err := validateFence(r); err != nil {
		return "", err
	}
	h := sha256.New()
	fmt.Fprintf(h, "%s|%s|%s|%s|%s|%d|%s|%d|%d|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s",
		r.EventIDempotencyKey, r.InputDigest, r.OutputDigest,
		r.ModelControlIncarnationID, r.ShardID, r.RouteEpoch,
		r.LogicalPoolID, r.PoolGeneration, r.BindingGeneration, r.ModelRevisionDigest, r.ModelBundleDigest,
		r.FeatureContractDigest, r.LabelContractDigest, r.OutputAdapterDigest, r.WireProfileDigest, r.RuntimeProfileDigest, r.Scope,
		r.OptimizationProfileDigest, r.StartupEnvelopeDigest, r.PoolObservationDigest, r.BindingDigest,
		ResultIdentityDigest(r))
	return "evt-" + hex.EncodeToString(h.Sum(nil))[:32], nil
}

// validateFence checks the inference result fence dimensions are well-formed
// and internally consistent (event/v1 EVENT-GENERATION-CONSISTENCY). Late/wrong
// generation is detected by the caller against current durable facts.
func validateFence(r InferenceResult) error {
	for name, value := range map[string]string{
		"event_idempotency_key":        r.EventIDempotencyKey,
		"model_control_incarnation_id": r.ModelControlIncarnationID,
		"shard_id":                     r.ShardID, "logical_pool_id": r.LogicalPoolID,
		"scope": r.Scope, "source_id": r.SourceWindow.SourceID,
		"window_id": r.SourceWindow.WindowID, "trace_id": r.TraceID,
	} {
		if len(value) == 0 || len(value) > 128 {
			return fmt.Errorf("event: %s length outside 1..128", name)
		}
	}
	if !identityRE.MatchString(r.EventIDempotencyKey) {
		return fmt.Errorf("event: bad event_idempotency_key")
	}
	if !digestRE.MatchString(r.InputDigest) {
		return fmt.Errorf("event: bad input_digest")
	}
	if !digestRE.MatchString(r.OutputDigest) {
		return fmt.Errorf("event: bad output_digest")
	}
	if !identityRE.MatchString(r.ModelControlIncarnationID) {
		return fmt.Errorf("event: bad model_control_incarnation_id")
	}
	if r.RouteEpoch < 1 || r.PoolGeneration < 1 || r.BindingGeneration < 1 {
		return fmt.Errorf("event: generations must be >= 1")
	}
	for name, digest := range map[string]string{
		"model_revision": r.ModelRevisionDigest, "model_bundle": r.ModelBundleDigest,
		"feature_contract": r.FeatureContractDigest, "label_contract": r.LabelContractDigest,
		"output_adapter": r.OutputAdapterDigest, "wire_profile": r.WireProfileDigest,
		"runtime_profile":      r.RuntimeProfileDigest,
		"optimization_profile": r.OptimizationProfileDigest, "startup_envelope": r.StartupEnvelopeDigest,
		"pool_observation": r.PoolObservationDigest, "binding": r.BindingDigest,
	} {
		if !digestRE.MatchString(digest) {
			return fmt.Errorf("event: bad %s digest", name)
		}
	}
	if !identityRE.MatchString(r.ShardID) || !identityRE.MatchString(r.LogicalPoolID) {
		return fmt.Errorf("event: shard/logical pool/scope required")
	}
	if !identityRE.MatchString(r.SourceWindow.SourceID) || !identityRE.MatchString(r.SourceWindow.WindowID) ||
		r.SourceWindow.WindowStartUnixMS <= 0 || r.SourceWindow.WindowEndUnixMS <= r.SourceWindow.WindowStartUnixMS ||
		r.SourceWindow.FinalizedAtUnixMS < r.SourceWindow.WindowEndUnixMS || r.SourceWindow.WatermarkUnixMS < r.SourceWindow.WindowEndUnixMS ||
		r.SourceWindow.EventTimeUnixMS != r.EventTimeUnixMS || r.EventTimeUnixMS != r.SourceWindow.WindowEndUnixMS {
		return fmt.Errorf("event: source window times required")
	}
	if !digestRE.MatchString(r.IngestBatchDigest) || r.TraceID == "" {
		return fmt.Errorf("event: ingest batch digest/trace required")
	}
	if !validQuality(r.Quality) {
		return fmt.Errorf("event: bad quality %q", r.Quality)
	}
	if err := validateInferenceOutcome(r); err != nil {
		return err
	}
	return nil
}

func validateInferenceOutcome(r InferenceResult) error {
	if r.Decision != DecisionBenign && r.Decision != DecisionAlert && r.Decision != DecisionAbstain {
		return fmt.Errorf("event: bad inference decision %q", r.Decision)
	}
	if r.ExecutionStatus != ExecutionOK && r.ExecutionStatus != ExecutionHold && r.ExecutionStatus != ExecutionError {
		return fmt.Errorf("event: bad inference execution status %q", r.ExecutionStatus)
	}
	if len(r.Scores) > 4096 || (r.ExecutionStatus == ExecutionOK && len(r.Scores) == 0) {
		return fmt.Errorf("event: score vector outside execution bounds")
	}
	for _, score := range r.Scores {
		if math.IsNaN(score) || math.IsInf(score, 0) {
			return fmt.Errorf("event: score vector contains NaN/Inf")
		}
	}
	if r.WorkerID == "" || len(r.WorkerID) > 128 || !identityRE.MatchString(r.WorkerID) ||
		!digestRE.MatchString(r.WorkerDigest) || r.WorkerAttemptID == "" ||
		len(r.WorkerAttemptID) > 128 || !identityRE.MatchString(r.WorkerAttemptID) {
		return fmt.Errorf("event: worker/attempt identity malformed")
	}
	if r.InferenceStartedAtUnixMS < 1 || r.InferenceCompletedAtUnixMS < r.InferenceStartedAtUnixMS {
		return fmt.Errorf("event: inference timestamps malformed")
	}
	if r.Abstain != (r.Decision == DecisionAbstain) || (r.OutOfDistribution && !r.Abstain) {
		return fmt.Errorf("event: abstain/OOD/decision semantics conflict")
	}
	if r.ExecutionStatus != ExecutionOK && !r.Abstain {
		return fmt.Errorf("event: non-OK execution must abstain")
	}
	if r.ExecutionStatus == ExecutionOK && r.ErrorCode != "" {
		return fmt.Errorf("event: successful execution cannot carry error_code")
	}
	if r.ExecutionStatus != ExecutionOK && (r.ErrorCode == "" || len(r.ErrorCode) > 64) {
		return fmt.Errorf("event: non-OK execution requires bounded error_code")
	}
	return nil
}

// ResultIdentityDigest binds the complete output and worker-attempt provenance
// used by the Event idempotency comparison. The output digest remains the
// cross-language payload digest; this digest prevents provenance fields from
// being silently changed under the same Event key.
func ResultIdentityDigest(r InferenceResult) string {
	h := sha256.New()
	fmt.Fprintf(h, "%s|%d|%s|%t|%t|%s|%s|%s|%s|%d|%d",
		r.OutputDigest, r.PredictedLabel, r.Decision, r.OutOfDistribution, r.Abstain,
		r.ExecutionStatus, r.ErrorCode, r.WorkerID, r.WorkerDigest,
		r.InferenceStartedAtUnixMS, r.InferenceCompletedAtUnixMS)
	fmt.Fprintf(h, "|attempt:%s", r.WorkerAttemptID)
	for _, score := range r.Scores {
		fmt.Fprintf(h, "|score:%016x", math.Float64bits(score))
	}
	return "sha256:" + hex.EncodeToString(h.Sum(nil))
}

func validQuality(q string) bool {
	switch q {
	case QualityValid, QualityPartial, QualityGap, QualityStale,
		QualityInvalid, QualityReset, QualityNotCovered, QualityNotMeasurable:
		return true
	}
	return false
}

// SourceWindowJSON serializes the source window identity for JSONB storage.
func SourceWindowJSON(w SourceWindowIdentity) ([]byte, error) {
	return json.Marshal(w)
}

// IsConflict reports whether the idempotency collision is a stable conflict
// (same key, different digest) versus a replay (same key, same digest).
func IsConflict(existingInput, existingOutput, newInput, newOutput string) bool {
	return existingInput != newInput || existingOutput != newOutput
}

// ReasonForQuality returns the explicit reason code for a low-quality result
// (never projected as normal committed).
func ReasonForQuality(q string) string {
	if !isLowQuality(q) {
		return "COMMITTED"
	}
	return strings.ToUpper(strings.ReplaceAll(q, "-", "_"))
}
