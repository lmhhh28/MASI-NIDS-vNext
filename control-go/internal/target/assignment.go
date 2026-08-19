package target

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

const (
	assignmentLeaseMinMS int64 = 1_000
	assignmentLeaseMaxMS int64 = 300_000
)

// AssignmentService owns the assignment handoff. The handoff order is
// (ADR-0015 §6):
//  1. freeze new claims for the target;
//  2. durable revoke/drain the old assignment (old actor draining);
//  3. finish/reconcile journal + release/lose mastership;
//  4. old revoke ACK or bounded lease expiry;
//  5. PG CAS new assignment_generation + strictly-higher election floor/range;
//  6. the new actor opens StreamChannel and proves primary (Edge's job).
//
// Go does NOT masquerade as P4 election or primary; connection success is NOT
// primary/writable (the StreamChannel proof is Edge's responsibility). All
// external waits occur OUTSIDE any DB transaction.
type AssignmentService struct {
	pool *db.Pool
}

func NewAssignmentService(pool *db.Pool) *AssignmentService {
	return &AssignmentService{pool: pool}
}

// HandoffResult is the outcome of an assignment handoff CAS.
type HandoffResult string

const (
	HandoffApplied    HandoffResult = "applied"
	HandoffConflict   HandoffResult = "conflict"
	HandoffLeaseAlive HandoffResult = "lease-alive"
)

// Handoff performs the PG CAS that advances the assignment generation and
// reserves a strictly-higher election floor/range for the new actor. The caller
// MUST have already driven the durable revoke/drain of the old assignment
// OUTSIDE this transaction (steps 1-4); this method only performs the final
// short-transaction CAS (step 5).
//
// newElectionFloor must be strictly greater than the prior assignment's
// election_ceiling (TARGET-HANDOFF-ELECTION-FLOOR). A lease that has not expired
// and was not revoked yields HandoffLeaseAlive (the handoff does not proceed).
func (s *AssignmentService) Handoff(ctx context.Context, a Assignment, priorGeneration int64, priorElectionCeiling int64) (HandoffResult, error) {
	if a.TargetID == "" || len(a.TargetID) > 128 || a.TraceID == "" || len(a.TraceID) > 128 {
		return HandoffConflict, errors.New("target: target_id/trace_id length outside 1..128")
	}
	if a.AssignmentGeneration != priorGeneration+1 {
		return HandoffConflict, fmt.Errorf("target: handoff generation must be prior+1")
	}
	if a.ElectionFloor <= priorElectionCeiling {
		return HandoffConflict, fmt.Errorf("target: handoff election_floor must be strictly above prior ceiling")
	}
	if a.ElectionCeiling < a.ElectionFloor {
		return HandoffConflict, fmt.Errorf("target: election_ceiling >= election_floor required")
	}
	if a.ExpiresAtUnixMS <= a.IssuedAtUnixMS {
		return HandoffConflict, fmt.Errorf("target: lease expires must be after issued")
	}
	leaseMS := a.ExpiresAtUnixMS - a.IssuedAtUnixMS
	if leaseMS < assignmentLeaseMinMS || leaseMS > assignmentLeaseMaxMS {
		return HandoffConflict, fmt.Errorf("target: lease duration must be %d..%d ms", assignmentLeaseMinMS, assignmentLeaseMaxMS)
	}
	if err := a.Actor.Validate(); err != nil || a.LeaseID == "" || a.IncarnationID == "" || a.EdgeWorkloadRef == "" {
		return HandoffConflict, fmt.Errorf("target: malformed assignment identity: %v", err)
	}
	var result HandoffResult
	err := s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		// Verify the prior assignment is no longer the active lease holder: its
		// lease must have expired or been revoked (status-driven by the caller).
		var priorExpires, durablePriorCeiling int64
		var revokedAt *int64
		err := tx.QueryRow(ctx, `
				SELECT expires_at_unix_ms, election_ceiling, revoked_at_unix_ms
				FROM target_assignments
				WHERE target_id = $1 AND assignment_generation = $2
				  AND assignment_generation=(SELECT max(x.assignment_generation) FROM target_assignments x WHERE x.target_id=$1)
				FOR UPDATE`, a.TargetID, priorGeneration).Scan(&priorExpires, &durablePriorCeiling, &revokedAt)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) && priorGeneration == 0 {
				priorExpires, durablePriorCeiling, revokedAt = 0, 0, nil
			} else if errors.Is(err, pgx.ErrNoRows) {
				result = HandoffConflict
				return nil
			}
			return err
		}
		// If the prior lease is still alive the handoff cannot proceed (the
		// caller must complete revoke/drain first). This is a HOLD, not an error
		// that overwrites the durable fact.
		if durablePriorCeiling != priorElectionCeiling || a.ElectionFloor <= durablePriorCeiling {
			result = HandoffConflict
			return nil
		}
		if priorExpires > a.IssuedAtUnixMS && revokedAt == nil {
			result = HandoffLeaseAlive
			return nil
		}
		// CAS-insert the new assignment generation with the strictly-higher range.
		_, err = tx.Exec(ctx, `
			INSERT INTO target_assignments (
				target_id, assignment_generation, incarnation_id, edge_workload_ref,
				lease_id, issued_at_unix_ms, expires_at_unix_ms, election_floor,
					election_ceiling, actor_runtime_epoch, application_generation,
					actor_ref, trace_id, actor_issuer, actor_subject)
				VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)`,
			a.TargetID, a.AssignmentGeneration, a.IncarnationID, a.EdgeWorkloadRef,
			a.LeaseID, a.IssuedAtUnixMS, a.ExpiresAtUnixMS, a.ElectionFloor,
			a.ElectionCeiling, a.ActorRuntimeEpoch, a.ApplicationGeneration,
			a.Actor.String(), a.TraceID, a.Actor.Issuer, a.Actor.Subject)
		if err != nil {
			return err
		}
		result = HandoffApplied
		return nil
	})
	if err != nil {
		return "", fmt.Errorf("target: handoff: %w", err)
	}
	return result, nil
}

// CurrentAssignment loads the highest generation assignment for a target.
func (s *AssignmentService) CurrentAssignment(ctx context.Context, targetID string) (*Assignment, error) {
	var a Assignment
	var actorIssuer, actorSubject string
	err := s.pool.Pool.QueryRow(ctx, `
		SELECT target_id, assignment_generation, incarnation_id, edge_workload_ref,
		       lease_id, issued_at_unix_ms, expires_at_unix_ms, election_floor,
		       election_ceiling, actor_runtime_epoch, application_generation,
		       actor_issuer, actor_subject, trace_id
		FROM target_assignments
		WHERE target_id = $1
		ORDER BY assignment_generation DESC
		LIMIT 1`, targetID).
		Scan(&a.TargetID, &a.AssignmentGeneration, &a.IncarnationID, &a.EdgeWorkloadRef,
			&a.LeaseID, &a.IssuedAtUnixMS, &a.ExpiresAtUnixMS, &a.ElectionFloor,
			&a.ElectionCeiling, &a.ActorRuntimeEpoch, &a.ApplicationGeneration,
			&actorIssuer, &actorSubject, &a.TraceID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil
		}
		return nil, fmt.Errorf("target: current assignment: %w", err)
	}
	a.Actor = security.Actor{Issuer: actorIssuer, Subject: actorSubject}
	if err := a.Actor.Validate(); err != nil {
		return nil, fmt.Errorf("target: persisted assignment actor: %w", err)
	}
	return &a, nil
}

// Revoke durably fences an assignment before handoff. Once revoked, an old
// TargetActor is read-only even if its original lease time has not elapsed.
func (s *AssignmentService) Revoke(ctx context.Context, targetID string, generation, revokedAtUnixMS int64) error {
	if targetID == "" || generation < 1 || revokedAtUnixMS < 1 {
		return errors.New("target: malformed revoke request")
	}
	tag, err := s.pool.Pool.Exec(ctx, `
		UPDATE target_assignments SET revoked_at_unix_ms=$1
		WHERE target_id=$2 AND assignment_generation=$3 AND revoked_at_unix_ms IS NULL`,
		revokedAtUnixMS, targetID, generation)
	if err != nil {
		return fmt.Errorf("target: revoke assignment: %w", err)
	}
	if tag.RowsAffected() != 1 {
		return errors.New("target: revoke CAS conflict")
	}
	return nil
}
