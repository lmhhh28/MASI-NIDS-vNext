package a2a

import (
	"strings"
	"testing"
	"time"
)

func TestInputAndArtifactDigestFences(t *testing.T) {
	d := "sha256:" + strings.Repeat("a", 64)
	now := time.Now()
	input := InputBundle{SchemaVersion: InputSchema, TaskID: "task-1", Skill: "analyze_nids_incident",
		PluginID: "masi.analysis.langgraph", BindingGeneration: 1, Scope: "scope-1", TargetSetDigest: d,
		EvidenceRefs: []FactRef{{ID: "evidence-1", Digest: d}}, DeadlineUnixMS: now.Add(10 * time.Second).UnixMilli(),
		Locale: "zh-CN", ContentRequest: "summary", IdempotencyKey: "task-idem-1", TraceID: "trace-1"}
	input.InputDigest = ComputeInputDigest(input)
	if err := ValidateInput(&input, now); err != nil {
		t.Fatal(err)
	}
	artifact := Artifact{SchemaVersion: ArtifactSchema, ArtifactID: "artifact-1", TaskID: input.TaskID,
		PluginID: input.PluginID, BindingGeneration: 1, InputDigest: input.InputDigest,
		AnalysisOutcome: "succeeded", ObservedClaims: []GroundedClaim{{Claim: "observed", EvidenceRefs: []string{"evidence-1"}}},
		InferredClaims: []string{"inference"}, Uncertainties: []string{"uncertainty"}, Limitations: []string{"limited context"},
		MissingEvidence: []string{"none"}, Recommendations: []Recommendation{{Text: "review", Risk: "low",
			Preconditions: []string{"human review"}, ExpiresAtUnixMS: now.Add(time.Hour).UnixMilli(), EvidenceRefs: []string{"evidence-1"}}},
		GeneratedContent:     []GeneratedContent{{MediaType: "text/markdown", Body: "# Analysis"}},
		ProviderMetadata:     ProviderMetadata{ProviderID: "provider-1", ModelID: "model-1", ProviderDigest: d},
		ToolTrajectoryDigest: d, TraceID: "trace-artifact-1", MediaType: "application/json",
		NonExecutable: true, DeploymentEligible: false}
	artifact.ArtifactDigest = ComputeArtifactDigest(artifact)
	if err := ValidateArtifact(&artifact, input); err != nil {
		t.Fatal(err)
	}
	artifact.ObservedClaims[0].EvidenceRefs[0] = "outside-bundle"
	artifact.ArtifactDigest = ComputeArtifactDigest(artifact)
	if err := ValidateArtifact(&artifact, input); err == nil {
		t.Fatal("ungrounded observed claim must be rejected")
	}
}
