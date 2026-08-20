package mcp

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"masi-nids/control-go/internal/config"
	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/plugin"
	"masi-nids/control-go/internal/security"
)

func TestMCPReadonlySessionAllowlistAndAuditPostgres(t *testing.T) {
	configPath := os.Getenv("MASI_CONTROL_E2E_CONFIG")
	dsn := os.Getenv("MASI_CONTROL_E2E_DSN")
	if configPath == "" || dsn == "" {
		if os.Getenv("MASI_CONTROL_E2E_REQUIRED") == "1" {
			t.Fatal("MCP PostgreSQL E2E required but DSN/CONFIG is missing")
		}
		t.Skip("set MASI_CONTROL_E2E_DSN/CONFIG for MCP PostgreSQL E2E")
	}
	cfg, err := config.Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.PostgreSQLDSN != dsn {
		t.Fatal("MCP PostgreSQL E2E DSN differs from validated config")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool, err := db.New(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()

	const (
		pluginID   = "masi.analysis.mcp-e2e"
		manifestID = "manifest-analysis-mcp-e2e"
		evidenceID = "evidence-mcp-e2e"
		scope      = "scope-analysis-mcp-e2e"
	)
	cleanup := func() {
		cleanCtx, cleanCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanCancel()
		for _, statement := range []string{
			`DELETE FROM mcp_access_audit WHERE plugin_id=$1`,
			`DELETE FROM plugin_audit_events WHERE plugin_id=$1`,
			`DELETE FROM plugin_bindings WHERE plugin_id=$1`,
			`DELETE FROM plugin_qualifications WHERE plugin_id=$1`,
			`DELETE FROM plugin_manifests WHERE plugin_id=$1`,
		} {
			_, _ = pool.Exec(cleanCtx, statement, pluginID)
		}
		_, _ = pool.Exec(cleanCtx, `DELETE FROM evidence_refs WHERE evidence_id=$1`, evidenceID)
	}
	cleanup()
	defer cleanup()

	d := "sha256:" + strings.Repeat("a", 64)
	if _, err := pool.Exec(ctx, `INSERT INTO evidence_refs(evidence_id,kind,reference_digest,source,trace_id,scope)
		VALUES($1,'observation',$2,'mcp-e2e','trace-mcp-evidence',$3)`, evidenceID, d, scope); err != nil {
		t.Fatal(err)
	}
	actor := security.Actor{Issuer: "https://issuer.example", Subject: "mcp-admin-e2e"}
	manifest := plugin.Manifest{ManifestID: manifestID, ManifestRevision: 1, PluginID: pluginID,
		Kind: plugin.KindAnalysisAgent, Publisher: "masi", Version: "1.0.0",
		Capabilities: []plugin.Capability{{CapabilityID: "masi.evidence.get", CapabilityKind: "mcp-tool", Declared: true}},
		ResourceLimits: plugin.ResourceLimits{CPUMilli: 100, MemoryBytes: 64 << 20, PIDCount: 8, FDCount: 32,
			DiskBytes: 1 << 20, DeadlineMS: 30_000, OutputBytes: 64 << 10, QueueDepth: 4},
		RuntimeProfile: plugin.RuntimeGRPCService, ServiceProtoDigest: d, SBOMDigest: d,
		ProvenanceDigest: d, SignatureStatus: "signed", Scope: scope}
	catalog := plugin.NewCatalogService(pool)
	registered, err := catalog.Register(ctx, manifest, actor, "trace-mcp-register")
	if err != nil {
		t.Fatal(err)
	}
	if err := catalog.Qualify(ctx, pluginID, manifestID, 1, plugin.Qualified, actor, "trace-mcp-qualify"); err != nil {
		t.Fatal(err)
	}
	var capabilities, resources []byte
	if err := pool.QueryRow(ctx, `SELECT capabilities,resource_limits FROM plugin_manifests WHERE plugin_id=$1`, pluginID).
		Scan(&capabilities, &resources); err != nil {
		t.Fatal(err)
	}
	if _, err := plugin.NewBindingService(pool).Activate(ctx, plugin.Binding{PluginID: pluginID, BindingGeneration: 1,
		ManifestID: manifestID, ManifestRevision: 1, ManifestDigest: registered.ManifestDigest,
		ConfigDigest: d, CapabilityDigest: testDigest(capabilities), ResourceProfileDigest: testDigest(resources),
		QualificationStatus: plugin.Qualified, Scope: scope}, actor, "trace-mcp-activate"); err != nil {
		t.Fatal(err)
	}

	server := &Server{Pool: pool, AllowedOrigin: cfg.PublicOrigin, AllowTestLoopback: true}
	call := func(body, sessionID, version string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodPost, "/mcp", strings.NewReader(body))
		req.RemoteAddr = "127.0.0.1:54321"
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Origin", cfg.PublicOrigin)
		req.Header.Set("MCP-Protocol-Version", version)
		req.Header.Set("X-MASI-Plugin-ID", pluginID)
		req.Header.Set("X-MASI-Test-Plugin-ID", pluginID)
		req.Header.Set("X-MASI-Binding-Generation", "1")
		req.Header.Set("X-MASI-Trace-ID", "trace-mcp-call")
		req.Header.Set("X-MASI-MCP-Round", "1")
		if sessionID != "" {
			req.Header.Set("MCP-Session-Id", sessionID)
		}
		response := httptest.NewRecorder()
		server.Handler(response, req)
		return response
	}

	initialized := call(`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"analysis-e2e","version":"1.0.0"}}}`, "", ProtocolVersion)
	if initialized.Code != http.StatusOK || initialized.Header().Get("MCP-Session-Id") == "" {
		t.Fatalf("initialize code=%d body=%s", initialized.Code, initialized.Body.String())
	}
	sessionID := initialized.Header().Get("MCP-Session-Id")
	ready := call(`{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}`, sessionID, ProtocolVersion)
	if ready.Code != http.StatusAccepted {
		t.Fatalf("initialized notification code=%d body=%s", ready.Code, ready.Body.String())
	}
	listed := call(`{"jsonrpc":"2.0","id":"list-1","method":"tools/list","params":{}}`, sessionID, ProtocolVersion)
	if listed.Code != http.StatusOK || !strings.Contains(listed.Body.String(), "masi.evidence.get") || strings.Contains(listed.Body.String(), "masi.events.get") {
		t.Fatalf("tools/list did not enforce manifest allowlist: %s", listed.Body.String())
	}
	called := call(`{"jsonrpc":"2.0","id":"call-1","method":"tools/call","params":{"name":"masi.evidence.get","arguments":{"evidence_id":"evidence-mcp-e2e"}}}`, sessionID, ProtocolVersion)
	if called.Code != http.StatusOK || !strings.Contains(called.Body.String(), evidenceID) || !strings.Contains(called.Body.String(), d) {
		t.Fatalf("allowlisted tool failed: %s", called.Body.String())
	}
	denied := call(`{"jsonrpc":"2.0","id":"call-2","method":"tools/call","params":{"name":"masi.events.get","arguments":{"event_id":"event-denied"}}}`, sessionID, ProtocolVersion)
	var deniedBody map[string]any
	_ = json.Unmarshal(denied.Body.Bytes(), &deniedBody)
	if denied.Code != http.StatusOK || deniedBody["error"] == nil {
		t.Fatalf("unallowlisted tool must return protocol error: %s", denied.Body.String())
	}
	old := call(`{"jsonrpc":"2.0","id":3,"method":"tools/list","params":{}}`, sessionID, "2025-03-26")
	if old.Code != http.StatusBadRequest {
		t.Fatalf("old MCP protocol must fail HTTP 400, got %d", old.Code)
	}
	var audits, deniedAudits int
	if err := pool.QueryRow(ctx, `SELECT count(*),count(*) FILTER(WHERE outcome='denied')
		FROM mcp_access_audit WHERE plugin_id=$1`, pluginID).Scan(&audits, &deniedAudits); err != nil || audits != 5 || deniedAudits != 1 {
		t.Fatalf("MCP audit count=%d denied=%d err=%v", audits, deniedAudits, err)
	}
}

func testDigest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}
