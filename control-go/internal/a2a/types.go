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

// InputBundle contains references to frozen canonical facts. It contains no DB
// credential, mutable object, prompt, arbitrary URL, or packet payload.
type InputBundle struct {
	SchemaVersion     string    `json:"schema_version"`
	TaskID            string    `json:"task_id"`
	Skill             string    `json:"skill"`
	PluginID          string    `json:"plugin_id"`
	BindingGeneration int       `json:"binding_generation"`
	Scope             string    `json:"scope"`
	TargetSetDigest   string    `json:"target_set_digest"`
	EventRefs         []FactRef `json:"event_refs"`
	IncidentRefs      []FactRef `json:"incident_refs"`
	EvidenceRefs      []FactRef `json:"evidence_refs"`
	RuntimeRefs       []FactRef `json:"runtime_refs"`
	InputDigest       string    `json:"input_digest"`
	DeadlineUnixMS    int64     `json:"deadline_unix_ms"`
	Locale            string    `json:"locale"`
	ContentRequest    string    `json:"content_request"`
	IdempotencyKey    string    `json:"idempotency_key"`
	TraceID           string    `json:"trace_id"`
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

// Artifact is the exact non-executable body accepted from an Analysis peer.
type Artifact struct {
	SchemaVersion        string             `json:"schema_version"`
	ArtifactID           string             `json:"artifact_id"`
	ArtifactDigest       string             `json:"artifact_digest"`
	TaskID               string             `json:"task_id"`
	PluginID             string             `json:"plugin_id"`
	BindingGeneration    int                `json:"binding_generation"`
	InputDigest          string             `json:"input_digest"`
	AnalysisOutcome      string             `json:"analysis_outcome"`
	ObservedClaims       []GroundedClaim    `json:"observed_claims"`
	InferredClaims       []string           `json:"inferred_claims"`
	Uncertainties        []string           `json:"uncertainties"`
	Limitations          []string           `json:"limitations"`
	MissingEvidence      []string           `json:"missing_evidence"`
	Recommendations      []Recommendation   `json:"recommendations"`
	GeneratedContent     []GeneratedContent `json:"generated_content"`
	ProviderMetadata     ProviderMetadata   `json:"provider_metadata"`
	ToolTrajectoryDigest string             `json:"tool_trajectory_digest"`
	TraceID              string             `json:"trace_id"`
	MediaType            string             `json:"media_type"`
	NonExecutable        bool               `json:"non_executable"`
	DeploymentEligible   bool               `json:"deployment_eligible"`
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
	if input.SchemaVersion == "" {
		input.SchemaVersion = InputSchema
	}
	if input.SchemaVersion != InputSchema || !validIdentity(input.TaskID) || !allowedSkills[input.Skill] ||
		!validIdentity(input.PluginID) || input.BindingGeneration < 1 || input.Scope == "" || len(input.Scope) > 256 ||
		!validDigest(input.TargetSetDigest) || input.DeadlineUnixMS <= now.UnixMilli() ||
		input.DeadlineUnixMS > now.Add(30*time.Second).UnixMilli() || input.Locale == "" || len(input.Locale) > 32 ||
		len(input.ContentRequest) > 2048 || !validIdentity(input.IdempotencyKey) || !validIdentity(input.TraceID) {
		return errors.New("a2a: input identity/scope/deadline/bounds malformed")
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
	expected := ComputeInputDigest(*input)
	if input.InputDigest == "" {
		input.InputDigest = expected
	} else if input.InputDigest != expected {
		return errors.New("a2a: caller input digest does not bind frozen bundle")
	}
	return nil
}

func ComputeInputDigest(input InputBundle) string {
	input.InputDigest = ""
	sortFactRefs(input.EventRefs)
	sortFactRefs(input.IncidentRefs)
	sortFactRefs(input.EvidenceRefs)
	sortFactRefs(input.RuntimeRefs)
	raw, _ := json.Marshal(input)
	return digest(raw)
}

func ValidateArtifact(artifact *Artifact, input InputBundle) error {
	if artifact == nil || artifact.SchemaVersion != ArtifactSchema || !validIdentity(artifact.ArtifactID) ||
		artifact.TaskID != input.TaskID || artifact.PluginID != input.PluginID ||
		artifact.BindingGeneration != input.BindingGeneration || artifact.InputDigest != input.InputDigest ||
		!validDigest(artifact.ToolTrajectoryDigest) || !validIdentity(artifact.TraceID) ||
		!artifact.NonExecutable || artifact.DeploymentEligible ||
		(artifact.MediaType != "application/json" && artifact.MediaType != "text/markdown") {
		return errors.New("a2a: artifact identity/non-executable fence invalid")
	}
	switch artifact.AnalysisOutcome {
	case "succeeded", "limited", "insufficient_evidence", "failed":
	default:
		return errors.New("a2a: artifact outcome unsupported")
	}
	if len(artifact.ObservedClaims) > 128 || len(artifact.InferredClaims) > 128 || len(artifact.Uncertainties) > 128 ||
		len(artifact.Limitations) > 128 || len(artifact.MissingEvidence) > 128 || len(artifact.Recommendations) > 64 ||
		len(artifact.GeneratedContent) > 16 {
		return errors.New("a2a: artifact collection bound exceeded")
	}
	trustedEvidence := map[string]bool{}
	for _, ref := range input.EvidenceRefs {
		trustedEvidence[ref.ID] = true
	}
	for _, claim := range artifact.ObservedClaims {
		if claim.Claim == "" || len(claim.Claim) > 4096 || len(claim.EvidenceRefs) == 0 || len(claim.EvidenceRefs) > 32 {
			return errors.New("a2a: observed claim is ungrounded or oversized")
		}
		for _, ref := range claim.EvidenceRefs {
			if !trustedEvidence[ref] {
				return errors.New("a2a: observed claim references evidence outside frozen bundle")
			}
		}
	}
	for _, values := range [][]string{artifact.InferredClaims, artifact.Uncertainties, artifact.Limitations, artifact.MissingEvidence} {
		for _, value := range values {
			if value == "" || len(value) > 4096 {
				return errors.New("a2a: artifact text field malformed or oversized")
			}
		}
	}
	for _, rec := range artifact.Recommendations {
		if rec.Text == "" || len(rec.Text) > 4096 || len(rec.Preconditions) > 32 || len(rec.EvidenceRefs) > 32 ||
			rec.ExpiresAtUnixMS < 1 {
			return errors.New("a2a: recommendation malformed")
		}
		for _, ref := range rec.EvidenceRefs {
			if !trustedEvidence[ref] {
				return errors.New("a2a: recommendation evidence outside frozen bundle")
			}
		}
	}
	for _, content := range artifact.GeneratedContent {
		if (content.MediaType != "application/json" && content.MediaType != "text/markdown") || len(content.Body) > 32*1024 {
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
	if len(raw) > 64*1024 {
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

func digest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func validDigest(value string) bool {
	if len(value) != 71 || !strings.HasPrefix(value, "sha256:") {
		return false
	}
	_, err := hex.DecodeString(strings.TrimPrefix(value, "sha256:"))
	return err == nil
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
