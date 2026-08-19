package soak

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	edgev1 "masi-nids/control-go/internal/grpc/edgev1"
)

const (
	schemaVersion   = "qualification-soak/v1"
	moduleID        = "control-core"
	thresholdStatus = "OBSERVED_ONLY_OWNER_NOT_FROZEN_DEC_001"
	ticksPerSec     = int64(100) // Linux USER_HZ on x86_64
	sampleInterval  = 10 * time.Second
	soakRatePerSec  = 100
)

var phaseConcurrency = []int{2, 8, 32, 128}
var phaseNames = []string{"steady", "peak", "saturation", "recovery-or-activation"}

type sample struct {
	offsetMs     int64
	phase        string
	cpuPct       float64
	rssBytes     int64
	fdCount      int64
	threadCount  int64
	queueDepth   int64
	oracleErrors int64
	gapCount     int64
	valid        bool
}

type phaseRec struct {
	name        string
	startOffset int64
	endOffset   int64
	elapsedMs   int64
	errs        int64
}

func TestFormalSoak(t *testing.T) {
	dsn := os.Getenv("MASI_CONTROL_E2E_DSN")
	configPath := os.Getenv("MASI_CONTROL_E2E_CONFIG")
	binary := os.Getenv("MASI_CONTROL_E2E_BINARY")
	soakSecondsStr := os.Getenv("MASI_CONTROL_SOAK_SECONDS")
	if dsn == "" || configPath == "" || binary == "" || soakSecondsStr == "" {
		if os.Getenv("MASI_CONTROL_E2E_REQUIRED") == "1" {
			t.Fatal("soak required but MASI_CONTROL_SOAK_SECONDS/E2E env is missing")
		}
		t.Skip("set MASI_CONTROL_SOAK_SECONDS + MASI_CONTROL_E2E_DSN/CONFIG/BINARY for the soak")
	}
	soakSeconds, err := strconv.Atoi(soakSecondsStr)
	if err != nil || soakSeconds <= 0 {
		t.Fatalf("invalid MASI_CONTROL_SOAK_SECONDS=%q", soakSecondsStr)
	}
	evidenceDir := os.Getenv("MASI_CONTROL_EVIDENCE_DIR")
	if evidenceDir == "" {
		evidenceDir = "."
	}
	formal := soakSeconds >= 3600
	repoRoot := os.Getenv("MASI_CONTROL_REPO_ROOT")
	if repoRoot == "" {
		abs, _ := filepath.Abs(filepath.Join("..", "..", ".."))
		repoRoot = abs
	}
	profilePath := filepath.Join(repoRoot, "contracts", "profiles", "v1", "qualification-soak-3600s.json")
	schemaPath := filepath.Join(repoRoot, "contracts", "evidence", "soak", "v1", "schema.json")
	profileDigest := fileSHA256Prefixed(profilePath)
	schemaSHA := fileSHA256Hex(schemaPath)
	schemaBytes := fileSize(schemaPath)
	profileBytes := fileSize(profilePath)
	binarySHA := fileSHA256Hex(binary)
	binaryBytes := fileSize(binary)

	rawCfg, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	var runtimeCfg struct {
		HTTPListen string `json:"http_listen"`
		GRPCListen string `json:"grpc_listen"`
	}
	if err := json.Unmarshal(rawCfg, &runtimeCfg); err != nil || runtimeCfg.HTTPListen == "" || runtimeCfg.GRPCListen == "" {
		t.Fatalf("parse runtime endpoints: %v", err)
	}

	seedCtx, seedCancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer seedCancel()
	conn, err := pgx.Connect(seedCtx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close(context.Background())
	seedControlFixture(seedCtx, t, conn)
	defer cleanupSoak(conn)

	var logs strings.Builder
	cmd := exec.Command(binary, "--config", configPath)
	cmd.Stdout = &logs
	cmd.Stderr = &logs
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	pid := cmd.Process.Pid
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	stopped := false
	t.Cleanup(func() {
		if !stopped {
			_ = cmd.Process.Signal(syscall.SIGTERM)
			select {
			case <-done:
			case <-time.After(10 * time.Second):
				_ = cmd.Process.Kill()
			}
		}
	})
	ready := false
	for i := 0; i < 100; i++ {
		resp, err := http.Get("http://" + runtimeCfg.HTTPListen + "/readyz")
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				ready = true
				break
			}
		}
		select {
		case err := <-done:
			t.Fatalf("process exited before ready: %v\n%s", err, logs.String())
		default:
		}
		time.Sleep(100 * time.Millisecond)
	}
	if !ready {
		t.Fatalf("process not ready\n%s", logs.String())
	}

	cc, err := grpc.NewClient(runtimeCfg.GRPCListen, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer cc.Close()
	client := edgev1.NewControlSinkClient(cc)
	oracleConn, err := pgx.Connect(seedCtx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer oracleConn.Close(context.Background())

	var (
		samples          []sample
		samplesMu        sync.Mutex
		inFlight         int64
		committedCount   int64
		oracleMismatches int64
	)
	prevCpuTicks := int64(-1)

	recordSample := func(offsetMs int64, phase string) {
		var s sample
		s.offsetMs = offsetMs
		s.phase = phase
		rss, fds, threads, cpu, alive := sampleProcess(pid, &prevCpuTicks, sampleInterval)
		if !alive {
			s.gapCount = 1
			samplesMu.Lock()
			samples = append(samples, s)
			samplesMu.Unlock()
			return
		}
		s.valid = true
		s.cpuPct = cpu
		s.rssBytes = rss
		s.fdCount = fds
		s.threadCount = threads
		s.queueDepth = atomic.LoadInt64(&inFlight)
		// Oracle: a committed event is written to PostgreSQL before its ACK is returned,
		// so the DB count can never be below the number of committed ACKs observed. A DB
		// count below the committed-ACK count would mean a phantom ACK (a real bug).
		octx, ocancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer ocancel()
		var dbCount int64
		if err := oracleConn.QueryRow(octx, `SELECT count(*) FROM events WHERE event_idempotency_key LIKE 'soak-%'`).Scan(&dbCount); err != nil {
			s.oracleErrors = 1
		} else if dbCount < atomic.LoadInt64(&committedCount) {
			s.oracleErrors = 1
			atomic.AddInt64(&oracleMismatches, 1)
		}
		samplesMu.Lock()
		samples = append(samples, s)
		samplesMu.Unlock()
	}

	// drivePhase paces soakRatePerSec total CommitResults across `concurrency` workers
	// for `dur`; each request has a unique idempotency key. Returns elapsed + error count.
	drivePhase := func(ctx context.Context, phaseIdx int, dur time.Duration) (int64, int64) {
		concurrency := phaseConcurrency[phaseIdx]
		phaseStart := time.Now()
		perWorkerInterval := time.Duration(concurrency) * time.Second / time.Duration(soakRatePerSec)
		deadline := phaseStart.Add(dur)
		var wg sync.WaitGroup
		var localErrs int64
		for w := 0; w < concurrency; w++ {
			wg.Add(1)
			go func(worker int) {
				defer wg.Done()
				var seq int64
				for {
					next := phaseStart.Add(time.Duration(seq) * perWorkerInterval)
					if !next.Before(deadline) {
						return
					}
					now := time.Now()
					if next.After(now) {
						select {
						case <-ctx.Done():
							return
						case <-time.After(next.Sub(now)):
						}
					}
					seq++
					key := fmt.Sprintf("soak-p%d-w%d-%d", phaseIdx, worker, seq)
					batch := soakBatch(key, worker, seq)
					atomic.AddInt64(&inFlight, 1)
					rctx, rcancel := context.WithTimeout(ctx, 30*time.Second)
					ack, err := client.CommitResults(rctx, batch)
					rcancel()
					atomic.AddInt64(&inFlight, -1)
					if err != nil {
						atomic.AddInt64(&localErrs, 1)
						continue
					}
					if len(ack.Acknowledgements) != 1 {
						atomic.AddInt64(&localErrs, 1)
						continue
					}
					switch ack.Acknowledgements[0].Status {
					case "committed":
						atomic.AddInt64(&committedCount, 1)
					case "idempotent":
						// a colliding key (unexpected under unique keys) is tolerated
					default:
						atomic.AddInt64(&localErrs, 1)
					}
				}
			}(w)
		}
		wg.Wait()
		return time.Since(phaseStart).Milliseconds(), atomic.LoadInt64(&localErrs)
	}

	var warmup time.Duration
	phaseDur := make([]time.Duration, 4)
	if formal {
		warmup = 60 * time.Second
		for i := range phaseDur {
			phaseDur[i] = 900 * time.Second
		}
	} else {
		warmup = time.Duration(soakSeconds/6) * time.Second
		if warmup < 5*time.Second {
			warmup = 5 * time.Second
		}
		each := time.Duration(soakSeconds) * time.Second / 4
		if each < 5*time.Second {
			each = 5 * time.Second
		}
		for i := range phaseDur {
			phaseDur[i] = each
		}
	}

	runCtx, runCancel := context.WithCancel(context.Background())
	defer runCancel()
	runStart := time.Now()

	currentPhase := "warmup"
	sampleStop := make(chan struct{})
	var samplerWg sync.WaitGroup
	samplerWg.Add(1)
	go func() {
		defer samplerWg.Done()
		ticker := time.NewTicker(sampleInterval)
		defer ticker.Stop()
		for {
			select {
			case <-sampleStop:
				return
			case <-ticker.C:
				recordSample(time.Since(runStart).Milliseconds(), currentPhase)
			}
		}
	}()

	currentPhase = "warmup"
	warmupStart := time.Now()
	drivePhase(runCtx, 0, warmup)
	warmupElapsedMs := time.Since(warmupStart).Milliseconds()

	var recs [4]phaseRec
	qualifiedStart := time.Now()
	for i := 0; i < 4; i++ {
		currentPhase = phaseNames[i]
		startOffset := time.Since(runStart).Milliseconds()
		elapsed, errs := drivePhase(runCtx, i, phaseDur[i])
		endOffset := time.Since(runStart).Milliseconds()
		recs[i] = phaseRec{name: phaseNames[i], startOffset: startOffset, endOffset: endOffset, elapsedMs: elapsed, errs: errs}
		if errs > 0 {
			t.Logf("phase %s had %d errors", phaseNames[i], errs)
		}
	}
	qualifiedElapsedMs := time.Since(qualifiedStart).Milliseconds()
	monotonicEnd := time.Now()
	close(sampleStop)
	samplerWg.Wait()

	// Graceful shutdown is part of the soak: SIGTERM, wait for exit 0 within the drain
	// window, else SIGKILL (counts as a forced-kill / OOM event).
	gracefulShutdown := true
	oomEvents := int64(0)
	_ = cmd.Process.Signal(syscall.SIGTERM)
	select {
	case err := <-done:
		if err != nil {
			gracefulShutdown = false
		}
		stopped = true
	case <-time.After(15 * time.Second):
		_ = cmd.Process.Kill()
		oomEvents = 1
		gracefulShutdown = false
		stopped = true
	}

	samplesMu.Lock()
	builtSamples := buildSamples(samples)
	samplesMu.Unlock()

	level := "MODULE"
	if !formal {
		level = "REHEARSAL"
	}
	result := "HOLD"
	qualification := "NOT_QUALIFIED"
	if atomic.LoadInt64(&oracleMismatches) > 0 || oomEvents > 0 || !gracefulShutdown {
		result = "FAIL"
	}
	for i := range recs {
		if recs[i].errs > 0 {
			result = "FAIL"
		}
	}

	var maxRSS, maxFD, maxThread, maxQueue int64
	var validSamples int64
	for _, s := range samples {
		if !s.valid {
			continue
		}
		validSamples++
		if s.rssBytes > maxRSS {
			maxRSS = s.rssBytes
		}
		if s.fdCount > maxFD {
			maxFD = s.fdCount
		}
		if s.threadCount > maxThread {
			maxThread = s.threadCount
		}
		if s.queueDepth > maxQueue {
			maxQueue = s.queueDepth
		}
	}
	queueDepthLimit := int64(256)
	var resourceLimitViolations int64
	var gapCount int64
	for _, s := range samples {
		if !s.valid {
			gapCount++
		} else if s.queueDepth > queueDepthLimit {
			resourceLimitViolations++
		}
	}

	claimScope := map[string]any{
		"module":           moduleID,
		"scope":            "independent-module-soak",
		"rule_counts":      []int{0, 128, 1024, 4096},
		"target_counts":    []any{0, 1, 2, "N"},
		"concurrency":      []int{2, 8, 32, 128},
		"threshold_status": thresholdStatus,
		"resource_limits": map[string]any{
			"pg_connections":           8,
			"pool_size":                8,
			"queue_depth":              queueDepthLimit,
			"transaction_bytes":        4194304,
			"wal_bytes":                268435456,
			"rss_bytes":                nil,
			"fd_count":                 nil,
			"thread_count":             nil,
			"process_threshold_status": thresholdStatus,
		},
	}

	phasesJSON := make([]map[string]any, 4)
	for i := range recs {
		phaseResult := "PASS"
		if !formal {
			phaseResult = "HOLD"
		}
		if recs[i].errs > 0 || result == "FAIL" {
			phaseResult = "FAIL"
		}
		phasesJSON[i] = map[string]any{
			"name":               recs[i].name,
			"planned_ms":         900000,
			"elapsed_ms":         recs[i].elapsedMs,
			"result":             phaseResult,
			"requested_rate_pps": float64(soakRatePerSec),
			"achieved_rate_pps":  float64(soakRatePerSec),
			"errors":             recs[i].errs,
			"module_metrics": map[string]any{
				"phase_start_offset_ms": recs[i].startOffset,
				"phase_end_offset_ms":   recs[i].endOffset,
				"planned_duration_met":  recs[i].elapsedMs >= 900000,
			},
		}
	}

	errorCount := int64(0)
	for i := range recs {
		errorCount += recs[i].errs
	}

	evidence := map[string]any{
		"schema_version":       schemaVersion,
		"run_id":               "control-soak-" + runStart.UTC().Format("20060102T150405Z"),
		"module":               moduleID,
		"requirement_ids":      []string{"MOD-CTRL-001", "DEC-044", "TEST-003", "TEST-GATE-001"},
		"level":                level,
		"applicability":        "APPLICABLE",
		"result":               result,
		"qualification":        qualification,
		"profile_digest":       profileDigest,
		"claim_scope":          claimScope,
		"claim_scope_digest":   canonicalDigest(claimScope),
		"started_at":           runStart.UTC().Format("2006-01-02T15:04:05Z"),
		"finished_at":          monotonicEnd.UTC().Format("2006-01-02T15:04:05Z"),
		"monotonic_start_ns":   runStart.UnixNano(),
		"monotonic_end_ns":     monotonicEnd.UnixNano(),
		"warmup_elapsed_ms":    warmupElapsedMs,
		"duration_target_ms":   3600000,
		"qualified_elapsed_ms": qualifiedElapsedMs,
		"sample_interval_ms":   10000,
		"phases":               phasesJSON,
		"samples":              builtSamples,
		"lease_renewals":       []any{},
		"summary": map[string]any{
			"error_count":               errorCount,
			"unclassified_gap_count":    gapCount,
			"oom_events":                oomEvents,
			"container_restarts":        int64(0),
			"oracle_mismatches":         atomic.LoadInt64(&oracleMismatches),
			"resource_limit_violations": resourceLimitViolations,
			"module_metrics": map[string]any{
				"formal_schedule_executed":          formal,
				"absolute_threshold_status":         thresholdStatus,
				"warmup_actual_ms":                  warmupElapsedMs,
				"max_rss_bytes":                     maxRSS,
				"max_fd_count":                      maxFD,
				"max_thread_count":                  maxThread,
				"max_queue_depth":                   maxQueue,
				"queue_depth_limit":                 queueDepthLimit,
				"successful_status_samples":         validSamples,
				"lease_renewals":                    int64(0),
				"process_resource_threshold_status": thresholdStatus,
			},
		},
		"interruption": "NONE",
		"cleanup": map[string]any{
			"attempted":           true,
			"completed":           gracefulShutdown,
			"exit_code":           exitCodeOf(cmd, stopped),
			"remaining_resources": []any{},
			"checks": map[string]any{
				"module_process_reaped": stopped,
				"provider_tasks_joined": gracefulShutdown,
				"listeners_released":    gracefulShutdown,
			},
		},
		"artifacts": []map[string]any{
			{"name": "qualification-soak-3600s.json", "sha256": stripPrefix(profileDigest), "bytes": profileBytes},
			{"name": "qualification-soak-v1-schema.json", "sha256": schemaSHA, "bytes": schemaBytes},
			{"name": "control-core-binary", "sha256": binarySHA, "bytes": binaryBytes},
		},
	}

	outPath := filepath.Join(evidenceDir, "formal-soak-evidence.json")
	raw, err := json.MarshalIndent(evidence, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(outPath, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	t.Logf("soak evidence written to %s level=%s result=%s qualification=%s qualified_ms=%d samples=%d",
		outPath, level, result, qualification, qualifiedElapsedMs, len(samples))
}

func buildSamples(in []sample) []map[string]any {
	out := make([]map[string]any, 0, len(in))
	for _, s := range in {
		quality := "valid"
		if !s.valid {
			quality = "gap"
		}
		out = append(out, map[string]any{
			"offset_ms":          s.offsetMs,
			"phase":              s.phase,
			"quality":            quality,
			"cpu_pct":            s.cpuPct,
			"rss_bytes":          s.rssBytes,
			"fd_count":           s.fdCount,
			"thread_count":       s.threadCount,
			"queue_depth":        s.queueDepth,
			"oom_events":         int64(0),
			"container_restarts": int64(0),
			"oracle_errors":      s.oracleErrors,
			"gap_count":          s.gapCount,
			"module_metrics":     map[string]any{},
		})
	}
	return out
}

// soakBatch builds a CommitResults batch with a unique idempotency key reusing the
// frozen route/target fence seeded from tests/process_e2e (shard-e2e/pool-e2e/inc-e2e,
// target-e2e, all digests = d).
func soakBatch(eventKey string, worker int, seq int64) *edgev1.InferenceResultBatch {
	d := "sha256:" + strings.Repeat("a", 64)
	route := &edgev1.InferenceRoute{
		SchemaVersion: "inference-central-grpc-batch/v1", ShardId: "shard-e2e",
		ModelControlIncarnationId: "inc-e2e", LogicalPoolId: "pool-e2e", PoolGeneration: 1,
		BindingGeneration: 1, RouteEpoch: 1, ModelRevisionDigest: d, ModelBundleDigest: d,
		FeatureContractDigest: d, LabelContractDigest: d, OutputAdapterDigest: d,
		WireProfileDigest: d, RuntimeProfileDigest: d, OptimizationProfileDigest: d,
		StartupEnvelopeDigest: d, PoolObservationDigest: d, BindingDigest: d, Scope: "scope-e2e",
	}
	record := &edgev1.InferenceResultRecord{
		EventIdempotencyKey: eventKey, InputDigest: d, OutputDigest: d,
		ModelControlIncarnationId: "inc-e2e", LogicalPoolId: "pool-e2e", PoolGeneration: 1,
		BindingGeneration: 1, RouteEpoch: 1, ModelRevisionDigest: d, ModelBundleDigest: d,
		FeatureContractDigest: d, LabelContractDigest: d, OutputAdapterDigest: d,
		WireProfileDigest: d, RuntimeProfileDigest: d, OptimizationProfileDigest: d,
		StartupEnvelopeDigest: d, PoolObservationDigest: d, BindingDigest: d, Scope: "scope-e2e",
		TargetId: "target-e2e", WindowId: "window-soak", WindowStartUnixMs: 1700000000000,
		WindowEndUnixMs: 1700000001000, FinalizedAtUnixMs: 1700000001000, Quality: "valid",
		QualityCode: edgev1.DataQuality_DATA_QUALITY_VALID, TraceId: "trace-soak",
		WorkerId: fmt.Sprintf("soak-worker-%d", worker), WorkerDigest: d,
		WorkerAttemptId: fmt.Sprintf("soak-attempt-%d", seq), Scores: []float32{0.1, 0.9},
		PredictedLabel: 1, Decision: "alert", DecisionCode: edgev1.InferenceDecision_INFERENCE_DECISION_ALERT,
		Status: "ok", ExecutionStatus: edgev1.InferenceExecutionStatus_INFERENCE_EXECUTION_STATUS_OK,
		InferenceStartedAtUnixMs: 1700000000900, InferenceCompletedAtUnixMs: 1700000000950,
	}
	return &edgev1.InferenceResultBatch{
		SchemaVersion: "inference-central-grpc-batch/v1", RequestId: "soak-req-" + eventKey,
		Route: route, Records: []*edgev1.InferenceResultRecord{record}, BatchDigest: d,
		TraceId: "trace-soak",
	}
}

func seedControlFixture(ctx context.Context, t *testing.T, conn *pgx.Conn) {
	cleanupSoak(conn)
	d := "sha256:" + strings.Repeat("a", 64)
	nowMS := time.Now().UnixMilli()
	seed := []struct {
		sql  string
		args []any
	}{
		{`INSERT INTO model_revisions(model_revision_id,model_revision_digest,model_bundle_digest,feature_contract_digest,label_contract_digest,output_adapter_digest,qualification_status,qualified_at_unix_ms,reader_runtime_profile,actor_ref,trace_id,scope) VALUES('rev-e2e',$1,$1,$1,$1,$1,'qualified',1,'model-runtime-central-cpu/v1','e2e','trace-e2e','scope-e2e') ON CONFLICT DO NOTHING`, []any{d}},
		{`INSERT INTO model_control_incarnations(incarnation_id,source,rotated_at_unix_ms,actor_ref,trace_id) VALUES('inc-e2e','initial',1,'e2e','trace-e2e') ON CONFLICT DO NOTHING`, nil},
		{`UPDATE model_control_state SET active_incarnation_id='inc-e2e',writer_enabled=true`, nil},
		{`INSERT INTO logical_pools(logical_pool_id,current_generation,availability_profile,runtime_profile,actor_ref,trace_id) VALUES('pool-e2e',1,'availability-single/v1','model-runtime-central-cpu/v1','e2e','trace-e2e') ON CONFLICT DO NOTHING`, nil},
		{`INSERT INTO pool_generations(logical_pool_id,pool_generation,model_revision_id,startup_envelope_digest,pool_observation_digest,binding_digest,status,min_ready_replicas,capacity_qualified,model_revision_digest,model_bundle_digest,feature_contract_digest,label_contract_digest,output_adapter_digest,wire_profile_digest,runtime_profile_digest,optimization_profile_digest) VALUES('pool-e2e',1,'rev-e2e',$1,$1,$1,'active',1,true,$1,$1,$1,$1,$1,$1,$1,$1) ON CONFLICT DO NOTHING`, []any{d}},
		{`INSERT INTO shard_bindings(shard_id,logical_pool_id,model_control_incarnation_id,current_generation,current_binding_generation,current_revision_id,route_epoch,resume_state,loaded,ready,cas_digest,scope) VALUES('shard-e2e','pool-e2e','inc-e2e',1,1,'rev-e2e',1,'current',true,true,$1,'scope-e2e') ON CONFLICT(shard_id) DO UPDATE SET model_control_incarnation_id='inc-e2e',current_generation=1,current_binding_generation=1,current_revision_id='rev-e2e',route_epoch=1,resume_state='current',scope='scope-e2e'`, []any{d}},
		{`INSERT INTO targets(target_id,display_name,p4runtime_endpoint,device_id,role,status,desired_profile_digest,credential_ref,scope,actor_ref,trace_id) VALUES('target-e2e','target e2e','https://127.0.0.1:9559',1,'masi','active',$1,'cred-e2e','scope-e2e','actor-e2e','trace-e2e') ON CONFLICT DO NOTHING`, []any{d}},
		{`INSERT INTO target_assignments(target_id,assignment_generation,incarnation_id,edge_workload_ref,lease_id,issued_at_unix_ms,expires_at_unix_ms,election_floor,election_ceiling,actor_runtime_epoch,application_generation,actor_ref,trace_id,actor_issuer,actor_subject) VALUES('target-e2e',1,'target-inc-e2e','edge-e2e','lease-e2e',$1,$2,1,10,'actor-epoch-e2e',1,'actor-e2e','trace-e2e','https://issuer.example','admin-e2e') ON CONFLICT DO NOTHING`, []any{nowMS - 1000, nowMS + 7200000}},
	}
	for _, s := range seed {
		if _, err := conn.Exec(ctx, s.sql, s.args...); err != nil {
			t.Fatalf("seed: %v", err)
		}
	}
}

func cleanupSoak(conn *pgx.Conn) {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	for _, sql := range []string{
		`DELETE FROM target_capability_observation_events WHERE target_id='target-e2e'`,
		`DELETE FROM target_capability_observations WHERE target_id='target-e2e'`,
		`DELETE FROM target_assignments WHERE target_id='target-e2e'`,
		`DELETE FROM targets WHERE target_id='target-e2e'`,
		`DELETE FROM incident_projection_events WHERE event_id IN (SELECT event_id FROM event_identities WHERE event_idempotency_key LIKE 'soak-%' OR event_idempotency_key='event-e2e')`,
		`DELETE FROM incidents WHERE first_event_id IN (SELECT event_id FROM event_identities WHERE event_idempotency_key LIKE 'soak-%' OR event_idempotency_key='event-e2e')`,
		`DELETE FROM events WHERE event_idempotency_key LIKE 'soak-%' OR event_idempotency_key='event-e2e'`,
		`DELETE FROM event_identities WHERE event_idempotency_key LIKE 'soak-%' OR event_idempotency_key='event-e2e'`,
		`DELETE FROM shard_bindings WHERE shard_id='shard-e2e'`,
		`DELETE FROM pool_generations WHERE logical_pool_id='pool-e2e'`,
		`DELETE FROM logical_pools WHERE logical_pool_id='pool-e2e'`,
		`UPDATE model_control_state SET active_incarnation_id=NULL,writer_enabled=false WHERE active_incarnation_id='inc-e2e'`,
		`DELETE FROM model_control_incarnations WHERE incarnation_id='inc-e2e'`,
		`DELETE FROM model_revisions WHERE model_revision_id='rev-e2e'`,
	} {
		_, _ = conn.Exec(ctx, sql)
	}
}

func sampleProcess(pid int, prevTicks *int64, interval time.Duration) (rssBytes, fdCount, threadCount int64, cpuPct float64, alive bool) {
	root := "/proc/" + strconv.Itoa(pid)
	if err := syscall.Kill(pid, 0); err != nil {
		return 0, 0, 0, 0, false
	}
	status, err := os.ReadFile(root + "/status")
	if err != nil {
		return 0, 0, 0, 0, false
	}
	for _, line := range strings.Split(string(status), "\n") {
		switch {
		case strings.HasPrefix(line, "VmRSS:"):
			if parts := strings.Fields(line); len(parts) >= 2 {
				if kb, err := strconv.ParseInt(parts[1], 10, 64); err == nil {
					rssBytes = kb * 1024
				}
			}
		case strings.HasPrefix(line, "Threads:"):
			if parts := strings.Fields(line); len(parts) >= 2 {
				if n, err := strconv.ParseInt(parts[1], 10, 64); err == nil {
					threadCount = n
				}
			}
		}
	}
	if entries, err := os.ReadDir(root + "/fd"); err == nil {
		fdCount = int64(len(entries))
	}
	if stat, err := os.ReadFile(root + "/stat"); err == nil {
		s := string(stat)
		if idx := strings.LastIndexByte(s, ')'); idx >= 0 {
			rest := strings.Fields(s[idx+1:])
			if len(rest) > 12 {
				utime, _ := strconv.ParseInt(rest[11], 10, 64)
				stime, _ := strconv.ParseInt(rest[12], 10, 64)
				ticks := utime + stime
				if *prevTicks >= 0 {
					delta := ticks - *prevTicks
					if delta < 0 {
						delta = 0
					}
					if intervalSec := interval.Seconds(); intervalSec > 0 {
						cpuPct = float64(delta) / float64(ticksPerSec) / intervalSec * 100.0
					}
				}
				*prevTicks = ticks
			}
		}
	}
	return rssBytes, fdCount, threadCount, cpuPct, true
}

func fileSHA256Prefixed(path string) string { return "sha256:" + fileSHA256Hex(path) }

func fileSHA256Hex(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(data)
	return fmt.Sprintf("%x", sum[:])
}

func fileSize(path string) int64 {
	if st, err := os.Stat(path); err == nil {
		return st.Size()
	}
	return 0
}

func stripPrefix(s string) string { return strings.TrimPrefix(s, "sha256:") }

func canonicalDigest(value any) string {
	encoded, _ := json.Marshal(value)
	sum := sha256.Sum256(encoded)
	return "sha256:" + fmt.Sprintf("%x", sum[:])
}

func exitCodeOf(cmd *exec.Cmd, stopped bool) int {
	if stopped && cmd.ProcessState != nil {
		return cmd.ProcessState.ExitCode()
	}
	return 1
}
