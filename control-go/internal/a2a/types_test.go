package a2a

import (
	"strings"
	"testing"
	"time"
)

func TestInputAndArtifactDigestFences(t *testing.T) {
	d := "sha256:" + strings.Repeat("a", 64)
	revision := strings.Repeat("1", 40)
	now := time.Now()
	expires := now.Add(time.Hour).UnixMilli()
	input := InputBundle{SchemaVersion: InputSchema, TaskID: "task-1", RunID: "run-1", Skill: "analyze_nids_incident",
		PluginID: "masi.analysis.langgraph", PluginRevision: revision, ConfigDigest: d, BindingGeneration: 1, Scope: "scope-1", TargetSetDigest: d,
		EvidenceRefs: []FactRef{{ID: "evidence-1", Digest: d}}, ModelResultEvidenceRefs: []string{"evidence-1"}, Quality: "valid",
		ProviderProfileDigest: d, PromptProfileDigest: d, ToolPolicyDigest: d, RedactionProfileDigest: d, ProvenanceDigest: d,
		Budgets: AnalysisBudgets{LLMCalls: 2, MCPRounds: 2, ToolCalls: 6, ToolParallelism: 3, ToolTimeoutMS: 2000,
			ToolResponseBytes: 32768, ToolTotalResponseBytes: 131072, LLMTimeoutMS: 6000, ResultBudgetMS: 8000,
			GraphDeadlineMS: 30000, ArtifactBytes: 65536, OutboundDelegations: 2, PollsPerTask: 3, A2AResponseBytes: 131072},
		DeadlineUnixMS: now.Add(10 * time.Second).UnixMilli(), ExpiresAtUnixMS: expires,
		Locale: "zh-CN", ContentRequest: "summary", IdempotencyKey: "task-idem-1", DelegationPath: []string{}, TraceID: "trace-1"}
	input.InputDigest = ComputeInputDigest(input)
	if err := ValidateInput(&input, now); err != nil {
		t.Fatal(err)
	}
	if input.EventRefs == nil || input.IncidentRefs == nil || input.RuntimeRefs == nil ||
		input.ModelExplanationEvidenceRefs == nil || input.DelegationPath == nil {
		t.Fatal("canonical input collections must serialize as arrays, never null")
	}
	artifact := Artifact{SchemaVersion: ArtifactSchema, ArtifactID: "artifact-1", TaskID: input.TaskID, RunID: input.RunID,
		PluginID: input.PluginID, PluginRevision: input.PluginRevision, ConfigDigest: input.ConfigDigest,
		BindingGeneration: 1, InputDigest: input.InputDigest, AnalysisOutcome: "succeeded", Quality: "valid",
		SourceFactRefs: []string{"evidence-1"}, ObservedClaims: []GroundedClaim{{Claim: "observed", EvidenceRefs: []string{"evidence-1"}}},
		ModelResultFacts: []GroundedClaim{{Claim: "model result fact", EvidenceRefs: []string{"evidence-1"}}},
		InferredClaims:   []string{"inference"}, LLMInterpretations: []string{"interpretation"},
		Uncertainties: []string{"uncertainty"}, Limitations: []string{"limited context"},
		MissingEvidence: []string{"none"}, Recommendations: []Recommendation{{Text: "review", Risk: "low",
			Preconditions: []string{"human review"}, ExpiresAtUnixMS: expires, EvidenceRefs: []string{"evidence-1"}}},
		GeneratedContent:     []GeneratedContent{{MediaType: "text/markdown", Body: "# Analysis"}},
		ProviderMetadata:     ProviderMetadata{ProviderID: "provider-1", ModelID: "model-1", ProviderDigest: d},
		ToolTrajectoryDigest: d, GraphVersion: "masi-analysis-graph/v1", TopologyDigest: d, TraceID: input.TraceID,
		TraceDigest: d, ProducedAtUnixMS: now.UnixMilli(), ExpiresAtUnixMS: expires, MediaType: "application/json",
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

func TestSafePeerErrorCode(t *testing.T) {
	for _, value := range []string{"MALFORMED_REQUEST", "VERSION_NOT_SUPPORTED", "A2A_400"} {
		if !safePeerErrorCode(value) {
			t.Fatalf("safe peer error code rejected: %s", value)
		}
	}
	for _, value := range []string{"", "lowercase", "1STARTS_WITH_DIGIT", "BAD-CODE", "BAD\nCODE", strings.Repeat("A", 65)} {
		if safePeerErrorCode(value) {
			t.Fatalf("unsafe peer error code accepted: %q", value)
		}
	}
}
