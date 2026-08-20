package governance_test

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"

	"masi-nids/control-go/internal/config"
	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/firewall"
	"masi-nids/control-go/internal/governance"
	"masi-nids/control-go/internal/security"
)

func TestEffectDispatchFirewallProjectionAndAckOutboxPostgres(t *testing.T) {
	configPath := os.Getenv("MASI_CONTROL_E2E_CONFIG")
	dsn := os.Getenv("MASI_CONTROL_E2E_DSN")
	if dsn == "" || configPath == "" {
		if os.Getenv("MASI_CONTROL_E2E_REQUIRED") == "1" {
			t.Fatal("effect dispatcher PostgreSQL E2E required but DSN/CONFIG is missing")
		}
		t.Skip("set MASI_CONTROL_E2E_DSN/CONFIG for effect dispatcher PostgreSQL E2E")
	}
	cfg, err := config.Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool, err := db.New(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()

	const (
		targetID    = "effect-target-e2e"
		proposalID  = "effect-proposal-e2e"
		intentID    = "effect-intent-e2e"
		operationID = "effect-operation-e2e"
		revisionID  = "firewall-revision-e2e"
	)
	cleanup := func() {
		cleanCtx, cleanCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanCancel()
		for _, item := range []struct {
			statement string
			argument  string
		}{
			{`DELETE FROM effect_acknowledgements WHERE operation_id=$1`, operationID},
			{`DELETE FROM effect_attempt_readback_entries WHERE operation_id=$1`, operationID},
			{`DELETE FROM effect_attempts WHERE operation_id=$1`, operationID},
			{`DELETE FROM firewall_activations WHERE operation_id=$1`, operationID},
			{`DELETE FROM firewall_bindings WHERE target_id=$1`, targetID},
			{`DELETE FROM effect_intents WHERE operation_id=$1`, operationID},
			{`DELETE FROM effect_decisions WHERE proposal_id=$1`, proposalID},
			{`DELETE FROM effect_proposals WHERE proposal_id=$1`, proposalID},
			{`DELETE FROM firewall_revisions WHERE revision_id=$1`, revisionID},
			{`DELETE FROM target_capability_observation_events WHERE target_id=$1`, targetID},
			{`DELETE FROM target_capability_observations WHERE target_id=$1`, targetID},
			{`DELETE FROM target_assignments WHERE target_id=$1`, targetID},
			{`DELETE FROM targets WHERE target_id=$1`, targetID},
		} {
			_, _ = pool.Exec(cleanCtx, item.statement, item.argument)
		}
	}
	cleanup()
	defer cleanup()

	d := "sha256:" + strings.Repeat("a", 64)
	nowMS := time.Now().UnixMilli()
	if _, err := pool.Exec(ctx, `INSERT INTO targets(target_id,display_name,p4runtime_endpoint,device_id,
		role,status,desired_profile_digest,credential_ref,scope,actor_ref,trace_id)
		VALUES($1,'effect target','https://127.0.0.1:9559',1,'primary','active',$2,'cred-effect',
		'scope-effect-e2e','actor-effect','trace-effect-seed')`, targetID, d); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO target_assignments(target_id,assignment_generation,incarnation_id,
		edge_workload_ref,lease_id,issued_at_unix_ms,expires_at_unix_ms,election_floor,election_ceiling,
		actor_runtime_epoch,application_generation,actor_ref,trace_id,actor_issuer,actor_subject)
		VALUES($1,1,'target-inc-effect','edge-effect','lease-effect',$2,$3,10,19,'actor-epoch-effect',1,
		'actor-effect','trace-effect-seed','https://issuer.example','admin-effect')`, targetID, nowMS-1000, nowMS+299_000); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO target_capability_observations(observation_id, target_id,
		target_control_incarnation_id,assignment_generation,actor_runtime_epoch,application_generation,
		p4info_digest,pipeline_digest,profile_digest,capacity_digest,capacity_available,lease_valid,
		p4_connected,primary_actor,pipeline_exact,high_priority_queue_depth,telemetry_queue_depth,
		observation_queue_depth,source_wal_bytes,input_wal_bytes,result_wal_bytes,freshness,reason_code,
		observed_at_unix_ms,expires_at_unix_ms,trace_id)
		VALUES('effect-observation-e2e',$1,'target-inc-effect',1,'actor-epoch-effect',1,$2,$2,$2,$2,true,true,true,true,true,
		0,0,0,0,0,0,'fresh','READY',$3,$4,'trace-cap-effect')`, targetID, d, nowMS, nowMS+120_000); err != nil {
		t.Fatal(err)
	}

	admin := security.Actor{Issuer: "https://issuer.example", Subject: "platform-admin-effect"}
	operator := security.Actor{Issuer: "https://issuer.example", Subject: "operator-effect"}
	rule := firewall.Rule{
		RuleID: "rule-effect-e2e", RuleRevision: 1, Priority: 100,
		SourceIPv4:      firewall.IPv4Prefix{Address: "192.0.2.0", PrefixLength: 24},
		DestinationIPv4: firewall.IPv4Prefix{Address: "198.51.100.10", PrefixLength: 32},
		Protocol:        firewall.OptionalUint32{Present: true, Value: 6},
		L4Present:       firewall.OptionalBool{Present: true, Value: true},
		DestinationPort: firewall.OptionalUint32{Present: true, Value: 22},
		FragmentClass:   firewall.FragmentNone, Action: "drop", Enabled: true,
		ActorRef: admin.String(), ReasonCode: "BASELINE_RULE_CREATED",
	}
	rule.CanonicalRuleDigest = firewall.ComputeRuleDigest(rule)
	revision, err := firewall.NewRevisionService(pool).Create(ctx, firewall.Revision{
		RevisionID: revisionID, TargetID: targetID, DefaultAction: firewall.DefaultDrop,
		Rules: []firewall.Rule{rule}, Scope: "scope-effect-e2e", ActorRef: admin.String(),
	})
	if err != nil {
		t.Fatal(err)
	}
	targetDigest := targetSetDigest([]string{targetID})
	mapping := &security.RoleScopeMapping{Version: "v1", Digest: d, DefaultDeny: true,
		ActorScopes: map[string][]security.Scope{
			admin.String(): {{ScopeID: "scope-effect-e2e", TargetSetDigest: targetDigest,
				EffectKinds: []string{string(governance.KindFirewallBaselineActivate)}, Levels: []security.AuthzContextLevel{security.LevelPlatformAdmin}}},
			operator.String(): {{ScopeID: "scope-effect-e2e", TargetSetDigest: targetDigest,
				EffectKinds: []string{string(governance.KindFirewallBaselineActivate)}, Levels: []security.AuthzContextLevel{security.LevelOperator}}},
		}}
	proposal, err := governance.NewProposalService(pool).Create(ctx, governance.Proposal{
		ProposalID: proposalID, Actor: admin, ActorLevel: security.LevelPlatformAdmin,
		Scope: "scope-effect-e2e", RiskLevel: security.R3,
		EffectKind: governance.KindFirewallBaselineActivate, TargetSetDigest: targetDigest,
		TargetIDs: []string{targetID}, PolicyDigest: revision.RevisionDigest,
		EvidenceRefs: []string{d}, ExpiresAtUnixMS: nowMS + 300_000, Note: "activate exact baseline",
		TraceID: "trace-effect-proposal", IdempotencyKey: "effect-proposal-key-e2e",
	})
	if err != nil {
		t.Fatal(err)
	}
	decision, err := governance.NewDecisionService(pool, mapping).ApproveWithReason(ctx, proposal.ProposalID,
		operator, governance.AuthzContext{Level: security.LevelOperator, StepUpType: security.StepUpWebAuthn,
			StepUpAgeMS: 0, PhishingResistant: true}, "BASELINE_APPROVED")
	if err != nil {
		t.Fatal(err)
	}
	preflight, err := governance.NewPreflightService(pool).Preflight(ctx, proposalID)
	if err != nil {
		t.Fatal(err)
	}
	intent, err := governance.NewIntentService(pool).Create(ctx, proposalID, decision.DecisionID, *preflight,
		governance.Intent{EffectIntentID: intentID, OperationID: operationID, TargetID: targetID,
			Fence: preflight.Fence, AuthorizationDigest: decision.DecisionDigest,
			EffectKind: governance.KindFirewallBaselineActivate, RiskLevel: security.R3,
			RequiredWriteAtomicity: "CONTINUE_ON_ERROR", DeadlineUnixMS: nowMS + 60_000,
			Actor: operator, TraceID: preflight.TraceID})
	if err != nil {
		t.Fatal(err)
	}
	if intent.EffectDigest == "" || intent.Payload.Operation != "baseline-activate" {
		t.Fatalf("canonical intent payload missing: %+v", intent)
	}
	var persistedPayload []byte
	if err := pool.QueryRow(ctx, `SELECT effect_payload FROM effect_intents WHERE effect_intent_id=$1`, intentID).Scan(&persistedPayload); err != nil {
		t.Fatal(err)
	}
	persistedIntent := *intent
	if err := json.Unmarshal(persistedPayload, &persistedIntent.Payload); err != nil {
		t.Fatal(err)
	}
	if digest := governance.ComputeEffectDigest(persistedIntent); digest != intent.EffectDigest {
		originalWire, _ := governance.ToEdgeEffectIntent(*intent)
		persistedWire, _ := governance.ToEdgeEffectIntent(persistedIntent)
		t.Fatalf("JSONB changed semantic effect digest: original=%+v persisted=%+v payload=%s", originalWire, persistedWire, persistedPayload)
	}
	if _, err := firewall.NewActivationService(pool).Activate(ctx, targetID, revisionID, operationID); err != nil {
		t.Fatalf("prepare firewall activation gate: %v", err)
	}

	edge := &effectEdgeFake{failAcknowledgements: 1}
	projector := firewall.NewIntentProjector()
	dispatcher := governance.NewDispatcher(pool, edge, governance.NewPreflightService(pool), 30*time.Second, "dispatcher-effect-e2e", projector)
	outcome, dispatchErr := dispatcher.Dispatch(ctx, intentID)
	if dispatchErr == nil || outcome.Status != governance.AttemptApplied || outcome.ReasonCode != "EDGE_ACK_PENDING" {
		t.Fatalf("first dispatch should apply canonically with durable ACK pending: outcome=%+v original=%+v err=%v", outcome, intent, dispatchErr)
	}
	var claimState, activationResult, currentRevision, ackState string
	if err := pool.QueryRow(ctx, `SELECT claim_state FROM effect_intents WHERE effect_intent_id=$1`, intentID).Scan(&claimState); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT result FROM firewall_activations WHERE operation_id=$1`, operationID).Scan(&activationResult); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT current_revision_id FROM firewall_bindings WHERE target_id=$1`, targetID).Scan(&currentRevision); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT state FROM effect_acknowledgements WHERE operation_id=$1`, operationID).Scan(&ackState); err != nil {
		t.Fatal(err)
	}
	if claimState != "finalized" || activationResult != "applied" || currentRevision != revisionID || ackState != "pending" {
		t.Fatalf("canonical facts=%s/%s/%s/%s", claimState, activationResult, currentRevision, ackState)
	}
	edge.failAcknowledgements = 0
	if _, err := pool.Exec(ctx, `UPDATE effect_acknowledgements SET next_attempt_at_unix_ms=$1 WHERE operation_id=$2`, time.Now().UnixMilli(), operationID); err != nil {
		t.Fatal(err)
	}
	acked, err := dispatcher.RetryPendingAcknowledgements(ctx, 4)
	if err != nil || acked != 1 {
		t.Fatalf("ACK retry count=%d err=%v", acked, err)
	}
	if err := pool.QueryRow(ctx, `SELECT state FROM effect_acknowledgements WHERE operation_id=$1`, operationID).Scan(&ackState); err != nil || ackState != "acked" {
		t.Fatalf("ACK state=%s err=%v", ackState, err)
	}
}

type effectEdgeFake struct {
	failAcknowledgements int
}

func (f *effectEdgeFake) ExecuteEffect(_ context.Context, intent governance.Intent) (governance.EdgeEffectResult, error) {
	if _, err := governance.ToEdgeEffectIntent(intent); err != nil {
		return governance.EdgeEffectResult{}, err
	}
	entry := governance.AppliedRuleReadback{EntityID: "entity-rule-effect-e2e", RuleID: "rule-effect-e2e",
		CanonicalEntryDigest:      "sha256:" + strings.Repeat("e", 64),
		MatchPriorityActionDigest: "sha256:" + strings.Repeat("f", 64), TableID: 1, DirectCounterID: 1, Bank: 1}
	return governance.EdgeEffectResult{OperationID: intent.OperationID, TargetID: intent.TargetID,
		EffectDigest: intent.EffectDigest, Outcome: "applied", ReadbackDigest: "sha256:" + strings.Repeat("c", 64),
		ResultDigest: "sha256:" + strings.Repeat("d", 64), ExpectedEntries: 1, ObservedEntries: 1,
		ActiveBank: 1, AppliedEntries: []governance.AppliedRuleReadback{entry},
		ReadbackManifestDigest: governance.ComputeReadbackManifestDigest([]governance.AppliedRuleReadback{entry})}, nil
}

func (f *effectEdgeFake) AcknowledgeEffect(_ context.Context, _ governance.Intent, _ governance.EdgeEffectResult, _ string, _ int64) error {
	if f.failAcknowledgements > 0 {
		f.failAcknowledgements--
		return context.DeadlineExceeded
	}
	return nil
}

func targetSetDigest(ids []string) string {
	sum := sha256.Sum256([]byte(strings.Join(ids, ",")))
	return "sha256:" + hex.EncodeToString(sum[:])
}
