package real_analysis_pairwise

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"testing"
	"time"

	"masi-nids/control-go/internal/a2a"
	"masi-nids/control-go/internal/config"
	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/plugin"
	"masi-nids/control-go/internal/security"
)

const (
	scope           = "scope-e2e"
	targetSetDigest = "sha256:b040b50e2f8f96bfcee9bdd70ca819d01ace68e4af8c5a28015c738eb7ac607e"
	evidenceID      = "analysis-real-pairwise-evidence"
	taskID          = "analysis-real-pairwise-task"
)

type runtimeBinding struct {
	PluginID          string `json:"plugin_id"`
	PluginRevision    string `json:"plugin_revision"`
	ConfigDigest      string `json:"config_digest"`
	BindingGeneration int    `json:"binding_generation"`
}

func digest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func required(t *testing.T, name string) string {
	t.Helper()
	value := os.Getenv(name)
	if value == "" {
		if os.Getenv("MASI_REAL_ANALYSIS_REQUIRED") == "1" {
			t.Fatalf("missing required %s", name)
		}
		t.Skip("real Analysis pairwise environment not configured")
	}
	return value
}

func request(t *testing.T, method, url, origin, cookie, csrf string, body any) (int, []byte, []*http.Cookie) {
	t.Helper()
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequest(method, url, reader)
	if err != nil {
		t.Fatal(err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if origin != "" {
		req.Header.Set("Origin", origin)
		req.Header.Set("Sec-Fetch-Site", "same-origin")
	}
	if cookie != "" {
		req.Header.Set("Cookie", cookie)
	}
	if csrf != "" {
		req.Header.Set("X-CSRF-Token", csrf)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, raw, resp.Cookies()
}

func TestRealControlToRealAnalysisA2A(t *testing.T) {
	dsn := required(t, "MASI_CONTROL_E2E_DSN")
	configPath := required(t, "MASI_CONTROL_E2E_CONFIG")
	bindingPath := required(t, "MASI_REAL_ANALYSIS_BINDING")
	evidencePath := required(t, "MASI_REAL_ANALYSIS_EVIDENCE")
	externalBaseURL := strings.TrimRight(os.Getenv("MASI_REAL_CONTROL_BASE_URL"), "/")
	requestOrigin := strings.TrimRight(os.Getenv("MASI_REAL_CONTROL_ORIGIN"), "/")

	bindingRaw, err := os.ReadFile(bindingPath)
	if err != nil {
		t.Fatal(err)
	}
	var binding runtimeBinding
	if err := json.Unmarshal(bindingRaw, &binding); err != nil {
		t.Fatal(err)
	}
	cfg, err := config.Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.PostgreSQLDSN != dsn {
		t.Fatal("validated config DSN differs from pairwise DSN")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	pool, err := db.New(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()

	for _, deletion := range []struct {
		statement string
		value     string
	}{
		{`DELETE FROM evidence_refs WHERE evidence_id IN
			(SELECT artifact_id FROM analysis_artifacts WHERE task_id=$1)`, taskID},
		{`DELETE FROM evidence_refs WHERE evidence_id=$1`, evidenceID},
		{`DELETE FROM analysis_artifacts WHERE task_id=$1`, taskID},
		{`DELETE FROM analysis_task_requests WHERE task_id=$1`, taskID},
		{`DELETE FROM plugin_audit_events WHERE plugin_id=$1`, binding.PluginID},
		{`DELETE FROM plugin_bindings WHERE plugin_id=$1`, binding.PluginID},
		{`DELETE FROM plugin_qualifications WHERE plugin_id=$1`, binding.PluginID},
		{`DELETE FROM plugin_manifests WHERE plugin_id=$1`, binding.PluginID},
	} {
		if _, err := pool.Exec(ctx, deletion.statement, deletion.value); err != nil {
			t.Fatal(err)
		}
	}
	d := "sha256:" + strings.Repeat("a", 64)
	if _, err := pool.Exec(ctx, `INSERT INTO evidence_refs(evidence_id,kind,reference_digest,source,trace_id,scope)
		VALUES($1,'observation',$2,'real-analysis-pairwise','trace-real-analysis-evidence',$3)`, evidenceID, d, scope); err != nil {
		t.Fatal(err)
	}
	actor := security.Actor{Issuer: "https://idp.example", Subject: "pairwise-seed"}
	manifest := plugin.Manifest{ManifestID: "manifest-real-analysis-pairwise", ManifestRevision: 1,
		PluginID: binding.PluginID, Kind: plugin.KindAnalysisAgent, Publisher: "masi", Version: "1.0.0",
		Capabilities: []plugin.Capability{
			{CapabilityID: "analysis-a2a/v1", CapabilityKind: "a2a-agent", Declared: true},
			{CapabilityID: "masi.evidence.get", CapabilityKind: "mcp-tool", Declared: true},
		},
		ResourceLimits: plugin.ResourceLimits{CPUMilli: 100, MemoryBytes: 64 << 20, PIDCount: 8,
			FDCount: 32, DiskBytes: 1 << 20, DeadlineMS: 30_000, OutputBytes: 64 << 10, QueueDepth: 4},
		RuntimeProfile: plugin.RuntimeGRPCService, ServiceProtoDigest: d, SBOMDigest: d,
		ProvenanceDigest: d, SignatureStatus: "signed", Scope: scope}
	registered, err := plugin.NewCatalogService(pool).Register(ctx, manifest, actor, "trace-real-analysis-register")
	if err != nil {
		t.Fatal(err)
	}
	if err := plugin.NewCatalogService(pool).Qualify(ctx, binding.PluginID, manifest.ManifestID, 1, plugin.Qualified, actor, "trace-real-analysis-qualify"); err != nil {
		t.Fatal(err)
	}
	var capabilities, resources []byte
	if err := pool.QueryRow(ctx, `SELECT capabilities,resource_limits FROM plugin_manifests WHERE plugin_id=$1`, binding.PluginID).Scan(&capabilities, &resources); err != nil {
		t.Fatal(err)
	}
	if _, err := plugin.NewBindingService(pool).Activate(ctx, plugin.Binding{PluginID: binding.PluginID,
		BindingGeneration: binding.BindingGeneration, ManifestID: manifest.ManifestID, ManifestRevision: 1,
		ManifestDigest: registered.ManifestDigest, ConfigDigest: binding.ConfigDigest,
		CapabilityDigest: digest(capabilities), ResourceProfileDigest: digest(resources),
		QualificationStatus: plugin.Qualified, Scope: scope}, actor, "trace-real-analysis-activate"); err != nil {
		t.Fatal(err)
	}

	var process *exec.Cmd
	var processLog bytes.Buffer
	baseURL := externalBaseURL
	if externalBaseURL == "" {
		binary := required(t, "MASI_CONTROL_E2E_BINARY")
		process = exec.Command(binary, "--config", configPath)
		process.Stdout, process.Stderr = &processLog, &processLog
		if err := process.Start(); err != nil {
			t.Fatal(err)
		}
		defer func() {
			_ = process.Process.Signal(syscall.SIGTERM)
			_ = process.Wait()
			if t.Failed() {
				t.Logf("control-core log:\n%s", processLog.String())
			}
		}()
		baseURL = "http://" + cfg.HTTPListen
	} else if !strings.HasPrefix(externalBaseURL, "http://127.0.0.1:") &&
		!strings.HasPrefix(externalBaseURL, "http://localhost:") {
		t.Fatal("external Control base URL must be an explicit loopback HTTP acceptance endpoint")
	}
	if requestOrigin == "" {
		requestOrigin = baseURL
	}
	ready := false
	for range 100 {
		resp, err := http.Get(baseURL + "/readyz")
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				ready = true
				break
			}
		}
		time.Sleep(100 * time.Millisecond)
	}
	if !ready {
		t.Fatalf("control-core did not become ready: %s", processLog.String())
	}

	status, raw, cookies := request(t, http.MethodPost, baseURL+"/oidc/test-login", requestOrigin, "", "",
		map[string]string{"issuer": "https://idp.example", "subject": "operator-a"})
	if status != http.StatusOK || len(cookies) == 0 {
		t.Fatalf("login status=%d body=%s", status, raw)
	}
	cookie := cookies[0].Name + "=" + cookies[0].Value
	status, raw, _ = request(t, http.MethodGet, baseURL+"/api/session", "", cookie, "", nil)
	var session struct {
		CSRFToken string `json:"csrf_token"`
	}
	if status != http.StatusOK || json.Unmarshal(raw, &session) != nil || session.CSRFToken == "" {
		t.Fatalf("session status=%d body=%s", status, raw)
	}

	now := time.Now()
	input := a2a.InputBundle{SchemaVersion: a2a.InputSchema, TaskID: taskID, RunID: "run-" + taskID,
		Skill: "analyze_nids_incident", PluginID: binding.PluginID, PluginRevision: binding.PluginRevision,
		ConfigDigest: binding.ConfigDigest, BindingGeneration: binding.BindingGeneration, Scope: scope,
		TargetSetDigest: targetSetDigest, EvidenceRefs: []a2a.FactRef{{ID: evidenceID, Digest: d}},
		ModelResultEvidenceRefs: []string{evidenceID}, Quality: "valid", ProviderProfileDigest: d,
		PromptProfileDigest: d, ToolPolicyDigest: d, RedactionProfileDigest: d, ProvenanceDigest: d,
		Budgets: a2a.AnalysisBudgets{LLMCalls: 2, MCPRounds: 2, ToolCalls: 6, ToolParallelism: 3,
			ToolTimeoutMS: 2000, ToolResponseBytes: 32768, ToolTotalResponseBytes: 131072,
			LLMTimeoutMS: 6000, ResultBudgetMS: 8000, GraphDeadlineMS: 30000, ArtifactBytes: 65536,
			OutboundDelegations: 0, DelegationDepth: 0, PollsPerTask: 3, A2AResponseBytes: 131072},
		DeadlineUnixMS: now.Add(20 * time.Second).UnixMilli(), ExpiresAtUnixMS: now.Add(time.Hour).UnixMilli(),
		Locale: "en-US", ContentRequest: "needs-tool", IdempotencyKey: taskID,
		DelegationPath: []string{}, TraceID: "trace-real-analysis-pairwise"}
	status, raw, _ = request(t, http.MethodPost, baseURL+"/api/analysis/tasks", requestOrigin, cookie, session.CSRFToken,
		map[string]any{"input": input, "idempotency_key": taskID})
	if status != http.StatusAccepted {
		t.Fatalf("submit status=%d body=%s", status, raw)
	}

	var projection a2a.TaskProjection
	if err := json.Unmarshal(raw, &projection); err != nil {
		t.Fatal(err)
	}
	if projection.Status == "unknown" {
		if externalBaseURL != "" {
			t.Fatalf("external Control Core submit was ambiguous: projection=%+v", projection)
		}
		diagnostic := input
		diagnostic.TaskID = "analysis-real-pairwise-diagnostic"
		diagnostic.RunID = "run-analysis-real-pairwise-diagnostic"
		diagnostic.IdempotencyKey = diagnostic.TaskID
		diagnostic.TraceID = "trace-real-analysis-diagnostic"
		diagnostic.InputDigest = ""
		diagnostic.DeadlineUnixMS = time.Now().Add(20 * time.Second).UnixMilli()
		diagnostic.ExpiresAtUnixMS = time.Now().Add(time.Hour).UnixMilli()
		directClient, clientErr := a2a.NewClient(pool, cfg.Outbound.Analysis, cfg.RuntimeProfile)
		var directProjection *a2a.TaskProjection
		var directErr error
		if clientErr == nil {
			directProjection, directErr = directClient.Submit(ctx, diagnostic, security.Actor{Issuer: "https://idp.example", Subject: "operator-a"})
		}
		t.Fatalf("Control Core submit was ambiguous: projection=%+v client_error=%v direct_projection=%+v direct_error=%v",
			projection, clientErr, directProjection, directErr)
	}
	for attempt := 0; attempt < 3 && projection.Status != "succeeded" && projection.Status != "limited"; attempt++ {
		time.Sleep(time.Duration(1<<attempt) * 100 * time.Millisecond)
		status, raw, _ = request(t, http.MethodPost, baseURL+"/api/analysis/tasks/"+taskID+"/poll", requestOrigin,
			cookie, session.CSRFToken, map[string]any{"scope": scope, "target_set_digest": targetSetDigest,
				"idempotency_key": fmt.Sprintf("poll-real-analysis-%d", attempt)})
		if status != http.StatusOK || json.Unmarshal(raw, &projection) != nil {
			var storedStatus, reasonCode string
			var pollCount int
			_ = pool.QueryRow(ctx, `SELECT status,reason_code,poll_count FROM analysis_task_requests WHERE task_id=$1`, taskID).
				Scan(&storedStatus, &reasonCode, &pollCount)
			if externalBaseURL != "" {
				t.Fatalf("external Control poll status=%d body=%s stored=%s reason=%s count=%d",
					status, raw, storedStatus, reasonCode, pollCount)
			}
			peerURL := cfg.Outbound.Analysis[0].BaseURL + "/tasks/" + projection.RemoteTaskID
			peerReq, _ := http.NewRequest(http.MethodGet, peerURL, nil)
			peerReq.Header.Set("A2A-Version", a2a.ProtocolVersion)
			peerReq.Header.Set("Accept", "application/a2a+json")
			peerResp, peerErr := http.DefaultClient.Do(peerReq)
			peerRaw := []byte{}
			if peerErr == nil {
				peerRaw, _ = io.ReadAll(peerResp.Body)
				_ = peerResp.Body.Close()
			}
			directClient, clientErr := a2a.NewClient(pool, cfg.Outbound.Analysis, cfg.RuntimeProfile)
			var directProjection *a2a.TaskProjection
			var directErr error
			if clientErr == nil {
				directProjection, directErr = directClient.Poll(ctx, taskID,
					security.Actor{Issuer: "https://idp.example", Subject: "operator-a"})
			}
			t.Fatalf("poll status=%d body=%s stored=%s reason=%s count=%d peer_error=%v client_error=%v direct_projection=%+v direct_error=%v peer=%s",
				status, raw, storedStatus, reasonCode, pollCount, peerErr, clientErr, directProjection, directErr, peerRaw)
		}
	}
	if projection.Status != "succeeded" && projection.Status != "limited" {
		t.Fatalf("analysis task did not reach an accepted terminal outcome: %+v", projection)
	}
	var artifactCount int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM analysis_artifacts WHERE task_id=$1 AND plugin_id=$2
		AND non_executable AND NOT deployment_eligible`, taskID, binding.PluginID).Scan(&artifactCount); err != nil || artifactCount != 1 {
		t.Fatalf("artifact count=%d err=%v", artifactCount, err)
	}
	var artifactID, artifactDigest, analysisOutcome string
	var nonExecutable, deploymentEligible bool
	if err := pool.QueryRow(ctx, `SELECT artifact_id,artifact_digest,analysis_outcome,non_executable,deployment_eligible
		FROM analysis_artifacts WHERE task_id=$1 AND plugin_id=$2`, taskID, binding.PluginID).
		Scan(&artifactID, &artifactDigest, &analysisOutcome, &nonExecutable, &deploymentEligible); err != nil ||
		!nonExecutable || deploymentEligible {
		t.Fatalf("artifact identity/semantics id=%s outcome=%s non_executable=%v deployment_eligible=%v err=%v",
			artifactID, analysisOutcome, nonExecutable, deploymentEligible, err)
	}
	evidence := map[string]any{"schema_version": "go-analysis-real-process-evidence/v1", "task_id": taskID,
		"plugin_id": binding.PluginID, "binding_generation": binding.BindingGeneration,
		"submit_status": http.StatusAccepted, "final_status": projection.Status, "poll_count": projection.PollCount,
		"artifact_count": artifactCount, "artifact_id": artifactID, "artifact_digest": artifactDigest,
		"analysis_outcome": analysisOutcome, "non_executable": nonExecutable,
		"deployment_eligible": deploymentEligible, "external_control": externalBaseURL != "", "result": "PASS"}
	encoded, _ := json.MarshalIndent(evidence, "", "  ")
	if err := os.WriteFile(evidencePath, append(encoded, '\n'), 0o600); err != nil {
		t.Fatal(err)
	}
}
