package a2a

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"masi-nids/control-go/internal/config"
	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/plugin"
	"masi-nids/control-go/internal/security"
)

func TestA2ASubmitPollArtifactPostgres(t *testing.T) {
	configPath := os.Getenv("MASI_CONTROL_E2E_CONFIG")
	dsn := os.Getenv("MASI_CONTROL_E2E_DSN")
	if configPath == "" || dsn == "" {
		if os.Getenv("MASI_CONTROL_E2E_REQUIRED") == "1" {
			t.Fatal("A2A PostgreSQL E2E required but DSN/CONFIG is missing")
		}
		t.Skip("set MASI_CONTROL_E2E_DSN/CONFIG for A2A PostgreSQL E2E")
	}
	cfg, err := config.Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.PostgreSQLDSN != dsn {
		t.Fatal("A2A PostgreSQL E2E DSN differs from validated config")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool, err := db.New(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()

	const (
		pluginID   = "masi.analysis.a2a-e2e"
		manifestID = "manifest-analysis-a2a-e2e"
		taskID     = "analysis-task-a2a-e2e"
		artifactID = "analysis-artifact-a2a-e2e"
		evidenceID = "analysis-input-evidence-a2a-e2e"
		scope      = "scope-analysis-a2a-e2e"
	)
	cleanup := func() {
		cleanCtx, cleanCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanCancel()
		for _, statement := range []string{
			`DELETE FROM evidence_refs WHERE evidence_id IN ($1,$2)`,
			`DELETE FROM analysis_artifacts WHERE task_id=$1`,
			`DELETE FROM analysis_task_requests WHERE task_id=$1`,
			`DELETE FROM plugin_audit_events WHERE plugin_id=$1`,
			`DELETE FROM plugin_bindings WHERE plugin_id=$1`,
			`DELETE FROM plugin_qualifications WHERE plugin_id=$1`,
			`DELETE FROM plugin_manifests WHERE plugin_id=$1`,
		} {
			switch {
			case strings.Contains(statement, "evidence_refs"):
				_, _ = pool.Exec(cleanCtx, statement, evidenceID, artifactID)
			case strings.Contains(statement, "analysis_"):
				_, _ = pool.Exec(cleanCtx, statement, taskID)
			default:
				_, _ = pool.Exec(cleanCtx, statement, pluginID)
			}
		}
	}
	cleanup()
	defer cleanup()

	d := "sha256:" + strings.Repeat("a", 64)
	if _, err := pool.Exec(ctx, `INSERT INTO evidence_refs(evidence_id,kind,reference_digest,source,trace_id,scope)
		VALUES($1,'observation',$2,'a2a-e2e','trace-a2a-evidence',$3)`, evidenceID, d, scope); err != nil {
		t.Fatal(err)
	}
	actor := security.Actor{Issuer: "https://issuer.example", Subject: "analysis-operator-a2a-e2e"}
	manifest := plugin.Manifest{ManifestID: manifestID, ManifestRevision: 1, PluginID: pluginID,
		Kind: plugin.KindAnalysisAgent, Publisher: "masi", Version: "1.0.0",
		Capabilities: []plugin.Capability{
			{CapabilityID: "analysis-a2a/v1", CapabilityKind: "a2a-agent", Declared: true},
			{CapabilityID: "masi.evidence.get", CapabilityKind: "mcp-tool", Declared: true},
		},
		ResourceLimits: plugin.ResourceLimits{CPUMilli: 100, MemoryBytes: 64 << 20, PIDCount: 8, FDCount: 32,
			DiskBytes: 1 << 20, DeadlineMS: 30_000, OutputBytes: 64 << 10, QueueDepth: 4},
		RuntimeProfile: plugin.RuntimeGRPCService, ServiceProtoDigest: d, SBOMDigest: d,
		ProvenanceDigest: d, SignatureStatus: "signed", Scope: scope}
	catalog := plugin.NewCatalogService(pool)
	registered, err := catalog.Register(ctx, manifest, actor, "trace-a2a-register")
	if err != nil {
		t.Fatal(err)
	}
	if err := catalog.Qualify(ctx, pluginID, manifestID, 1, plugin.Qualified, actor, "trace-a2a-qualify"); err != nil {
		t.Fatal(err)
	}
	var capabilities, resources []byte
	if err := pool.QueryRow(ctx, `SELECT capabilities,resource_limits FROM plugin_manifests WHERE plugin_id=$1`, pluginID).
		Scan(&capabilities, &resources); err != nil {
		t.Fatal(err)
	}
	if _, err := plugin.NewBindingService(pool).Activate(ctx, plugin.Binding{PluginID: pluginID, BindingGeneration: 1,
		ManifestID: manifestID, ManifestRevision: 1, ManifestDigest: registered.ManifestDigest,
		ConfigDigest: d, CapabilityDigest: digest(capabilities), ResourceProfileDigest: digest(resources),
		QualificationStatus: plugin.Qualified, Scope: scope}, actor, "trace-a2a-activate"); err != nil {
		t.Fatal(err)
	}

	var mu sync.Mutex
	var frozen InputBundle
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("A2A-Version") != ProtocolVersion {
			http.Error(w, "bad version", http.StatusBadRequest)
			return
		}
		w.Header().Set("A2A-Version", ProtocolVersion)
		w.Header().Set("Content-Type", "application/a2a+json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/message:send":
			var request a2aSendRequest
			if err := json.NewDecoder(r.Body).Decode(&request); err != nil || len(request.Message.Parts) != 1 {
				http.Error(w, "bad request", http.StatusBadRequest)
				return
			}
			mu.Lock()
			_ = json.Unmarshal(request.Message.Parts[0].Data, &frozen)
			mu.Unlock()
			_ = json.NewEncoder(w).Encode(a2aResponse{Task: &a2aTask{ID: "remote-analysis-task-a2a-e2e",
				ContextID: "remote-analysis-context-a2a-e2e", Status: a2aTaskStatus{State: "TASK_STATE_WORKING"}}})
		case r.Method == http.MethodGet && r.URL.Path == "/tasks/remote-analysis-task-a2a-e2e":
			mu.Lock()
			input := frozen
			mu.Unlock()
			artifact := Artifact{SchemaVersion: ArtifactSchema, ArtifactID: artifactID, TaskID: input.TaskID,
				PluginID: input.PluginID, BindingGeneration: input.BindingGeneration, InputDigest: input.InputDigest,
				AnalysisOutcome: "succeeded", ObservedClaims: []GroundedClaim{{Claim: "bounded observation", EvidenceRefs: []string{evidenceID}}},
				InferredClaims: []string{"bounded inference"}, Uncertainties: []string{"bounded uncertainty"},
				Limitations: []string{"bounded limitation"}, MissingEvidence: []string{"none"},
				Recommendations: []Recommendation{{Text: "human review", Risk: "low", Preconditions: []string{"review"},
					ExpiresAtUnixMS: time.Now().Add(time.Hour).UnixMilli(), EvidenceRefs: []string{evidenceID}}},
				GeneratedContent:     []GeneratedContent{{MediaType: "text/markdown", Body: "# Bounded analysis"}},
				ProviderMetadata:     ProviderMetadata{ProviderID: "fixture-provider", ModelID: "fixture-model", ProviderDigest: d},
				ToolTrajectoryDigest: d, TraceID: "trace-a2a-artifact", MediaType: "application/json",
				NonExecutable: true, DeploymentEligible: false}
			artifact.ArtifactDigest = ComputeArtifactDigest(artifact)
			artifactRaw, _ := json.Marshal(artifact)
			_ = json.NewEncoder(w).Encode(a2aResponse{Task: &a2aTask{ID: "remote-analysis-task-a2a-e2e",
				ContextID: "remote-analysis-context-a2a-e2e", Status: a2aTaskStatus{State: "TASK_STATE_COMPLETED"},
				Artifacts: []a2aArtifact{{ArtifactID: artifactID, Parts: []a2aPart{{Data: artifactRaw, MediaType: "application/json"}}}}}})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := NewClient(pool, []config.A2APeer{{PeerID: "analysis-peer-a2a-e2e", PluginID: pluginID,
		BaseURL: server.URL, AllowedIPs: []string{"127.0.0.1"}, MaxResponseBytes: 128 << 10}}, false)
	if err != nil {
		t.Fatal(err)
	}
	input := InputBundle{SchemaVersion: InputSchema, TaskID: taskID, Skill: "analyze_nids_incident",
		PluginID: pluginID, BindingGeneration: 1, Scope: scope, TargetSetDigest: d,
		EvidenceRefs: []FactRef{{ID: evidenceID}}, DeadlineUnixMS: time.Now().Add(20 * time.Second).UnixMilli(),
		Locale: "zh-CN", ContentRequest: "bounded summary", IdempotencyKey: "analysis-task-a2a-e2e",
		TraceID: "trace-analysis-task-a2a-e2e"}
	submitted, err := client.Submit(ctx, input, actor)
	if err != nil || submitted.Status != "working" || submitted.RemoteTaskID == "" {
		t.Fatalf("submit projection=%+v err=%v", submitted, err)
	}
	completed, err := client.Poll(ctx, taskID, actor)
	if err != nil || completed.Status != "succeeded" || completed.PollCount != 1 {
		t.Fatalf("poll projection=%+v err=%v", completed, err)
	}
	var count int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM analysis_artifacts WHERE task_id=$1 AND non_executable
		AND NOT deployment_eligible AND artifact_digest=$2`, taskID, func() string {
		var value string
		_ = pool.QueryRow(ctx, `SELECT artifact_digest FROM analysis_artifacts WHERE task_id=$1`, taskID).Scan(&value)
		return value
	}()).Scan(&count); err != nil || count != 1 {
		t.Fatalf("persisted artifact count=%d err=%v", count, err)
	}
}
