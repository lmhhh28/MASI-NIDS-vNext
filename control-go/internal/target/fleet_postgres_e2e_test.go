package target

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"

	"masi-nids/control-go/internal/config"
	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/governance"
)

func TestFleetProjectionPostgres(t *testing.T) {
	configPath := os.Getenv("MASI_CONTROL_E2E_CONFIG")
	dsn := os.Getenv("MASI_CONTROL_E2E_DSN")
	if dsn == "" || configPath == "" {
		if os.Getenv("MASI_CONTROL_E2E_REQUIRED") == "1" {
			t.Fatal("fleet PostgreSQL E2E required but DSN/CONFIG is missing")
		}
		t.Skip("set MASI_CONTROL_E2E_DSN/CONFIG for fleet PostgreSQL E2E")
	}
	cfg, err := config.Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.PostgreSQLDSN != dsn {
		t.Fatal("fleet PostgreSQL E2E DSN differs from validated config")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool, err := db.New(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()

	cleanup := func() {
		cleanCtx, cleanCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanCancel()
		for _, statement := range []string{
			`DELETE FROM fleet_child_intents WHERE fleet_operation_id LIKE 'fleet-projection-e2e%'`,
			`DELETE FROM fleet_operations WHERE fleet_operation_id LIKE 'fleet-projection-e2e%'`,
			`DELETE FROM effect_attempts WHERE intent_id LIKE 'fleet-projection-intent-e2e%'`,
			`DELETE FROM effect_intents WHERE effect_intent_id LIKE 'fleet-projection-%-e2e%'`,
			`DELETE FROM effect_decisions WHERE decision_id LIKE 'fleet-projection-decision-e2e%'`,
			`DELETE FROM effect_proposals WHERE proposal_id LIKE 'fleet-projection-proposal-e2e%'`,
		} {
			_, _ = pool.Exec(cleanCtx, statement)
		}
	}
	cleanup()
	defer cleanup()

	coordinator := NewFleetCoordinator(pool)
	seed := func(suffix string, failurePolicy FailurePolicy) (string, string, string) {
		t.Helper()
		opID := "fleet-projection-e2e-" + suffix
		proposalID := "fleet-projection-proposal-e2e-" + suffix
		decisionID := "fleet-projection-decision-e2e-" + suffix
		parentID := "fleet-projection-parent-e2e-" + suffix
		child0 := "fleet-projection-intent-e2e-" + suffix + "-0"
		child1 := "fleet-projection-intent-e2e-" + suffix + "-1"
		d := "sha256:" + strings.Repeat("a", 64)
		if suffix != "success" {
			d = "sha256:" + strings.Repeat("b", 64)
		}
		nowMS := time.Now().UnixMilli()
		waves, _ := json.Marshal([]Wave{
			{WaveIndex: 0, TargetIDs: []string{"target-" + suffix + "-0"}, ParallelLimit: 1, FailurePolicy: failurePolicy, GateState: GateOpen},
			{WaveIndex: 1, TargetIDs: []string{"target-" + suffix + "-1"}, ParallelLimit: 1, FailurePolicy: failurePolicy, GateState: GateClosed},
		})
		if _, err := pool.Exec(ctx, `INSERT INTO effect_proposals(proposal_id,proposal_digest,actor_ref,scope,risk_level,
			effect_kind,target_set_digest,policy_digest,evidence_refs,expires_at_unix_ms,note,created_at_unix_ms,
			trace_id,reason_code,actor_issuer,actor_subject,actor_level,target_ids,idempotency_key)
			VALUES($1,$2,'actor-e2e','scope-e2e','R2','firewall-overlay',$2,$2,'[]',$3,'fleet e2e',$4,
			'trace-e2e','CREATED','https://issuer.example','maker-e2e','analyst',$5,'idem-'||$1)`,
			proposalID, d, nowMS+600000, nowMS, []string{"target-" + suffix + "-0", "target-" + suffix + "-1"}); err != nil {
			t.Fatal(err)
		}
		if _, err := pool.Exec(ctx, `INSERT INTO effect_decisions(decision_id,proposal_id,proposal_digest,actor_ref,
			risk_level,decision,authz_context,decision_digest,expires_at_unix_ms,created_at_unix_ms,trace_id,
			reason_code,actor_issuer,actor_subject)
			VALUES($1,$2,$3,'checker-e2e','R2','approve','{}',$3,$4,$5,'trace-e2e','APPROVED',
			'https://issuer.example','checker-e2e')`, decisionID, proposalID, d, nowMS+600000, nowMS); err != nil {
			t.Fatal(err)
		}
		insertIntent := func(id, targetID string, parent, gate bool) {
			var state any = "unclaimed"
			if parent {
				state = nil
			}
			if _, err := pool.Exec(ctx, `INSERT INTO effect_intents(effect_intent_id,operation_id,proposal_id,proposal_digest,decision_id,
					target_id,fleet_operation_id,is_fleet_parent,fence,effect_digest,authorization_digest,effect_kind,
				risk_level,required_write_atomicity,deadline_unix_ms,claim_state,actor_ref,trace_id,reason_code,
				actor_issuer,actor_subject,gate_open)
					VALUES($1,'operation-'||$1,$2,$7,$3,$4,$5,$6,'{}',$7,$7,'firewall-overlay','R2',
				'CONTINUE_ON_ERROR',$8,$9,'checker-e2e','trace-e2e','CREATED','https://issuer.example','checker-e2e',$10)`,
				id, proposalID, decisionID, targetID, opID, parent, d, nowMS+600000, state, gate); err != nil {
				t.Fatal(err)
			}
		}
		insertIntent(parentID, "fleet:"+opID, true, false)
		insertIntent(child0, "target-"+suffix+"-0", false, true)
		insertIntent(child1, "target-"+suffix+"-1", false, false)
		if _, err := pool.Exec(ctx, `INSERT INTO fleet_operations(fleet_operation_id,target_set_digest,wave_count,waves,
			parent_intent_id,aggregate_status,actor_ref,scope,reason_code,trace_id,created_at_unix_ms)
			VALUES($1,$2,2,$3,$4,'planned','actor-e2e','scope-e2e','PLANNED','trace-e2e',$5)`,
			opID, fleetTargetSetDigest([]string{"target-" + suffix + "-0", "target-" + suffix + "-1"}), waves, parentID, nowMS); err != nil {
			t.Fatal(err)
		}
		for index, child := range []string{child0, child1} {
			if _, err := pool.Exec(ctx, `INSERT INTO fleet_child_intents(fleet_operation_id,target_id,effect_digest,
				child_intent_id,wave_index,status,reason_code,gate_open)
				VALUES($1,$2,$3,$4,$5,'pending','PENDING',$6)`, opID, "target-"+suffix+"-"+string(rune('0'+index)), d, child, index, index == 0); err != nil {
				t.Fatal(err)
			}
		}
		return opID, child0, child1
	}

	project := func(intentID string, state governance.ClaimState, outcome governance.AttemptStatus, reason string) {
		t.Helper()
		if err := pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
			return coordinator.ProjectIntentState(ctx, tx, intentID, state, outcome, reason)
		}); err != nil {
			t.Fatal(err)
		}
	}

	opID, first, second := seed("success", FailFast)
	project(first, governance.ClaimClaimed, "", "CLAIMED")
	project(first, governance.ClaimUnknown, governance.AttemptUnknown, "TIMEOUT_UNKNOWN")
	var aggregate string
	if err := pool.QueryRow(ctx, `SELECT aggregate_status FROM fleet_operations WHERE fleet_operation_id=$1`, opID).Scan(&aggregate); err != nil || aggregate != "reconciling" {
		t.Fatalf("unknown aggregate=%s err=%v", aggregate, err)
	}
	project(first, governance.ClaimFinalized, governance.AttemptApplied, "RECONCILED_APPLIED")
	if err := coordinator.AdvanceWaveGate(ctx, opID, 0, 1); err != nil {
		t.Fatal(err)
	}
	project(second, governance.ClaimClaimed, "", "CLAIMED")
	project(second, governance.ClaimFinalized, governance.AttemptApplied, "APPLIED")
	if err := pool.QueryRow(ctx, `SELECT aggregate_status FROM fleet_operations WHERE fleet_operation_id=$1`, opID).Scan(&aggregate); err != nil || aggregate != "applied" {
		t.Fatalf("applied aggregate=%s err=%v", aggregate, err)
	}

	failID, failFirst, failSecond := seed("failfast", FailFast)
	project(failFirst, governance.ClaimState("hold"), governance.AttemptHold, "PRECONDITION_HOLD")
	var childStatus, claimState string
	if err := pool.QueryRow(ctx, `SELECT status FROM fleet_child_intents WHERE child_intent_id=$1`, failSecond).Scan(&childStatus); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT claim_state FROM effect_intents WHERE effect_intent_id=$1`, failSecond).Scan(&claimState); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT aggregate_status FROM fleet_operations WHERE fleet_operation_id=$1`, failID).Scan(&aggregate); err != nil {
		t.Fatal(err)
	}
	if childStatus != "blocked" || claimState != "blocked" || aggregate != "failed" {
		t.Fatalf("fail-fast child=%s claim=%s aggregate=%s", childStatus, claimState, aggregate)
	}
}
