package pluginstat

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
	"masi-nids/control-go/internal/plugin"
	"masi-nids/control-go/internal/security"
)

// TestStatisticsLifecyclePostgres exercises the real run ledger, exact
// qualification/binding fence, frozen input, dispatch, artifact/current/history
// commit, schedule revisions, idempotency conflict, and revoke-before-result
// behavior. The executor is an explicit module-boundary fake.
func TestStatisticsLifecyclePostgres(t *testing.T) {
	configPath := os.Getenv("MASI_CONTROL_E2E_CONFIG")
	dsn := os.Getenv("MASI_CONTROL_E2E_DSN")
	if dsn == "" || configPath == "" {
		if os.Getenv("MASI_CONTROL_E2E_REQUIRED") == "1" {
			t.Fatal("plugin statistics PostgreSQL E2E required but DSN/CONFIG is missing")
		}
		t.Skip("set MASI_CONTROL_E2E_DSN/CONFIG for plugin statistics PostgreSQL E2E")
	}
	cfg, err := config.Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.PostgreSQLDSN != dsn {
		t.Fatal("plugin statistics PostgreSQL E2E DSN differs from validated config")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool, err := db.New(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()

	const (
		pluginID     = "plugin-statistics-e2e"
		manifestID   = "manifest-statistics-e2e"
		definitionID = "definition-statistics-e2e"
		runID        = "run-statistics-e2e-1"
		runID2       = "run-statistics-e2e-2"
		scheduleID   = "schedule-statistics-e2e"
	)
	cleanup := func() {
		cleanCtx, cleanCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanCancel()
		for _, statement := range []string{
			`DELETE FROM plugin_statistics_current WHERE definition_id=$1`,
			`DELETE FROM plugin_statistics_history WHERE definition_id=$1`,
			`DELETE FROM plugin_statistic_artifacts WHERE definition_id=$1`,
			`DELETE FROM plugin_statistic_input_bundles WHERE definition_id=$1`,
			`DELETE FROM plugin_statistic_runs WHERE definition_id=$1`,
			`DELETE FROM plugin_statistic_schedules WHERE definition_id=$1`,
			`DELETE FROM plugin_statistics_definitions WHERE definition_id=$1`,
		} {
			_, _ = pool.Exec(cleanCtx, statement, definitionID)
		}
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
	actor := security.Actor{Issuer: "https://issuer.example", Subject: "statistics-admin-e2e"}
	manifest := plugin.Manifest{
		ManifestID: manifestID, ManifestRevision: 1, PluginID: pluginID,
		Kind: plugin.KindPureTransform, Publisher: "masi", Version: "1.0.0",
		Capabilities:   []plugin.Capability{{CapabilityID: "statistics-v1", CapabilityKind: "host-projection", Declared: true}},
		ResourceLimits: plugin.ResourceLimits{CPUMilli: 100, MemoryBytes: 64 << 20, PIDCount: 8, FDCount: 32, DeadlineMS: 1000, OutputBytes: 4096, QueueDepth: 4},
		RuntimeProfile: plugin.RuntimeWasmComponent, WitDigest: digest, SBOMDigest: digest,
		ProvenanceDigest: digest, SignatureStatus: "signed", Scope: "scope-statistics-e2e",
	}
	catalog := plugin.NewCatalogService(pool)
	registered, err := catalog.Register(ctx, manifest, actor, "trace-statistics-register")
	if err != nil {
		t.Fatal(err)
	}
	if err := catalog.Qualify(ctx, pluginID, manifestID, 1, plugin.Qualified, actor, "trace-statistics-qualify"); err != nil {
		t.Fatal(err)
	}
	var capabilities, resources []byte
	if err := pool.QueryRow(ctx, `SELECT capabilities,resource_limits FROM plugin_manifests WHERE plugin_id=$1`, pluginID).
		Scan(&capabilities, &resources); err != nil {
		t.Fatal(err)
	}
	binding := plugin.Binding{
		PluginID: pluginID, BindingGeneration: 1, ManifestID: manifestID, ManifestRevision: 1,
		ManifestDigest: registered.ManifestDigest, ConfigDigest: digest,
		CapabilityDigest: pluginstatDigestBytes(capabilities), ResourceProfileDigest: pluginstatDigestBytes(resources),
		QualificationStatus: plugin.Qualified, Scope: manifest.Scope,
	}
	bindings := plugin.NewBindingService(pool)
	if _, err := bindings.Activate(ctx, binding, actor, "trace-statistics-activate"); err != nil {
		t.Fatal(err)
	}

	definition := Definition{
		DefinitionID: definitionID, PluginID: pluginID, PluginRevision: manifestID + ":1",
		PluginKind: KindPureTransform, BindingGeneration: 1,
		HostProjectionRefs: []string{"projection.events-current"}, MetricKind: MetricSum,
		Temporality: TemporalCumulative, DimensionLabels: []string{"quality"},
		SeriesCardinalityLimit: 64, DeadlineMS: 1000, DisplayHint: DisplayMetricCard,
	}
	definition.DefinitionDigest = ComputeDefinitionDigest(definition)
	service := NewService(pool)
	if err := service.RegisterDefinition(ctx, definition, manifestID, 1, manifest.Scope, "security-metadata"); err != nil {
		t.Fatal(err)
	}
	var qualificationDigest, definitionManifestDigest string
	if err := pool.QueryRow(ctx, `SELECT qualification_digest,manifest_digest FROM plugin_statistics_definitions WHERE definition_id=$1`, definitionID).
		Scan(&qualificationDigest, &definitionManifestDigest); err != nil || !strings.HasPrefix(qualificationDigest, "sha256:") || definitionManifestDigest != registered.ManifestDigest {
		t.Fatalf("definition qualification fence missing: qualification=%s manifest=%s err=%v", qualificationDigest, definitionManifestDigest, err)
	}

	nowMS := time.Now().UnixMilli()
	targetSetDigest := "sha256:" + strings.Repeat("b", 64)
	auth := RunAuthorization{Scope: manifest.Scope, DataClass: "security-metadata", SourceRead: true, StatisticsRun: true, TargetSetDigest: targetSetDigest}
	request := OnDemandRequest{RunID: runID, DefinitionID: definitionID, DefinitionDigest: definition.DefinitionDigest,
		IdempotencyKey: "statistics-e2e-key-1", Scope: manifest.Scope, DataClass: auth.DataClass,
		WindowStartUnixMS: nowMS - 60_000, WindowEndUnixMS: nowMS, TraceID: "trace-statistics-run-1",
		TargetSetDigest: targetSetDigest}
	created, err := service.StartOnDemand(ctx, request, actor, auth)
	if err != nil || created != runID {
		t.Fatalf("start run=%s err=%v", created, err)
	}
	replay := request
	replay.RunID = "run-statistics-e2e-replay-identity"
	if got, err := service.StartOnDemand(ctx, replay, actor, auth); err != nil || got != runID {
		t.Fatalf("idempotent replay=%s err=%v", got, err)
	}
	conflict := request
	conflict.RunID = "run-statistics-e2e-conflict"
	conflict.WindowStartUnixMS--
	if _, err := service.StartOnDemand(ctx, conflict, actor, auth); err == nil || !strings.Contains(err.Error(), ErrIdempotencyConflict.Error()) {
		t.Fatalf("same idempotency key with changed request must conflict, got %v", err)
	}

	executor := &statisticsExecutorFake{definition: definition, pluginRevision: manifestID + ":1", pluginID: pluginID}
	authorize := func(candidate security.Actor, scope, dataClass, targetDigest string) bool {
		return candidate == actor && scope == auth.Scope && dataClass == auth.DataClass && targetDigest == targetSetDigest
	}
	dispatched, err := service.DispatchNext(ctx, "statistics-dispatcher-e2e", executor, authorize)
	if err != nil || !dispatched {
		t.Fatalf("dispatch=%v err=%v", dispatched, err)
	}
	if !executor.sawFrozenDigest {
		t.Fatal("executor did not receive the stored frozen_input_digest")
	}
	var runStatus, currentRunID string
	var historyCount, artifactCount int
	if err := pool.QueryRow(ctx, `SELECT status FROM plugin_statistic_runs WHERE run_id=$1`, runID).Scan(&runStatus); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT run_id FROM plugin_statistics_current WHERE definition_id=$1`, definitionID).Scan(&currentRunID); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM plugin_statistics_history WHERE definition_id=$1`, definitionID).Scan(&historyCount); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM plugin_statistic_artifacts WHERE definition_id=$1`, definitionID).Scan(&artifactCount); err != nil {
		t.Fatal(err)
	}
	if runStatus != string(RunSucceeded) || currentRunID != runID || historyCount != 1 || artifactCount != 1 {
		t.Fatalf("run/current/history/artifact=%s/%s/%d/%d", runStatus, currentRunID, historyCount, artifactCount)
	}

	scheduleAuth := ScheduleAuthorization{Scope: manifest.Scope, DataClass: auth.DataClass, PlatformAdmin: true,
		CSRFVerified: true, StepUpFresh: true, TargetSetDigest: targetSetDigest, SourceRead: true, StatisticsRun: true}
	if revision, err := service.CreateScheduleRevision(ctx, scheduleID, definitionID, definition.DefinitionDigest, 60, false, actor, scheduleAuth, "trace-schedule-create"); err != nil || revision != 1 {
		t.Fatalf("create schedule revision=%d err=%v", revision, err)
	}
	if revision, err := service.CreateScheduleRevision(ctx, scheduleID, definitionID, definition.DefinitionDigest, 60, true, actor, scheduleAuth, "trace-schedule-disable"); err != nil || revision != 2 {
		t.Fatalf("disable schedule revision=%d err=%v", revision, err)
	}

	request2 := request
	request2.RunID = runID2
	request2.IdempotencyKey = "statistics-e2e-key-2"
	request2.TraceID = "trace-statistics-run-2"
	if _, err := service.StartOnDemand(ctx, request2, actor, auth); err != nil {
		t.Fatal(err)
	}
	token, ok, err := service.ClaimNextPending(ctx, "statistics-dispatcher-e2e", 30_000, authorize)
	if err != nil || !ok || token.RunID != runID2 {
		t.Fatalf("claim second token=%+v ok=%v err=%v", token, ok, err)
	}
	if err := bindings.Revoke(ctx, pluginID, actor, "trace-statistics-revoke"); err != nil {
		t.Fatal(err)
	}
	artifact := executor.artifactFor(*token)
	if err := service.FinishRun(ctx, *token, RunSucceeded, artifact); err != nil {
		t.Fatal(err)
	}
	var secondStatus string
	if err := pool.QueryRow(ctx, `SELECT status FROM plugin_statistic_runs WHERE run_id=$1`, runID2).Scan(&secondStatus); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM plugin_statistics_history WHERE definition_id=$1`, definitionID).Scan(&historyCount); err != nil {
		t.Fatal(err)
	}
	if secondStatus != string(RunFenced) || historyCount != 1 {
		t.Fatalf("revoked late result status=%s history=%d, want fenced/1", secondStatus, historyCount)
	}
}

type statisticsExecutorFake struct {
	definition      Definition
	pluginID        string
	pluginRevision  string
	sawFrozenDigest bool
}

func (f *statisticsExecutorFake) ExecuteStatistics(_ context.Context, token ClaimToken, bundle InputBundle) (Artifact, error) {
	f.sawFrozenDigest = digestRE.MatchString(bundle.FrozenInputDigest) && bundle.DefinitionDigest == token.DefinitionDigest
	return f.artifactFor(token), nil
}

func (f *statisticsExecutorFake) artifactFor(token ClaimToken) Artifact {
	a := FinalizeArtifact(Artifact{
		ArtifactID: "artifact-" + token.RunID, RunID: token.RunID,
		DefinitionID: token.DefinitionID, DefinitionDigest: token.DefinitionDigest,
		Status: RunSucceeded, Quality: QualityValid,
		Metrics:    []Metric{{MetricID: "event-count", MetricKind: MetricSum, Temporality: TemporalCumulative, Value: 0, Unit: "events"}},
		Truncation: Truncation{ReasonCode: "NONE"},
		Provenance: Provenance{PluginID: token.PluginID, PluginRevision: token.PluginRevision,
			ComputedAtUnixMS: time.Now().UnixMilli(), DefinitionID: token.DefinitionID, DefinitionDigest: token.DefinitionDigest,
			RunID: token.RunID, BindingGeneration: token.BindingGeneration},
		ActorRef: token.PluginID, ReasonCode: "SUCCEEDED", TraceID: "statistics:" + token.RunID,
	})
	return a
}

func pluginstatDigestBytes(raw []byte) string {
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}
