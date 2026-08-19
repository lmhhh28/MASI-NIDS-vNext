package target

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/governance"
	"masi-nids/control-go/internal/security"
)

// FleetCoordinator freezes the target set and ordered waves, and forms the
// non-claimable parent + bounded per-target child intents in ONE short
// transaction. The parent projects its aggregate status from the FULL child
// vector: any child unknown/reconciling -> parent reconciling; there is no
// cross-target atomic commit and no majority-success = applied (ADR-0015).
type FleetCoordinator struct {
	pool *db.Pool
}

func NewFleetCoordinator(pool *db.Pool) *FleetCoordinator {
	return &FleetCoordinator{pool: pool}
}

// ProjectAggregate derives the parent aggregate status from the full child
// vector. This is pure logic (no DB), so it is unit-testable and is the single
// authority for the projection — the UI/API always returns the child vector,
// never a majority/average/green-badge.
//
// Rules:
//   - any child unknown/reconciling -> reconciling
//   - all children applied -> applied
//   - any child failed and failure_policy fail-fast -> failed
//   - all children terminal, some hold/skipped -> partial
//   - otherwise -> running
func ProjectAggregate(children []ChildIntent, waves []Wave) AggregateStatus {
	if len(children) == 0 {
		return AggregatePlanned
	}
	applied, failed, hold, skipped, blocked, pending, unknownOrReconciling := 0, 0, 0, 0, 0, 0, 0
	failFastStopped := false
	for _, c := range children {
		switch c.Status {
		case ChildApplied:
			applied++
		case ChildFailed:
			failed++
			if waveFailurePolicy(waves, c.WaveIndex) == FailFast {
				failFastStopped = true
			}
		case ChildHold:
			hold++
			if waveFailurePolicy(waves, c.WaveIndex) == FailFast {
				failFastStopped = true
			}
		case ChildSkipped:
			skipped++
		case ChildBlocked:
			blocked++
			if waveFailurePolicy(waves, c.WaveIndex) == FailFast {
				failFastStopped = true
			}
		case ChildUnknown, ChildReconciling:
			unknownOrReconciling++
		case ChildPending, ChildClaimed, ChildExecuting:
			pending++
		}
	}
	if unknownOrReconciling > 0 {
		return AggregateReconciling
	}
	if applied == len(children) {
		return AggregateApplied
	}
	if failed > 0 || failFastStopped {
		// fail-fast: any failed child blocks unstarted children; but the
		// aggregate is failed only if no unknown/reconciling (already handled).
		return AggregateFailed
	}
	if pending > 0 {
		return AggregateRunning
	}
	// All terminal, not all applied, no failed -> partial (hold/skipped mix).
	if hold > 0 || skipped > 0 || blocked > 0 {
		return AggregatePartial
	}
	return AggregateRunning
}

func waveFailurePolicy(waves []Wave, waveIndex int) FailurePolicy {
	if waveIndex >= 0 && waveIndex < len(waves) {
		return waves[waveIndex].FailurePolicy
	}
	return ""
}

// ProjectIntentState implements governance.IntentStateProjector. It updates the
// fleet child and parent projection in the caller's canonical effect
// transaction. Non-fleet intents are a no-op.
func (s *FleetCoordinator) ProjectIntentState(ctx context.Context, tx *db.Tx, intentID string, state governance.ClaimState, outcome governance.AttemptStatus, reasonCode string) error {
	status, relevant, err := fleetChildStatus(state, outcome)
	if err != nil || !relevant {
		return err
	}
	if reasonCode == "" {
		reasonCode = strings.ToUpper(strings.ReplaceAll(string(status), "-", "_"))
	}
	var fleetOpID string
	var waveIndex int
	tag, err := tx.Exec(ctx, `
		UPDATE fleet_child_intents SET status=$1,reason_code=$2,updated_at=now()
		WHERE child_intent_id=$3`, string(status), reasonCode, intentID)
	if err != nil {
		return fmt.Errorf("fleet: project child state: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return nil
	}
	if err := tx.QueryRow(ctx, `SELECT fleet_operation_id,wave_index FROM fleet_child_intents WHERE child_intent_id=$1`, intentID).
		Scan(&fleetOpID, &waveIndex); err != nil {
		return fmt.Errorf("fleet: load projected child: %w", err)
	}

	var wavesJSON string
	if err := tx.QueryRow(ctx, `SELECT waves::text FROM fleet_operations WHERE fleet_operation_id=$1 FOR UPDATE`, fleetOpID).Scan(&wavesJSON); err != nil {
		return fmt.Errorf("fleet: lock parent projection: %w", err)
	}
	var waves []Wave
	if err := json.Unmarshal([]byte(wavesJSON), &waves); err != nil {
		return fmt.Errorf("fleet: parse parent waves: %w", err)
	}
	if waveFailurePolicy(waves, waveIndex) == FailFast &&
		(status == ChildFailed || status == ChildHold || status == ChildBlocked) {
		// Stop only unstarted future-wave children. Already claimed/executing/
		// unknown/applied rows retain their exact state.
		if _, err := tx.Exec(ctx, `
			UPDATE effect_intents i SET claim_state='blocked',gate_open=false
			FROM fleet_child_intents fc
			WHERE fc.child_intent_id=i.effect_intent_id AND fc.fleet_operation_id=$1
			  AND fc.wave_index>$2 AND fc.status='pending' AND i.claim_state='unclaimed'`, fleetOpID, waveIndex); err != nil {
			return fmt.Errorf("fleet: block future effect intents: %w", err)
		}
		if _, err := tx.Exec(ctx, `
			UPDATE fleet_child_intents SET status='blocked',reason_code='FAIL_FAST_BLOCKED',gate_open=false,updated_at=now()
			WHERE fleet_operation_id=$1 AND wave_index>$2 AND status='pending'`, fleetOpID, waveIndex); err != nil {
			return fmt.Errorf("fleet: block future child vector: %w", err)
		}
	}

	rows, err := tx.Query(ctx, `SELECT target_id,child_intent_id,wave_index,status,reason_code
		FROM fleet_child_intents WHERE fleet_operation_id=$1 ORDER BY wave_index,target_id`, fleetOpID)
	if err != nil {
		return fmt.Errorf("fleet: load full child vector: %w", err)
	}
	defer rows.Close()
	children := make([]ChildIntent, 0)
	for rows.Next() {
		var child ChildIntent
		if err := rows.Scan(&child.TargetID, &child.IntentID, &child.WaveIndex, &child.Status, &child.ReasonCode); err != nil {
			return err
		}
		children = append(children, child)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	aggregate := ProjectAggregate(children, waves)
	tag, err = tx.Exec(ctx, `UPDATE fleet_operations SET aggregate_status=$1,updated_at=now() WHERE fleet_operation_id=$2`,
		string(aggregate), fleetOpID)
	if err != nil {
		return fmt.Errorf("fleet: update parent projection: %w", err)
	}
	if tag.RowsAffected() != 1 {
		return errors.New("fleet: parent projection missing")
	}
	return nil
}

func fleetChildStatus(state governance.ClaimState, outcome governance.AttemptStatus) (ChildStatus, bool, error) {
	switch state {
	case governance.ClaimClaimed:
		return ChildClaimed, true, nil
	case governance.ClaimExecuting:
		return ChildExecuting, true, nil
	case governance.ClaimUnknown:
		return ChildUnknown, true, nil
	case governance.ClaimFinalized:
		if outcome == governance.AttemptApplied {
			return ChildApplied, true, nil
		}
		if outcome == governance.AttemptReconciling {
			return ChildReconciling, true, nil
		}
		return "", false, fmt.Errorf("fleet: finalized intent has invalid outcome %q", outcome)
	case governance.ClaimState("hold"):
		return ChildHold, true, nil
	case governance.ClaimState("blocked"):
		return ChildBlocked, true, nil
	case governance.ClaimFenced:
		return ChildHold, true, nil
	default:
		return "", false, nil
	}
}

// CreateFleetOperation freezes the target set + waves and forms the
// non-claimable parent + bounded per-target child intents in ONE short
// transaction. Future-wave children are durable but their gate is closed (not
// claimable) until AdvanceWaveGate opens them.
func (s *FleetCoordinator) CreateFleetOperation(ctx context.Context, fo FleetOperation) (*FleetOperation, error) {
	return nil, errors.New("fleet: direct creation is disabled; Decision and full child vector must commit atomically")
}

// CreateApprovedFleetOperation appends the exact approved Decision, the
// non-claimable parent, and the complete bounded per-target child vector in one
// serializable transaction. No external call is allowed in the callback.
func (s *FleetCoordinator) CreateApprovedFleetOperation(ctx context.Context, decisions *governance.DecisionService,
	proposalID string, approver security.Actor, authz governance.AuthzContext, decisionReason string,
	fo FleetOperation) (*FleetOperation, *governance.Decision, error) {
	if decisions == nil {
		return nil, nil, errors.New("fleet: decision service required")
	}
	fo.Actor = approver
	wavesJSON, err := prepareFleetOperation(&fo)
	if err != nil {
		return nil, nil, err
	}
	decision, err := decisions.ApproveWithReasonAtomic(ctx, proposalID, approver, authz, decisionReason,
		func(ctx context.Context, tx *db.Tx, decision *governance.Decision, inserted bool) error {
			if !decision.Approved {
				return errors.New("fleet: canonical decision is not approval")
			}
			var effectKind, risk, scope, targetSetDigest, policyDigest string
			var targetIDs []string
			var proposalExpiry int64
			if err := tx.QueryRow(ctx, `SELECT effect_kind,risk_level,scope,target_set_digest,policy_digest,target_ids,expires_at_unix_ms
				FROM effect_proposals WHERE proposal_id=$1 FOR SHARE`, proposalID).Scan(&effectKind, &risk, &scope,
				&targetSetDigest, &policyDigest, &targetIDs, &proposalExpiry); err != nil {
				return err
			}
			allTargets := make([]string, 0)
			for _, wave := range fo.Waves {
				allTargets = append(allTargets, wave.TargetIDs...)
			}
			if scope != fo.Scope || risk != string(decision.RiskLevel) || targetSetDigest != fo.TargetSetDigest ||
				fleetTargetSetDigest(targetIDs) != fo.TargetSetDigest || fleetTargetSetDigest(allTargets) != fo.TargetSetDigest {
				return errors.New("fleet: proposal scope/risk/target vector mismatch")
			}
			bindFleetDecision(&fo, proposalID, decision, governance.EffectKind(effectKind), proposalExpiry)
			for index := range fo.ChildIntents {
				intent := &fo.ChildIntents[index].Intent
				payload, err := governance.LoadCanonicalEffectPayloadTx(ctx, tx, governance.EffectKind(effectKind),
					policyDigest, fo.ChildIntents[index].TargetID, fo.Scope, intent.EffectIntentID)
				if err != nil {
					return err
				}
				intent.Payload = payload
				intent.EffectDigest = governance.ComputeEffectDigest(*intent)
				if intent.EffectDigest == "" {
					return errors.New("fleet: canonical child effect digest unavailable")
				}
			}
			if !inserted {
				var storedDigest, storedParent, storedDecision string
				err := tx.QueryRow(ctx, `SELECT fo.target_set_digest,fo.parent_intent_id,i.decision_id
					FROM fleet_operations fo JOIN effect_intents i ON i.effect_intent_id=fo.parent_intent_id
					WHERE fo.fleet_operation_id=$1`, fo.FleetOperationID).Scan(&storedDigest, &storedParent, &storedDecision)
				if err != nil || storedDigest != fo.TargetSetDigest || storedParent != fo.ParentIntentID || storedDecision != decision.DecisionID {
					return errors.New("fleet: existing Decision has no identical atomic fleet vector")
				}
				return nil
			}
			return insertFleetOperationTx(ctx, tx, fo, wavesJSON)
		})
	if err != nil {
		return nil, nil, fmt.Errorf("fleet: atomic approve/create: %w", err)
	}
	return &fo, decision, nil
}

func prepareFleetOperation(fo *FleetOperation) ([]byte, error) {
	if err := validateFleetOperation(*fo); err != nil {
		return nil, err
	}
	if err := fo.Actor.Validate(); err != nil || fo.Scope == "" {
		return nil, errors.New("fleet: actor and scope required")
	}
	if len(fo.ChildIntents) == 0 {
		return nil, errors.New("fleet: operation must contain one exact child intent per target")
	}
	fo.AggregateStatus = AggregatePlanned
	for i := range fo.Waves {
		if i == 0 {
			fo.Waves[i].GateState = GateOpen
		} else {
			fo.Waves[i].GateState = GateClosed
		}
	}
	wavesJSON, err := json.Marshal(fo.Waves)
	if err != nil {
		return nil, fmt.Errorf("fleet: marshal waves: %w", err)
	}
	fo.ParentIntentID = "fparent-" + shortID(fo.FleetOperationID)
	fo.ParentIntent.EffectIntentID = fo.ParentIntentID
	fo.ParentIntent.FleetOperationID = fo.FleetOperationID
	fo.ParentIntent.IsFleetParent = true
	fo.ParentIntent.TargetID = "fleet:" + fo.FleetOperationID
	fo.ParentIntent.Actor = fo.Actor
	if fo.ParentIntent.RequiredWriteAtomicity == "" {
		fo.ParentIntent.RequiredWriteAtomicity = "CONTINUE_ON_ERROR"
	}
	for i := range fo.ChildIntents {
		if fo.ChildIntents[i].Status == "" {
			fo.ChildIntents[i].Status = ChildPending
		}
		if fo.ChildIntents[i].ReasonCode == "" {
			fo.ChildIntents[i].ReasonCode = "PENDING"
		}
	}
	return wavesJSON, nil
}

func bindFleetDecision(fo *FleetOperation, proposalID string, decision *governance.Decision,
	effectKind governance.EffectKind, proposalExpiry int64) {
	fo.Actor = decision.Actor
	fo.ParentIntent.ProposalID = proposalID
	fo.ParentIntent.DecisionID = decision.DecisionID
	fo.ParentIntent.AuthorizationDigest = decision.DecisionDigest
	fo.ParentIntent.EffectKind = effectKind
	fo.ParentIntent.RiskLevel = decision.RiskLevel
	fo.ParentIntent.Actor = decision.Actor
	if fo.ParentIntent.OperationID == "" {
		fo.ParentIntent.OperationID = "fop-" + shortID(fo.FleetOperationID)
	}
	deadline := decision.ExpiresAtUnixMS
	if proposalExpiry < deadline {
		deadline = proposalExpiry
	}
	fo.ParentIntent.DeadlineUnixMS = deadline
	fo.ParentIntent.EffectDigest = fleetParentDigest(*fo, decision.DecisionDigest)
	for index := range fo.ChildIntents {
		intent := &fo.ChildIntents[index].Intent
		intent.EffectIntentID = fo.ChildIntents[index].IntentID
		intent.ProposalID = proposalID
		intent.DecisionID = decision.DecisionID
		intent.AuthorizationDigest = decision.DecisionDigest
		intent.EffectKind = effectKind
		intent.RiskLevel = decision.RiskLevel
		intent.Actor = decision.Actor
		intent.TargetID = fo.ChildIntents[index].TargetID
		intent.FleetOperationID = fo.FleetOperationID
		if intent.DeadlineUnixMS < 1 || intent.DeadlineUnixMS > deadline {
			intent.DeadlineUnixMS = deadline
		}
	}
}

func fleetParentDigest(fo FleetOperation, authorizationDigest string) string {
	wavesJSON, _ := json.Marshal(fo.Waves)
	h := sha256.New()
	fmt.Fprintf(h, "%s|%s|%s|%s", fo.FleetOperationID, fo.TargetSetDigest, authorizationDigest, wavesJSON)
	return "sha256:" + hex.EncodeToString(h.Sum(nil))
}

func insertFleetOperationTx(ctx context.Context, tx *db.Tx, fo FleetOperation, wavesJSON []byte) error {
	// The non-claimable parent intent row lives in effect_intents (migration
	// 0002) with is_fleet_parent=true and claim_state=NULL. The caller's
	// governance flow creates the Decision; here we record the fleet
	// operation + child mapping.
	if err := insertFleetIntent(ctx, tx, fo.ParentIntent, false); err != nil {
		return fmt.Errorf("insert parent effect intent: %w", err)
	}
	_, err := tx.Exec(ctx, `
			INSERT INTO fleet_operations (
				fleet_operation_id, target_set_digest, wave_count, waves,
				parent_intent_id, aggregate_status, actor_ref, scope, reason_code,
				trace_id, created_at_unix_ms)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)`,
		fo.FleetOperationID, fo.TargetSetDigest, fo.WaveCount, wavesJSON,
		fo.ParentIntentID, string(fo.AggregateStatus), fo.Actor.String(), fo.Scope,
		"FLEET_PLANNED", fo.TraceID, time.Now().UnixMilli())
	if err != nil {
		return err
	}
	for _, c := range fo.ChildIntents {
		intent := c.Intent
		intent.EffectIntentID = c.IntentID
		intent.TargetID = c.TargetID
		intent.FleetOperationID = fo.FleetOperationID
		intent.IsFleetParent = false
		if intent.RequiredWriteAtomicity == "" {
			intent.RequiredWriteAtomicity = "CONTINUE_ON_ERROR"
		}
		gateOpen := c.WaveIndex == 0
		if err := insertFleetIntent(ctx, tx, intent, gateOpen); err != nil {
			return fmt.Errorf("insert child effect intent %s: %w", c.IntentID, err)
		}
		_, err := tx.Exec(ctx, `
					INSERT INTO fleet_child_intents (
						fleet_operation_id, target_id, effect_digest, child_intent_id,
						wave_index, status, reason_code, gate_open)
					VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
			fo.FleetOperationID, c.TargetID, intent.EffectDigest, c.IntentID, c.WaveIndex,
			string(c.Status), c.ReasonCode, gateOpen)
		if err != nil {
			return err
		}
	}
	return nil
}

// AdvanceWaveGate CAS-opens the next wave's gate only when the current wave's
// FULL child vector has reached a terminal set (no unknown/reconciling, no
// pending/claimed/executing), the deadline is unexpired, and the scope/target-
// set digest has not drifted. There is no cross-target atomic gate.
func (s *FleetCoordinator) AdvanceWaveGate(ctx context.Context, fleetOpID string, currentWave, nextWave int) error {
	if currentWave < 0 || nextWave != currentWave+1 {
		return fmt.Errorf("fleet: next wave must be current+1")
	}
	var result error
	err := s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		// Verify current-wave full child vector is terminal.
		rows, err := tx.Query(ctx, `
			SELECT status FROM fleet_child_intents
			WHERE fleet_operation_id = $1 AND wave_index = $2`, fleetOpID, currentWave)
		if err != nil {
			return err
		}
		defer rows.Close()
		count := 0
		terminal := 0
		for rows.Next() {
			var st string
			if err := rows.Scan(&st); err != nil {
				return err
			}
			count++
			if isTerminalChild(ChildStatus(st)) {
				terminal++
			}
		}
		if err := rows.Err(); err != nil {
			return err
		}
		if count == 0 {
			return errors.New("fleet: current wave has no children")
		}
		if terminal != count {
			return errors.New("fleet: current wave not fully terminal (unknown/reconciling/pending present)")
		}
		// CAS-open the next wave gate by updating the fleet_operations waves JSONB.
		// The waves array is the frozen plan; only gate_state transitions.
		var wavesJSON, targetSetDigest, scope string
		if err := tx.QueryRow(ctx, `SELECT waves,target_set_digest,scope FROM fleet_operations WHERE fleet_operation_id = $1 FOR UPDATE`, fleetOpID).Scan(&wavesJSON, &targetSetDigest, &scope); err != nil {
			return err
		}
		var waves []Wave
		if err := json.Unmarshal([]byte(wavesJSON), &waves); err != nil {
			return fmt.Errorf("fleet: parse waves: %w", err)
		}
		if nextWave >= len(waves) {
			return errors.New("fleet: next wave index out of range")
		}
		allTargets := []string{}
		for _, wave := range waves {
			allTargets = append(allTargets, wave.TargetIDs...)
		}
		if fleetTargetSetDigest(allTargets) != targetSetDigest {
			return errors.New("fleet: frozen target-set digest drift")
		}
		if waves[currentWave].FailurePolicy == FailFast {
			var nonApplied int
			if err := tx.QueryRow(ctx, `SELECT count(*) FROM fleet_child_intents WHERE fleet_operation_id=$1 AND wave_index=$2 AND status<>'applied'`, fleetOpID, currentWave).Scan(&nonApplied); err != nil {
				return err
			}
			if nonApplied > 0 {
				return errors.New("fleet: fail-fast wave has non-applied child; next gate remains closed")
			}
		}
		var nextValid bool
		if err := tx.QueryRow(ctx, `SELECT count(*)>0 AND bool_and(i.deadline_unix_ms>$3 AND d.decision='approve'
		 AND d.expires_at_unix_ms>$3 AND p.expires_at_unix_ms>$3 AND p.scope=$4)
		 FROM fleet_child_intents fc JOIN effect_intents i ON i.effect_intent_id=fc.child_intent_id
		 JOIN effect_decisions d ON d.decision_id=i.decision_id JOIN effect_proposals p ON p.proposal_id=i.proposal_id
		 WHERE fc.fleet_operation_id=$1 AND fc.wave_index=$2`, fleetOpID, nextWave, time.Now().UnixMilli(), scope).Scan(&nextValid); err != nil {
			return err
		}
		if !nextValid {
			return errors.New("fleet: next wave authorization/deadline revalidation failed")
		}
		if waves[nextWave].GateState == GateOpen {
			result = errors.New("fleet: next wave gate already open")
			return nil
		}
		waves[nextWave].GateState = GateOpen
		newJSON, err := json.Marshal(waves)
		if err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `UPDATE fleet_operations SET waves = $1 WHERE fleet_operation_id = $2`, newJSON, fleetOpID)
		if err == nil {
			_, err = tx.Exec(ctx, `
				UPDATE fleet_child_intents SET gate_open=true, updated_at=now()
				WHERE fleet_operation_id=$1 AND wave_index=$2 AND gate_open=false`, fleetOpID, nextWave)
		}
		if err == nil {
			_, err = tx.Exec(ctx, `
				UPDATE effect_intents i SET gate_open=true
				FROM fleet_child_intents fc
				WHERE fc.child_intent_id=i.effect_intent_id
				  AND fc.fleet_operation_id=$1 AND fc.wave_index=$2`, fleetOpID, nextWave)
		}
		result = err
		return err
	})
	if err != nil {
		return fmt.Errorf("fleet: advance gate: %w", err)
	}
	return result
}

// Rollback creates a NEW fleet parent + per-target child intents to the exact
// previous state. It is a new durable operation, never an in-place overwrite of
// the original fleet operation (ADR-0015: rollback = new fleet parent + per-
// target intents to exact previous).
func (s *FleetCoordinator) Rollback(ctx context.Context, originalFleetOpID string, actor security.Actor, scope, traceID string, parent governance.Intent, children []ChildIntent) (string, error) {
	rollbackID := "fo-rollback-" + shortID(originalFleetOpID+traceID)
	// Load the original to copy the frozen target set + exact-previous wave plan.
	var targetSetDigest string
	var wavesJSON string
	err := s.pool.Pool.QueryRow(ctx, `SELECT target_set_digest, waves FROM fleet_operations WHERE fleet_operation_id = $1`, originalFleetOpID).
		Scan(&targetSetDigest, &wavesJSON)
	if err != nil {
		return "", fmt.Errorf("fleet: rollback load original: %w", err)
	}
	var waves []Wave
	if err := json.Unmarshal([]byte(wavesJSON), &waves); err != nil {
		return "", fmt.Errorf("fleet: rollback parse waves: %w", err)
	}
	// Reverse wave order: rollback applies the exact-previous in reverse.
	rollbackWaves := make([]Wave, 0, len(waves))
	for i := len(waves) - 1; i >= 0; i-- {
		w := waves[i]
		w.GateState = GateClosed
		w.WaveIndex = len(waves) - 1 - i
		rollbackWaves = append(rollbackWaves, w)
	}
	rb := FleetOperation{
		FleetOperationID: rollbackID,
		TargetSetDigest:  targetSetDigest,
		WaveCount:        len(rollbackWaves),
		Waves:            rollbackWaves,
		ParentIntent:     parent,
		ChildIntents:     children,
		AggregateStatus:  AggregatePlanned,
		Actor:            actor,
		Scope:            scope,
		TraceID:          traceID,
	}
	if len(children) == 0 {
		return "", errors.New("fleet: rollback requires exact previous per-target child intents")
	}
	if _, err := s.CreateFleetOperation(ctx, rb); err != nil {
		return "", err
	}
	return rollbackID, nil
}

func validateFleetOperation(fo FleetOperation) error {
	if fo.FleetOperationID == "" {
		return errors.New("fleet: fleet_operation_id required")
	}
	if fo.WaveCount < 1 || fo.WaveCount > 64 {
		return errors.New("fleet: wave_count must be 1..64")
	}
	if len(fo.Waves) != fo.WaveCount {
		return errors.New("fleet: wave_count must equal len(waves)")
	}
	targetWave := make(map[string]int)
	for i, w := range fo.Waves {
		if w.WaveIndex != i {
			return fmt.Errorf("fleet: wave %d index mismatch", i)
		}
		if !IsValidFailurePolicy(w.FailurePolicy) {
			return fmt.Errorf("fleet: wave %d bad failure_policy", i)
		}
		if w.ParallelLimit < 1 {
			return fmt.Errorf("fleet: wave %d parallel_limit >= 1 required", i)
		}
		if len(w.TargetIDs) == 0 {
			return fmt.Errorf("fleet: wave %d needs >= 1 target", i)
		}
		if w.ParallelLimit > len(w.TargetIDs) {
			return fmt.Errorf("fleet: wave %d parallel_limit exceeds target count", i)
		}
		for _, targetID := range w.TargetIDs {
			if targetID == "" {
				return fmt.Errorf("fleet: wave %d has empty target", i)
			}
			if _, exists := targetWave[targetID]; exists {
				return fmt.Errorf("fleet: target %s appears in multiple waves", targetID)
			}
			targetWave[targetID] = i
		}
	}
	if len(fo.ChildIntents) > 1024 {
		return errors.New("fleet: too many child intents (max 1024)")
	}
	if len(fo.ChildIntents) > 0 {
		if len(fo.ChildIntents) != len(targetWave) {
			return errors.New("fleet: child intent vector must exactly match frozen target set")
		}
		seenIntent := map[string]struct{}{}
		for _, child := range fo.ChildIntents {
			wave, ok := targetWave[child.TargetID]
			if !ok || wave != child.WaveIndex || child.IntentID == "" {
				return errors.New("fleet: child target/wave/intent identity mismatch")
			}
			if _, exists := seenIntent[child.IntentID]; exists {
				return errors.New("fleet: duplicate child intent id")
			}
			seenIntent[child.IntentID] = struct{}{}
		}
		ids := make([]string, 0, len(targetWave))
		for id := range targetWave {
			ids = append(ids, id)
		}
		if fleetTargetSetDigest(ids) != fo.TargetSetDigest {
			return errors.New("fleet: target_set_digest does not match exact wave membership")
		}
	}
	return nil
}

func insertFleetIntent(ctx context.Context, tx *db.Tx, intent governance.Intent, gateOpen bool) error {
	if intent.ProposalID == "" || intent.DecisionID == "" || intent.OperationID == "" ||
		intent.EffectDigest == "" || intent.AuthorizationDigest == "" || intent.DeadlineUnixMS <= time.Now().UnixMilli() {
		return errors.New("fleet: incomplete or expired governance intent")
	}
	if err := intent.Actor.Validate(); err != nil {
		return err
	}
	if err := governance.ValidateFence(intent.Fence); err != nil {
		return err
	}
	payloadJSON := []byte(`{}`)
	if !intent.IsFleetParent {
		if err := governance.ValidateEffectPayload(intent.Payload); err != nil {
			return err
		}
		if governance.ComputeEffectDigest(intent) != intent.EffectDigest {
			return errors.New("fleet: child effect digest does not bind payload")
		}
		var err error
		payloadJSON, err = json.Marshal(intent.Payload)
		if err != nil {
			return err
		}
	}
	var authorized bool
	if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM effect_proposals p JOIN effect_decisions d ON d.proposal_id=p.proposal_id
	 WHERE p.proposal_id=$1 AND d.decision_id=$2 AND d.decision='approve' AND p.expires_at_unix_ms>$3 AND d.expires_at_unix_ms>$3
	 AND d.decision_digest=$4 AND p.effect_kind=$5 AND p.risk_level=$6 AND d.actor_issuer=$7 AND d.actor_subject=$8
	 AND ($9 OR $10=ANY(p.target_ids)))`, intent.ProposalID, intent.DecisionID, time.Now().UnixMilli(), intent.AuthorizationDigest,
		string(intent.EffectKind), string(intent.RiskLevel), intent.Actor.Issuer, intent.Actor.Subject, intent.IsFleetParent, intent.TargetID).Scan(&authorized); err != nil {
		return err
	}
	if !authorized {
		return errors.New("fleet: intent authorization/target fence mismatch")
	}
	fenceJSON, err := json.Marshal(intent.Fence)
	if err != nil {
		return err
	}
	var claimState any = string(governance.ClaimUnclaimed)
	if intent.IsFleetParent {
		claimState = nil
	}
	_, err = tx.Exec(ctx, `
		INSERT INTO effect_intents (
		 effect_intent_id,operation_id,proposal_id,decision_id,target_id,fleet_operation_id,
		 is_fleet_parent,fence,effect_digest,authorization_digest,effect_kind,risk_level,
		 required_write_atomicity,deadline_unix_ms,claim_state,actor_ref,trace_id,reason_code,
		 actor_issuer,actor_subject,gate_open,effect_payload)
	VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22)`,
		intent.EffectIntentID, intent.OperationID, intent.ProposalID, intent.DecisionID, intent.TargetID,
		intent.FleetOperationID, intent.IsFleetParent, fenceJSON, intent.EffectDigest, intent.AuthorizationDigest,
		string(intent.EffectKind), string(intent.RiskLevel), intent.RequiredWriteAtomicity, intent.DeadlineUnixMS,
		claimState, intent.Actor.String(), intent.TraceID, "FLEET_INTENT_CREATED",
		intent.Actor.Issuer, intent.Actor.Subject, gateOpen, payloadJSON)
	return err
}

func fleetTargetSetDigest(ids []string) string {
	sorted := append([]string(nil), ids...)
	sort.Strings(sorted)
	h := sha256.Sum256([]byte(strings.Join(sorted, ",")))
	return "sha256:" + hex.EncodeToString(h[:])
}

func shortID(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:16])
}
