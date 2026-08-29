// Package a2a implements the bounded A2A 1.0 HTTP+JSON client used by Go
// Control to submit frozen analysis bundles and poll tasks. It never streams,
// configures push notifications, performs dynamic discovery, or accepts
// executable Artifacts.
package a2a

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"

	"masi-nids/control-go/internal/security"
)

const (
	ProtocolVersion = "1.0"
	InputSchema     = "masi-analysis-input/v1"
	ArtifactSchema  = "masi-analysis-artifact/v1"
)

var allowedSkills = map[string]bool{
	"analyze_nids_incident":     true,
	"compare_event_windows":     true,
	"draft_mitigation_advice":   true,
	"generate_incident_content": true,
}

type FactRef struct {
	ID     string `json:"id"`
	Digest string `json:"digest"`
}

type AnalysisBudgets struct {
	LLMCalls               int `json:"llm_calls"`
	MCPRounds              int `json:"mcp_rounds"`
	ToolCalls              int `json:"tool_calls"`
	ToolParallelism        int `json:"tool_parallelism"`
	ToolTimeoutMS          int `json:"tool_timeout_ms"`
	ToolResponseBytes      int `json:"tool_response_bytes"`
	ToolTotalResponseBytes int `json:"tool_total_response_bytes"`
	LLMTimeoutMS           int `json:"llm_timeout_ms"`
	ResultBudgetMS         int `json:"result_budget_ms"`
	GraphDeadlineMS        int `json:"graph_deadline_ms"`
	ArtifactBytes          int `json:"artifact_bytes"`
	OutboundDelegations    int `json:"outbound_delegations"`
	DelegationDepth        int `json:"delegation_depth"`
	PollsPerTask           int `json:"polls_per_task"`
	A2AResponseBytes       int `json:"a2a_response_bytes"`
}

// InputBundle contains references to frozen canonical facts. It contains no DB
// credential, mutable object, prompt, arbitrary URL, or packet payload.
type InputBundle struct {
	SchemaVersion                string          `json:"schema_version"`
	TaskID                       string          `json:"task_id"`
	RunID                        string          `json:"run_id"`
	Skill                        string          `json:"skill"`
	PluginID                     string          `json:"plugin_id"`
	PluginRevision               string          `json:"plugin_revision"`
	ConfigDigest                 string          `json:"config_digest"`
	BindingGeneration            int             `json:"binding_generation"`
	Scope                        string          `json:"scope"`
	TargetSetDigest              string          `json:"target_set_digest"`
	EventRefs                    []FactRef       `json:"event_refs"`
	IncidentRefs                 []FactRef       `json:"incident_refs"`
	EvidenceRefs                 []FactRef       `json:"evidence_refs"`
	RuntimeRefs                  []FactRef       `json:"runtime_refs"`
	ModelResultEvidenceRefs      []string        `json:"model_result_evidence_refs"`
	ModelExplanationEvidenceRefs []string        `json:"model_explanation_evidence_refs"`
	Quality                      string          `json:"quality"`
	ProviderProfileDigest        string          `json:"provider_profile_digest"`
	PromptProfileDigest          string          `json:"prompt_profile_digest"`
	ToolPolicyDigest             string          `json:"tool_policy_digest"`
	RedactionProfileDigest       string          `json:"redaction_profile_digest"`
	ProvenanceDigest             string          `json:"provenance_digest"`
	Budgets                      AnalysisBudgets `json:"budgets"`
	InputDigest                  string          `json:"input_digest"`
	DeadlineUnixMS               int64           `json:"deadline_unix_ms"`
	ExpiresAtUnixMS              int64           `json:"expires_at_unix_ms"`
	Locale                       string          `json:"locale"`
	ContentRequest               string          `json:"content_request"`
	IdempotencyKey               string          `json:"idempotency_key"`
	DelegationPath               []string        `json:"delegation_path"`
	TraceID                      string          `json:"trace_id"`
}

type GroundedClaim struct {
	Claim        string   `json:"claim"`
	EvidenceRefs []string `json:"evidence_refs"`
}

type Recommendation struct {
	Text            string   `json:"text"`
	Risk            string   `json:"risk"`
	Preconditions   []string `json:"preconditions"`
	ExpiresAtUnixMS int64    `json:"expires_at_unix_ms"`
	EvidenceRefs    []string `json:"evidence_refs"`
}

type GeneratedContent struct {
	MediaType string `json:"media_type"`
	Body      string `json:"body"`
}

type ProviderMetadata struct {
	ProviderID     string `json:"provider_id"`
	ModelID        string `json:"model_id"`
	ProviderDigest string `json:"provider_digest"`
}

type ModelExplanationFact struct {
	Claim            string   `json:"claim"`
	EvidenceRefs     []string `json:"evidence_refs"`
	Method           string   `json:"method"`
	BackgroundDigest *string  `json:"background_digest"`
	ModelDigest      string   `json:"model_digest"`
	ScalerDigest     *string  `json:"scaler_digest"`
	SampleDigest     string   `json:"sample_digest"`
	Coverage         float64  `json:"coverage"`
	Truncated        bool     `json:"truncated"`
	Limitations      []string `json:"limitations"`
}

// Artifact is the exact non-executable body accepted from an Analysis peer.
type Artifact struct {
	SchemaVersion         string                 `json:"schema_version"`
	ArtifactID            string                 `json:"artifact_id"`
	ArtifactDigest        string                 `json:"artifact_digest"`
	TaskID                string                 `json:"task_id"`
	RunID                 string                 `json:"run_id"`
	PluginID              string                 `json:"plugin_id"`
	PluginRevision        string                 `json:"plugin_revision"`
	ConfigDigest          string                 `json:"config_digest"`
	BindingGeneration     int                    `json:"binding_generation"`
	InputDigest           string                 `json:"input_digest"`
	AnalysisOutcome       string                 `json:"analysis_outcome"`
	Quality               string                 `json:"quality"`
	SourceFactRefs        []string               `json:"source_fact_refs"`
	ObservedClaims        []GroundedClaim        `json:"observed_claims"`
	ModelResultFacts      []GroundedClaim        `json:"model_result_facts"`
	ModelExplanationFacts []ModelExplanationFact `json:"model_explanation_facts"`
	InferredClaims        []string               `json:"inferred_claims"`
	LLMInterpretations    []string               `json:"llm_interpretations"`
	Uncertainties         []string               `json:"uncertainties"`
	Limitations           []string               `json:"limitations"`
	MissingEvidence       []string               `json:"missing_evidence"`
	Recommendations       []Recommendation       `json:"recommendations"`
	GeneratedContent      []GeneratedContent     `json:"generated_content"`
	ProviderMetadata      ProviderMetadata       `json:"provider_metadata"`
	ToolTrajectoryDigest  string                 `json:"tool_trajectory_digest"`
	GraphVersion          string                 `json:"graph_version"`
	TopologyDigest        string                 `json:"topology_digest"`
	TraceID               string                 `json:"trace_id"`
	TraceDigest           string                 `json:"trace_digest"`
	ProducedAtUnixMS      int64                  `json:"produced_at_unix_ms"`
	ExpiresAtUnixMS       int64                  `json:"expires_at_unix_ms"`
	MediaType             string                 `json:"media_type"`
	NonExecutable         bool                   `json:"non_executable"`
	DeploymentEligible    bool                   `json:"deployment_eligible"`
}

type TaskProjection struct {
	TaskID            string `json:"task_id"`
	RemoteTaskID      string `json:"remote_task_id,omitempty"`
	RemoteContextID   string `json:"remote_context_id,omitempty"`
	PluginID          string `json:"plugin_id"`
	BindingGeneration int    `json:"binding_generation"`
	Status            string `json:"status"`
	PollCount         int    `json:"poll_count"`
	RequestDigest     string `json:"request_digest"`
	ResponseDigest    string `json:"response_digest,omitempty"`
}

func ValidateInput(input *InputBundle, now time.Time) error {
	if input == nil {
		return errors.New("a2a: input required")
	}
	normalizeInputCollections(input)
	if input.SchemaVersion == "" {
		input.SchemaVersion = InputSchema
	}
	if input.SchemaVersion != InputSchema || !validIdentity(input.TaskID) || !validIdentity(input.RunID) || !allowedSkills[input.Skill] ||
		!validIdentity(input.PluginID) || !validRevision(input.PluginRevision) || !validDigest(input.ConfigDigest) ||
		input.BindingGeneration < 1 || input.Scope == "" || len(input.Scope) > 256 || !validDigest(input.TargetSetDigest) ||
		input.DeadlineUnixMS <= now.UnixMilli() || input.DeadlineUnixMS > now.Add(30*time.Second).UnixMilli() ||
		input.ExpiresAtUnixMS < input.DeadlineUnixMS || input.Locale == "" || len(input.Locale) > 32 ||
		len(input.ContentRequest) > 2048 || !validIdentity(input.IdempotencyKey) || !validIdentity(input.TraceID) {
		return errors.New("a2a: input identity/scope/deadline/bounds malformed")
	}
	if input.Quality != "valid" && input.Quality != "partial" && input.Quality != "low" {
		return errors.New("a2a: stale/invalid/unknown analysis input quality rejected")
	}
	for _, value := range []string{input.ProviderProfileDigest, input.PromptProfileDigest, input.ToolPolicyDigest,
		input.RedactionProfileDigest, input.ProvenanceDigest} {
		if !validDigest(value) {
			return errors.New("a2a: analysis profile/provenance digest malformed")
		}
	}
	if !validAnalysisBudgets(input.Budgets) || input.DeadlineUnixMS-now.UnixMilli() > int64(input.Budgets.GraphDeadlineMS) {
		return errors.New("a2a: analysis budget malformed or inconsistent with deadline")
	}
	if len(input.EventRefs) > 128 || len(input.IncidentRefs) > 32 || len(input.EvidenceRefs) > 128 || len(input.RuntimeRefs) > 64 ||
		len(input.EventRefs)+len(input.IncidentRefs)+len(input.EvidenceRefs) == 0 {
		return errors.New("a2a: frozen fact reference bounds invalid")
	}
	for _, group := range [][]FactRef{input.EventRefs, input.IncidentRefs, input.EvidenceRefs, input.RuntimeRefs} {
		seen := map[string]bool{}
		for _, ref := range group {
			if !validIdentity(ref.ID) || !validDigest(ref.Digest) || seen[ref.ID] {
				return errors.New("a2a: fact reference malformed or duplicated")
			}
			seen[ref.ID] = true
		}
	}
	trustedEvidence := map[string]bool{}
	for _, ref := range input.EvidenceRefs {
		trustedEvidence[ref.ID] = true
	}
	for _, group := range [][]string{input.ModelResultEvidenceRefs, input.ModelExplanationEvidenceRefs} {
		if len(group) > 128 {
			return errors.New("a2a: model evidence reference bound exceeded")
		}
		seen := map[string]bool{}
		for _, id := range group {
			if !validIdentity(id) || seen[id] || !trustedEvidence[id] {
				return errors.New("a2a: model evidence reference is not a unique frozen evidence identity")
			}
			seen[id] = true
		}
	}
	if len(input.DelegationPath) > 2 || len(input.DelegationPath) != input.Budgets.DelegationDepth {
		return errors.New("a2a: delegation path/depth malformed")
	}
	seenPath := map[string]bool{}
	for _, id := range input.DelegationPath {
		if !validIdentity(id) || seenPath[id] || id == input.PluginID {
			return errors.New("a2a: delegation loop detected")
		}
		seenPath[id] = true
	}
	expected := ComputeInputDigest(*input)
	if input.InputDigest == "" {
		input.InputDigest = expected
	} else if input.InputDigest != expected {
		return errors.New("a2a: caller input digest does not bind frozen bundle")
	}
	return nil
}

func ComputeInputDigest(input InputBundle) string {
	normalizeInputCollections(&input)
	input.InputDigest = ""
	sortFactRefs(input.EventRefs)
	sortFactRefs(input.IncidentRefs)
	sortFactRefs(input.EvidenceRefs)
	sortFactRefs(input.RuntimeRefs)
	raw, _ := json.Marshal(input)
	return digest(raw)
}

func normalizeInputCollections(input *InputBundle) {
	if input.EventRefs == nil {
		input.EventRefs = []FactRef{}
	}
	if input.IncidentRefs == nil {
		input.IncidentRefs = []FactRef{}
	}
	if input.EvidenceRefs == nil {
		input.EvidenceRefs = []FactRef{}
	}
	if input.RuntimeRefs == nil {
		input.RuntimeRefs = []FactRef{}
	}
	if input.ModelResultEvidenceRefs == nil {
		input.ModelResultEvidenceRefs = []string{}
	}
	if input.ModelExplanationEvidenceRefs == nil {
		input.ModelExplanationEvidenceRefs = []string{}
	}
	if input.DelegationPath == nil {
		input.DelegationPath = []string{}
	}
}

func ValidateArtifact(artifact *Artifact, input InputBundle) error {
	if artifact == nil || artifact.SchemaVersion != ArtifactSchema || !validIdentity(artifact.ArtifactID) ||
		artifact.TaskID != input.TaskID || artifact.RunID != input.RunID || artifact.PluginID != input.PluginID ||
		artifact.PluginRevision != input.PluginRevision || artifact.ConfigDigest != input.ConfigDigest ||
		artifact.BindingGeneration != input.BindingGeneration || artifact.InputDigest != input.InputDigest ||
		!validDigest(artifact.ToolTrajectoryDigest) || artifact.GraphVersion != "masi-analysis-graph/v1" ||
		!validDigest(artifact.TopologyDigest) || !validIdentity(artifact.TraceID) || artifact.TraceID != input.TraceID ||
		!validDigest(artifact.TraceDigest) || artifact.ProducedAtUnixMS < 1 || artifact.ExpiresAtUnixMS != input.ExpiresAtUnixMS ||
		artifact.ProducedAtUnixMS > artifact.ExpiresAtUnixMS ||
		!artifact.NonExecutable || artifact.DeploymentEligible ||
		(artifact.MediaType != "application/json" && artifact.MediaType != "text/markdown") {
		return errors.New("a2a: artifact identity/non-executable fence invalid")
	}
	switch artifact.AnalysisOutcome {
	case "succeeded", "limited", "insufficient_evidence", "failed":
	default:
		return errors.New("a2a: artifact outcome unsupported")
	}
	if artifact.Quality != "valid" && artifact.Quality != "limited" && artifact.Quality != "low" && artifact.Quality != "invalid" {
		return errors.New("a2a: artifact quality unsupported")
	}
	if artifact.AnalysisOutcome == "succeeded" && artifact.Quality != "valid" {
		return errors.New("a2a: succeeded artifact must have valid quality")
	}
	if len(artifact.SourceFactRefs) > 352 || len(artifact.ObservedClaims) > 128 || len(artifact.ModelResultFacts) > 64 ||
		len(artifact.ModelExplanationFacts) > 64 || len(artifact.InferredClaims) > 128 || len(artifact.LLMInterpretations) > 128 || len(artifact.Uncertainties) > 128 ||
		len(artifact.Limitations) > 128 || len(artifact.MissingEvidence) > 128 || len(artifact.Recommendations) > 64 ||
		len(artifact.GeneratedContent) > 16 {
		return errors.New("a2a: artifact collection bound exceeded")
	}
	trustedEvidence := map[string]bool{}
	trustedSourceFacts := map[string]bool{}
	for _, group := range [][]FactRef{input.EventRefs, input.IncidentRefs, input.EvidenceRefs, input.RuntimeRefs} {
		for _, ref := range group {
			trustedSourceFacts[ref.ID] = true
		}
	}
	for _, ref := range input.EvidenceRefs {
		trustedEvidence[ref.ID] = true
	}
	if err := validateIdentitySubset(artifact.SourceFactRefs, trustedSourceFacts, "source fact"); err != nil {
		return err
	}
	if err := validateGroundedClaims(artifact.ObservedClaims, trustedEvidence, "observed"); err != nil {
		return err
	}
	trustedModelResults := identitySet(input.ModelResultEvidenceRefs)
	if err := validateGroundedClaims(artifact.ModelResultFacts, trustedModelResults, "model result"); err != nil {
		return err
	}
	trustedExplanations := identitySet(input.ModelExplanationEvidenceRefs)
	for _, explanation := range artifact.ModelExplanationFacts {
		if err := validateGroundedClaims([]GroundedClaim{{Claim: explanation.Claim, EvidenceRefs: explanation.EvidenceRefs}}, trustedExplanations, "model explanation"); err != nil {
			return err
		}
		if causalAnalysisText(explanation.Claim) || len(explanation.Limitations) > 32 || math.IsNaN(explanation.Coverage) ||
			math.IsInf(explanation.Coverage, 0) || explanation.Coverage < 0 || explanation.Coverage > 1 ||
			!validDigest(explanation.ModelDigest) || !validDigest(explanation.SampleDigest) ||
			(explanation.BackgroundDigest != nil && !validDigest(*explanation.BackgroundDigest)) ||
			(explanation.ScalerDigest != nil && !validDigest(*explanation.ScalerDigest)) {
			return errors.New("a2a: model explanation metadata malformed or causal")
		}
		switch explanation.Method {
		case "logistic-contribution", "tree-shap", "reconstruction-residual", "other-qualified":
		default:
			return errors.New("a2a: model explanation method unsupported")
		}
		if err := validateTextValues(explanation.Limitations, 1024); err != nil {
			return err
		}
	}
	for _, values := range [][]string{artifact.InferredClaims, artifact.LLMInterpretations, artifact.Uncertainties, artifact.Limitations, artifact.MissingEvidence} {
		if err := validateTextValues(values, 4096); err != nil {
			return err
		}
	}
	for _, rec := range artifact.Recommendations {
		if rec.Text == "" || len(rec.Text) > 4096 || unsafeAnalysisText(rec.Text) || len(rec.Risk) < 1 || len(rec.Risk) > 128 ||
			len(rec.Preconditions) > 32 || len(rec.EvidenceRefs) > 32 || rec.ExpiresAtUnixMS < 1 || rec.ExpiresAtUnixMS > artifact.ExpiresAtUnixMS {
			return errors.New("a2a: recommendation malformed")
		}
		if err := validateTextValues(rec.Preconditions, 1024); err != nil {
			return err
		}
		for _, ref := range rec.EvidenceRefs {
			if !trustedEvidence[ref] {
				return errors.New("a2a: recommendation evidence outside frozen bundle")
			}
		}
	}
	for _, content := range artifact.GeneratedContent {
		if (content.MediaType != "application/json" && content.MediaType != "text/markdown") || len(content.Body) > 32*1024 || unsafeAnalysisText(content.Body) {
			return errors.New("a2a: generated content type/size unsupported")
		}
	}
	if !validIdentity(artifact.ProviderMetadata.ProviderID) || !validIdentity(artifact.ProviderMetadata.ModelID) ||
		!validDigest(artifact.ProviderMetadata.ProviderDigest) {
		return errors.New("a2a: provider provenance malformed")
	}
	expected := ComputeArtifactDigest(*artifact)
	if artifact.ArtifactDigest != expected {
		return fmt.Errorf("a2a: artifact digest mismatch")
	}
	raw, _ := json.Marshal(artifact)
	if len(raw) > 64*1024 || (input.Budgets.ArtifactBytes > 0 && len(raw) > input.Budgets.ArtifactBytes) {
		return errors.New("a2a: artifact exceeds 64 KiB")
	}
	return nil
}

func ComputeArtifactDigest(artifact Artifact) string {
	artifact.ArtifactDigest = ""
	raw, _ := json.Marshal(artifact)
	return digest(raw)
}

func sortFactRefs(refs []FactRef) {
	sort.Slice(refs, func(i, j int) bool {
		if refs[i].ID != refs[j].ID {
			return refs[i].ID < refs[j].ID
		}
		return refs[i].Digest < refs[j].Digest
	})
}

func validAnalysisBudgets(value AnalysisBudgets) bool {
	return value.LLMCalls >= 0 && value.LLMCalls <= 2 && value.MCPRounds >= 0 && value.MCPRounds <= 2 &&
		value.ToolCalls >= 0 && value.ToolCalls <= 6 && value.ToolParallelism >= 1 && value.ToolParallelism <= 4 &&
		value.ToolTimeoutMS >= 1 && value.ToolTimeoutMS <= 5000 && value.ToolResponseBytes >= 1 && value.ToolResponseBytes <= 65536 &&
		value.ToolTotalResponseBytes >= 1 && value.ToolTotalResponseBytes <= 262144 && value.LLMTimeoutMS >= 1 && value.LLMTimeoutMS <= 6000 &&
		value.ResultBudgetMS >= 1 && value.ResultBudgetMS <= 8000 && value.GraphDeadlineMS >= 1 && value.GraphDeadlineMS <= 30000 &&
		value.ArtifactBytes >= 1024 && value.ArtifactBytes <= 65536 && value.OutboundDelegations >= 0 && value.OutboundDelegations <= 2 &&
		value.DelegationDepth >= 0 && value.DelegationDepth <= 1 && value.PollsPerTask >= 0 && value.PollsPerTask <= 3 &&
		value.A2AResponseBytes >= 1024 && value.A2AResponseBytes <= 131072
}

func identitySet(values []string) map[string]bool {
	out := make(map[string]bool, len(values))
	for _, value := range values {
		out[value] = true
	}
	return out
}

func validateIdentitySubset(values []string, allowed map[string]bool, label string) error {
	seen := map[string]bool{}
	for _, value := range values {
		if !validIdentity(value) || seen[value] || !allowed[value] {
			return fmt.Errorf("a2a: %s identity outside frozen input or duplicated", label)
		}
		seen[value] = true
	}
	return nil
}

func validateGroundedClaims(values []GroundedClaim, allowed map[string]bool, label string) error {
	for _, claim := range values {
		if claim.Claim == "" || len(claim.Claim) > 4096 || unsafeAnalysisText(claim.Claim) ||
			len(claim.EvidenceRefs) == 0 || len(claim.EvidenceRefs) > 32 {
			return fmt.Errorf("a2a: %s claim is ungrounded, unsafe, or oversized", label)
		}
		if err := validateIdentitySubset(claim.EvidenceRefs, allowed, label+" evidence"); err != nil {
			return err
		}
	}
	return nil
}

func validateTextValues(values []string, maximum int) error {
	for _, value := range values {
		if value == "" || len(value) > maximum || unsafeAnalysisText(value) {
			return errors.New("a2a: artifact text field malformed, unsafe, or oversized")
		}
	}
	return nil
}

func unsafeAnalysisText(value string) bool {
	lower := strings.ToLower(value)
	for _, token := range []string{"p4runtime", "tableentry", "effect_intent", "effect-intent", "approval_token", "approval-token",
		"javascript:", "<script", "<iframe", "<object", "http://", "https://"} {
		if strings.Contains(lower, token) {
			return true
		}
	}
	return false
}

func causalAnalysisText(value string) bool {
	lower := strings.ToLower(value)
	return strings.Contains(lower, "root cause") || strings.Contains(lower, " caused ") || strings.HasPrefix(lower, "cause ") ||
		strings.Contains(value, "导致") || strings.Contains(value, "根因") || strings.Contains(value, "引发")
}

func digest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func validDigest(value string) bool {
	return security.ValidDigest(value)
}

func validRevision(value string) bool {
	return security.ValidRevision(value)
}

func validIdentity(value string) bool {
	if value == "" || len(value) > 128 {
		return false
	}
	for i, r := range value {
		ok := (r >= 'A' && r <= 'Z') || (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') ||
			r == '.' || r == '_' || r == ':' || r == '-'
		if !ok || (i == 0 && !((r >= 'A' && r <= 'Z') || (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9'))) {
			return false
		}
	}
	return true
}

func actorRef(actor security.Actor) (string, error) {
	if err := actor.Validate(); err != nil {
		return "", err
	}
	return actor.String(), nil
}
