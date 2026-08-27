// Package main is the Go Control Core entrypoint. It loads and validates
// configuration, opens the bounded PostgreSQL pool, verifies schema
// compatibility (fail-closed), wires the authorization foundation, and serves
// the health/readiness/liveness HTTP endpoints with graceful drain/shutdown.
//
// Business subdomains (Event, Governance/Effect, Firewall, Target/Fleet, Rule,
// Model, Plugin, Statistics, API/OIDC, gRPC) are wired in subsequent phases.
package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/go-chi/chi/v5"
	"google.golang.org/grpc"

	"masi-nids/control-go/internal/a2a"
	"masi-nids/control-go/internal/api"
	"masi-nids/control-go/internal/config"
	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/event"
	"masi-nids/control-go/internal/firewall"
	"masi-nids/control-go/internal/governance"
	grpcapi "masi-nids/control-go/internal/grpc"
	adapterv1 "masi-nids/control-go/internal/grpc/adapterv1"
	"masi-nids/control-go/internal/grpc/edgev1"
	"masi-nids/control-go/internal/health"
	"masi-nids/control-go/internal/logging"
	controlmcp "masi-nids/control-go/internal/mcp"
	controlmetrics "masi-nids/control-go/internal/metrics"
	"masi-nids/control-go/internal/model"
	"masi-nids/control-go/internal/plugin"
	"masi-nids/control-go/internal/pluginstat"
	"masi-nids/control-go/internal/ruleobs"
	"masi-nids/control-go/internal/security"
	"masi-nids/control-go/internal/target"
)

func main() {
	if err := run(); err != nil {
		os.Stderr.WriteString(err.Error() + "\n")
		os.Exit(1)
	}
}

func run() error {
	cfgPath := flag.String("config", "", "path to control-core config JSON (or set MASI_CTRL_CONFIG)")
	flag.Parse()

	logger := logging.New()
	slog := logger.With("component", "control-core")

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		return err
	}
	slog.Info("config loaded", "release", cfg.ReleaseVersion)

	// Bounded PostgreSQL pool. The application does NOT run migrations.
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(cfg.Probe.StartupMaxSeconds)*time.Second)
	defer cancel()

	pool, err := db.New(ctx, cfg)
	if err != nil {
		return err
	}
	defer pool.Close()
	slog.Info("postgres pool ready", "max_conns", cfg.Resource.MaxPoolConnections)

	// Schema compatibility check (fail closed on unknown/wrong version).
	mr := db.NewMigrationReader(pool, 21, cfg.SchemaMigrationDigest)
	if err := mr.CheckSchemaVersion(ctx); err != nil {
		// In the first release the schema_meta row may not yet exist when the
		// DB is brought up by the (not-yet-started) db/ module. We fail closed
		// rather than silently operating on an unknown schema.
		return err
	}

	var effectClient governance.EdgeEffectClient = stubEdgeEffect{}
	var effectCompiler governance.EdgeEffectPreflightClient = stubEdgeEffect{}
	var effectReadback governance.EdgeEffectReadbackClient = stubEdgeEffect{}
	var deploymentClient model.DeploymentAdapterClient = stubDeploymentAdapter{}
	var routeClient model.EdgeRouteClient = stubEdgeRoute{}
	var statisticsExecutor pluginstat.Executor
	var outbound *outboundRuntime
	if cfg.RuntimeProfile == "production" {
		outbound, err = buildOutboundRuntime(ctx, cfg, pool)
		if err != nil {
			return err
		}
		defer outbound.Close()
		effectClient = outbound.edge
		effectCompiler = outbound.edge
		effectReadback = outbound.edge
		deploymentClient = outbound.deployment
		routeClient = outbound.edge
		statisticsExecutor = outbound.statistics
		slog.Info("production outbound mTLS clients ready", "edge_workloads", len(cfg.Outbound.Edges))
	} else if cfg.RuntimeProfile == "acceptance" && cfg.Outbound.PluginStatistics.Endpoint != "" {
		statisticsConn, dialErr := dialMTLSClient(ctx, cfg.Outbound.PluginStatistics)
		if dialErr != nil {
			return fmt.Errorf("runtime: dial acceptance plugin statistics executor: %w", dialErr)
		}
		defer statisticsConn.Close()
		statisticsExecutor = grpcapi.NewStatisticsExecutor(
			adapterv1.NewPluginStatisticsExecutorClient(statisticsConn),
		)
		slog.Info("acceptance plugin statistics mTLS client ready")
	}

	// Authorization foundation. An empty mapping remains default-deny; a
	// digest-pinned mapping is loaded whenever a role_mapping_path is configured
	// (production requires it; the test profile points it at a fixture). The
	// mapping is the sole authorization source — the test login route mints
	// identity only and never grants scopes.
	mapping := &security.RoleScopeMapping{
		Version: "v1", Digest: "sha256:0000000000000000000000000000000000000000000000000000000000000000",
		ActorScopes: map[string][]security.Scope{}, DefaultDeny: true,
	}
	if cfg.RoleMappingPath != "" {
		mapping, err = security.LoadRoleScopeMapping(cfg.RoleMappingPath, cfg.RoleMappingDigest)
		if err != nil {
			return err
		}
	}

	// Health checker + HTTP server.
	checker := health.New()
	checker.SetDomain(health.DomainStatus{Domain: "postgres", Status: "ready"})
	if cfg.RuntimeProfile == "production" {
		checker.SetDomain(health.DomainStatus{Domain: "outbound-mtls", Status: "ready"})
	}

	// Subdomain services (Phases 2-9). Production uses the authenticated outbound
	// clients built above; the explicit test profile uses deterministic fail-closed
	// fakes. The API read projections query the pool directly, while mutations are
	// routed through the owning services.
	ingest := event.NewIngestService(pool)
	proposals := governance.NewProposalService(pool)
	decisions := governance.NewDecisionService(pool, mapping)
	intents := governance.NewIntentService(pool)
	preflight := governance.NewPreflightService(pool)
	capture := governance.NewCaptureService(pool)
	captureProjector := governance.NewCaptureProjector()
	targetRegistry := target.NewRegistryService(pool)
	fleet := target.NewFleetCoordinator(pool)
	assignmentSvc := target.NewAssignmentService(pool)

	firewallRevisions := firewall.NewRevisionService(pool)
	firewallOverlays := firewall.NewOverlayService(pool)
	firewallActivation := firewall.NewActivationService(pool)
	firewallProjector := firewall.NewIntentProjector()
	effectOwner, err := runtimeOwner("control-core-effect")
	if err != nil {
		return err
	}
	statisticsOwner, err := runtimeOwner("control-core-statistics")
	if err != nil {
		return err
	}
	dispatcher := governance.NewDispatcher(pool, effectClient, preflight, 30*time.Second, effectOwner, firewallProjector, fleet, captureProjector)
	reconcile := governance.NewReconcileService(pool, effectReadback, firewallProjector, fleet, captureProjector)

	modelRevisions := model.NewRevisionService(pool)
	modelIncarnation := model.NewIncarnationService(pool)
	modelRollout := model.NewRolloutService(pool, deploymentClient, routeClient)

	pluginCatalog := plugin.NewCatalogService(pool)
	pluginBinding := plugin.NewBindingService(pool)
	pluginStat := pluginstat.NewService(pool)

	ruleObs := ruleobs.NewProjector(pool)
	analysisClient, err := a2a.NewClient(pool, cfg.Outbound.Analysis, cfg.RuntimeProfile)
	if err != nil {
		return err
	}

	// HTTP surface: health + same-origin API + SSE.
	store := api.NewSessionStoreWithStepUpACRs(cfg.SessionCookieName, cfg.OIDCStepUpACRValues)
	hub := api.NewHub()
	cursorKey := make([]byte, 32)
	if cfg.RuntimeProfile == "production" {
		secret, err := readSecretReference(cfg.SSEHMACKeyRef)
		if err != nil {
			return err
		}
		digest := sha256.Sum256([]byte(secret))
		copy(cursorKey, digest[:])
	} else if _, err := rand.Read(cursorKey); err != nil {
		return err
	}
	cursorCodec, err := api.NewCursorCodec(cursorKey)
	if err != nil {
		return err
	}
	var oidc *api.OIDCClient
	if cfg.RuntimeProfile == "production" {
		secret, err := readSecretReference(cfg.OIDCClientSecretRef)
		if err != nil {
			return err
		}
		oidc = api.NewOIDCClient(cfg.OIDCIssuer, cfg.OIDCClientID, cfg.OIDCRedirectURL, secret)
		if err := oidc.Initialize(ctx); err != nil {
			return err
		}
	}
	deps := api.Deps{
		Pool: pool, Proposals: proposals, Decisions: decisions, Intents: intents,
		Mapping:            mapping,
		Cursor:             cursorCodec,
		Secure:             cfg.RuntimeProfile == "production",
		TestLogin:          cfg.RuntimeProfile != "production",
		RequestTimeout:     cfg.Resource.HTTPRequestTimeout,
		FirewallRevisions:  firewallRevisions,
		FirewallOverlays:   firewallOverlays,
		FirewallActivation: firewallActivation,
		TargetRegistry:     targetRegistry,
		FleetCoordinator:   fleet,
		Assignment:         assignmentSvc,
		ModelRevisions:     modelRevisions,
		ModelIncarnation:   modelIncarnation,
		ModelRollout:       modelRollout,
		PluginCatalog:      pluginCatalog,
		PluginBinding:      pluginBinding,
		PluginStat:         pluginStat,
		RuleObs:            ruleObs,
		Preflight:          preflight,
		FirewallCompiler:   effectCompiler,
		Dispatcher:         dispatcher,
		Reconcile:          reconcile,
		Capture:            capture,
		Analysis:           analysisClient,
		AcceptMutation:     checker.AcceptingMutations,
	}
	mcpServer := &controlmcp.Server{Pool: pool, AllowedOrigin: cfg.PublicOrigin,
		Production: cfg.RuntimeProfile == "production", AllowTestLoopback: cfg.RuntimeProfile != "production"}
	// The OIDC client is configured via deployment profile (issuer/client id
	// from the secret store); a nil client disables browser login while the
	// health/API surface still serves probes.
	r := chi.NewRouter()
	metricRegistry := controlmetrics.New(pool)
	runtimeConfigPath := strings.TrimSpace(*cfgPath)
	if runtimeConfigPath == "" {
		runtimeConfigPath = strings.TrimSpace(os.Getenv("MASI_CTRL_CONFIG"))
	}
	if err := metricRegistry.ConfigureRuntimeFiles(runtimeConfigPath, cfg.TLS.HTTPCertFile, cfg.TLS.GRPCCertFile); err != nil {
		return err
	}
	r.Use(metricRegistry.Middleware)
	r.Get("/healthz", checker.HandleHealth)
	r.Get("/readyz", checker.HandleReady)
	r.Get("/livez", checker.HandleLive)
	r.Get("/metrics", metricRegistry.Handler)
	r.Handle("/mcp", http.HandlerFunc(mcpServer.Handler))
	r.Mount("/", api.Router(deps, oidc, store, hub, cfg.PublicOrigin))
	httpTLS, err := httpTLSConfig(cfg)
	if err != nil {
		return err
	}

	httpSrv := &http.Server{
		Addr:              cfg.HTTPListen,
		Handler:           r,
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       cfg.Resource.HTTPRequestTimeout,
		IdleTimeout:       cfg.Resource.HTTPIdleTimeout,
		MaxHeaderBytes:    64 * 1024,
		TLSConfig:         httpTLS,
	}
	httpLn, httpErr := net.Listen("tcp", cfg.HTTPListen)
	if httpErr != nil {
		return httpErr
	}

	// gRPC surface: ControlSink (Edge → Go). mTLS termination is configured
	// by the deployment profile; the server itself enforces schema bounds. The
	// rule-observation projector and the per-shard current-binding resolver are
	// wired so PublishRuleObservations and the late-generation fence are live.
	grpcOpts, err := grpcServerOptions(cfg)
	if err != nil {
		return err
	}
	grpcSrv := grpc.NewServer(grpcOpts...)
	sink := &grpcapi.ControlSinkServer{
		Ingest:          ingest,
		RuleObs:         ruleObs,
		TargetRegistry:  targetRegistry,
		MaxBatchRecords: 256,
		MaxBatchBytes:   4 * 1024 * 1024,
		Accepting:       checker.AcceptingMutations,
	}
	if cfg.RuntimeProfile != "test" {
		sink.AuthorizeTarget = func(ctx context.Context, targetID string) error {
			return grpcapi.AuthorizeTargetPeer(ctx, pool, targetID)
		}
	}
	edgev1.RegisterControlSinkServer(grpcSrv, sink)
	grpcLn, grpcErr := net.Listen("tcp", cfg.GRPCListen)
	if grpcErr != nil {
		_ = httpLn.Close()
		return grpcErr
	}
	serverErr := make(chan error, 2)
	go func() {
		if err := grpcSrv.Serve(grpcLn); err != nil {
			serverErr <- err
		}
	}()
	slog.Info("grpc serving", "listen", cfg.GRPCListen)

	go func() {
		var err error
		if cfg.RuntimeProfile == "production" {
			err = httpSrv.ServeTLS(httpLn, cfg.TLS.HTTPCertFile, cfg.TLS.HTTPKeyFile)
		} else {
			err = httpSrv.Serve(httpLn)
		}
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			serverErr <- err
		}
	}()
	slog.Info("http serving", "listen", cfg.HTTPListen)
	checker.SetDomain(health.DomainStatus{Domain: "http-api", Status: "ready"})
	checker.SetDomain(health.DomainStatus{Domain: "grpc-control-sink", Status: "ready"})
	checker.SetState(health.StateReady, 0)
	maintenanceCtx, maintenanceCancel := context.WithCancel(context.Background())
	defer maintenanceCancel()
	// Statistics execution remains disabled until a real Host/direct typed
	// adapter is configured. A nil executor leaves durable runs pending; it must
	// never fabricate a successful Artifact.
	startMaintenance(maintenanceCtx, slog, pool, dispatcher, reconcile, firewallOverlays, pluginStat,
		statisticsExecutor, ingest, ruleObs, mapping, cfg.Resource.EventRetention,
		cfg.Resource.PluginStatRetention, cfg.Resource.IdempotencyRetention, statisticsOwner, checker.MarkProgress)

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	select {
	case s := <-sig:
		slog.Info("drain signal", "signal", s.String())
	case err := <-serverErr:
		if err != nil {
			return err
		}
	}

	// Drain: stop new mutation/claim, wait bounded in-flight, release leases,
	// never blindly retry external actions at shutdown (§12).
	checker.SetState(health.StateDraining, cfg.Probe.DrainTimeout)
	maintenanceCancel()
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), cfg.Probe.DrainTimeout)
	defer shutdownCancel()
	if _, err := dispatcher.FenceOwnedClaimsOnShutdown(shutdownCtx); err != nil {
		slog.Error("fence effect claims during drain", "err", err)
	}
	if _, err := pluginStat.ReleaseOwnedClaimsOnShutdown(shutdownCtx, statisticsOwner); err != nil {
		slog.Error("release statistics claims during drain", "err", err)
	}
	grpcStopped := make(chan struct{})
	go func() { grpcSrv.GracefulStop(); close(grpcStopped) }()
	select {
	case <-grpcStopped:
	case <-shutdownCtx.Done():
		grpcSrv.Stop()
	}
	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		slog.Error("http shutdown", "err", err)
	}
	checker.SetState(health.StateStopped, 0)
	slog.Info("stopped")
	return nil
}

func runtimeOwner(prefix string) (string, error) {
	nonce := make([]byte, 16)
	if _, err := rand.Read(nonce); err != nil {
		return "", err
	}
	return prefix + "-" + hex.EncodeToString(nonce), nil
}
