package firewall

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
	"masi-nids/control-go/internal/governance"
	"masi-nids/control-go/internal/security"
)

func TestOverlayAtomicIntentsPostgres(t *testing.T) {
	configPath := os.Getenv("MASI_CONTROL_E2E_CONFIG")
	if configPath == "" || os.Getenv("MASI_CONTROL_E2E_DSN") == "" {
		if os.Getenv("MASI_CONTROL_E2E_REQUIRED") == "1" {
			t.Fatal("overlay PostgreSQL E2E required but DSN/CONFIG is missing")
		}
		t.Skip("set MASI_CONTROL_E2E_DSN/CONFIG for overlay PostgreSQL E2E")
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
		targetID   = "overlay-target-e2e"
		proposalID = "overlay-proposal-e2e"
		overlayID  = "overlay-rule-e2e"
		upsertID   = "overlay-upsert-intent-e2e"
		deleteID   = "overlay-delete-intent-e2e"
	)
	cleanup := func() {
		cleanCtx, cleanCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanCancel()
		for _, statement := range []string{
			`DELETE FROM firewall_overlays WHERE overlay_rule_id='overlay-rule-e2e'`,
			`DELETE FROM effect_attempts WHERE intent_id IN ('overlay-upsert-intent-e2e','overlay-delete-intent-e2e')`,
			`DELETE FROM effect_intents WHERE effect_intent_id IN ('overlay-upsert-intent-e2e','overlay-delete-intent-e2e','overlay-conflict-intent-e2e')`,
			`DELETE FROM effect_decisions WHERE proposal_id='overlay-proposal-e2e'`,
			`DELETE FROM effect_proposals WHERE proposal_id='overlay-proposal-e2e'`,
			`DELETE FROM target_capability_observations WHERE target_id='overlay-target-e2e'`,
			`DELETE FROM target_assignments WHERE target_id='overlay-target-e2e'`,
			`DELETE FROM targets WHERE target_id='overlay-target-e2e'`,
		} {
			_, _ = pool.Exec(cleanCtx, statement)
		}
	}
	cleanup()
	defer cleanup()

	d := "sha256:" + strings.Repeat("a", 64)
	now := time.Now().UTC().Truncate(time.Second)
	nowMS := now.UnixMilli()
	if _, err := pool.Exec(ctx, `INSERT INTO targets(target_id,display_name,p4runtime_endpoint,device_id,role,status,
	 desired_profile_digest,credential_ref,scope,actor_ref,trace_id)
	 VALUES($1,'overlay target','https://127.0.0.1:9565',65,'primary','active',$2,'overlay-credential',
	 'scope-overlay-e2e','seed','trace-overlay-seed')`, targetID, d); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO target_assignments(target_id,assignment_generation,incarnation_id,
	 edge_workload_ref,lease_id,issued_at_unix_ms,expires_at_unix_ms,election_floor,election_ceiling,
	 actor_runtime_epoch,application_generation,actor_ref,trace_id,actor_issuer,actor_subject)
	 VALUES($1,1,'overlay-inc','edge-overlay','overlay-lease',$2,$3,20,29,'overlay-epoch',1,
	 'seed','trace-overlay-seed','https://issuer.example','overlay-operator')`, targetID, nowMS-1000, nowMS+299_000); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO target_capability_observations(target_id,
	 target_control_incarnation_id,assignment_generation,actor_runtime_epoch,application_generation,
	 p4info_digest,pipeline_digest,profile_digest,capacity_digest,capacity_available,lease_valid,p4_connected,
	 primary_actor,pipeline_exact,high_priority_queue_depth,telemetry_queue_depth,observation_queue_depth,
	 source_wal_bytes,input_wal_bytes,result_wal_bytes,freshness,reason_code,observed_at_unix_ms,
	 expires_at_unix_ms,trace_id)
	 VALUES($1,'overlay-inc',1,'overlay-epoch',1,$2,$2,$2,$2,true,true,true,true,true,0,0,0,0,0,0,
	 'fresh','READY',$3,$4,'trace-overlay-capability')`, targetID, d, nowMS, nowMS+120_000); err != nil {
		t.Fatal(err)
	}

	operator := security.Actor{Issuer: "https://issuer.example", Subject: "overlay-operator"}
	expires := now.Add(4 * time.Minute)
	rule := OverlayRule{RuleID: overlayID, RuleRevision: 1, StagePrecedence: "before-baseline",
		SourceIPv4: "192.0.2.10", DestinationIPv4: "198.51.100.20", Protocol: 6,
		SourcePort: 40000, DestinationPort: 443, Action: "drop", Enabled: true,
		ExpiresAt: expires.Format(time.RFC3339), ActorRef: operator.String(), ReasonCode: "INCIDENT_RESPONSE"}
	rule.CanonicalRuleDigest = ComputeOverlayRuleDigest(rule)
	targetDigest := overlayTargetSetDigest([]string{targetID})
	mapping := &security.RoleScopeMapping{Version: "v1", Digest: d, DefaultDeny: true,
		ActorScopes: map[string][]security.Scope{operator.String(): {{ScopeID: "scope-overlay-e2e",
			TargetSetDigest: targetDigest, EffectKinds: []string{string(governance.KindFirewallOverlay)},
			Levels: []security.AuthzContextLevel{security.LevelOperator}}}}}
	proposal, err := governance.NewProposalService(pool).Create(ctx, governance.Proposal{
		ProposalID: proposalID, Actor: operator, ActorLevel: security.LevelOperator, Scope: "scope-overlay-e2e",
		RiskLevel: security.R1, EffectKind: governance.KindFirewallOverlay, TargetSetDigest: targetDigest,
		TargetIDs: []string{targetID}, PolicyDigest: rule.CanonicalRuleDigest, EvidenceRefs: []string{d},
		ExpiresAtUnixMS: now.Add(10 * time.Minute).UnixMilli(), Note: "bounded response overlay",
		TraceID: "trace-overlay-proposal", IdempotencyKey: "overlay-proposal-key-e2e"})
	if err != nil {
		t.Fatal(err)
	}
	decision, err := governance.NewDecisionService(pool, mapping).ApproveWithReason(ctx, proposalID, operator,
		governance.AuthzContext{Level: security.LevelOperator}, "OVERLAY_APPROVED")
	if err != nil {
		t.Fatal(err)
	}
	fence := governance.Fence{TargetControlIncarnationID: "overlay-inc", TargetAssignmentGeneration: 1,
		EdgeWorkloadRef: "edge-overlay", ActorRuntimeEpoch: "overlay-epoch", ApplicationGeneration: 1,
		ElectionIDLow: 20, P4InfoDigest: d, PipelineDigest: d, CapacityDigest: d}
	base := governance.Intent{ProposalID: proposal.ProposalID, DecisionID: decision.DecisionID,
		TargetID: targetID, Fence: fence, AuthorizationDigest: decision.DecisionDigest,
		EffectKind: governance.KindFirewallOverlay, RiskLevel: security.R1,
		RequiredWriteAtomicity: "CONTINUE_ON_ERROR", Actor: operator}
	upsert := base
	upsert.EffectIntentID, upsert.OperationID, upsert.DeadlineUnixMS, upsert.TraceID =
		upsertID, "overlay-upsert-operation-e2e", now.Add(2*time.Minute).UnixMilli(), "trace-overlay-upsert"
	deleteIntent := base
	deleteIntent.EffectIntentID, deleteIntent.OperationID, deleteIntent.DeadlineUnixMS, deleteIntent.TraceID =
		deleteID, "overlay-delete-operation-e2e", expires.Add(time.Minute).UnixMilli(), "trace-overlay-delete"
	overlay := Overlay{OverlayRuleID: overlayID, TargetID: targetID, Rule: rule,
		ExpiresAtUnixMS: expires.UnixMilli(), OperationID: upsert.OperationID, Scope: "scope-overlay-e2e",
		Actor: operator, TraceID: "trace-overlay-create", UpsertIntent: upsert, DeleteIntent: deleteIntent}
	service := NewOverlayService(pool)
	if err := service.Create(ctx, overlay); err != nil {
		t.Fatal(err)
	}
	if err := service.Create(ctx, overlay); err != nil {
		t.Fatalf("same overlay identity must be idempotent: %v", err)
	}
	var overlayCount, intentCount int
	var upsertOperation, deleteOperation string
	var notBefore int64
	if err := pool.QueryRow(ctx, `SELECT count(*),min(upsert_intent_id),min(delete_intent_id)
	 FROM firewall_overlays WHERE overlay_rule_id=$1`, overlayID).Scan(&overlayCount, &upsertOperation, &deleteOperation); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM effect_intents WHERE effect_intent_id IN ($1,$2)`, upsertID, deleteID).Scan(&intentCount); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT not_before_unix_ms FROM effect_intents WHERE effect_intent_id=$1`, deleteID).Scan(&notBefore); err != nil {
		t.Fatal(err)
	}
	if overlayCount != 1 || intentCount != 2 || upsertOperation != upsertID || deleteOperation != deleteID || notBefore != expires.UnixMilli() {
		t.Fatalf("overlay atomic facts=%d/%d/%s/%s/%d", overlayCount, intentCount, upsertOperation, deleteOperation, notBefore)
	}
	var persistedPayload []byte
	if err := pool.QueryRow(ctx, `SELECT effect_payload FROM effect_intents WHERE effect_intent_id=$1`, upsertID).Scan(&persistedPayload); err != nil {
		t.Fatal(err)
	}
	var payload governance.EffectPayload
	if json.Unmarshal(persistedPayload, &payload) != nil || payload.Operation != "overlay-upsert" {
		t.Fatalf("upsert payload invalid: %s", persistedPayload)
	}
	if err := pool.QueryRow(ctx, `SELECT effect_digest,effect_payload FROM effect_intents WHERE effect_intent_id=$1`, deleteID).
		Scan(&deleteIntent.EffectDigest, &persistedPayload); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(persistedPayload, &deleteIntent.Payload); err != nil {
		t.Fatal(err)
	}
	if err := governance.NewPreflightService(pool).ValidateIntent(ctx, deleteIntent); err == nil ||
		!strings.Contains(err.Error(), "early/stale") {
		t.Fatalf("delete intent must not become executable before durable expiry: %v", err)
	}
}

func overlayTargetSetDigest(ids []string) string {
	sum := sha256.Sum256([]byte(strings.Join(ids, ",")))
	return "sha256:" + hex.EncodeToString(sum[:])
}
