package governance

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

// ReconcileService converges intents left in the 'unknown' state (timeout-after-
// effect). It queries the ORIGINAL operation id/readback and reconciles along
// that identity — it NEVER creates a second intent, NEVER blind-retries, and
// NEVER auto-recovers (§5.2, ADR-0004 §7). A reconciling intent freezes same-
// target baseline mutation until readback converges.
type ReconcileService struct {
	pool       *db.Pool
	edge       EdgeEffectReadbackClient
	projectors []IntentStateProjector
	now        func() time.Time
}

func NewReconcileService(pool *db.Pool, edge EdgeEffectReadbackClient, projectors ...IntentStateProjector) *ReconcileService {
	return &ReconcileService{pool: pool, edge: edge, projectors: projectors, now: time.Now}
}

// ReconcileUnknown converges one unknown intent by querying the original
// operation's readback. If Edge reports the original write applied with exact
// readback, the intent is CAS-finalized to finalized. If the write is provably
// absent, the intent is marked hold (no blind retry). If unprovable, the intent
// stays unknown/reconciling.
//
// The Edge query is OUTSIDE any DB transaction; only the final CAS is a short txn.
func (r *ReconcileService) ReconcileUnknown(ctx context.Context, intentID string) (DispatchResult, error) {
	intent, err := r.loadUnknownIntent(ctx, intentID)
	if err != nil {
		return DispatchResult{}, err
	}
	// Query the original operation readback OUTSIDE any DB transaction.
	edgeRes, rpcErr := r.edge.QueryEffectReadback(ctx, *intent)
	if rpcErr != nil {
		return DispatchResult{IntentID: intentID, Status: AttemptReconciling, Reconciling: true, ReasonCode: "RECONCILE_RPC_ERROR"}, rpcErr
	}
	// Converge via a NEW short txn, CAS on the original operation id.
	status := AttemptReconciling
	reason := "RECONCILING"
	identityExact := edgeRes.OperationID == intent.OperationID && edgeRes.TargetID == intent.TargetID &&
		edgeRes.EffectDigest == intent.EffectDigest && validSHA256(edgeRes.ResultDigest)
	if identityExact && edgeRes.Outcome == "applied" && edgeRes.MismatchedEntries == 0 &&
		edgeRes.ReadbackDigest != "" && edgeRes.ExpectedEntries == edgeRes.ObservedEntries {
		status = AttemptApplied
		reason = "RECONCILED_APPLIED"
	} else if identityExact && (edgeRes.Outcome == "absent" || edgeRes.Outcome == "hold") {
		status = AttemptHold
		reason = "RECONCILED_ABSENT_NO_BLIND_RETRY"
	}
	committedAt := r.now().UnixMilli()
	err = r.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		tag, err := tx.Exec(ctx, `
			UPDATE effect_intents
			SET claim_state = CASE WHEN $1 = 'applied' THEN 'finalized'
			                       WHEN $1 = 'reconciling' THEN 'unknown'
			                       ELSE 'hold' END
				WHERE effect_intent_id = $2 AND operation_id = $3 AND claim_state='unknown'`,
			string(status), intent.EffectIntentID, intent.OperationID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			return fmt.Errorf("reconcile: CAS conflict (operation id mismatch)")
		}
		var attemptNumber int
		if err := tx.QueryRow(ctx, `SELECT COALESCE(MAX(attempt_number),0)+1 FROM effect_attempts WHERE intent_id=$1`,
			intent.EffectIntentID).Scan(&attemptNumber); err != nil {
			return err
		}
		if attemptNumber < 1 || attemptNumber > 64 {
			return errors.New("reconcile: effect attempt bound exhausted")
		}
		attemptID := "att-" + shortID(fmt.Sprintf("%s:reconcile:%d", intent.EffectIntentID, attemptNumber))
		var readback any
		if edgeRes.ReadbackDigest != "" {
			readback = edgeRes.ReadbackDigest
		}
		var activeBank any
		if edgeRes.ActiveBank >= 0 {
			activeBank = edgeRes.ActiveBank
		}
		if _, err := tx.Exec(ctx, `INSERT INTO effect_attempts(
		 attempt_id,intent_id,operation_id,attempt_number,status,plan_digest,readback_digest,
		 expected_entries,observed_entries,mismatched_entries,active_bank,started_at_unix_ms,
		 finished_at_unix_ms,trace_id,reason_code)
		 VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$12,$13,$14)`, attemptID,
			intent.EffectIntentID, intent.OperationID, attemptNumber, string(status), intent.EffectDigest,
			readback, edgeRes.ExpectedEntries, edgeRes.ObservedEntries, edgeRes.MismatchedEntries,
			activeBank, committedAt, intent.TraceID, reason); err != nil {
			return err
		}
		claimState := ClaimUnknown
		if status == AttemptApplied {
			claimState = ClaimFinalized
		} else if status == AttemptHold {
			claimState = ClaimState("hold")
		}
		for _, projector := range r.projectors {
			if projector == nil {
				continue
			}
			if err := projector.ProjectIntentState(ctx, tx, intent.EffectIntentID, claimState, status, reason); err != nil {
				return err
			}
		}
		if status == AttemptApplied || status == AttemptHold {
			if err := insertEffectAckTx(ctx, tx, intent, edgeRes, committedAt); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return DispatchResult{IntentID: intentID, Status: AttemptReconciling, Reconciling: true, ReasonCode: "RECONCILE_CAS_ERROR"}, err
	}
	if status == AttemptApplied || status == AttemptHold {
		if err := r.edge.AcknowledgeEffect(ctx, *intent, edgeRes, intent.EffectIntentID, committedAt); err != nil {
			return DispatchResult{IntentID: intentID, Status: status, Readback: edgeRes.ReadbackDigest, Reconciling: true, ReasonCode: "EDGE_ACK_PENDING"}, err
		}
		tag, err := r.pool.Pool.Exec(ctx, `UPDATE effect_acknowledgements SET state='acked',acked_at_unix_ms=$1,last_error='',updated_at=now()
			WHERE operation_id=$2 AND state='pending'`, r.now().UnixMilli(), intent.OperationID)
		if err != nil {
			return DispatchResult{IntentID: intentID, Status: status, Readback: edgeRes.ReadbackDigest, Reconciling: true, ReasonCode: "ACK_PROJECTION_ERROR"}, err
		}
		if tag.RowsAffected() != 1 {
			return DispatchResult{IntentID: intentID, Status: status, Readback: edgeRes.ReadbackDigest, Reconciling: true, ReasonCode: "ACK_PROJECTION_ERROR"}, errors.New("reconcile: acknowledgement state CAS conflict")
		}
	}
	return DispatchResult{IntentID: intentID, Status: status, Readback: edgeRes.ReadbackDigest, Reconciling: status == AttemptReconciling, ReasonCode: reason}, nil
}

func (r *ReconcileService) loadUnknownIntent(ctx context.Context, intentID string) (*Intent, error) {
	var intent Intent
	var actorIssuer, actorSubject, fenceJSON, claimState, payloadJSON string
	err := r.pool.Pool.QueryRow(ctx, `
		SELECT effect_intent_id, operation_id, proposal_id, decision_id, target_id,
		       is_fleet_parent, fence, effect_digest, authorization_digest,
		       effect_kind, risk_level,required_write_atomicity,deadline_unix_ms, claim_state,
		       actor_issuer, actor_subject, trace_id,effect_payload::text
		FROM effect_intents WHERE effect_intent_id = $1 AND claim_state = 'unknown'`,
		intentID).
		Scan(&intent.EffectIntentID, &intent.OperationID, &intent.ProposalID,
			&intent.DecisionID, &intent.TargetID, &intent.IsFleetParent, &fenceJSON,
			&intent.EffectDigest, &intent.AuthorizationDigest, &intent.EffectKind,
			&intent.RiskLevel, &intent.RequiredWriteAtomicity, &intent.DeadlineUnixMS, &claimState,
			&actorIssuer, &actorSubject, &intent.TraceID, &payloadJSON)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, fmt.Errorf("reconcile: intent %s not unknown", intentID)
		}
		return nil, fmt.Errorf("reconcile: load: %w", err)
	}
	if err := unmarshalFence(fenceJSON, &intent.Fence); err != nil {
		return nil, err
	}
	if err := json.Unmarshal([]byte(payloadJSON), &intent.Payload); err != nil {
		return nil, fmt.Errorf("reconcile: parse effect payload: %w", err)
	}
	intent.Actor = security.Actor{Issuer: actorIssuer, Subject: actorSubject}
	if err := intent.Actor.Validate(); err != nil {
		return nil, fmt.Errorf("reconcile: persisted actor: %w", err)
	}
	if err := ValidateEffectPayload(intent.Payload); err != nil || ComputeEffectDigest(intent) != intent.EffectDigest {
		return nil, errors.New("reconcile: effect payload/digest invalid")
	}
	return &intent, nil
}

func unmarshalFence(s string, f *Fence) error {
	if s == "" {
		return nil
	}
	return json.Unmarshal([]byte(s), f)
}
