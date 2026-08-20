package firewall

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/db"
)

// ActivationService prepares the firewall projection for an already-authorized
// durable effect intent. It never calls Edge and never executes P4; the sole
// governance dispatcher owns claim → Edge journal/write/readback → PostgreSQL
// finalize, while IntentProjector derives current/previous in that finalize
// transaction.
type ActivationService struct {
	pool *db.Pool
}

func NewActivationService(pool *db.Pool) *ActivationService {
	return &ActivationService{pool: pool}
}

type ActivationOutcome struct {
	OperationID string           `json:"operation_id"`
	TargetID    string           `json:"target_id"`
	Stage       ActivationStage  `json:"current_stage"`
	Result      ActivationResult `json:"result"`
	Readback    string           `json:"readback_digest,omitempty"`
	ReasonCode  string           `json:"reason_code"`
}

// Activate is intentionally a prepare-only API. It proves that operationID is
// the exact unclaimed effect intent for target/revision and appends the prepared
// projection. Returning prepared never means a device side effect occurred.
func (s *ActivationService) Activate(ctx context.Context, targetID, desiredRevisionID, operationID string) (ActivationOutcome, error) {
	if s == nil || s.pool == nil || targetID == "" || desiredRevisionID == "" || operationID == "" {
		return ActivationOutcome{OperationID: operationID, TargetID: targetID, Result: ActivationHold, ReasonCode: "ACTIVATION_IDENTITY_MALFORMED"},
			errors.New("firewall: activation identity/service malformed")
	}
	completed, _ := json.Marshal([]ActivationStage{StagePrepared})
	err := s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		var intentID, policyDigest, revisionDigest, claimState string
		err := tx.QueryRow(ctx, `SELECT i.effect_intent_id,p.policy_digest,r.revision_digest,i.claim_state
			FROM effect_intents i JOIN effect_proposals p ON p.proposal_id=i.proposal_id
			JOIN firewall_revisions r ON r.revision_id=$1 AND r.revision_digest=p.policy_digest
			WHERE i.operation_id=$2 AND i.target_id=$3 AND i.effect_kind IN
			 ('firewall-baseline-activate','firewall-rollback') FOR UPDATE OF i`,
			desiredRevisionID, operationID, targetID).Scan(&intentID, &policyDigest, &revisionDigest, &claimState)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return errors.New("firewall: exact authorized effect intent unavailable")
			}
			return err
		}
		if intentID == "" || policyDigest != revisionDigest || claimState != "unclaimed" {
			return errors.New("firewall: activation intent is stale, drifted, or already claimed")
		}
		var expectedVersion int64
		var expectedCurrent *string
		var expectedBank int
		expectedCAS := "sha256:0000000000000000000000000000000000000000000000000000000000000000"
		err = tx.QueryRow(ctx, `SELECT binding_version,current_revision_id,active_bank,cas_digest
			FROM firewall_bindings WHERE target_id=$1 FOR UPDATE`, targetID).
			Scan(&expectedVersion, &expectedCurrent, &expectedBank, &expectedCAS)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		tag, err := tx.Exec(ctx, `INSERT INTO firewall_activations(operation_id,target_id,desired_revision_id,
			current_stage,completed_stages,expected_entries,observed_entries,mismatched_entries,
			default_readback,selector_readback,result,reason_code,expected_binding_version,
			expected_current_revision_id,expected_active_bank,expected_cas_digest)
			VALUES($1,$2,$3,'prepared',$4,0,0,0,'missing','old-bank','prepared','PREPARED',$5,$6,$7,$8)
			ON CONFLICT(operation_id) DO NOTHING`, operationID, targetID, desiredRevisionID, completed,
			expectedVersion, expectedCurrent, expectedBank, expectedCAS)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			var existingTarget, existingRevision, existingCAS string
			var existingVersion int64
			var existingCurrent *string
			var existingBank int
			if err := tx.QueryRow(ctx, `SELECT target_id,desired_revision_id FROM firewall_activations
					WHERE operation_id=$1`, operationID).Scan(&existingTarget, &existingRevision); err != nil {
				return err
			}
			if err := tx.QueryRow(ctx, `SELECT expected_binding_version,expected_current_revision_id,
					expected_active_bank,expected_cas_digest FROM firewall_activations WHERE operation_id=$1`,
				operationID).Scan(&existingVersion, &existingCurrent, &existingBank, &existingCAS); err != nil {
				return err
			}
			if existingTarget != targetID || existingRevision != desiredRevisionID ||
				existingVersion != expectedVersion || existingBank != expectedBank ||
				existingCAS != expectedCAS || !sameNullableString(existingCurrent, expectedCurrent) {
				return errors.New("firewall: activation operation identity conflict")
			}
		}
		tag, err = tx.Exec(ctx, `UPDATE effect_intents SET gate_open=true,reason_code='ACTIVATION_PREPARED'
			WHERE effect_intent_id=$1 AND operation_id=$2 AND claim_state='unclaimed' AND NOT gate_open`, intentID, operationID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			var alreadyOpen bool
			if err := tx.QueryRow(ctx, `SELECT gate_open FROM effect_intents WHERE effect_intent_id=$1`, intentID).Scan(&alreadyOpen); err != nil || !alreadyOpen {
				return errors.New("firewall: activation gate CAS conflict")
			}
		}
		return nil
	})
	if err != nil {
		return ActivationOutcome{OperationID: operationID, TargetID: targetID, Result: ActivationHold, ReasonCode: "PREPARE_REJECTED"}, err
	}
	return ActivationOutcome{OperationID: operationID, TargetID: targetID, Stage: StagePrepared,
		Result: ActivationPrepared, ReasonCode: "PREPARED"}, nil
}

func sameNullableString(left, right *string) bool {
	if left == nil || right == nil {
		return left == nil && right == nil
	}
	return *left == *right
}

// Load returns the durable activation projection without inferring success from
// Edge/Gateway availability or an RPC response.
func (s *ActivationService) Load(ctx context.Context, operationID string) (ActivationOutcome, error) {
	var out ActivationOutcome
	var stage, result string
	err := s.pool.Pool.QueryRow(ctx, `SELECT operation_id,target_id,current_stage,result,reason_code
		FROM firewall_activations WHERE operation_id=$1`, operationID).
		Scan(&out.OperationID, &out.TargetID, &stage, &result, &out.ReasonCode)
	if err != nil {
		return ActivationOutcome{}, fmt.Errorf("firewall: load activation: %w", err)
	}
	out.Stage = ActivationStage(stage)
	out.Result = ActivationResult(result)
	return out, nil
}
