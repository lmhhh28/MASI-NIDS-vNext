package plugin

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"strings"
	"testing"
	"time"

	"masi-nids/control-go/internal/config"
	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

// TestPluginAuditLifecyclePostgres exercises the real PostgreSQL state machine.
// It is opt-in so ordinary unit tests never silently substitute a fake database.
func TestPluginAuditLifecyclePostgres(t *testing.T) {
	configPath := os.Getenv("MASI_CONTROL_E2E_CONFIG")
	if os.Getenv("MASI_CONTROL_E2E_DSN") == "" || configPath == "" {
		if os.Getenv("MASI_CONTROL_E2E_REQUIRED") == "1" {
			t.Fatal("plugin PostgreSQL E2E required but DSN/CONFIG is missing")
		}
		t.Skip("set MASI_CONTROL_E2E_DSN/CONFIG for plugin PostgreSQL E2E")
	}
	cfg, err := config.Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	// Do not trust a second, divergent DSN hidden in the configuration.
	if cfg.PostgreSQLDSN != os.Getenv("MASI_CONTROL_E2E_DSN") {
		t.Fatal("plugin PostgreSQL E2E DSN differs from validated config")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool, err := db.New(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()

	const pluginID = "plugin-audit-e2e"
	cleanup := func() {
		cleanCtx, cleanCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanCancel()
		for _, statement := range []string{
			`DELETE FROM plugin_audit_events WHERE plugin_id=$1`,
			`DELETE FROM plugin_bindings WHERE plugin_id=$1`,
			`DELETE FROM plugin_qualifications WHERE plugin_id=$1`,
			`DELETE FROM plugin_manifests WHERE plugin_id=$1`,
		} {
			_, _ = pool.Exec(cleanCtx, statement, pluginID)
		}
	}
	cleanup()
	defer cleanup()

	digest := "sha256:" + strings.Repeat("a", 64)
	actor := security.Actor{Issuer: "https://issuer.example", Subject: "plugin-operator-e2e"}
	manifest := Manifest{
		ManifestID: "manifest-audit-e2e", ManifestRevision: 1, PluginID: pluginID,
		Kind: KindPureTransform, Publisher: "masi", Version: "1.0.0",
		Capabilities:   []Capability{{CapabilityID: "events-v1", CapabilityKind: "host-projection", Declared: true}},
		ResourceLimits: ResourceLimits{CPUMilli: 100, MemoryBytes: 64 << 20, PIDCount: 8, FDCount: 32, DiskBytes: 0, DeadlineMS: 1000, OutputBytes: 4096, QueueDepth: 4},
		RuntimeProfile: RuntimeWasmComponent, WitDigest: digest, SBOMDigest: digest,
		ProvenanceDigest: digest, SignatureStatus: "signed", Scope: "scope-e2e",
	}
	catalog := NewCatalogService(pool)
	registered, err := catalog.Register(ctx, manifest, actor, "trace-plugin-register")
	if err != nil {
		t.Fatal(err)
	}
	if err := catalog.Qualify(ctx, pluginID, manifest.ManifestID, 1, Qualified, actor, "trace-plugin-qualify-1"); err != nil {
		t.Fatal(err)
	}
	var capabilities, resources []byte
	if err := pool.QueryRow(ctx, `SELECT capabilities,resource_limits FROM plugin_manifests WHERE plugin_id=$1`, pluginID).
		Scan(&capabilities, &resources); err != nil {
		t.Fatal(err)
	}
	binding := Binding{
		PluginID: pluginID, BindingGeneration: 1, ManifestID: manifest.ManifestID,
		ManifestRevision: 1, ManifestDigest: registered.ManifestDigest, ConfigDigest: digest,
		CapabilityDigest: digestJSONBytes(capabilities), ResourceProfileDigest: digestJSONBytes(resources),
		QualificationStatus: Qualified, Scope: manifest.Scope,
	}
	bindings := NewBindingService(pool)
	if _, err := bindings.Activate(ctx, binding, actor, "trace-plugin-activate-1"); err != nil {
		t.Fatal(err)
	}
	if err := bindings.Drain(ctx, pluginID, actor, "trace-plugin-drain"); err != nil {
		t.Fatal(err)
	}
	if err := bindings.Revoke(ctx, pluginID, actor, "trace-plugin-revoke"); err != nil {
		t.Fatal(err)
	}

	binding.BindingGeneration = 2
	if _, err := bindings.Activate(ctx, binding, actor, "trace-plugin-reactivate-without-qualification"); err == nil ||
		!strings.Contains(err.Error(), "newer qualification") {
		t.Fatalf("reactivation after revoke must require new qualification, got %v", err)
	}
	if err := catalog.Qualify(ctx, pluginID, manifest.ManifestID, 1, Qualified, actor, "trace-plugin-qualify-2"); err != nil {
		t.Fatal(err)
	}
	if _, err := bindings.Activate(ctx, binding, actor, "trace-plugin-activate-2"); err != nil {
		t.Fatal(err)
	}

	var auditCount int
	var wrongIdentity int
	if err := pool.QueryRow(ctx, `
		SELECT count(*),count(*) FILTER (WHERE actor_ref<>$2 OR actor_issuer<>$3 OR actor_subject<>$4)
		FROM plugin_audit_events WHERE plugin_id=$1`,
		pluginID, actor.String(), actor.Issuer, actor.Subject).Scan(&auditCount, &wrongIdentity); err != nil {
		t.Fatal(err)
	}
	if auditCount != 7 || wrongIdentity != 0 {
		t.Fatalf("plugin audit count=%d wrong_identity=%d, want 7/0", auditCount, wrongIdentity)
	}
	var activeGeneration int
	if err := pool.QueryRow(ctx, `SELECT binding_generation FROM plugin_bindings WHERE plugin_id=$1 AND activation_state='active'`, pluginID).
		Scan(&activeGeneration); err != nil || activeGeneration != 2 {
		t.Fatalf("active binding generation=%d err=%v, want 2", activeGeneration, err)
	}
}

func digestJSONBytes(raw []byte) string {
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}
