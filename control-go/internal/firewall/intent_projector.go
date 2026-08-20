package firewall

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/governance"
)

// IntentProjector derives firewall activation/overlay facts inside the
// dispatcher's canonical claim/finalize transaction. It performs no external
// call and creates no queue; effect_intents remains the sole work ledger.
type IntentProjector struct{}

func NewIntentProjector() *IntentProjector { return &IntentProjector{} }

func (p *IntentProjector) ProjectIntentState(ctx context.Context, tx *db.Tx, intentID string, state governance.ClaimState, outcome governance.AttemptStatus, reasonCode string) error {
	var operationID, targetID, kind, policyDigest, payloadJSON string
	err := tx.QueryRow(ctx, `SELECT i.operation_id,i.target_id,i.effect_kind,p.policy_digest,i.effect_payload::text
		FROM effect_intents i JOIN effect_proposals p ON p.proposal_id=i.proposal_id
		WHERE i.effect_intent_id=$1`, intentID).Scan(&operationID, &targetID, &kind, &policyDigest, &payloadJSON)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		return err
	}
	var payload governance.EffectPayload
	if err := json.Unmarshal([]byte(payloadJSON), &payload); err != nil {
		return err
	}
	if err := governance.ValidateEffectPayload(payload); err != nil {
		return err
	}
	switch governance.EffectKind(kind) {
	case governance.KindFirewallBaselineActivate, governance.KindFirewallRollback:
		return p.projectBaseline(ctx, tx, intentID, operationID, targetID, policyDigest, state, outcome, reasonCode)
	case governance.KindFirewallOverlay:
		return p.projectOverlay(ctx, tx, intentID, payload.Operation, outcome, reasonCode)
	default:
		return nil
	}
}

func (p *IntentProjector) projectBaseline(ctx context.Context, tx *db.Tx, intentID, operationID, targetID, policyDigest string, state governance.ClaimState, outcome governance.AttemptStatus, reasonCode string) error {
	var revisionID string
	if err := tx.QueryRow(ctx, `SELECT revision_id FROM firewall_revisions
		WHERE revision_digest=$1 AND target_id=$2`, policyDigest, targetID).Scan(&revisionID); err != nil {
		return fmt.Errorf("firewall projector: exact revision unavailable: %w", err)
	}
	switch {
	case state == governance.ClaimClaimed:
		completed, _ := json.Marshal([]ActivationStage{StagePrepared})
		var expectedVersion int64
		var expectedCurrent *string
		var expectedBank int
		expectedCAS := "sha256:0000000000000000000000000000000000000000000000000000000000000000"
		err := tx.QueryRow(ctx, `SELECT binding_version,current_revision_id,active_bank,cas_digest
			FROM firewall_bindings WHERE target_id=$1 FOR UPDATE`, targetID).
			Scan(&expectedVersion, &expectedCurrent, &expectedBank, &expectedCAS)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		_, err = tx.Exec(ctx, `INSERT INTO firewall_activations(operation_id,target_id,desired_revision_id,
			current_stage,completed_stages,expected_entries,observed_entries,mismatched_entries,
			default_readback,selector_readback,result,reason_code,expected_binding_version,
			expected_current_revision_id,expected_active_bank,expected_cas_digest)
			VALUES($1,$2,$3,'prepared',$4,0,0,0,'missing','old-bank','prepared','CLAIMED',$5,$6,$7,$8)
			ON CONFLICT(operation_id) DO NOTHING`, operationID, targetID, revisionID, completed,
			expectedVersion, expectedCurrent, expectedBank, expectedCAS)
		return err
	case state == governance.ClaimUnknown || outcome == governance.AttemptReconciling:
		if _, err := tx.Exec(ctx, `UPDATE firewall_activations SET result='reconciling',reason_code=$1,updated_at=now()
			WHERE operation_id=$2`, reasonCode, operationID); err != nil {
			return err
		}
		_, err := tx.Exec(ctx, `UPDATE firewall_bindings SET selector_state='reconciling',updated_at=now()
			WHERE target_id=$1`, targetID)
		return err
	case outcome == governance.AttemptHold || state == governance.ClaimState("hold"):
		_, err := tx.Exec(ctx, `UPDATE firewall_activations SET result='hold',reason_code=$1,updated_at=now()
			WHERE operation_id=$2`, reasonCode, operationID)
		return err
	case state == governance.ClaimFinalized && outcome == governance.AttemptApplied:
		var expected, observed, mismatched, activeBank int
		var readbackDigest string
		if err := tx.QueryRow(ctx, `SELECT expected_entries,observed_entries,mismatched_entries,
			active_bank,readback_digest FROM effect_attempts WHERE intent_id=$1 AND status='applied'
			ORDER BY created_at DESC LIMIT 1`, intentID).Scan(&expected, &observed, &mismatched, &activeBank, &readbackDigest); err != nil {
			return err
		}
		if expected != observed || mismatched != 0 || readbackDigest == "" || activeBank < 0 || activeBank > 1 {
			return errors.New("firewall projector: applied attempt lacks exact readback")
		}
		var expectedVersion int64
		var expectedRevision *string
		var expectedBank int
		var expectedCAS string
		if err := tx.QueryRow(ctx, `SELECT expected_binding_version,expected_current_revision_id,
			expected_active_bank,expected_cas_digest FROM firewall_activations
			WHERE operation_id=$1 AND target_id=$2 FOR UPDATE`, operationID, targetID).
			Scan(&expectedVersion, &expectedRevision, &expectedBank, &expectedCAS); err != nil {
			return err
		}
		var currentRevision *string
		err := tx.QueryRow(ctx, `SELECT current_revision_id FROM firewall_bindings WHERE target_id=$1 FOR UPDATE`, targetID).
			Scan(&currentRevision)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		if errors.Is(err, pgx.ErrNoRows) {
			tag, err := tx.Exec(ctx, `INSERT INTO firewall_bindings(target_id,current_revision_id,
					previous_revision_id,active_bank,selector_state,operation_id,cas_digest,binding_version)
					SELECT $1,$2,NULL,$3,'stable',$4,$5,1
						WHERE $6=0 AND $7::text IS NULL`, targetID, revisionID, activeBank, operationID,
				policyDigest, expectedVersion, expectedRevision)
			if err != nil {
				return err
			}
			if tag.RowsAffected() != 1 {
				return errors.New("firewall projector: initial selector/current binding CAS conflict")
			}
		} else {
			var previous any
			if currentRevision != nil {
				previous = *currentRevision
			}
			tag, err := tx.Exec(ctx, `UPDATE firewall_bindings SET current_revision_id=$1,
					previous_revision_id=$2,active_bank=$3,selector_state='stable',operation_id=$4,
					cas_digest=$5,binding_version=binding_version+1,updated_at=now()
					WHERE target_id=$6 AND binding_version=$7
					  AND current_revision_id IS NOT DISTINCT FROM $8
					  AND active_bank=$9 AND cas_digest=$10`, revisionID, previous,
				activeBank, operationID, policyDigest, targetID, expectedVersion,
				expectedRevision, expectedBank, expectedCAS)
			if err != nil {
				return err
			}
			if tag.RowsAffected() != 1 {
				return errors.New("firewall projector: selector/current binding CAS conflict")
			}
		}
		completed, _ := json.Marshal(ActivationStagesInOrder)
		tag, err := tx.Exec(ctx, `UPDATE firewall_activations SET current_stage='cleanup',completed_stages=$1,
			expected_entries=$2,observed_entries=$3,mismatched_entries=0,default_readback='exact',
			selector_readback='new-bank',result='applied',reason_code='APPLIED',updated_at=now()
			WHERE operation_id=$4 AND target_id=$5 AND desired_revision_id=$6`, completed, expected,
			observed, operationID, targetID, revisionID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return errors.New("firewall projector: activation projection missing")
		}
		return nil
	default:
		return nil
	}
}

func (p *IntentProjector) projectOverlay(ctx context.Context, tx *db.Tx, intentID, operation string, outcome governance.AttemptStatus, reasonCode string) error {
	if outcome == governance.AttemptApplied {
		if operation == "overlay-delete" {
			_, err := tx.Exec(ctx, `UPDATE firewall_overlays SET deleted=true,reason_code='OVERLAY_DELETE_APPLIED'
				WHERE delete_intent_id=$1`, intentID)
			return err
		}
		_, err := tx.Exec(ctx, `UPDATE firewall_overlays SET reason_code='OVERLAY_INSTALL_APPLIED'
			WHERE operation_id=(SELECT operation_id FROM effect_intents WHERE effect_intent_id=$1)`, intentID)
		return err
	}
	if outcome == governance.AttemptHold || outcome == governance.AttemptReconciling || outcome == governance.AttemptUnknown {
		_, err := tx.Exec(ctx, `UPDATE firewall_overlays SET reason_code=$1 WHERE delete_intent_id=$2`, reasonCode, intentID)
		return err
	}
	return nil
}
