package real_plugin_host_pairwise

import (
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/health/grpc_health_v1"

	"masi-nids/control-go/internal/config"
	"masi-nids/control-go/internal/db"
	grpcapi "masi-nids/control-go/internal/grpc"
	adapterv1 "masi-nids/control-go/internal/grpc/adapterv1"
	"masi-nids/control-go/internal/plugin"
	"masi-nids/control-go/internal/pluginstat"
	"masi-nids/control-go/internal/security"
)

const (
	pluginID     = "plugin.wasm.statistics.fixture"
	manifestID   = "wasm-statistics-revision-1"
	definitionID = "fixture.row-count"
	scope        = "tenant:test"
)

type runtimeFixture struct {
	SchemaVersion          string `json:"schema_version"`
	Endpoint               string `json:"endpoint"`
	ServerName             string `json:"server_name"`
	CAPath                 string `json:"ca_path"`
	ManagerCertificatePath string `json:"manager_certificate_path"`
	ManagerPrivateKeyPath  string `json:"manager_private_key_path"`
	PluginID               string `json:"plugin_id"`
	PluginRevision         string `json:"plugin_revision"`
	BindingGeneration      int64  `json:"binding_generation"`
	ConfigDigest           string `json:"config_digest"`
	DefinitionID           string `json:"definition_id"`
	DefinitionRevision     string `json:"definition_revision"`
	DefinitionDigest       string `json:"definition_digest"`
	Scope                  string `json:"scope"`
	ArtifactDigest         string `json:"artifact_digest"`
	EnvelopeDigest         string `json:"envelope_digest"`
}

func acceptanceDefinition() pluginstat.Definition {
	definition := pluginstat.Definition{
		DefinitionID: definitionID, PluginID: pluginID, PluginRevision: manifestID + ":1",
		PluginKind: pluginstat.KindPureTransform, BindingGeneration: 1,
		HostProjectionRefs: []string{"projection.events-current"}, MetricKind: pluginstat.MetricGauge,
		Temporality: pluginstat.TemporalDelta, SeriesCardinalityLimit: 64, DeadlineMS: 5000,
		DisplayHint: pluginstat.DisplayMetricCard,
	}
	definition.DefinitionDigest = pluginstat.ComputeDefinitionDigest(definition)
	return definition
}

func TestExportDefinitionFixture(t *testing.T) {
	path := os.Getenv("MASI_PLUGIN_DEFINITION_EXPORT")
	if path == "" {
		return
	}
	definition := acceptanceDefinition()
	raw, err := json.MarshalIndent(map[string]any{
		"schema_version":    "go-plugin-statistics-definition-fixture/v1",
		"definition":        definition,
		"definition_digest": definition.DefinitionDigest,
	}, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	if _, err := file.Write(append(raw, '\n')); err != nil {
		t.Fatal(err)
	}
}

func TestRealGoDispatcherToHostWasm(t *testing.T) {
	if os.Getenv("MASI_REAL_PLUGIN_HOST_REQUIRED") != "1" {
		t.Skip("real Plugin Host acceptance not requested")
	}
	configPath := os.Getenv("MASI_CONTROL_E2E_CONFIG")
	dsn := os.Getenv("MASI_CONTROL_E2E_DSN")
	runtimePath := os.Getenv("MASI_REAL_PLUGIN_HOST_RUNTIME")
	evidencePath := os.Getenv("MASI_REAL_PLUGIN_HOST_EVIDENCE")
	externalDispatcher := os.Getenv("MASI_EXTERNAL_STATISTICS_DISPATCHER") == "1"
	if configPath == "" || dsn == "" || runtimePath == "" || evidencePath == "" {
		t.Fatal("real Plugin Host acceptance inputs are incomplete")
	}
	var runtime runtimeFixture
	raw, err := os.ReadFile(runtimePath)
	if err != nil || json.Unmarshal(raw, &runtime) != nil {
		t.Fatalf("load runtime fixture: %v", err)
	}
	definition := acceptanceDefinition()
	if runtime.SchemaVersion != "go-plugin-host-fixture/v1" || runtime.PluginID != pluginID ||
		runtime.PluginRevision != definition.PluginRevision || runtime.BindingGeneration != 1 ||
		runtime.DefinitionID != definition.DefinitionID || runtime.DefinitionRevision != definition.DefinitionID ||
		runtime.DefinitionDigest != definition.DefinitionDigest || runtime.Scope != scope {
		t.Fatalf("cross-language fixture identity drifted: %+v", runtime)
	}
	cfg, err := config.Load(configPath)
	if err != nil || cfg.PostgreSQLDSN != dsn {
		t.Fatalf("load Control config: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool, err := db.New(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()

	cleanup := func() error {
		cleanCtx, cleanCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanCancel()
		for _, statement := range []string{
			`DELETE FROM plugin_statistics_current WHERE definition_id=$1`,
			`DELETE FROM plugin_statistics_history WHERE definition_id=$1`,
			`DELETE FROM plugin_statistic_artifacts WHERE definition_id=$1`,
			`DELETE FROM plugin_statistic_input_bundles WHERE definition_id=$1`,
			`DELETE FROM plugin_statistic_runs WHERE definition_id=$1`,
			`DELETE FROM plugin_statistics_definitions WHERE definition_id=$1`,
		} {
			if _, err := pool.Exec(cleanCtx, statement, definitionID); err != nil {
				return err
			}
		}
		for _, statement := range []string{
			`DELETE FROM plugin_audit_events WHERE plugin_id=$1`,
			`DELETE FROM plugin_bindings WHERE plugin_id=$1`,
			`DELETE FROM plugin_qualifications WHERE plugin_id=$1`,
			`DELETE FROM plugin_manifests WHERE plugin_id=$1`,
		} {
			if _, err := pool.Exec(cleanCtx, statement, pluginID); err != nil {
				return err
			}
		}
		return nil
	}
	if err := cleanup(); err != nil {
		t.Fatalf("clean fixture namespace before test: %v", err)
	}
	if os.Getenv("MASI_PRESERVE_PLUGIN_EVIDENCE") != "1" {
		defer func() {
			if err := cleanup(); err != nil {
				t.Errorf("clean fixture namespace after test: %v", err)
			}
		}()
	}

	digest := "sha256:" + strings.Repeat("a", 64)
	actor := security.Actor{Issuer: "https://issuer.example", Subject: "plugin-host-acceptance"}
	if externalDispatcher {
		actor = security.Actor{Issuer: "https://idp.example", Subject: "operator-a"}
	}
	manifest := plugin.Manifest{
		ManifestID: manifestID, ManifestRevision: 1, PluginID: pluginID,
		Kind: plugin.KindPureTransform, Publisher: "masi", Version: "1.0.0", Scope: scope,
		Capabilities: []plugin.Capability{{CapabilityID: "plugin.statistics.execute", CapabilityKind: "host-projection", Declared: true}},
		ResourceLimits: plugin.ResourceLimits{CPUMilli: 100, MemoryBytes: 64 << 20, PIDCount: 4, FDCount: 32,
			DeadlineMS: 5000, OutputBytes: 1 << 20, QueueDepth: 32},
		RuntimeProfile: plugin.RuntimeWasmComponent, WitDigest: digest, SBOMDigest: digest,
		ProvenanceDigest: digest, SignatureStatus: "signed",
	}
	catalog := plugin.NewCatalogService(pool)
	registered, err := catalog.Register(ctx, manifest, actor, "trace-plugin-host-register")
	if err != nil {
		t.Fatal(err)
	}
	if err := catalog.Qualify(ctx, pluginID, manifestID, 1, plugin.Qualified, actor, "trace-plugin-host-qualify"); err != nil {
		t.Fatal(err)
	}
	var capabilities, resources []byte
	if err := pool.QueryRow(ctx, `SELECT capabilities,resource_limits FROM plugin_manifests WHERE plugin_id=$1`, pluginID).
		Scan(&capabilities, &resources); err != nil {
		t.Fatal(err)
	}
	binding := plugin.Binding{
		PluginID: pluginID, BindingGeneration: 1, ManifestID: manifestID, ManifestRevision: 1,
		ManifestDigest: registered.ManifestDigest, ConfigDigest: runtime.ConfigDigest,
		CapabilityDigest: digestBytes(capabilities), ResourceProfileDigest: digestBytes(resources),
		QualificationStatus: plugin.Qualified, Scope: scope,
	}
	bindings := plugin.NewBindingService(pool)
	if _, err := bindings.Activate(ctx, binding, actor, "trace-plugin-host-activate"); err != nil {
		t.Fatal(err)
	}
	service := pluginstat.NewService(pool)
	if err := service.RegisterDefinition(ctx, definition, manifestID, 1, scope, "security-metadata"); err != nil {
		t.Fatal(err)
	}

	nowMS := time.Now().UnixMilli()
	targetSetDigest := "sha256:" + strings.Repeat("b", 64)
	authorization := pluginstat.RunAuthorization{Scope: scope, DataClass: "security-metadata", SourceRead: true,
		StatisticsRun: true, TargetSetDigest: targetSetDigest}
	runID := "run-real-plugin-host-1"
	request := pluginstat.OnDemandRequest{RunID: runID, DefinitionID: definitionID,
		DefinitionDigest: definition.DefinitionDigest, IdempotencyKey: "real-plugin-host-key-1",
		Scope: scope, DataClass: authorization.DataClass, WindowStartUnixMS: nowMS - 60_000,
		WindowEndUnixMS: nowMS, TraceID: "trace-real-plugin-host", TargetSetDigest: targetSetDigest}
	if created, err := service.StartOnDemand(ctx, request, actor, authorization); err != nil || created != runID {
		t.Fatalf("start run=%s err=%v", created, err)
	}

	if externalDispatcher {
		deadline := time.Now().Add(20 * time.Second)
		for {
			var observedStatus string
			if err := pool.QueryRow(ctx, `SELECT status FROM plugin_statistic_runs WHERE run_id=$1`, runID).
				Scan(&observedStatus); err != nil {
				t.Fatal(err)
			}
			if observedStatus == string(pluginstat.RunSucceeded) {
				break
			}
			if observedStatus == string(pluginstat.RunFailed) || time.Now().After(deadline) {
				t.Fatalf("control-core statistics dispatcher terminal/timeout status=%s", observedStatus)
			}
			time.Sleep(100 * time.Millisecond)
		}
	} else {
		caPEM, err := os.ReadFile(runtime.CAPath)
		if err != nil {
			t.Fatal(err)
		}
		roots := x509.NewCertPool()
		if !roots.AppendCertsFromPEM(caPEM) {
			t.Fatal("fixture CA contains no certificate")
		}
		identity, err := tls.LoadX509KeyPair(runtime.ManagerCertificatePath, runtime.ManagerPrivateKeyPath)
		if err != nil {
			t.Fatal(err)
		}
		conn, err := grpc.NewClient(strings.TrimPrefix(runtime.Endpoint, "https://"),
			grpc.WithTransportCredentials(credentials.NewTLS(&tls.Config{MinVersion: tls.VersionTLS13,
				MaxVersion: tls.VersionTLS13, RootCAs: roots, Certificates: []tls.Certificate{identity}, ServerName: runtime.ServerName})),
			grpc.WithDisableRetry(), grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(4<<20), grpc.MaxCallSendMsgSize(4<<20)))
		if err != nil {
			t.Fatal(err)
		}
		defer conn.Close()
		if _, err := grpc_health_v1.NewHealthClient(conn).Check(ctx, &grpc_health_v1.HealthCheckRequest{}); err != nil {
			t.Fatalf("real Host health: %v", err)
		}
		executor := grpcapi.NewStatisticsExecutor(adapterv1.NewPluginStatisticsExecutorClient(conn))
		authorize := func(candidate security.Actor, candidateScope, dataClass, targetDigest string) bool {
			return candidate == actor && candidateScope == scope && dataClass == authorization.DataClass && targetDigest == targetSetDigest
		}
		if dispatched, err := service.DispatchNext(ctx, "real-plugin-host-dispatcher", executor, authorize); err != nil || !dispatched {
			t.Fatalf("dispatch=%v err=%v", dispatched, err)
		}
		if dispatched, err := service.DispatchNext(ctx, "real-plugin-host-dispatcher", executor, authorize); err != nil || dispatched {
			t.Fatalf("duplicate dispatch=%v err=%v", dispatched, err)
		}
	}

	var runStatus, currentRunID, artifactID, artifactDigest string
	var historyCount, artifactCount int
	var artifactRaw []byte
	if err := pool.QueryRow(ctx, `SELECT status FROM plugin_statistic_runs WHERE run_id=$1`, runID).Scan(&runStatus); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT run_id FROM plugin_statistics_current WHERE definition_id=$1`, definitionID).Scan(&currentRunID); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM plugin_statistics_history WHERE definition_id=$1`, definitionID).Scan(&historyCount); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM plugin_statistic_artifacts WHERE definition_id=$1`, definitionID).
		Scan(&artifactCount); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT artifact_id,artifact_digest,artifact FROM plugin_statistic_artifacts WHERE definition_id=$1`, definitionID).
		Scan(&artifactID, &artifactDigest, &artifactRaw); err != nil {
		t.Fatal(err)
	}
	var artifact pluginstat.Artifact
	if err := json.Unmarshal(artifactRaw, &artifact); err != nil {
		t.Fatal(err)
	}
	if runStatus != string(pluginstat.RunSucceeded) || currentRunID != runID || historyCount != 1 || artifactCount != 1 ||
		artifact.ArtifactDigest != artifactDigest || len(artifact.Metrics) != 1 || artifact.Metrics[0].MetricID != "row-count" ||
		artifact.Metrics[0].Value != 2 {
		t.Fatalf("run/current/history/artifact=%s/%s/%d/%d artifact=%+v", runStatus, currentRunID, historyCount, artifactCount, artifact)
	}

	evidence := map[string]any{
		"schema_version": "go-plugin-host-real-process-evidence/v1", "run_id": runID,
		"plugin_id": pluginID, "binding_generation": 1, "definition_id": definitionID,
		"definition_digest": definition.DefinitionDigest, "host_envelope_digest": runtime.EnvelopeDigest,
		"host_artifact_digest": runtime.ArtifactDigest, "artifact_id": artifactID, "artifact_digest": artifactDigest,
		"run_status": runStatus, "current_run_id": currentRunID, "history_count": historyCount,
		"artifact_count": artifactCount, "metric_id": artifact.Metrics[0].MetricID,
		"metric_value": artifact.Metrics[0].Value, "tls_version": "TLSv1.3", "mutual_tls": true,
		"external_dispatcher": externalDispatcher,
		"dispatcher_owner":    map[bool]string{true: "control-core-maintenance", false: "test-harness"}[externalDispatcher],
		"result":              "PASS",
	}
	encoded, _ := json.MarshalIndent(evidence, "", "  ")
	file, err := os.OpenFile(filepath.Clean(evidencePath), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	if _, err := file.Write(append(encoded, '\n')); err != nil {
		t.Fatal(err)
	}
}

func digestBytes(raw []byte) string {
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}
