package process_e2e

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	edgev1 "masi-nids/control-go/internal/grpc/edgev1"
	targetdomain "masi-nids/control-go/internal/target"
)

func TestRealControlProcessCommitResults(t *testing.T) {
	dsn := os.Getenv("MASI_CONTROL_E2E_DSN")
	binary := os.Getenv("MASI_CONTROL_E2E_BINARY")
	configPath := os.Getenv("MASI_CONTROL_E2E_CONFIG")
	if dsn == "" || binary == "" || configPath == "" {
		if os.Getenv("MASI_CONTROL_E2E_REQUIRED") == "1" {
			t.Fatal("real-process E2E required but DSN/BINARY/CONFIG is missing")
		}
		t.Skip("set MASI_CONTROL_E2E_DSN/BINARY/CONFIG for real-process E2E")
	}
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
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close(context.Background())
	cleanupSeed := func() {
		cleanCtx, cleanCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanCancel()
		for _, sql := range []string{
			`DELETE FROM target_capability_observation_events WHERE target_id='target-e2e'`,
			`DELETE FROM target_capability_observations WHERE target_id='target-e2e'`,
			`DELETE FROM target_assignments WHERE target_id='target-e2e'`,
			`DELETE FROM targets WHERE target_id='target-e2e'`,
			`DELETE FROM incident_projection_events WHERE event_id IN (SELECT event_id FROM event_identities WHERE event_idempotency_key='event-e2e')`,
			`DELETE FROM incidents WHERE first_event_id IN (SELECT event_id FROM event_identities WHERE event_idempotency_key='event-e2e')`,
			`DELETE FROM events WHERE event_idempotency_key='event-e2e'`,
			`DELETE FROM event_identities WHERE event_idempotency_key='event-e2e'`,
			`DELETE FROM shard_bindings WHERE shard_id='shard-e2e'`,
			`DELETE FROM pool_generations WHERE logical_pool_id='pool-e2e'`,
			`DELETE FROM logical_pools WHERE logical_pool_id='pool-e2e'`,
			`UPDATE model_control_state SET active_incarnation_id=NULL,writer_enabled=false WHERE active_incarnation_id='inc-e2e'`,
			`DELETE FROM model_control_incarnations WHERE incarnation_id='inc-e2e'`,
			`DELETE FROM model_revisions WHERE model_revision_id='rev-e2e'`,
		} {
			_, _ = conn.Exec(cleanCtx, sql)
		}
	}
	cleanupSeed()
	defer cleanupSeed()
	d := "sha256:" + strings.Repeat("a", 64)
	seed := []string{
		`INSERT INTO model_revisions(model_revision_id,model_revision_digest,model_bundle_digest,feature_contract_digest,label_contract_digest,output_adapter_digest,qualification_status,qualified_at_unix_ms,reader_runtime_profile,actor_ref,trace_id,scope) VALUES('rev-e2e',$1,$1,$1,$1,$1,'qualified',1,'model-runtime-central-cpu/v1','e2e','trace-e2e','scope-e2e') ON CONFLICT DO NOTHING`,
		`INSERT INTO model_control_incarnations(incarnation_id,source,rotated_at_unix_ms,actor_ref,trace_id) VALUES('inc-e2e','initial',1,'e2e','trace-e2e') ON CONFLICT DO NOTHING`,
		`INSERT INTO model_control_state (singleton, writer_enabled) VALUES (true, false) ON CONFLICT (singleton) DO NOTHING`,
		`UPDATE model_control_state SET active_incarnation_id='inc-e2e',writer_enabled=true`,
		`INSERT INTO logical_pools(logical_pool_id,current_generation,availability_profile,runtime_profile,actor_ref,trace_id) VALUES('pool-e2e',1,'availability-single/v1','model-runtime-central-cpu/v1','e2e','trace-e2e') ON CONFLICT DO NOTHING`,
		`INSERT INTO pool_generations(logical_pool_id,model_control_incarnation_id,pool_generation,model_revision_id,startup_envelope_digest,pool_observation_digest,binding_digest,status,min_ready_replicas,capacity_qualified,model_revision_digest,model_bundle_digest,feature_contract_digest,label_contract_digest,output_adapter_digest,wire_profile_digest,runtime_profile_digest,optimization_profile_digest) VALUES('pool-e2e','inc-e2e',1,'rev-e2e',$1,$1,$1,'active',1,true,$1,$1,$1,$1,$1,$1,$1,$1) ON CONFLICT DO NOTHING`,
		`INSERT INTO shard_bindings(shard_id,logical_pool_id,model_control_incarnation_id,current_generation,current_binding_generation,current_revision_id,route_epoch,resume_state,loaded,ready,cas_digest,scope) VALUES('shard-e2e','pool-e2e','inc-e2e',1,1,'rev-e2e',1,'current',true,true,$1,'scope-e2e') ON CONFLICT(shard_id) DO UPDATE SET model_control_incarnation_id='inc-e2e',current_generation=1,current_binding_generation=1,current_revision_id='rev-e2e',route_epoch=1,resume_state='current',scope='scope-e2e'`,
	}
	for _, sql := range seed {
		var args []any
		if strings.Contains(sql, "$1") {
			args = []any{d}
		}
		if _, err := conn.Exec(ctx, sql, args...); err != nil {
			t.Fatalf("seed: %v", err)
		}
	}
	nowMS := time.Now().UnixMilli()
	if _, err := conn.Exec(ctx, `INSERT INTO targets(target_id,display_name,p4runtime_endpoint,device_id,role,status,
		desired_profile_digest,credential_ref,scope,actor_ref,trace_id)
		VALUES('target-e2e','target e2e','https://127.0.0.1:9559',1,'masi','active',$1,'cred-e2e','scope-e2e','actor-e2e','trace-e2e')`, d); err != nil {
		t.Fatalf("seed target: %v", err)
	}
	if _, err := conn.Exec(ctx, `INSERT INTO target_assignments(target_id,assignment_generation,incarnation_id,
		edge_workload_ref,lease_id,issued_at_unix_ms,expires_at_unix_ms,election_floor,election_ceiling,
		actor_runtime_epoch,application_generation,actor_ref,trace_id,actor_issuer,actor_subject)
		VALUES('target-e2e',1,'target-inc-e2e','edge-e2e','lease-e2e',$1,$2,1,10,'actor-epoch-e2e',1,
		'actor-e2e','trace-e2e','https://issuer.example','admin-e2e')`, nowMS-1000, nowMS+299000); err != nil {
		t.Fatalf("seed assignment: %v", err)
	}
	var logs bytes.Buffer
	cmd := exec.Command(binary, "--config", configPath)
	// Run from the control-go root so relative config paths (role_mapping_path,
	// contract_root) resolve regardless of the test binary's cwd.
	cmd.Dir = filepath.Dir(filepath.Dir(configPath))
	cmd.Stdout = &logs
	cmd.Stderr = &logs
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	stopped := false
	t.Cleanup(func() {
		if !stopped {
			_ = cmd.Process.Signal(syscall.SIGTERM)
			select {
			case <-done:
			case <-time.After(5 * time.Second):
				_ = cmd.Process.Kill()
			}
		}
	})
	ready := false
	for i := 0; i < 50; i++ {
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
	batch := validBatch(d)
	ack, err := client.CommitResults(ctx, batch)
	if err != nil {
		t.Fatalf("commit: %v\n%s", err, logs.String())
	}
	if ack.SchemaVersion != "canonical-event-ack/v1" ||
		!strings.HasPrefix(ack.AckBatchDigest, "sha256:") ||
		len(ack.Acknowledgements) != 1 || ack.Acknowledgements[0].Status != "committed" {
		t.Fatalf("unexpected ACK: %+v", ack)
	}
	replay, err := client.CommitResults(ctx, batch)
	if err != nil {
		t.Fatal(err)
	}
	if replay.Acknowledgements[0].Status != "idempotent" {
		t.Fatalf("replay status=%s", replay.Acknowledgements[0].Status)
	}
	conflictBatch := validBatch(d)
	conflictBatch.Records[0].OutputDigest = "sha256:" + strings.Repeat("b", 64)
	conflict, err := client.CommitResults(ctx, conflictBatch)
	if err != nil {
		t.Fatal(err)
	}
	if conflict.Acknowledgements[0].Status != "conflict" {
		t.Fatalf("conflict status=%s", conflict.Acknowledgements[0].Status)
	}
	var count int
	var inputDigest, outputDigest, commitStatus, canonicalID, incarnation string
	var decision, resultIdentityDigest, workerID string
	var routeEpoch, bindingGeneration int64
	if err := conn.QueryRow(ctx, `SELECT count(*) OVER(),input_digest,output_digest,commit_status,canonical_event_id,
	 model_control_incarnation_id,route_epoch,binding_generation,decision,result_identity_digest,worker_id
	 FROM events WHERE event_idempotency_key='event-e2e'`).Scan(
		&count, &inputDigest, &outputDigest, &commitStatus, &canonicalID, &incarnation, &routeEpoch,
		&bindingGeneration, &decision, &resultIdentityDigest, &workerID); err != nil || count != 1 ||
		inputDigest != d || outputDigest != d || commitStatus != "committed" || canonicalID != ack.Acknowledgements[0].CanonicalEventId ||
		incarnation != "inc-e2e" || routeEpoch != 1 || bindingGeneration != 1 || decision != "alert" ||
		!strings.HasPrefix(resultIdentityDigest, "sha256:") || workerID != "worker-e2e" {
		t.Fatalf("DB oracle mismatch count=%d status=%s canonical=%s err=%v", count, commitStatus, canonicalID, err)
	}
	var incidentCount int
	if err := conn.QueryRow(ctx, `SELECT count(*) FROM incidents i JOIN incident_projection_events p
	 ON p.incident_id=i.incident_id WHERE p.event_id=$1 AND i.status='open' AND i.severity='high'`, canonicalID).
		Scan(&incidentCount); err != nil || incidentCount != 1 {
		t.Fatalf("Incident projection oracle count=%d err=%v", incidentCount, err)
	}
	targetAck, err := client.PublishTargetStatus(ctx, validTargetStatusBatch(d, nowMS))
	if err != nil || targetAck.ReasonCode != "ACCEPTED" {
		t.Fatalf("target status publish ack=%+v err=%v", targetAck, err)
	}
	var observationID string
	var historyCount int
	if err := conn.QueryRow(ctx, `SELECT observation_id FROM target_capability_observations WHERE target_id='target-e2e'`).Scan(&observationID); err != nil {
		t.Fatal(err)
	}
	if err := conn.QueryRow(ctx, `SELECT count(*) FROM target_capability_observation_events WHERE target_id='target-e2e'`).Scan(&historyCount); err != nil {
		t.Fatal(err)
	}
	if observationID != "target-observation-e2e" || historyCount != 1 {
		t.Fatalf("target observation oracle current=%s history=%d", observationID, historyCount)
	}
	_ = cmd.Process.Signal(syscall.SIGTERM)
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("process shutdown: %v\n%s", err, logs.String())
		}
		stopped = true
	case <-time.After(8 * time.Second):
		t.Fatalf("process did not drain\n%s", logs.String())
	}
}

func validBatch(d string) *edgev1.InferenceResultBatch {
	route := &edgev1.InferenceRoute{SchemaVersion: "inference-route/v1", ShardId: "shard-e2e", ModelControlIncarnationId: "inc-e2e", LogicalPoolId: "pool-e2e", PoolGeneration: 1, BindingGeneration: 1, RouteEpoch: 1, ModelRevisionDigest: d, ModelBundleDigest: d, FeatureContractDigest: d, LabelContractDigest: d, OutputAdapterDigest: d, WireProfileDigest: d, RuntimeProfileDigest: d, OptimizationProfileDigest: d, StartupEnvelopeDigest: d, PoolObservationDigest: d, BindingDigest: d, Scope: "scope-e2e"}
	record := &edgev1.InferenceResultRecord{EventIdempotencyKey: "event-e2e", InputDigest: d, OutputDigest: d, ModelControlIncarnationId: "inc-e2e", LogicalPoolId: "pool-e2e", PoolGeneration: 1, BindingGeneration: 1, RouteEpoch: 1, ModelRevisionDigest: d, ModelBundleDigest: d, FeatureContractDigest: d, LabelContractDigest: d, OutputAdapterDigest: d, WireProfileDigest: d, RuntimeProfileDigest: d, OptimizationProfileDigest: d, StartupEnvelopeDigest: d, PoolObservationDigest: d, BindingDigest: d, Scope: "scope-e2e", TargetId: "target-e2e", WindowId: "window-e2e", WindowStartUnixMs: 1700000000000, WindowEndUnixMs: 1700000001000, FinalizedAtUnixMs: 1700000001000, Quality: "valid", QualityCode: edgev1.DataQuality_DATA_QUALITY_VALID, TraceId: "trace-e2e", WorkerId: "worker-e2e", WorkerDigest: d, WorkerAttemptId: "attempt-e2e", Scores: []float32{0.1, 0.9}, PredictedLabel: 1, Decision: "alert", DecisionCode: edgev1.InferenceDecision_INFERENCE_DECISION_ALERT, Status: "OK", ErrorCode: "NONE", ExecutionStatus: edgev1.InferenceExecutionStatus_INFERENCE_EXECUTION_STATUS_OK, InferenceStartedAtUnixMs: 1700000000900, InferenceCompletedAtUnixMs: 1700000000950}
	return &edgev1.InferenceResultBatch{SchemaVersion: "inference-central-grpc-batch/v1", RequestId: "request-e2e", Route: route, Records: []*edgev1.InferenceResultRecord{record}, BatchDigest: d, TraceId: "trace-e2e"}
}

func validTargetStatusBatch(d string, nowMS int64) *edgev1.TargetStatusBatch {
	observation := targetdomain.CapabilityObservation{
		ObservationID: "target-observation-e2e", TargetID: "target-e2e",
		TargetControlIncarnationID: "target-inc-e2e", AssignmentGeneration: 1,
		ActorRuntimeEpoch: "actor-epoch-e2e", ApplicationGeneration: 1,
		P4InfoDigest: d, PipelineDigest: d, ProfileDigest: d, CapacityDigest: d,
		CapacityAvailable: true, LeaseValid: true, P4Connected: true, Primary: true,
		PipelineExact: true, Freshness: "fresh", ReasonCode: "READY",
		ObservedAtUnixMS: nowMS, ExpiresAtUnixMS: nowMS + 75000, TraceID: "trace-target-e2e",
	}
	observation.ObservationDigest = targetdomain.ComputeCapabilityObservationDigest(observation)
	return &edgev1.TargetStatusBatch{
		SchemaVersion: "masi-target-status/v1", TraceId: observation.TraceID,
		BatchId: "target-status-batch-e2e", BatchDigest: d,
		Targets: []*edgev1.TargetStatus{{
			TargetId: observation.TargetID,
			Fence: &edgev1.Fence{TargetControlIncarnationId: observation.TargetControlIncarnationID,
				TargetAssignmentGeneration: 1, ActorRuntimeEpoch: observation.ActorRuntimeEpoch, ApplicationGeneration: 1},
			LeaseValid: true, P4Connected: true, Primary: true, PipelineExact: true,
			PipelineDigest: d, P4InfoDigest: d, ProfileDigest: d, CapacityDigest: d,
			CapacityAvailable: true, FreshnessCode: edgev1.FreshnessStatus_FRESHNESS_STATUS_FRESH,
			ReasonCode: "READY", ObservedAtUnixMs: nowMS, ExpiresAtUnixMs: nowMS + 75000,
			ObservationId: observation.ObservationID, ObservationDigest: observation.ObservationDigest,
		}},
	}
}
