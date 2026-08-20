package db

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
)

// CASResult is the outcome of a compare-and-set claim/finalize.
type CASResult string

const (
	CASClaimed  CASResult = "claimed"
	CASConflict CASResult = "conflict"
	CASExpired  CASResult = "expired"
	CASFenced   CASResult = "fenced"
	CASBlocked  CASResult = "blocked"
	CASReleased CASResult = "released"
)

// ClaimLease is the durable lease attached to a claimed intent/run. A lease
// contains owner/runtime identity, issued/expiry/fence; timeout recovery still
// requires CAS — process-alive never implies ownership (effect-cas contract).
type ClaimLease struct {
	OwnerIdentity string    `json:"owner_identity"`
	IssuedAt      time.Time `json:"issued_at"`
	ExpiresAt     time.Time `json:"expires_at"`
	Fence         string    `json:"fence"`
}

// ClaimIntent atomically claims an effect_intent row using FOR UPDATE SKIP
// LOCKED. This is the single claim mechanism on the single durable ledger
// (effect_intents); there is no separate outbox table or second queue (D4).
//
// The transaction is short and serializable; the external Edge RPC happens
// AFTER this call returns and commits. expectedState guards against stale
// claims; expectedEffectDigest guards against effect-digest drift (the row's
// effect_digest must match the dispatcher's view). Fleet parents
// (is_fleet_parent=true) are never claimable.
func (p *Pool) ClaimIntent(ctx context.Context, claimKey, expectedState, expectedEffectDigest, ownerIdentity string, leaseTTL time.Duration) (CASResult, *ClaimLease, error) {
	var result CASResult
	var lease *ClaimLease
	err := p.WithTx(ctx, []TxOption{Serializable()}, func(tx *Tx) error {
		var currentState, currentDigest string
		var isParent, gateOpen, authorizationCurrent, targetFenceCurrent, capabilityCurrent bool
		var deadlineUnixMS, notBeforeUnixMS int64
		var claimExpires *int64
		now := time.Now().UTC()
		nowMS := now.UnixMilli()
		err := tx.QueryRow(ctx, `
				SELECT i.claim_state, i.effect_digest, i.is_fleet_parent, i.deadline_unix_ms,
			       i.claim_expires_at_unix_ms, i.not_before_unix_ms,
				       i.gate_open AND NOT EXISTS (
				           SELECT 1 FROM firewall_activations fa
				           WHERE fa.target_id=i.target_id AND fa.result IN ('prepared','reconciling')
				             AND fa.operation_id<>i.operation_id)
			       AND (i.fleet_operation_id IS NULL OR EXISTS (
				           SELECT 1 FROM fleet_child_intents fc
				           WHERE fc.child_intent_id=i.effect_intent_id AND fc.gate_open)),
				       (d.decision='approve' AND d.expires_at_unix_ms > $2
				        AND p.expires_at_unix_ms > $2 AND d.proposal_digest=p.proposal_digest),
				       EXISTS (
				           SELECT 1 FROM targets t JOIN target_assignments a ON a.target_id=t.target_id
				           WHERE t.target_id=i.target_id AND t.status='active'
				             AND a.assignment_generation=(SELECT max(a2.assignment_generation) FROM target_assignments a2 WHERE a2.target_id=i.target_id)
				             AND a.revoked_at_unix_ms IS NULL AND a.expires_at_unix_ms > $2
					             AND a.assignment_generation=(i.fence->>'target_assignment_generation')::bigint
					             AND a.incarnation_id=i.fence->>'target_control_incarnation_id'
					             AND a.actor_runtime_epoch=i.fence->>'actor_runtime_epoch'
					             AND a.application_generation=(i.fence->>'application_generation')::bigint
					             AND (i.fence->>'election_id_low')::numeric BETWEEN a.election_floor AND a.election_ceiling),
				       EXISTS (
				           SELECT 1 FROM target_capability_observations c
				           WHERE c.target_id=i.target_id AND c.expires_at_unix_ms > $2 AND c.capacity_available
					             AND c.target_control_incarnation_id=i.fence->>'target_control_incarnation_id'
					             AND c.assignment_generation=(i.fence->>'target_assignment_generation')::bigint
					             AND c.actor_runtime_epoch=i.fence->>'actor_runtime_epoch'
				             AND c.application_generation=(i.fence->>'application_generation')::bigint
				             AND c.p4info_digest=i.fence->>'p4info_digest'
				             AND c.pipeline_digest=i.fence->>'pipeline_digest'
				             AND c.capacity_digest=i.fence->>'capacity_digest')
				FROM effect_intents i
				JOIN effect_proposals p ON p.proposal_id=i.proposal_id
				JOIN effect_decisions d ON d.decision_id=i.decision_id AND d.proposal_id=p.proposal_id
				WHERE i.effect_intent_id = $1
				FOR UPDATE SKIP LOCKED`, claimKey, nowMS).Scan(
			&currentState, &currentDigest, &isParent, &deadlineUnixMS, &claimExpires, &notBeforeUnixMS,
			&gateOpen, &authorizationCurrent, &targetFenceCurrent, &capabilityCurrent)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				result = CASConflict
				return nil
			}
			return err
		}
		if isParent {
			result = CASFenced // fleet parent is never claimable
			return nil
		}
		if deadlineUnixMS <= nowMS || !authorizationCurrent {
			result = CASExpired
			return nil
		}
		if notBeforeUnixMS > nowMS {
			result = CASBlocked
			return nil
		}
		if !gateOpen || !targetFenceCurrent || !capabilityCurrent {
			result = CASFenced
			return nil
		}
		if currentState != expectedState {
			result = CASConflict
			return nil
		}
		if currentDigest != expectedEffectDigest {
			result = CASFenced
			return nil
		}
		exp := now.Add(leaseTTL)
		leaseNonce := make([]byte, 16)
		if _, err := rand.Read(leaseNonce); err != nil {
			return fmt.Errorf("mint claim lease: %w", err)
		}
		leaseID := ownerIdentity + ":" + hex.EncodeToString(leaseNonce)
		tag, err := tx.Exec(ctx, `
					UPDATE effect_intents
					SET claim_state = 'claimed', claim_lease_id = $1,
					    claim_expires_at_unix_ms = $2
					WHERE effect_intent_id = $3
						  AND claim_state = $4
						  AND effect_digest = $5`,
			leaseID, exp.UnixMilli(), claimKey, expectedState, expectedEffectDigest)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			result = CASConflict
			return nil
		}
		result = CASClaimed
		lease = &ClaimLease{OwnerIdentity: ownerIdentity, IssuedAt: now, ExpiresAt: exp, Fence: leaseID}
		return nil
	})
	if err != nil {
		return "", nil, fmt.Errorf("db: claim intent: %w", err)
	}
	return result, lease, nil
}

// RenewIntentLease extends only the exact current effect claim. Losing renewal
// fences the in-flight caller; an expired claim is reconciled by operation ID and
// is never reclaimed for a second blind ExecuteEffect call.
func (p *Pool) RenewIntentLease(ctx context.Context, intentID, leaseID string, leaseTTL time.Duration) (bool, error) {
	if intentID == "" || leaseID == "" || leaseTTL < time.Second || leaseTTL > 10*time.Minute {
		return false, errors.New("db: renew intent lease arguments outside bounds")
	}
	nowMS := time.Now().UTC().UnixMilli()
	tag, err := p.Pool.Exec(ctx, `UPDATE effect_intents
		SET claim_expires_at_unix_ms=$1
		WHERE effect_intent_id=$2 AND claim_state='claimed' AND claim_lease_id=$3
		  AND claim_expires_at_unix_ms>$4`, nowMS+leaseTTL.Milliseconds(), intentID, leaseID, nowMS)
	if err != nil {
		return false, err
	}
	return tag.RowsAffected() == 1, nil
}

// FinalizeIntent CAS-finalizes an intent with (operation_id, claim_generation,
// attempt, expected_status/version). An old claim or old application-generation
// late result must NOT overwrite new facts (ADR-0004 §7).
func (p *Pool) FinalizeIntent(ctx context.Context, claimKey, claimLeaseID, newState string, expectedLeaseID string) (CASResult, error) {
	var result CASResult
	err := p.WithTx(ctx, []TxOption{ReadCommitted()}, func(tx *Tx) error {
		tag, err := tx.Exec(ctx, `
			UPDATE effect_intents
			SET claim_state = $1
			WHERE effect_intent_id = $2 AND claim_lease_id = $3`,
			newState, claimKey, expectedLeaseID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			result = CASConflict
			return nil
		}
		result = CASClaimed
		return nil
	})
	if err != nil {
		return "", fmt.Errorf("db: finalize intent: %w", err)
	}
	return result, nil
}

// IdempotencyCheck returns the original record for (key, digest) or reports a
// stable conflict (same key, different digest). same id+same digest -> original;
// same id+different digest -> conflict (effect-cas / event contracts).
type IdempotencyResult string

const (
	IdempotencyOriginal IdempotencyResult = "original"
	IdempotencyConflict IdempotencyResult = "conflict"
	IdempotencyNew      IdempotencyResult = "new"
)

func (p *Pool) IdempotencyCheck(ctx context.Context, table, keyCol, digestCol, key, digest string) (IdempotencyResult, string, error) {
	var existingDigest string
	err := p.Pool.QueryRow(ctx,
		fmt.Sprintf(`SELECT %s FROM %s WHERE %s = $1`, digestCol, table, keyCol), key).Scan(&existingDigest)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return IdempotencyNew, "", nil
		}
		return "", "", err
	}
	if existingDigest == digest {
		return IdempotencyOriginal, existingDigest, nil
	}
	return IdempotencyConflict, existingDigest, nil
}
