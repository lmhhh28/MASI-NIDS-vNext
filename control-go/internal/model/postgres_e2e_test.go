package model

import (
	"context"
	"errors"
	"os"
	"strings"
	"testing"
	"time"

	"masi-nids/control-go/internal/config"
	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

// TestModelRevisionAuditPostgres exercises immutable registration,
// idempotency/conflict handling, and append-only revocation audit against the
// real PostgreSQL 18 test database. It is mandatory in the module E2E profile.
func TestModelRevisionAuditPostgres(t *testing.T) {
	configPath := os.Getenv("MASI_CONTROL_E2E_CONFIG")
	dsn := os.Getenv("MASI_CONTROL_E2E_DSN")
	if dsn == "" || configPath == "" {
		if os.Getenv("MASI_CONTROL_E2E_REQUIRED") == "1" {
			t.Fatal("model revision PostgreSQL E2E required but DSN/CONFIG is missing")
		}
		t.Skip("set MASI_CONTROL_E2E_DSN/CONFIG for model revision PostgreSQL E2E")
	}
	cfg, err := config.Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.PostgreSQLDSN != dsn {
		t.Fatal("model revision PostgreSQL E2E DSN differs from validated config")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool, err := db.New(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()

	const revisionID = "model-revision-audit-e2e"
	const conflictingID = "model-revision-audit-e2e-conflict"
	cleanup := func() {
		cleanCtx, cleanCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanCancel()
		_, _ = pool.Exec(cleanCtx, `DELETE FROM model_revision_events WHERE model_revision_id IN ($1,$2)`, revisionID, conflictingID)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM model_revisions WHERE model_revision_id IN ($1,$2)`, revisionID, conflictingID)
	}
	cleanup()
	defer cleanup()

	digest := func(ch string) string { return "sha256:" + strings.Repeat(ch, 64) }
	registrar := security.Actor{Issuer: "https://issuer.example", Subject: "model-admin-e2e"}
	revoker := security.Actor{Issuer: "https://issuer.example", Subject: "model-revoker-e2e"}
	revision := ModelRevision{
		ModelRevisionID: revisionID, ModelRevisionDigest: digest("a"), ModelBundleDigest: digest("b"),
		FeatureContractDigest: digest("c"), LabelContractDigest: digest("d"), OutputAdapterDigest: digest("e"),
		QualificationStatus: Qualified, ReaderRuntimeProfile: "model-runtime-central-cpu/v1",
		Scope: "scope-e2e", Actor: registrar, TraceID: "trace-model-register-e2e",
	}
	service := NewRevisionService(pool)
	service.now = func() time.Time { return time.UnixMilli(10_000) }

	registered, err := service.RegisterRevision(ctx, revision)
	if err != nil {
		t.Fatal(err)
	}
	if registered.QualifiedAtUnixMS != 10_000 {
		t.Fatalf("qualified_at=%d, want 10000", registered.QualifiedAtUnixMS)
	}
	replayed, err := service.RegisterRevision(ctx, revision)
	if err != nil || replayed.ModelRevisionDigest != revision.ModelRevisionDigest {
		t.Fatalf("idempotent registration returned %+v err=%v", replayed, err)
	}

	conflict := revision
	conflict.ModelBundleDigest = digest("f")
	if _, err := service.RegisterRevision(ctx, conflict); err == nil || !strings.Contains(err.Error(), "immutable revision identity conflict") {
		t.Fatalf("same ID with changed immutable digest must conflict, got %v", err)
	}
	conflict = revision
	conflict.ModelRevisionID = conflictingID
	if _, err := service.RegisterRevision(ctx, conflict); err == nil || !strings.Contains(err.Error(), "immutable revision identity conflict") {
		t.Fatalf("same digest with changed ID must conflict, got %v", err)
	}

	service.now = func() time.Time { return time.UnixMilli(20_000) }
	if err := service.Revoke(ctx, revisionID, revoker, "SECURITY_REVOKE", "trace-model-revoke-e2e"); err != nil {
		t.Fatal(err)
	}
	if err := service.Revoke(ctx, revisionID, revoker, "SECURITY_REVOKE", "trace-model-revoke-e2e"); err != nil {
		t.Fatalf("duplicate revoke must be idempotent: %v", err)
	}

	var status string
	var eventCount, badActorCount int
	if err := pool.QueryRow(ctx, `SELECT qualification_status FROM model_revisions WHERE model_revision_id=$1`, revisionID).Scan(&status); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT count(*),count(*) FILTER (
		WHERE (event_type='REGISTERED' AND (actor_issuer<>$2 OR actor_subject<>$3))
		   OR (event_type='REVOKED' AND (actor_issuer<>$4 OR actor_subject<>$5)))
		FROM model_revision_events WHERE model_revision_id=$1`, revisionID,
		registrar.Issuer, registrar.Subject, revoker.Issuer, revoker.Subject).Scan(&eventCount, &badActorCount); err != nil {
		t.Fatal(err)
	}
	if status != string(Revoked) || eventCount != 2 || badActorCount != 0 {
		t.Fatalf("status=%s events=%d bad_actor=%d, want revoked/2/0", status, eventCount, badActorCount)
	}
}

// TestRolloutPostgres verifies the complete durable route sequence against a
// real database. The boundary fakes only emulate external public services; all
// operation, pool-generation, and current/previous facts are real PostgreSQL.
func TestRolloutPostgres(t *testing.T) {
	configPath := os.Getenv("MASI_CONTROL_E2E_CONFIG")
	dsn := os.Getenv("MASI_CONTROL_E2E_DSN")
	if dsn == "" || configPath == "" {
		if os.Getenv("MASI_CONTROL_E2E_REQUIRED") == "1" {
			t.Fatal("model rollout PostgreSQL E2E required but DSN/CONFIG is missing")
		}
		t.Skip("set MASI_CONTROL_E2E_DSN/CONFIG for model rollout PostgreSQL E2E")
	}
	cfg, err := config.Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.PostgreSQLDSN != dsn {
		t.Fatal("model rollout PostgreSQL E2E DSN differs from validated config")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool, err := db.New(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()

	const (
		incarnationID = "model-incarnation-rollout-e2e"
		poolID        = "model-pool-rollout-e2e"
		shardID       = "model-shard-rollout-e2e"
		shardID2      = "model-shard-rollout-e2e-2"
		groupID       = "model-rollout-group-e2e"
		oldRevisionID = "model-revision-rollout-old-e2e"
		newRevisionID = "model-revision-rollout-new-e2e"
		operationID   = "model-operation-rollout-e2e"
	)
	cleanup := func() {
		cleanCtx, cleanCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanCancel()
		_, _ = pool.Exec(cleanCtx, `DELETE FROM model_pool_observations_current WHERE logical_pool_id=$1`, poolID)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM model_pool_observation_events WHERE logical_pool_id=$1`, poolID)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM model_rollout_group_shards WHERE group_id=$1`, groupID)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM model_rollout_groups WHERE group_id=$1`, groupID)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM model_rollout_operations WHERE logical_pool_id=$1`, poolID)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM shard_bindings WHERE shard_id IN ($1,$2)`, shardID, shardID2)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM pool_generations WHERE logical_pool_id=$1`, poolID)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM logical_pools WHERE logical_pool_id=$1`, poolID)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM target_assignments WHERE target_id IN ($1,$2)`, shardID, shardID2)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM targets WHERE target_id IN ($1,$2)`, shardID, shardID2)
		_, _ = pool.Exec(cleanCtx, `UPDATE model_control_state SET active_incarnation_id=NULL,writer_enabled=false WHERE singleton=true AND active_incarnation_id=$1`, incarnationID)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM model_control_incarnations WHERE incarnation_id=$1`, incarnationID)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM model_revision_events WHERE model_revision_id IN ($1,$2)`, oldRevisionID, newRevisionID)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM model_revisions WHERE model_revision_id IN ($1,$2)`, oldRevisionID, newRevisionID)
	}
	cleanup()
	defer cleanup()

	digest := func(ch string) string { return "sha256:" + strings.Repeat(ch, 64) }
	actor := security.Actor{Issuer: "https://issuer.example", Subject: "model-rollout-admin-e2e"}
	revisions := NewRevisionService(pool)
	oldRevision := ModelRevision{
		ModelRevisionID: oldRevisionID, ModelRevisionDigest: digest("0"), ModelBundleDigest: digest("1"),
		FeatureContractDigest: digest("2"), LabelContractDigest: digest("3"), OutputAdapterDigest: digest("4"),
		QualificationStatus: Qualified, ReaderRuntimeProfile: "model-runtime-central-cpu/v1",
		Scope: "scope-model-e2e", Actor: actor, TraceID: "trace-model-old-e2e",
	}
	newRevision := ModelRevision{
		ModelRevisionID: newRevisionID, ModelRevisionDigest: digest("5"), ModelBundleDigest: digest("6"),
		FeatureContractDigest: digest("7"), LabelContractDigest: digest("8"), OutputAdapterDigest: digest("9"),
		QualificationStatus: Qualified, ReaderRuntimeProfile: "model-runtime-central-cpu/v1",
		Scope: "scope-model-e2e", Actor: actor, TraceID: "trace-model-new-e2e",
	}
	if _, err := revisions.RegisterRevision(ctx, oldRevision); err != nil {
		t.Fatal(err)
	}
	if _, err := revisions.RegisterRevision(ctx, newRevision); err != nil {
		t.Fatal(err)
	}
	nowMS := time.Now().UnixMilli()
	if _, err := pool.Exec(ctx, `INSERT INTO targets(target_id,display_name,p4runtime_endpoint,device_id,
		role,status,desired_profile_digest,credential_ref,scope,actor_ref,trace_id)
		VALUES($1,'model shard e2e','https://127.0.0.1:9561',2,'primary','active',$2,
		'credential-model-e2e','scope-model-e2e',$3,'trace-target-model-e2e')`, shardID, digest("a"), actor.String()); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO target_assignments(target_id,assignment_generation,
		incarnation_id,edge_workload_ref,lease_id,issued_at_unix_ms,expires_at_unix_ms,election_floor,
		election_ceiling,actor_runtime_epoch,application_generation,actor_ref,trace_id,actor_issuer,actor_subject)
		VALUES($1,1,'target-inc-model-e2e','edge-model-e2e','lease-model-e2e',$2,$3,1,10,
		'actor-runtime-model-e2e',1,$4,'trace-assignment-model-e2e',$5,$6)`, shardID,
		nowMS-1000, nowMS+299_000, actor.String(), actor.Issuer, actor.Subject); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO model_control_incarnations(
		incarnation_id,source,rotated_at_unix_ms,actor_ref,trace_id) VALUES($1,'initial',1000,$2,'trace-incarnation-e2e')`, incarnationID, actor.String()); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `UPDATE model_control_state SET active_incarnation_id=$1,writer_enabled=true,updated_at=now() WHERE singleton=true`, incarnationID); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO logical_pools(logical_pool_id,current_generation,
		availability_profile,runtime_profile,actor_ref,trace_id) VALUES($1,1,'availability-single/v1',
		'model-runtime-central-cpu/v1',$2,'trace-pool-e2e')`, poolID, actor.String()); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO pool_generations(logical_pool_id,model_control_incarnation_id,pool_generation,
			model_revision_id,startup_envelope_digest,pool_observation_digest,binding_digest,status,
		min_ready_replicas,capacity_qualified,model_revision_digest,model_bundle_digest,
		feature_contract_digest,label_contract_digest,output_adapter_digest,wire_profile_digest,
		runtime_profile_digest,optimization_profile_digest)
			VALUES($1,$2,1,$3,$4,$5,$6,'active',1,true,$7,$8,$9,$10,$11,$12,$13,$14)`,
		poolID, incarnationID, oldRevisionID, digest("a"), digest("b"), digest("c"), oldRevision.ModelRevisionDigest,
		oldRevision.ModelBundleDigest, oldRevision.FeatureContractDigest, oldRevision.LabelContractDigest,
		oldRevision.OutputAdapterDigest, digest("d"), digest("e"), digest("f")); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO shard_bindings(shard_id,logical_pool_id,
		model_control_incarnation_id,current_generation,current_binding_generation,current_revision_id,
		route_epoch,resume_state,loaded,ready,cas_digest,scope)
		VALUES($1,$2,$3,1,1,$4,1,'current',true,true,$5,'scope-model-e2e')`,
		shardID, poolID, incarnationID, oldRevisionID, digest("f")); err != nil {
		t.Fatal(err)
	}

	deployment := &rolloutDeploymentFake{availability: "availability-single/v1", endpoint: "https://inference.test:9443"}
	edge := &rolloutEdgeFake{}
	service := NewRolloutService(pool, deployment, edge)
	request := RolloutRequest{
		OperationID: operationID, ShardID: shardID, LogicalPoolID: poolID, TargetGeneration: 2,
		TargetRevisionID: newRevisionID, Kind: OpRollout, ActorRef: actor.String(), ReasonCode: "MODEL_ROLLOUT",
		TraceID: "trace-model-rollout-e2e", Scope: "scope-model-e2e",
		WireProfile: "inference-central-grpc-batch/v1", WireProfileDigest: digest("a"),
		RuntimeProfileDigest: digest("b"), OptimizationProfileDigest: digest("c"),
		AvailabilityProfile: "availability-single/v1", MinReadyReplicas: 1,
		ModelControlIncarnationID: incarnationID,
	}
	outcome, err := service.Rollout(ctx, request)
	if err != nil {
		t.Fatal(err)
	}
	if outcome.Status != OpApplied || outcome.NewGeneration != 2 || outcome.RouteEpoch != 2 || outcome.ResumeState != ResumeCurrent {
		t.Fatalf("rollout outcome=%+v", outcome)
	}
	if got := strings.Join(append(deployment.calls, edge.calls...), ","); got != "stage,start,capacity,prepare,commit,resume" {
		t.Fatalf("external sequence=%s", got)
	}
	if edge.route.OperationID != operationID || edge.route.BindingGeneration != 2 ||
		edge.route.ExpectedBindingGeneration != 1 || edge.route.ProposedBindingGeneration != 2 ||
		edge.route.RouteEpoch != 2 || edge.route.Endpoint != deployment.endpoint ||
		edge.route.ModelRevisionDigest != newRevision.ModelRevisionDigest {
		t.Fatalf("committed route identity mismatch: %+v", edge.route)
	}

	var currentGen, previousGen, bindingGen, previousBindingGen, routeEpoch int64
	var currentRevision, previousRevision, resumeState, operationStatus, newPoolStatus, oldPoolStatus string
	if err := pool.QueryRow(ctx, `SELECT current_generation,previous_generation,current_binding_generation,
		previous_binding_generation,current_revision_id,previous_revision_id,route_epoch,resume_state
		FROM shard_bindings WHERE shard_id=$1`, shardID).Scan(&currentGen, &previousGen, &bindingGen,
		&previousBindingGen, &currentRevision, &previousRevision, &routeEpoch, &resumeState); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT status FROM model_rollout_operations WHERE operation_id=$1`, operationID).Scan(&operationStatus); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT status FROM pool_generations WHERE logical_pool_id=$1 AND model_control_incarnation_id=$2 AND pool_generation=2`, poolID, incarnationID).Scan(&newPoolStatus); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT status FROM pool_generations WHERE logical_pool_id=$1 AND model_control_incarnation_id=$2 AND pool_generation=1`, poolID, incarnationID).Scan(&oldPoolStatus); err != nil {
		t.Fatal(err)
	}
	if currentGen != 2 || previousGen != 1 || bindingGen != 2 || previousBindingGen != 1 ||
		currentRevision != newRevisionID || previousRevision != oldRevisionID || routeEpoch != 2 ||
		resumeState != string(ResumeCurrent) || operationStatus != string(OpApplied) ||
		newPoolStatus != string(PoolActive) || oldPoolStatus != string(PoolDraining) {
		t.Fatalf("binding/op/pools mismatch: current=%d previous=%d binding=%d/%d revisions=%s/%s epoch=%d resume=%s op=%s pools=%s/%s",
			currentGen, previousGen, bindingGen, previousBindingGen, currentRevision, previousRevision,
			routeEpoch, resumeState, operationStatus, newPoolStatus, oldPoolStatus)
	}

	// Add a second shard at the old generation, then roll both shards in a
	// frozen order to generation 3. The pool deployment must occur once; shard 2
	// reuses the exact persisted pool observation.
	if _, err := pool.Exec(ctx, `INSERT INTO targets(target_id,display_name,p4runtime_endpoint,device_id,
		role,status,desired_profile_digest,credential_ref,scope,actor_ref,trace_id)
		VALUES($1,'model shard e2e 2','https://127.0.0.1:9563',3,'primary','active',$2,
		'credential-model-e2e-2','scope-model-e2e',$3,'trace-target-model-e2e-2')`, shardID2, digest("a"), actor.String()); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO target_assignments(target_id,assignment_generation,
		incarnation_id,edge_workload_ref,lease_id,issued_at_unix_ms,expires_at_unix_ms,election_floor,
		election_ceiling,actor_runtime_epoch,application_generation,actor_ref,trace_id,actor_issuer,actor_subject)
		VALUES($1,1,'target-inc-model-e2e-2','edge-model-e2e','lease-model-e2e-2',$2,$3,11,20,
		'actor-runtime-model-e2e-2',1,$4,'trace-assignment-model-e2e-2',$5,$6)`, shardID2,
		nowMS-1000, nowMS+299_000, actor.String(), actor.Issuer, actor.Subject); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO shard_bindings(shard_id,logical_pool_id,
		model_control_incarnation_id,current_generation,current_binding_generation,current_revision_id,
		route_epoch,resume_state,loaded,ready,cas_digest,scope)
		VALUES($1,$2,$3,1,1,$4,1,'current',true,true,$5,'scope-model-e2e')`,
		shardID2, poolID, incarnationID, oldRevisionID, digest("e")); err != nil {
		t.Fatal(err)
	}
	deployment.calls = nil
	edge.calls = nil
	template := request
	template.OperationID = ""
	template.ShardID = ""
	template.TargetGeneration = 3
	group, err := service.CreateRolloutGroup(ctx, RolloutGroupRequest{GroupID: groupID,
		OrderedShards: []string{shardID, shardID2}, Template: template})
	if err != nil || group.Status != "planned" || len(group.Shards) != 2 {
		t.Fatalf("create rollout group=%+v err=%v", group, err)
	}
	group, err = service.AdvanceRolloutGroup(ctx, groupID)
	if err != nil {
		t.Fatal(err)
	}
	if group.Status != "completed" || group.NextIndex != 2 || group.Shards[0].Status != "applied" || group.Shards[1].Status != "applied" {
		t.Fatalf("completed rollout group=%+v", group)
	}
	if got := strings.Join(deployment.calls, ","); got != "stage,start,capacity" {
		t.Fatalf("pool generation must deploy exactly once across ordered shards, calls=%s", got)
	}
	var generation1, generation2 int64
	if err := pool.QueryRow(ctx, `SELECT current_generation FROM shard_bindings WHERE shard_id=$1`, shardID).Scan(&generation1); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT current_generation FROM shard_bindings WHERE shard_id=$1`, shardID2).Scan(&generation2); err != nil {
		t.Fatal(err)
	}
	if generation1 != 3 || generation2 != 3 {
		t.Fatalf("group shard generations=%d/%d, want 3/3", generation1, generation2)
	}

	postCAS := request
	postCAS.OperationID = "model-operation-recovery-post-cas-e2e"
	postCAS.ShardID = shardID
	postCAS.TargetGeneration = 4
	edge.failResume = 1
	postOutcome, postErr := service.Rollout(ctx, postCAS)
	if postErr == nil || postOutcome == nil || postOutcome.Status != OpResumePending {
		t.Fatalf("post-CAS resume loss outcome=%+v err=%v", postOutcome, postErr)
	}
	recovered, err := service.ReconcileOperation(ctx, postCAS.OperationID)
	if err != nil || recovered.Status != OpApplied || recovered.NewGeneration != 4 {
		t.Fatalf("post-CAS recovery=%+v err=%v", recovered, err)
	}

	preCAS := request
	preCAS.OperationID = "model-operation-recovery-pre-cas-e2e"
	preCAS.ShardID = shardID2
	preCAS.TargetGeneration = 5
	edge.failCommit = 1
	preOutcome, preErr := service.Rollout(ctx, preCAS)
	if preErr == nil || preOutcome == nil || preOutcome.Status != OpResumePending || preOutcome.NewGeneration != 3 {
		t.Fatalf("pre-CAS commit loss outcome=%+v err=%v", preOutcome, preErr)
	}
	recovered, err = service.ReconcileOperation(ctx, preCAS.OperationID)
	if err != nil || recovered.Status != OpApplied || recovered.NewGeneration != 5 {
		t.Fatalf("pre-CAS recovery=%+v err=%v", recovered, err)
	}
}

type rolloutDeploymentFake struct {
	availability string
	endpoint     string
	calls        []string
}

func (f *rolloutDeploymentFake) StagePool(_ context.Context, _ PoolGeneration) error {
	f.calls = append(f.calls, "stage")
	return nil
}

func (f *rolloutDeploymentFake) StartReplicas(_ context.Context, pool PoolGeneration) (Readback, error) {
	f.calls = append(f.calls, "start")
	return Readback{
		LogicalPoolID: pool.LogicalPoolID, PoolGeneration: pool.PoolGeneration,
		BindingGeneration: pool.BindingGeneration, ModelControlIncarnationID: pool.ModelControlIncarnationID,
		OperationID:         pool.OperationID,
		ModelRevisionDigest: pool.ModelRevisionDigest, ModelBundleDigest: pool.ModelBundleDigest,
		FeatureContractDigest: pool.FeatureContractDigest, LabelContractDigest: pool.LabelContractDigest,
		OutputAdapterDigest: pool.OutputAdapterDigest, WireProfileDigest: pool.WireProfileDigest,
		RuntimeProfileDigest: pool.RuntimeProfileDigest, OptimizationProfileDigest: pool.OptimizationProfileDigest,
		WireProfile: pool.WireProfile, RuntimeProfile: pool.RuntimeProfile,
		AvailabilityProfile: f.availability, ReadyReplicas: pool.MinReadyReplicas,
		ReadbackAttemptID: "readback-attempt-rollout-e2e", ObservedAtUnixMS: time.Now().UnixMilli(),
		StartupEnvelopeDigest: pool.StartupEnvelopeDigest, PoolObservationDigest: pool.PoolObservationDigest,
		BindingDigest: pool.BindingDigest, Endpoint: f.endpoint, TLSServerName: "inference.test",
		TLSIdentityRef:  "inference-client-e2e",
		EligibleWorkers: []WorkerIdentity{{WorkerID: "worker-rollout-e2e", WorkerDigest: "sha256:" + strings.Repeat("a", 64)}},
		Loaded:          true,
	}, nil
}

func (f *rolloutDeploymentFake) WarmupAndCapacity(_ context.Context, pool PoolGeneration) (CapacityResult, error) {
	f.calls = append(f.calls, "capacity")
	return CapacityResult{LogicalPoolID: pool.LogicalPoolID, PoolGeneration: pool.PoolGeneration, Qualified: true, MinReadyMet: true, ReasonCode: "QUALIFIED"}, nil
}

type rolloutEdgeFake struct {
	calls      []string
	route      InferenceRoute
	failCommit int
	failResume int
}

func (f *rolloutEdgeFake) PrepareRoute(_ context.Context, prepare RoutePrepare) (RouteReadback, error) {
	f.calls = append(f.calls, "prepare")
	return RouteReadback{ShardID: prepare.ShardID, State: "draining", RouteEpoch: prepare.ProposedRouteEpoch, ReasonCode: "ROUTE_DRAINING"}, nil
}

func (f *rolloutEdgeFake) CommitRoute(_ context.Context, route InferenceRoute, _ string) (RouteReadback, error) {
	f.calls = append(f.calls, "commit")
	f.route = route
	if f.failCommit > 0 {
		f.failCommit--
		return RouteReadback{}, errors.New("injected commit response loss")
	}
	return RouteReadback{ShardID: route.ShardID, State: "ready", RouteEpoch: route.RouteEpoch, ReasonCode: "ROUTE_READY", BindingDigest: route.BindingDigest}, nil
}

func (f *rolloutEdgeFake) ResumeRoute(_ context.Context, hs CommittedBindingHandshake, _ string) (RouteReadback, error) {
	f.calls = append(f.calls, "resume")
	if f.failResume > 0 {
		f.failResume--
		return RouteReadback{}, errors.New("injected resume response loss")
	}
	return RouteReadback{ShardID: hs.ShardID, State: "active", RouteEpoch: hs.RouteEpoch, ReasonCode: "ROUTE_ACTIVE", BindingDigest: hs.BindingDigest}, nil
}
