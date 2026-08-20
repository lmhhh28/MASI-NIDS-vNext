package governance_test

import (
	"context"
	"errors"
	"os"
	"strings"
	"testing"
	"time"

	"masi-nids/control-go/internal/config"
	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/governance"
	"masi-nids/control-go/internal/security"
)

type captureEdgeFake struct{}

type capturePreflightRejectFake struct {
	ackCalls int
}

func (*capturePreflightRejectFake) ExecuteEffect(context.Context, governance.Intent) (governance.EdgeEffectResult, error) {
	return governance.EdgeEffectResult{}, &governance.PreflightRejectedError{Cause: errors.New("capture profile unsupported")}
}

func (f *capturePreflightRejectFake) AcknowledgeEffect(context.Context, governance.Intent, governance.EdgeEffectResult, string, int64) error {
	f.ackCalls++
	return errors.New("preflight rejection must not be acknowledged")
}

func (captureEdgeFake) ExecuteEffect(_ context.Context, intent governance.Intent) (governance.EdgeEffectResult, error) {
	spec := intent.Payload.BoundedCapture
	started := time.Now().UnixMilli()
	readback := &governance.BoundedCaptureReadback{SchemaVersion: "bounded-capture-readback/v1",
		CaptureID: spec.CaptureID, CaptureDigest: spec.CaptureDigest, CaptureSessionID: "capture-session-e2e",
		StartedAtUnixMS: started, FinishedAtUnixMS: started + 10, ObservedSamples: 2, ObservedBytes: 256,
		ContentDigest: spec.CaptureDigest, ObservedFlowDigest: spec.CaptureDigest,
		CaptureWindowDigest: spec.CaptureDigest, Truncated: false, Gap: false}
	return governance.EdgeEffectResult{OperationID: intent.OperationID, TargetID: intent.TargetID,
		EffectDigest: intent.EffectDigest, Outcome: "applied", ReadbackDigest: governance.ComputeCaptureReadbackDigest(*readback),
		ResultDigest: spec.CaptureDigest, BoundedCapture: readback}, nil
}

func (captureEdgeFake) AcknowledgeEffect(context.Context, governance.Intent, governance.EdgeEffectResult, string, int64) error {
	return nil
}

func TestBoundedCaptureSameIntentQueuePostgres(t *testing.T) {
	configPath := os.Getenv("MASI_CONTROL_E2E_CONFIG")
	dsn := os.Getenv("MASI_CONTROL_E2E_DSN")
	if configPath == "" || dsn == "" {
		if os.Getenv("MASI_CONTROL_E2E_REQUIRED") == "1" {
			t.Fatal("bounded capture PostgreSQL E2E required but DSN/CONFIG is missing")
		}
		t.Skip("set MASI_CONTROL_E2E_DSN/CONFIG for bounded capture PostgreSQL E2E")
	}
	cfg, err := config.Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool, err := db.New(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()

	const targetID = "capture-target-e2e"
	captureIDs := []string{"capture-request-e2e-1", "capture-request-e2e-2"}
	proposalIDs := []string{"capture-proposal-e2e-1", "capture-proposal-e2e-2"}
	intentIDs := []string{"capture-intent-e2e-1", "capture-intent-e2e-2"}
	operationIDs := []string{"capture-operation-e2e-1", "capture-operation-e2e-2"}
	cleanup := func() {
		cleanCtx, cleanCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanCancel()
		_, _ = pool.Exec(cleanCtx, `DELETE FROM evidence_refs WHERE source='edge-bounded-capture' AND trace_id LIKE 'trace-capture-%'`)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM bounded_capture_results WHERE capture_id=ANY($1)`, captureIDs)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM effect_acknowledgements WHERE operation_id=ANY($1)`, operationIDs)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM effect_attempt_readback_entries WHERE operation_id=ANY($1)`, operationIDs)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM effect_attempts WHERE operation_id=ANY($1)`, operationIDs)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM bounded_capture_requests WHERE capture_id=ANY($1)`, captureIDs)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM effect_intents WHERE effect_intent_id=ANY($1)`, intentIDs)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM effect_decisions WHERE proposal_id=ANY($1)`, proposalIDs)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM effect_proposals WHERE proposal_id=ANY($1)`, proposalIDs)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM target_capability_observation_events WHERE target_id=$1`, targetID)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM target_capability_observations WHERE target_id=$1`, targetID)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM target_assignments WHERE target_id=$1`, targetID)
		_, _ = pool.Exec(cleanCtx, `DELETE FROM targets WHERE target_id=$1`, targetID)
	}
	cleanup()
	defer cleanup()

	d := "sha256:" + strings.Repeat("a", 64)
	nowMS := time.Now().UnixMilli()
	if _, err := pool.Exec(ctx, `INSERT INTO targets(target_id,display_name,p4runtime_endpoint,device_id,
		role,status,desired_profile_digest,credential_ref,scope,actor_ref,trace_id)
		VALUES($1,'capture target','https://127.0.0.1:9559',2,'primary','active',$2,'cred-capture',
		'scope-capture-e2e','actor-capture','trace-capture-seed')`, targetID, d); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO target_assignments(target_id,assignment_generation,incarnation_id,
		edge_workload_ref,lease_id,issued_at_unix_ms,expires_at_unix_ms,election_floor,election_ceiling,
		actor_runtime_epoch,application_generation,actor_ref,trace_id,actor_issuer,actor_subject)
		VALUES($1,1,'capture-incarnation-e2e','edge-capture-e2e','capture-lease-e2e',$2,$3,20,29,
		'capture-actor-epoch-e2e',1,'actor-capture','trace-capture-seed','https://issuer.example','capture-admin')`,
		targetID, nowMS-1000, nowMS+299_000); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO target_capability_observations(observation_id,target_id,
		target_control_incarnation_id,assignment_generation,actor_runtime_epoch,application_generation,
		p4info_digest,pipeline_digest,profile_digest,capacity_digest,capacity_available,lease_valid,
		p4_connected,primary_actor,pipeline_exact,high_priority_queue_depth,telemetry_queue_depth,
		observation_queue_depth,source_wal_bytes,input_wal_bytes,result_wal_bytes,freshness,reason_code,
		observed_at_unix_ms,expires_at_unix_ms,trace_id)
		VALUES('capture-observation-e2e',$1,'capture-incarnation-e2e',1,'capture-actor-epoch-e2e',1,
		$2,$2,$2,$2,true,true,true,true,true,0,0,0,0,0,0,'fresh','READY',$3,$4,'trace-capture-capability')`,
		targetID, d, nowMS, nowMS+120_000); err != nil {
		t.Fatal(err)
	}
	operator := security.Actor{Issuer: "https://issuer.example", Subject: "capture-operator-e2e"}
	targetDigest := targetSetDigest([]string{targetID})
	mapping := &security.RoleScopeMapping{Version: "v1", Digest: d, DefaultDeny: true,
		ActorScopes: map[string][]security.Scope{operator.String(): {{ScopeID: "scope-capture-e2e",
			TargetSetDigest: targetDigest, EffectKinds: []string{string(governance.KindBoundedCapture)},
			Levels: []security.AuthzContextLevel{security.LevelOperator}}}}}
	captureService := governance.NewCaptureService(pool)
	proposalService := governance.NewProposalService(pool)
	decisionService := governance.NewDecisionService(pool, mapping)
	preflightService := governance.NewPreflightService(pool)
	intentService := governance.NewIntentService(pool)

	created := make([]*governance.BoundedCaptureSpec, 0, 2)
	for index := 0; index < 2; index++ {
		spec, err := captureService.Register(ctx, governance.BoundedCaptureSpec{CaptureID: captureIDs[index],
			DurationMS: 1000, SampleLimit: 10, ByteLimit: 4096, ExpiresAtUnixMS: nowMS + 120_000,
			MaxConcurrentOnTarget: 1, Filter: governance.CaptureFilter{}}, targetID, "scope-capture-e2e",
			operator, "trace-capture-request-"+string(rune('1'+index)))
		if err != nil {
			t.Fatal(err)
		}
		created = append(created, spec)
		proposal, err := proposalService.Create(ctx, governance.Proposal{ProposalID: proposalIDs[index],
			Actor: operator, ActorLevel: security.LevelOperator, Scope: "scope-capture-e2e", RiskLevel: security.R0,
			EffectKind: governance.KindBoundedCapture, TargetSetDigest: targetDigest, TargetIDs: []string{targetID},
			PolicyDigest: spec.CaptureDigest, EvidenceRefs: []string{d}, ExpiresAtUnixMS: nowMS + 90_000,
			Note: "bounded metadata-only capture", TraceID: "trace-capture-proposal-" + string(rune('1'+index)),
			IdempotencyKey: "capture-proposal-idem-" + string(rune('1'+index))})
		if err != nil {
			t.Fatal(err)
		}
		decision, err := decisionService.ApproveWithReason(ctx, proposal.ProposalID, operator,
			governance.AuthzContext{Level: security.LevelOperator}, "CAPTURE_APPROVED")
		if err != nil {
			t.Fatal(err)
		}
		token, err := preflightService.Preflight(ctx, proposal.ProposalID)
		if err != nil {
			t.Fatal(err)
		}
		intent, createErr := intentService.Create(ctx, proposal.ProposalID, decision.DecisionID, *token,
			governance.Intent{EffectIntentID: intentIDs[index], OperationID: operationIDs[index], TargetID: targetID,
				Fence: token.Fence, AuthorizationDigest: decision.DecisionDigest, EffectKind: governance.KindBoundedCapture,
				RiskLevel: security.R0, RequiredWriteAtomicity: "CONTINUE_ON_ERROR", DeadlineUnixMS: nowMS + 60_000,
				Actor: operator, TraceID: token.TraceID})
		if index == 1 {
			if createErr == nil || !strings.Contains(createErr.Error(), "concurrency limit") {
				t.Fatalf("second concurrent capture must fail closed, intent=%+v err=%v", intent, createErr)
			}
			continue
		}
		if createErr != nil || intent.Payload.BoundedCapture == nil || intent.Payload.BoundedCapture.CaptureDigest != spec.CaptureDigest {
			t.Fatalf("canonical capture intent=%+v err=%v", intent, createErr)
		}
	}
	if len(created) != 2 {
		t.Fatal("capture setup did not create two immutable requests")
	}
	dispatcher := governance.NewDispatcher(pool, captureEdgeFake{}, preflightService, 30*time.Second,
		"capture-dispatcher-e2e", governance.NewCaptureProjector())
	out, err := dispatcher.Dispatch(ctx, intentIDs[0])
	if err != nil || out.Status != governance.AttemptApplied {
		t.Fatalf("capture dispatch outcome=%+v err=%v", out, err)
	}
	var state, evidenceID, captureDigest string
	var samples int
	if err := pool.QueryRow(ctx, `SELECT r.state,x.evidence_id,x.capture_digest,x.observed_samples
		FROM bounded_capture_requests r JOIN bounded_capture_results x USING(capture_id)
		WHERE r.capture_id=$1`, captureIDs[0]).Scan(&state, &evidenceID, &captureDigest, &samples); err != nil {
		t.Fatal(err)
	}
	if state != "applied" || captureDigest != created[0].CaptureDigest || samples != 2 || evidenceID == "" {
		t.Fatalf("capture projection state=%s evidence=%s digest=%s samples=%d", state, evidenceID, captureDigest, samples)
	}
	secondToken, err := preflightService.Preflight(ctx, proposalIDs[1])
	if err != nil {
		t.Fatal(err)
	}
	var secondDecisionID, secondDecisionDigest string
	if err := pool.QueryRow(ctx, `SELECT decision_id,decision_digest FROM effect_decisions
		WHERE proposal_id=$1 AND decision='approve'`, proposalIDs[1]).Scan(&secondDecisionID, &secondDecisionDigest); err != nil {
		t.Fatal(err)
	}
	if _, err := intentService.Create(ctx, proposalIDs[1], secondDecisionID, *secondToken,
		governance.Intent{EffectIntentID: intentIDs[1], OperationID: operationIDs[1], TargetID: targetID,
			Fence: secondToken.Fence, AuthorizationDigest: secondDecisionDigest, EffectKind: governance.KindBoundedCapture,
			RiskLevel: security.R0, RequiredWriteAtomicity: "CONTINUE_ON_ERROR", DeadlineUnixMS: nowMS + 60_000,
			Actor: operator, TraceID: secondToken.TraceID}); err != nil {
		t.Fatal(err)
	}
	rejectingEdge := &capturePreflightRejectFake{}
	rejectingDispatcher := governance.NewDispatcher(pool, rejectingEdge, preflightService, 30*time.Second,
		"capture-preflight-reject-e2e", governance.NewCaptureProjector())
	hold, err := rejectingDispatcher.Dispatch(ctx, intentIDs[1])
	if err != nil || hold.Status != governance.AttemptHold || hold.ReasonCode != "EDGE_PREFLIGHT_HOLD" {
		t.Fatalf("preflight rejection must be a known pre-side-effect HOLD: outcome=%+v err=%v", hold, err)
	}
	var requestState, claimState, attemptStatus string
	var readbackPresent bool
	if err := pool.QueryRow(ctx, `SELECT r.state,i.claim_state,a.status,(a.readback_digest IS NOT NULL)
		FROM bounded_capture_requests r JOIN effect_intents i ON i.effect_intent_id=r.effect_intent_id
		JOIN effect_attempts a ON a.intent_id=i.effect_intent_id WHERE r.capture_id=$1`, captureIDs[1]).
		Scan(&requestState, &claimState, &attemptStatus, &readbackPresent); err != nil {
		t.Fatal(err)
	}
	if requestState != "hold" || claimState != "hold" || attemptStatus != "hold" || readbackPresent || rejectingEdge.ackCalls != 0 {
		t.Fatalf("preflight HOLD facts=%s/%s/%s readback=%v ack_calls=%d",
			requestState, claimState, attemptStatus, readbackPresent, rejectingEdge.ackCalls)
	}
}
