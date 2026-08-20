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

// Dispatcher is the bounded effect dispatcher. It is the ONLY component that
// claims effect_intents. The dispatch order (ADR-0001/0004) is:
//
//  1. Claim: a SHORT serializable transaction claims one unclaimed non-parent
//     intent via FOR UPDATE SKIP LOCKED and sets claim_state=claimed + lease.
//     The transaction COMMITS before any external call.
//  2. Edge RPC: ExecuteEffect is called OUTSIDE any DB transaction / held
//     connection. The lease is held in process memory, not a DB connection.
//  3. Finalize: a NEW short transaction CAS-finalizes (claim_lease_id match)
//     to applied/finalized or unknown. An old claim or old application-
//     generation late result does NOT overwrite new facts.
//
// Timeout-after-effect marks the intent unknown and reconciles along the
// ORIGINAL operation id/readback — no second intent, no blind retry (§5.2).
type Dispatcher struct {
	pool       *db.Pool
	edge       EdgeEffectClient
	preflight  *PreflightService
	leaseTTL   time.Duration
	now        func() time.Time
	owner      string
	projectors []IntentStateProjector
}

func NewDispatcher(pool *db.Pool, edge EdgeEffectClient, preflight *PreflightService, leaseTTL time.Duration, owner string, projectors ...IntentStateProjector) *Dispatcher {
	return &Dispatcher{pool: pool, edge: edge, preflight: preflight, leaseTTL: leaseTTL, now: time.Now, owner: owner, projectors: projectors}
}

// DispatchResult is the outcome of dispatching one intent.
type DispatchResult struct {
	IntentID    string        `json:"intent_id"`
	Status      AttemptStatus `json:"status"`
	Readback    string        `json:"readback_digest,omitempty"`
	Reconciling bool          `json:"reconciling"`
	ReasonCode  string        `json:"reason_code"`
}

// Dispatch claims, executes via Edge, and CAS-finalizes one intent. The Edge
// RPC is NEVER inside a DB transaction (the critical invariant).
func (d *Dispatcher) Dispatch(ctx context.Context, intentID string) (DispatchResult, error) {
	intent, err := d.loadIntent(ctx, intentID)
	if err != nil {
		return DispatchResult{}, err
	}
	if intent.IsFleetParent {
		return DispatchResult{IntentID: intentID, Status: AttemptHold, ReasonCode: "FLEET_PARENT_NOT_CLAIMABLE"}, nil
	}
	if d.preflight == nil || d.preflight.ValidateIntent(ctx, *intent) != nil {
		if err := d.holdBeforeSideEffect(ctx, intent, "PREFLIGHT_STALE"); err != nil {
			return DispatchResult{IntentID: intentID, Status: AttemptHold, ReasonCode: "PREFLIGHT_HOLD_ERROR"}, err
		}
		return DispatchResult{IntentID: intentID, Status: AttemptHold, ReasonCode: "PREFLIGHT_STALE"}, nil
	}

	// ---- 1. Claim (short serializable txn, FOR UPDATE SKIP LOCKED) ----
	// No external call occurs while this transaction is open.
	res, lease, err := d.pool.ClaimIntent(ctx, intentID, string(ClaimUnclaimed),
		intent.EffectDigest, d.owner, d.leaseTTL)
	if err != nil {
		return DispatchResult{IntentID: intentID, Status: AttemptHold, ReasonCode: "CLAIM_ERROR"}, err
	}
	if res != db.CASClaimed {
		return DispatchResult{IntentID: intentID, Status: AttemptHold, ReasonCode: "CLAIM_" + string(res)}, nil
	}
	if err := d.projectState(ctx, intent.EffectIntentID, ClaimClaimed, "", "CLAIMED"); err != nil {
		// The durable claim remains leased and will expire. Do not call Edge when
		// the canonical fleet vector cannot be updated consistently.
		return DispatchResult{IntentID: intentID, Status: AttemptHold, ReasonCode: "CLAIM_PROJECTION_ERROR"}, err
	}
	// Transaction has COMMITTED here. The lease is in memory; no DB connection
	// is held across the Edge RPC below.

	// ---- 2. Edge RPC (OUTSIDE any DB transaction) ----
	started := d.now().UnixMilli()
	edgeCtx, cancel := context.WithTimeout(ctx, time.Until(time.UnixMilli(intent.DeadlineUnixMS)))
	defer cancel()
	renewDone := make(chan struct{})
	renewResult := make(chan error, 1)
	go func() {
		renewResult <- d.renewLease(edgeCtx, intent.EffectIntentID, lease.Fence, renewDone, cancel)
	}()
	edgeRes, rpcErr := d.edge.ExecuteEffect(edgeCtx, *intent)
	close(renewDone)
	// Join the renewal goroutine before accepting the result. A non-blocking
	// receive here creates a race in which an already-lost lease can be missed
	// just as ExecuteEffect returns.
	var preflightRejected *PreflightRejectedError
	failedBeforeSideEffect := errors.As(rpcErr, &preflightRejected)
	if leaseErr := <-renewResult; leaseErr != nil {
		rpcErr = leaseErr
		failedBeforeSideEffect = false
	}
	if failedBeforeSideEffect {
		return d.finalizePreflightHold(ctx, intent, lease.Fence, started)
	}
	if rpcErr == nil {
		rpcErr = validateEffectResultIdentity(*intent, edgeRes)
	}

	// ---- 3. Finalize (NEW short txn, CAS on claim_lease_id) ----
	// An old-lease late result does NOT overwrite new facts.
	if rpcErr != nil || edgeRes.TimedOut || edgeRes.Outcome == "unknown" || edgeRes.Outcome == "reconciling" {
		return d.markUnknown(ctx, intent, lease.Fence, started, edgeRes, rpcErr)
	}
	if edgeRes.Outcome == "hold" {
		return d.finalizeKnownHold(ctx, intent, lease.Fence, started, edgeRes)
	}
	if err := validateAppliedReadback(*intent, edgeRes); err != nil {
		return d.markUnknown(ctx, intent, lease.Fence, started, edgeRes, err)
	}
	return d.finalizeApplied(ctx, intent, lease.Fence, started, edgeRes)
}

func (d *Dispatcher) renewLease(ctx context.Context, intentID, leaseID string, done <-chan struct{}, cancel context.CancelFunc) error {
	interval := d.leaseTTL / 3
	if interval < time.Second {
		interval = time.Second
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-done:
			return nil
		case <-ctx.Done():
			if errors.Is(ctx.Err(), context.Canceled) {
				return nil
			}
			return ctx.Err()
		case <-ticker.C:
			ok, err := d.pool.RenewIntentLease(ctx, intentID, leaseID, d.leaseTTL)
			if err != nil || !ok {
				if err == nil {
					err = errors.New("dispatcher: effect lease renewal fence lost")
				}
				cancel()
				return err
			}
		}
	}
}

func (d *Dispatcher) holdBeforeSideEffect(ctx context.Context, intent *Intent, reason string) error {
	return d.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		tag, err := tx.Exec(ctx, `UPDATE effect_intents SET claim_state='hold',reason_code=$1
		 WHERE effect_intent_id=$2 AND operation_id=$3 AND claim_state='unclaimed'`,
			reason, intent.EffectIntentID, intent.OperationID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return errors.New("dispatcher: preflight hold CAS conflict")
		}
		for _, projector := range d.projectors {
			if projector != nil {
				if err := projector.ProjectIntentState(ctx, tx, intent.EffectIntentID, ClaimState("hold"), AttemptHold, reason); err != nil {
					return err
				}
			}
		}
		return nil
	})
}

func (d *Dispatcher) loadIntent(ctx context.Context, intentID string) (*Intent, error) {
	var intent Intent
	var actorIssuer, actorSubject, fenceJSON, payloadJSON string
	var claimState, fleetOperationID *string
	var claimLease *string
	var claimExpires *int64
	err := d.pool.Pool.QueryRow(ctx, `
		SELECT effect_intent_id, operation_id, proposal_id, decision_id, target_id,
		       fleet_operation_id, is_fleet_parent, fence, effect_digest,
		       authorization_digest, effect_kind, risk_level, required_write_atomicity, deadline_unix_ms,
		       claim_state, claim_lease_id, claim_expires_at_unix_ms,
		       actor_issuer, actor_subject, trace_id,effect_payload::text
		FROM effect_intents WHERE effect_intent_id = $1`, intentID).
		Scan(&intent.EffectIntentID, &intent.OperationID, &intent.ProposalID,
			&intent.DecisionID, &intent.TargetID, &fleetOperationID,
			&intent.IsFleetParent, &fenceJSON, &intent.EffectDigest,
			&intent.AuthorizationDigest, &intent.EffectKind, &intent.RiskLevel,
			&intent.RequiredWriteAtomicity, &intent.DeadlineUnixMS, &claimState, &claimLease, &claimExpires,
			&actorIssuer, &actorSubject, &intent.TraceID, &payloadJSON)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, fmt.Errorf("dispatcher: intent %s not found", intentID)
		}
		return nil, fmt.Errorf("dispatcher: load intent: %w", err)
	}
	if fleetOperationID != nil {
		intent.FleetOperationID = *fleetOperationID
	}
	if claimState != nil {
		intent.ClaimState = ClaimState(*claimState)
	}
	if claimLease != nil {
		intent.ClaimLeaseID = *claimLease
	}
	if claimExpires != nil {
		intent.ClaimExpiresAtUnixMS = *claimExpires
	}
	if err := json.Unmarshal([]byte(fenceJSON), &intent.Fence); err != nil {
		return nil, fmt.Errorf("dispatcher: parse fence: %w", err)
	}
	if err := json.Unmarshal([]byte(payloadJSON), &intent.Payload); err != nil {
		return nil, fmt.Errorf("dispatcher: parse effect payload: %w", err)
	}
	intent.Actor = security.Actor{Issuer: actorIssuer, Subject: actorSubject}
	if err := intent.Actor.Validate(); err != nil {
		return nil, fmt.Errorf("dispatcher: persisted actor: %w", err)
	}
	if intent.IsFleetParent {
		if !validSHA256(intent.EffectDigest) || intent.ClaimState != "" {
			return nil, errors.New("dispatcher: malformed/non-fenced fleet parent projection")
		}
		return &intent, nil
	}
	if err := ValidateEffectPayload(intent.Payload); err != nil {
		return nil, fmt.Errorf("dispatcher: persisted effect payload invalid: %w", err)
	}
	if computed := ComputeEffectDigest(intent); computed != intent.EffectDigest {
		return nil, fmt.Errorf("dispatcher: persisted effect payload digest mismatch: computed=%s stored=%s", computed, intent.EffectDigest)
	}
	return &intent, nil
}

func validateEffectResultIdentity(intent Intent, result EdgeEffectResult) error {
	if result.OperationID != intent.OperationID || result.TargetID != intent.TargetID || result.EffectDigest != intent.EffectDigest {
		return fmt.Errorf("dispatcher: Edge result identity/fence mismatch")
	}
	if !validSHA256(result.ResultDigest) {
		return fmt.Errorf("dispatcher: Edge result digest malformed")
	}
	return nil
}

func validateAppliedReadback(intent Intent, result EdgeEffectResult) error {
	if result.Outcome != "applied" {
		return fmt.Errorf("dispatcher: Edge did not prove applied outcome: %s", result.Outcome)
	}
	if !validSHA256(result.ReadbackDigest) || result.ExpectedEntries < 0 || result.ExpectedEntries > 4096 ||
		result.ObservedEntries < 0 || result.ObservedEntries > 4096 || result.MismatchedEntries < 0 ||
		result.MismatchedEntries > 4096 || result.ExpectedEntries != result.ObservedEntries || result.MismatchedEntries != 0 {
		return fmt.Errorf("dispatcher: Edge readback is not exact")
	}
	return validateAppliedReadbackManifest(intent, result)
}

func (d *Dispatcher) finalizeApplied(ctx context.Context, intent *Intent, leaseID string, started int64, edgeRes EdgeEffectResult) (DispatchResult, error) {
	status := AttemptApplied
	reason := "APPLIED"
	if edgeRes.MismatchedEntries > 0 {
		status = AttemptReconciling
		reason = "READBACK_MISMATCH"
	}
	committedAt := d.now().UnixMilli()
	err := d.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		// CAS finalize: only the current lease may set the state.
		tag, err := tx.Exec(ctx, `
			UPDATE effect_intents
			SET claim_state = CASE WHEN $1 = 'applied' THEN 'finalized' ELSE 'unknown' END
			WHERE effect_intent_id = $2 AND claim_lease_id = $3`,
			string(status), intent.EffectIntentID, leaseID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			return fmt.Errorf("dispatcher: finalize CAS conflict (stale lease)")
		}
		if err := d.insertAttempt(ctx, tx, intent, status, leaseID, started, edgeRes, reason); err != nil {
			return err
		}
		if err := d.projectStateTx(ctx, tx, intent.EffectIntentID, ClaimFinalized, status, reason); err != nil {
			return err
		}
		return insertEffectAckTx(ctx, tx, intent, edgeRes, committedAt)
	})
	if err != nil {
		return DispatchResult{IntentID: intent.EffectIntentID, Status: AttemptReconciling, Reconciling: true, ReasonCode: "FINALIZE_ERROR"}, err
	}
	if err := d.edge.AcknowledgeEffect(ctx, *intent, edgeRes, intent.EffectIntentID, committedAt); err != nil {
		_ = d.recordAckFailure(ctx, intent.OperationID, err)
		return DispatchResult{IntentID: intent.EffectIntentID, Status: status, Readback: edgeRes.ReadbackDigest, Reconciling: true, ReasonCode: "EDGE_ACK_PENDING"}, err
	}
	if err := d.markAcked(ctx, intent.OperationID); err != nil {
		return DispatchResult{IntentID: intent.EffectIntentID, Status: status, Readback: edgeRes.ReadbackDigest, Reconciling: true, ReasonCode: "ACK_PROJECTION_ERROR"}, err
	}
	return DispatchResult{IntentID: intent.EffectIntentID, Status: status, Readback: edgeRes.ReadbackDigest, ReasonCode: reason}, nil
}

func (d *Dispatcher) finalizeKnownHold(ctx context.Context, intent *Intent, leaseID string, started int64, edgeRes EdgeEffectResult) (DispatchResult, error) {
	committedAt := d.now().UnixMilli()
	err := d.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		tag, err := tx.Exec(ctx, `UPDATE effect_intents SET claim_state='hold'
			WHERE effect_intent_id=$1 AND claim_lease_id=$2`, intent.EffectIntentID, leaseID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return errors.New("dispatcher: hold finalize CAS conflict")
		}
		if err := d.insertAttempt(ctx, tx, intent, AttemptHold, leaseID, started, edgeRes, "EDGE_HOLD"); err != nil {
			return err
		}
		if err := d.projectStateTx(ctx, tx, intent.EffectIntentID, ClaimState("hold"), AttemptHold, "EDGE_HOLD"); err != nil {
			return err
		}
		return insertEffectAckTx(ctx, tx, intent, edgeRes, committedAt)
	})
	if err != nil {
		return DispatchResult{IntentID: intent.EffectIntentID, Status: AttemptHold, ReasonCode: "HOLD_FINALIZE_ERROR"}, err
	}
	if err := d.edge.AcknowledgeEffect(ctx, *intent, edgeRes, intent.EffectIntentID, committedAt); err != nil {
		_ = d.recordAckFailure(ctx, intent.OperationID, err)
		return DispatchResult{IntentID: intent.EffectIntentID, Status: AttemptHold, Reconciling: true, ReasonCode: "EDGE_ACK_PENDING"}, err
	}
	if err := d.markAcked(ctx, intent.OperationID); err != nil {
		return DispatchResult{IntentID: intent.EffectIntentID, Status: AttemptHold, Reconciling: true, ReasonCode: "ACK_PROJECTION_ERROR"}, err
	}
	return DispatchResult{IntentID: intent.EffectIntentID, Status: AttemptHold, ReasonCode: "EDGE_HOLD"}, nil
}

func (d *Dispatcher) finalizePreflightHold(ctx context.Context, intent *Intent, leaseID string, started int64) (DispatchResult, error) {
	const reason = "EDGE_PREFLIGHT_HOLD"
	edgeRes := EdgeEffectResult{ActiveBank: -1}
	err := d.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		tag, err := tx.Exec(ctx, `UPDATE effect_intents SET claim_state='hold',reason_code=$1
			WHERE effect_intent_id=$2 AND claim_lease_id=$3`, reason, intent.EffectIntentID, leaseID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return errors.New("dispatcher: preflight hold finalize CAS conflict")
		}
		if err := d.insertAttempt(ctx, tx, intent, AttemptHold, leaseID, started, edgeRes, reason); err != nil {
			return err
		}
		return d.projectStateTx(ctx, tx, intent.EffectIntentID, ClaimState("hold"), AttemptHold, reason)
	})
	if err != nil {
		return DispatchResult{IntentID: intent.EffectIntentID, Status: AttemptHold, ReasonCode: "PREFLIGHT_HOLD_FINALIZE_ERROR"}, err
	}
	return DispatchResult{IntentID: intent.EffectIntentID, Status: AttemptHold, ReasonCode: reason}, nil
}

func (d *Dispatcher) markUnknown(ctx context.Context, intent *Intent, leaseID string, started int64, edgeRes EdgeEffectResult, rpcErr error) (DispatchResult, error) {
	// Timeout-after-effect: mark unknown and reconcile along the ORIGINAL
	// operation. No second intent, no blind retry (§5.2).
	_ = rpcErr
	err := d.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		tag, err := tx.Exec(ctx, `
			UPDATE effect_intents SET claim_state = 'unknown'
			WHERE effect_intent_id = $1 AND claim_lease_id = $2`,
			intent.EffectIntentID, leaseID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			return fmt.Errorf("dispatcher: mark-unknown CAS conflict (stale lease)")
		}
		if err := d.insertAttempt(ctx, tx, intent, AttemptUnknown, leaseID, started, edgeRes, "TIMEOUT_UNKNOWN"); err != nil {
			return err
		}
		return d.projectStateTx(ctx, tx, intent.EffectIntentID, ClaimUnknown, AttemptUnknown, "TIMEOUT_UNKNOWN")
	})
	if err != nil {
		return DispatchResult{IntentID: intent.EffectIntentID, Status: AttemptReconciling, Reconciling: true, ReasonCode: "MARK_UNKNOWN_ERROR"}, err
	}
	return DispatchResult{IntentID: intent.EffectIntentID, Status: AttemptUnknown, Reconciling: true, ReasonCode: "TIMEOUT_UNKNOWN"}, nil
}

func (d *Dispatcher) projectState(ctx context.Context, intentID string, state ClaimState, outcome AttemptStatus, reason string) error {
	if len(d.projectors) == 0 {
		return nil
	}
	return d.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		return d.projectStateTx(ctx, tx, intentID, state, outcome, reason)
	})
}

func (d *Dispatcher) projectStateTx(ctx context.Context, tx *db.Tx, intentID string, state ClaimState, outcome AttemptStatus, reason string) error {
	for _, projector := range d.projectors {
		if projector == nil {
			continue
		}
		if err := projector.ProjectIntentState(ctx, tx, intentID, state, outcome, reason); err != nil {
			return err
		}
	}
	return nil
}

func (d *Dispatcher) insertAttempt(ctx context.Context, tx *db.Tx, intent *Intent, status AttemptStatus, leaseID string, started int64, edgeRes EdgeEffectResult, reason string) error {
	var attemptNumber int
	if err := tx.QueryRow(ctx, `SELECT COALESCE(MAX(attempt_number),0)+1 FROM effect_attempts WHERE intent_id=$1`,
		intent.EffectIntentID).Scan(&attemptNumber); err != nil {
		return err
	}
	if attemptNumber < 1 || attemptNumber > 64 {
		return errors.New("dispatcher: effect attempt bound exhausted")
	}
	attemptID := "att-" + shortID(fmt.Sprintf("%s:%d:%s", intent.EffectIntentID, attemptNumber, leaseID))
	finished := d.now().UnixMilli()
	var activeBank any
	if edgeRes.ActiveBank >= 0 {
		activeBank = edgeRes.ActiveBank
	}
	_, err := tx.Exec(ctx, `
			INSERT INTO effect_attempts (
				attempt_id, intent_id, operation_id, attempt_number, status,
				plan_digest, readback_digest, expected_entries, observed_entries,
				mismatched_entries, active_bank, started_at_unix_ms, finished_at_unix_ms,
				trace_id, reason_code,readback_manifest_digest,readback_entry_count)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)`,
		attemptID, intent.EffectIntentID, intent.OperationID, attemptNumber, string(status),
		intent.EffectDigest, nullableString(edgeRes.ReadbackDigest),
		edgeRes.ExpectedEntries, edgeRes.ObservedEntries, edgeRes.MismatchedEntries,
		activeBank, started, finished, intent.TraceID, reason,
		nullableString(edgeRes.ReadbackManifestDigest), len(edgeRes.AppliedEntries))
	if err != nil {
		return err
	}
	return persistReadbackManifestTx(ctx, tx, attemptID, intent, edgeRes)
}

func insertEffectAckTx(ctx context.Context, tx *db.Tx, intent *Intent, result EdgeEffectResult, committedAtUnixMS int64) error {
	if !validSHA256(result.ResultDigest) {
		return errors.New("dispatcher: canonical Edge result digest required for acknowledgement")
	}
	_, err := tx.Exec(ctx, `INSERT INTO effect_acknowledgements(
		operation_id,effect_intent_id,target_id,result_digest,canonical_effect_reference,
		committed_at_unix_ms,state,attempts,next_attempt_at_unix_ms,trace_id)
		VALUES($1,$2,$3,$4,$2,$5,'pending',0,$5,$6)
		ON CONFLICT(operation_id) DO NOTHING`, intent.OperationID, intent.EffectIntentID,
		intent.TargetID, result.ResultDigest, committedAtUnixMS, intent.TraceID)
	return err
}

func (d *Dispatcher) markAcked(ctx context.Context, operationID string) error {
	tag, err := d.pool.Pool.Exec(ctx, `UPDATE effect_acknowledgements SET state='acked',
		acked_at_unix_ms=$1,last_error='',updated_at=now() WHERE operation_id=$2 AND state='pending'`,
		d.now().UnixMilli(), operationID)
	if err != nil {
		return err
	}
	if tag.RowsAffected() != 1 {
		return errors.New("dispatcher: acknowledgement state CAS conflict")
	}
	return nil
}

func (d *Dispatcher) recordAckFailure(ctx context.Context, operationID string, cause error) error {
	message := cause.Error()
	if len(message) > 512 {
		message = message[:512]
	}
	_, err := d.pool.Pool.Exec(ctx, `UPDATE effect_acknowledgements SET
		attempts=attempts+1,state=CASE WHEN attempts+1>=8 THEN 'exhausted' ELSE 'pending' END,
		next_attempt_at_unix_ms=$1,last_error=$2,updated_at=now()
		WHERE operation_id=$3 AND state='pending'`, d.now().Add(5*time.Second).UnixMilli(), message, operationID)
	return err
}

// RetryPendingAcknowledgements drains a bounded durable ACK outbox. It never
// re-executes P4: only Edge's post-PostgreSQL journal checkpoint RPC is called.
func (d *Dispatcher) RetryPendingAcknowledgements(ctx context.Context, limit int) (int, error) {
	if limit < 1 || limit > 32 {
		return 0, errors.New("dispatcher: acknowledgement retry limit must be 1..32")
	}
	rows, err := d.pool.Pool.Query(ctx, `SELECT operation_id,effect_intent_id,result_digest,
		committed_at_unix_ms,canonical_effect_reference FROM effect_acknowledgements
		WHERE state='pending' AND attempts<8 AND next_attempt_at_unix_ms<=$1
		ORDER BY committed_at_unix_ms LIMIT $2`, d.now().UnixMilli(), limit)
	if err != nil {
		return 0, err
	}
	type pendingAck struct {
		operationID, intentID, resultDigest, canonicalReference string
		committedAt                                             int64
	}
	items := make([]pendingAck, 0, limit)
	for rows.Next() {
		var item pendingAck
		if err := rows.Scan(&item.operationID, &item.intentID, &item.resultDigest, &item.committedAt, &item.canonicalReference); err != nil {
			rows.Close()
			return len(items), err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return 0, err
	}
	rows.Close()
	acked := 0
	for _, item := range items {
		intent, err := d.loadIntent(ctx, item.intentID)
		if err != nil {
			_ = d.recordAckFailure(ctx, item.operationID, err)
			continue
		}
		result := EdgeEffectResult{OperationID: item.operationID, TargetID: intent.TargetID,
			EffectDigest: intent.EffectDigest, ResultDigest: item.resultDigest}
		if err := d.edge.AcknowledgeEffect(ctx, *intent, result, item.canonicalReference, item.committedAt); err != nil {
			_ = d.recordAckFailure(ctx, item.operationID, err)
			continue
		}
		if err := d.markAcked(ctx, item.operationID); err != nil {
			return acked, err
		}
		acked++
	}
	return acked, nil
}

// FenceExpiredClaims converts expired in-flight effects to unknown under the
// original identity. A successor may query readback, but cannot execute the
// side effect again merely because a process lease elapsed.
func (d *Dispatcher) FenceExpiredClaims(ctx context.Context, limit int) (int, error) {
	if limit < 1 || limit > 32 {
		return 0, errors.New("dispatcher: expired claim limit must be 1..32")
	}
	count := 0
	err := d.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		rows, err := tx.Query(ctx, `SELECT effect_intent_id,claim_lease_id FROM effect_intents
			WHERE claim_state='claimed' AND claim_expires_at_unix_ms<=$1
			ORDER BY claim_expires_at_unix_ms FOR UPDATE SKIP LOCKED LIMIT $2`, d.now().UnixMilli(), limit)
		if err != nil {
			return err
		}
		type expired struct{ intentID, leaseID string }
		items := make([]expired, 0, limit)
		for rows.Next() {
			var item expired
			if err := rows.Scan(&item.intentID, &item.leaseID); err != nil {
				rows.Close()
				return err
			}
			items = append(items, item)
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return err
		}
		rows.Close()
		for _, item := range items {
			tag, err := tx.Exec(ctx, `UPDATE effect_intents SET claim_state='unknown',
				claim_expires_at_unix_ms=NULL,reason_code='CLAIM_LEASE_EXPIRED_UNKNOWN'
				WHERE effect_intent_id=$1 AND claim_state='claimed' AND claim_lease_id=$2`,
				item.intentID, item.leaseID)
			if err != nil {
				return err
			}
			if tag.RowsAffected() == 1 {
				if err := d.projectStateTx(ctx, tx, item.intentID, ClaimUnknown, AttemptUnknown, "CLAIM_LEASE_EXPIRED_UNKNOWN"); err != nil {
					return err
				}
				count++
			}
		}
		return nil
	})
	return count, err
}

// FenceOwnedClaimsOnShutdown conservatively turns this process's unfinished
// claims into unknown. A successor may only reconcile the original operation;
// it cannot blindly reclaim and re-execute it.
func (d *Dispatcher) FenceOwnedClaimsOnShutdown(ctx context.Context) (int64, error) {
	tag, err := d.pool.Pool.Exec(ctx, `UPDATE effect_intents SET claim_state='unknown',
		claim_expires_at_unix_ms=NULL WHERE claim_state='claimed' AND claim_lease_id LIKE $1`, d.owner+":%")
	if err != nil {
		return 0, err
	}
	return tag.RowsAffected(), nil
}

func nullableString(s string) any {
	if s == "" {
		return nil
	}
	return s
}
