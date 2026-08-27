package a2a

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func readAnalysisGolden(t *testing.T, name string, target any) []byte {
	t.Helper()
	path := filepath.Join("..", "..", "..", "contracts", "analysis", "v1", "golden", name)
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	trimmed := bytes.TrimSuffix(raw, []byte("\n"))
	if target != nil {
		decoder := json.NewDecoder(bytes.NewReader(trimmed))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(target); err != nil {
			t.Fatal(err)
		}
	}
	return trimmed
}

func TestSharedAnalysisInputArtifactAndA2AGolden(t *testing.T) {
	var input InputBundle
	readAnalysisGolden(t, "input-v1.json", &input)
	now := time.UnixMilli(input.DeadlineUnixMS - 10_000)
	if err := ValidateInput(&input, now); err != nil {
		t.Fatalf("input golden rejected: %v", err)
	}
	if got := ComputeInputDigest(input); got != input.InputDigest {
		t.Fatalf("input digest got %s want %s", got, input.InputDigest)
	}
	var artifact Artifact
	readAnalysisGolden(t, "artifact-v1.json", &artifact)
	if got := ComputeArtifactDigest(artifact); got != artifact.ArtifactDigest {
		t.Fatalf("artifact digest got %s want %s", got, artifact.ArtifactDigest)
	}
	if err := ValidateArtifact(&artifact, input); err != nil {
		t.Fatalf("artifact golden rejected: %v", err)
	}
	_, raw, _, err := buildSendRequest(input)
	if err != nil {
		t.Fatal(err)
	}
	want := readAnalysisGolden(t, "a2a-send-request-v1.json", nil)
	if !bytes.Equal(raw, want) {
		t.Fatalf("A2A send bytes drifted\ngot:  %s\nwant: %s", raw, want)
	}
}

func TestAnalysisGoldenSafetyNegatives(t *testing.T) {
	var input InputBundle
	readAnalysisGolden(t, "input-v1.json", &input)
	var artifact Artifact
	readAnalysisGolden(t, "artifact-v1.json", &artifact)
	artifact.GeneratedContent[0].Body = "execute P4Runtime now"
	artifact.ArtifactDigest = ComputeArtifactDigest(artifact)
	if err := ValidateArtifact(&artifact, input); err == nil {
		t.Fatal("executable P4 text must be rejected")
	}
	readAnalysisGolden(t, "artifact-v1.json", &artifact)
	artifact.ModelExplanationFacts[0].Claim = "This feature caused the attack."
	artifact.ArtifactDigest = ComputeArtifactDigest(artifact)
	if err := ValidateArtifact(&artifact, input); err == nil {
		t.Fatal("causal explanation must be rejected")
	}
}
