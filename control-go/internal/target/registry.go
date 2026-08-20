package target

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"time"

	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

var (
	endpointRE = regexp.MustCompile(`^https://[^/?#]+:[0-9]{1,5}$`)
	identityRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]*$`)
	digestRE   = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	reasonRE   = regexp.MustCompile(`^[A-Z][A-Z0-9_]{0,63}$`)
)

// RegistryService owns the stable target identity and lifecycle. target_id is
// never reused: rename/address/chassis change cannot reuse an ID, and a retired
// ID is never reassigned (ADR-0015).
type RegistryService struct {
	pool  *db.Pool
	now   func() time.Time
	idGen func(seed string) string
}

// NewRegistryService constructs the registry. idGen is injectable for tests; the
// default derives a never-reused target_id from a unique seed.
func NewRegistryService(pool *db.Pool) *RegistryService {
	return &RegistryService{pool: pool, now: time.Now, idGen: defaultTargetID}
}

func defaultTargetID(seed string) string {
	h := sha256.Sum256([]byte(seed + time.Now().UTC().Format(time.RFC3339Nano)))
	return "tgt-" + hex.EncodeToString(h[:16])
}

// RegisterTarget creates a candidate target with a never-reused target_id. The
// target starts as 'candidate'; activation to 'verified'/'active' requires an
// Admin step-up (Activate). External inventory imports use ImportCandidate
// (provenance/digest only, no auto-active).
func (s *RegistryService) RegisterTarget(ctx context.Context, t Target, auth LifecycleAuthorization) (*Target, error) {
	if err := validateTarget(t); err != nil {
		return nil, err
	}
	if err := validateLifecycleAuthorization(t, auth); err != nil {
		return nil, err
	}
	t.TargetID = s.idGen(t.P4RuntimeEndpoint + ":" + t.DisplayName + ":" + t.Actor.String())
	t.Status = StatusCandidate
	err := s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		tag, err := tx.Exec(ctx, `INSERT INTO targets (
			target_id,display_name,p4runtime_endpoint,device_id,role,status,desired_profile_digest,
			credential_ref,scope,provenance,actor_ref,trace_id,actor_issuer,actor_subject,idempotency_key)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,NULL,$10,$11,$12,$13,$14)
			ON CONFLICT(actor_issuer,actor_subject,idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING`,
			t.TargetID, t.DisplayName, t.P4RuntimeEndpoint, t.DeviceID, t.Role, string(t.Status),
			t.DesiredProfileDigest, t.CredentialRef, t.Scope, t.Actor.String(), t.TraceID,
			t.Actor.Issuer, t.Actor.Subject, t.IdempotencyKey)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			var existing Target
			if err := tx.QueryRow(ctx, `SELECT target_id,display_name,p4runtime_endpoint,device_id,role,
				desired_profile_digest,credential_ref,scope,status FROM targets
				WHERE actor_issuer=$1 AND actor_subject=$2 AND idempotency_key=$3`,
				t.Actor.Issuer, t.Actor.Subject, t.IdempotencyKey).Scan(&existing.TargetID, &existing.DisplayName,
				&existing.P4RuntimeEndpoint, &existing.DeviceID, &existing.Role, &existing.DesiredProfileDigest,
				&existing.CredentialRef, &existing.Scope, &existing.Status); err != nil {
				return err
			}
			if existing.DisplayName != t.DisplayName || existing.P4RuntimeEndpoint != t.P4RuntimeEndpoint ||
				existing.DeviceID != t.DeviceID || existing.Role != t.Role || existing.DesiredProfileDigest != t.DesiredProfileDigest ||
				existing.CredentialRef != t.CredentialRef || existing.Scope != t.Scope {
				return errors.New("target: idempotency key reused with different registration")
			}
			t.TargetID = existing.TargetID
			t.Status = existing.Status
			return nil
		}
		return appendLifecycleEvent(ctx, tx, t.TargetID, "", string(StatusCandidate), t.Scope,
			t.Actor, "TARGET_REGISTERED", t.TraceID, s.now().UnixMilli())
	})
	if err != nil {
		return nil, fmt.Errorf("target: register: %w", err)
	}
	return &t, nil
}

// ImportCandidate imports a target from external inventory as a candidate with
// provenance/digest only. It is NEVER auto-activated; an Admin must confirm the
// diff before any canonical active write (ADR-0015 §2).
func (s *RegistryService) ImportCandidate(ctx context.Context, t Target, p Provenance, auth LifecycleAuthorization) (*Target, error) {
	if err := validateTarget(t); err != nil {
		return nil, err
	}
	if p.SourceID == "" || p.Digest == "" {
		return nil, errors.New("target: import candidate requires provenance source + digest")
	}
	if err := validateLifecycleAuthorization(t, auth); err != nil {
		return nil, err
	}
	t.TargetID = s.idGen(t.P4RuntimeEndpoint + ":" + t.DisplayName + ":import:" + p.Digest)
	t.Status = StatusCandidate
	t.Provenance = &p
	provJSON := fmt.Sprintf(`{"source_id":%q,"source_revision":%q,"retrieved_at_unix_ms":%d,"digest":%q}`,
		p.SourceID, p.SourceRevision, p.RetrievedAtUnixMS, p.Digest)
	err := s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		tag, err := tx.Exec(ctx, `INSERT INTO targets(target_id,display_name,p4runtime_endpoint,device_id,
			role,status,desired_profile_digest,credential_ref,scope,provenance,actor_ref,trace_id,
			actor_issuer,actor_subject,idempotency_key)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
			ON CONFLICT(actor_issuer,actor_subject,idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING`,
			t.TargetID, t.DisplayName, t.P4RuntimeEndpoint, t.DeviceID, t.Role, string(t.Status),
			t.DesiredProfileDigest, t.CredentialRef, t.Scope, provJSON, t.Actor.String(), t.TraceID,
			t.Actor.Issuer, t.Actor.Subject, t.IdempotencyKey)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			var targetID, endpoint, scope, digest string
			if err := tx.QueryRow(ctx, `SELECT target_id,p4runtime_endpoint,scope,provenance->>'digest'
				FROM targets WHERE actor_issuer=$1 AND actor_subject=$2 AND idempotency_key=$3`,
				t.Actor.Issuer, t.Actor.Subject, t.IdempotencyKey).Scan(&targetID, &endpoint, &scope, &digest); err != nil {
				return err
			}
			if endpoint != t.P4RuntimeEndpoint || scope != t.Scope || digest != p.Digest {
				return errors.New("target: import idempotency key conflict")
			}
			t.TargetID = targetID
			return nil
		}
		return appendLifecycleEvent(ctx, tx, t.TargetID, "", string(StatusCandidate), t.Scope,
			t.Actor, "INVENTORY_CANDIDATE_IMPORTED", t.TraceID, s.now().UnixMilli())
	})
	if err != nil {
		return nil, fmt.Errorf("target: import candidate: %w", err)
	}
	return &t, nil
}

// Activate moves a candidate/verified target to active. Activation requires a
// scoped Platform Admin (R3-style step-up is enforced by the caller via the
// governance/Decision flow). The partial-unique index on
// (endpoint, device_id, role) WHERE status='active' prevents a duplicate active
// target — a second activation with the same identity is a conflict, not a
// silent duplicate.
func (s *RegistryService) Activate(ctx context.Context, targetID string, actor security.Actor, auth LifecycleAuthorization, reasonCode, traceID string) error {
	err := s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		var previous, scope string
		if err := tx.QueryRow(ctx, `SELECT status,scope FROM targets WHERE target_id=$1 FOR UPDATE`, targetID).Scan(&previous, &scope); err != nil {
			return err
		}
		if err := validateLifecycleMutation(actor, auth, scope, reasonCode, traceID); err != nil {
			return err
		}
		tag, err := tx.Exec(ctx, `UPDATE targets SET status='active'
			WHERE target_id=$1 AND status IN ('candidate','verified','disabled','quarantined')`, targetID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			return fmt.Errorf("target: %s not in an activatable state (retired is terminal)", targetID)
		}
		return appendLifecycleEvent(ctx, tx, targetID, previous, string(StatusActive), scope, actor,
			reasonCode, traceID, s.now().UnixMilli())
	})
	if err != nil {
		// A duplicate active identity raises a unique-violation.
		return fmt.Errorf("target: activate (duplicate active identity rejected): %w", err)
	}
	return err
}

// Verify marks a candidate verified only when the current authenticated Edge
// observation is fresh and proves the exact desired profile/pipeline/capacity.
func (s *RegistryService) Verify(ctx context.Context, targetID string, actor security.Actor,
	auth LifecycleAuthorization, reasonCode, traceID string) error {
	return s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		var previous, scope, desired string
		if err := tx.QueryRow(ctx, `SELECT status,scope,desired_profile_digest FROM targets
			WHERE target_id=$1 FOR UPDATE`, targetID).Scan(&previous, &scope, &desired); err != nil {
			return err
		}
		if err := validateLifecycleMutation(actor, auth, scope, reasonCode, traceID); err != nil {
			return err
		}
		var exact bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM target_capability_observations c
			WHERE c.target_id=$1 AND c.profile_digest=$2 AND c.capacity_available AND c.lease_valid
			  AND c.p4_connected AND c.primary_actor AND c.pipeline_exact AND c.freshness='fresh'
			  AND c.expires_at_unix_ms>$3)`, targetID, desired, s.now().UnixMilli()).Scan(&exact); err != nil {
			return err
		}
		if previous != string(StatusCandidate) || !exact {
			return errors.New("target: candidate lacks fresh exact capability verification")
		}
		tag, err := tx.Exec(ctx, `UPDATE targets SET status='verified' WHERE target_id=$1 AND status='candidate'`, targetID)
		if err != nil || tag.RowsAffected() != 1 {
			return errors.New("target: verify CAS conflict")
		}
		return appendLifecycleEvent(ctx, tx, targetID, previous, string(StatusVerified), scope, actor,
			reasonCode, traceID, s.now().UnixMilli())
	})
}

// Transition performs the non-terminal operational lifecycle transitions. It
// does not wait for an external device; Assignment revoke/drain is a separate,
// explicit operation and must precede handoff.
func (s *RegistryService) Transition(ctx context.Context, targetID string, desired TargetStatus,
	actor security.Actor, auth LifecycleAuthorization, reasonCode, traceID string) error {
	if desired != StatusDraining && desired != StatusDisabled && desired != StatusQuarantined {
		return errors.New("target: unsupported operational lifecycle transition")
	}
	return s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		var previous, scope string
		if err := tx.QueryRow(ctx, `SELECT status,scope FROM targets WHERE target_id=$1 FOR UPDATE`, targetID).
			Scan(&previous, &scope); err != nil {
			return err
		}
		if err := validateLifecycleMutation(actor, auth, scope, reasonCode, traceID); err != nil {
			return err
		}
		allowed := false
		switch desired {
		case StatusDraining:
			allowed = previous == string(StatusActive)
		case StatusDisabled:
			allowed = previous == string(StatusActive) || previous == string(StatusDraining) || previous == string(StatusQuarantined)
		case StatusQuarantined:
			allowed = previous == string(StatusActive) || previous == string(StatusDraining) || previous == string(StatusVerified)
		}
		if !allowed {
			return fmt.Errorf("target: transition %s -> %s rejected", previous, desired)
		}
		tag, err := tx.Exec(ctx, `UPDATE targets SET status=$1 WHERE target_id=$2 AND status=$3`, string(desired), targetID, previous)
		if err != nil || tag.RowsAffected() != 1 {
			return errors.New("target: lifecycle transition CAS conflict")
		}
		return appendLifecycleEvent(ctx, tx, targetID, previous, string(desired), scope, actor,
			reasonCode, traceID, s.now().UnixMilli())
	})
}

// Retire marks a target retired. A retired ID is never reassigned and can never
// return to active/verified (the CHECK constraint enforces terminality).
func (s *RegistryService) Retire(ctx context.Context, targetID string, actor security.Actor, auth LifecycleAuthorization, reasonCode, traceID string) error {
	return s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		var previous, scope string
		if err := tx.QueryRow(ctx, `SELECT status,scope FROM targets WHERE target_id=$1 FOR UPDATE`, targetID).Scan(&previous, &scope); err != nil {
			return err
		}
		if err := validateLifecycleMutation(actor, auth, scope, reasonCode, traceID); err != nil {
			return err
		}
		if previous == string(StatusRetired) {
			return fmt.Errorf("target: %s already retired", targetID)
		}
		if _, err := tx.Exec(ctx, `UPDATE targets SET status='retired',retired_at=now() WHERE target_id=$1`, targetID); err != nil {
			return err
		}
		return appendLifecycleEvent(ctx, tx, targetID, previous, string(StatusRetired), scope, actor,
			reasonCode, traceID, s.now().UnixMilli())
	})
}

// IsRetired reports whether a target is retired (terminal, never reassigned).
func (s *RegistryService) IsRetired(ctx context.Context, targetID string) (bool, error) {
	var status string
	err := s.pool.Pool.QueryRow(ctx, `SELECT status FROM targets WHERE target_id = $1`, targetID).Scan(&status)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return false, fmt.Errorf("target: %s not found", targetID)
		}
		return false, err
	}
	return status == string(StatusRetired), nil
}

// RecordCapabilityObservations appends authenticated Edge observations and CAS
// updates the per-target current projection. A stale/old assignment result is
// reported per record and cannot advance current; the entire bounded batch is
// handled in one short transaction with no external wait.
func (s *RegistryService) RecordCapabilityObservations(ctx context.Context, observations []CapabilityObservation) ([]CapabilityObservationResult, error) {
	if err := db.AssertPoolNotNil(s.pool); err != nil {
		return nil, err
	}
	if len(observations) == 0 || len(observations) > 256 {
		return nil, errors.New("target: capability observation batch must contain 1..256 records")
	}
	seenTargets := make(map[string]struct{}, len(observations))
	for i := range observations {
		if err := validateCapabilityObservation(observations[i]); err != nil {
			return nil, fmt.Errorf("target: capability observation %d: %w", i, err)
		}
		if _, exists := seenTargets[observations[i].TargetID]; exists {
			return nil, fmt.Errorf("target: duplicate target %s in observation batch", observations[i].TargetID)
		}
		seenTargets[observations[i].TargetID] = struct{}{}
	}
	results := make([]CapabilityObservationResult, len(observations))
	err := s.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		for i, observation := range observations {
			result := CapabilityObservationResult{TargetID: observation.TargetID}
			var fenceExact bool
			if err := tx.QueryRow(ctx, `
				SELECT EXISTS (
				 SELECT 1 FROM targets t
				 JOIN target_assignments a ON a.target_id=t.target_id
				 WHERE t.target_id=$1 AND t.status <> 'retired'
				   AND t.desired_profile_digest=$2
				   AND a.assignment_generation=$3 AND a.incarnation_id=$4
				   AND a.actor_runtime_epoch=$5 AND a.application_generation=$6
				   AND a.revoked_at_unix_ms IS NULL AND a.expires_at_unix_ms >= $7
				   AND a.assignment_generation=(SELECT max(x.assignment_generation) FROM target_assignments x WHERE x.target_id=t.target_id))`,
				observation.TargetID, observation.ProfileDigest, observation.AssignmentGeneration,
				observation.TargetControlIncarnationID, observation.ActorRuntimeEpoch,
				observation.ApplicationGeneration, observation.ObservedAtUnixMS).Scan(&fenceExact); err != nil {
				return err
			}
			if !fenceExact {
				result.ReasonCode = "ASSIGNMENT_FENCE_MISMATCH"
				results[i] = result
				continue
			}

			tag, err := tx.Exec(ctx, `
				INSERT INTO target_capability_observation_events (
				 observation_id,observation_digest,target_id,target_control_incarnation_id,
				 assignment_generation,actor_runtime_epoch,application_generation,p4info_digest,
				 pipeline_digest,profile_digest,capacity_digest,capacity_available,lease_valid,
				 p4_connected,primary_actor,pipeline_exact,high_priority_queue_depth,
				 telemetry_queue_depth,observation_queue_depth,source_wal_bytes,input_wal_bytes,
				 result_wal_bytes,last_successful_read_unix_ms,freshness,reason_code,
				 observed_at_unix_ms,expires_at_unix_ms,trace_id,current_eligible)
				VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,
				 $20,$21,$22,$23,$24,$25,$26,$27,$28,true)
				ON CONFLICT (observation_id) DO NOTHING`,
				observation.ObservationID, observation.ObservationDigest, observation.TargetID,
				observation.TargetControlIncarnationID, observation.AssignmentGeneration,
				observation.ActorRuntimeEpoch, observation.ApplicationGeneration, observation.P4InfoDigest,
				observation.PipelineDigest, observation.ProfileDigest, observation.CapacityDigest,
				observation.CapacityAvailable, observation.LeaseValid, observation.P4Connected,
				observation.Primary, observation.PipelineExact, observation.HighPriorityQueueDepth,
				observation.TelemetryQueueDepth, observation.ObservationQueueDepth, observation.SourceWALBytes,
				observation.InputWALBytes, observation.ResultWALBytes, observation.LastSuccessfulReadUnixMS,
				observation.Freshness, observation.ReasonCode, observation.ObservedAtUnixMS,
				observation.ExpiresAtUnixMS, observation.TraceID)
			if err != nil {
				return err
			}
			if tag.RowsAffected() == 0 {
				var existingDigest string
				if err := tx.QueryRow(ctx, `SELECT observation_digest FROM target_capability_observation_events WHERE observation_id=$1`,
					observation.ObservationID).Scan(&existingDigest); err != nil {
					return err
				}
				if existingDigest != observation.ObservationDigest {
					return errors.New("target: observation identity digest conflict")
				}
				result.Idempotent = true
			}

			tag, err = tx.Exec(ctx, `
				INSERT INTO target_capability_observations (
				 target_id,target_control_incarnation_id,assignment_generation,actor_runtime_epoch,
				 application_generation,p4info_digest,pipeline_digest,profile_digest,capacity_digest,
				 capacity_available,lease_valid,p4_connected,primary_actor,pipeline_exact,
				 high_priority_queue_depth,telemetry_queue_depth,observation_queue_depth,
				 source_wal_bytes,input_wal_bytes,result_wal_bytes,last_successful_read_unix_ms,
				 freshness,reason_code,observation_id,observation_digest,observed_at_unix_ms,
				 expires_at_unix_ms,trace_id)
				VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,
				 $20,$21,$22,$23,$24,$25,$26,$27,$28)
				ON CONFLICT (target_id) DO UPDATE SET
				 target_control_incarnation_id=EXCLUDED.target_control_incarnation_id,
				 assignment_generation=EXCLUDED.assignment_generation,actor_runtime_epoch=EXCLUDED.actor_runtime_epoch,
				 application_generation=EXCLUDED.application_generation,p4info_digest=EXCLUDED.p4info_digest,
				 pipeline_digest=EXCLUDED.pipeline_digest,profile_digest=EXCLUDED.profile_digest,
				 capacity_digest=EXCLUDED.capacity_digest,capacity_available=EXCLUDED.capacity_available,
				 lease_valid=EXCLUDED.lease_valid,p4_connected=EXCLUDED.p4_connected,
				 primary_actor=EXCLUDED.primary_actor,pipeline_exact=EXCLUDED.pipeline_exact,
				 high_priority_queue_depth=EXCLUDED.high_priority_queue_depth,
				 telemetry_queue_depth=EXCLUDED.telemetry_queue_depth,
				 observation_queue_depth=EXCLUDED.observation_queue_depth,
				 source_wal_bytes=EXCLUDED.source_wal_bytes,input_wal_bytes=EXCLUDED.input_wal_bytes,
				 result_wal_bytes=EXCLUDED.result_wal_bytes,
				 last_successful_read_unix_ms=EXCLUDED.last_successful_read_unix_ms,
				 freshness=EXCLUDED.freshness,reason_code=EXCLUDED.reason_code,
				 observation_id=EXCLUDED.observation_id,observation_digest=EXCLUDED.observation_digest,
				 observed_at_unix_ms=EXCLUDED.observed_at_unix_ms,
				 expires_at_unix_ms=EXCLUDED.expires_at_unix_ms,trace_id=EXCLUDED.trace_id
				WHERE target_capability_observations.observed_at_unix_ms < EXCLUDED.observed_at_unix_ms`,
				observation.TargetID, observation.TargetControlIncarnationID, observation.AssignmentGeneration,
				observation.ActorRuntimeEpoch, observation.ApplicationGeneration, observation.P4InfoDigest,
				observation.PipelineDigest, observation.ProfileDigest, observation.CapacityDigest,
				observation.CapacityAvailable, observation.LeaseValid, observation.P4Connected,
				observation.Primary, observation.PipelineExact, observation.HighPriorityQueueDepth,
				observation.TelemetryQueueDepth, observation.ObservationQueueDepth, observation.SourceWALBytes,
				observation.InputWALBytes, observation.ResultWALBytes, observation.LastSuccessfulReadUnixMS,
				observation.Freshness, observation.ReasonCode, observation.ObservationID,
				observation.ObservationDigest, observation.ObservedAtUnixMS, observation.ExpiresAtUnixMS,
				observation.TraceID)
			if err != nil {
				return err
			}
			if tag.RowsAffected() == 0 && !result.Idempotent {
				result.ReasonCode = "STALE_OBSERVATION"
				results[i] = result
				continue
			}
			result.Accepted = true
			if result.Idempotent {
				result.ReasonCode = "IDEMPOTENT"
			} else {
				result.ReasonCode = "CURRENT_UPDATED"
			}
			results[i] = result
		}
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("target: record capability observations: %w", err)
	}
	return results, nil
}

func ComputeCapabilityObservationDigest(observation CapabilityObservation) string {
	copy := observation
	copy.ObservationDigest = ""
	raw, _ := json.Marshal(copy)
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func validateCapabilityObservation(observation CapabilityObservation) error {
	for name, value := range map[string]string{
		"observation_id": observation.ObservationID, "target_id": observation.TargetID,
		"target_control_incarnation_id": observation.TargetControlIncarnationID,
		"actor_runtime_epoch":           observation.ActorRuntimeEpoch, "freshness": observation.Freshness,
		"reason_code": observation.ReasonCode, "trace_id": observation.TraceID,
	} {
		if !identityRE.MatchString(value) || len(value) > 128 {
			return fmt.Errorf("%s malformed", name)
		}
	}
	for name, digest := range map[string]string{
		"observation": observation.ObservationDigest, "p4info": observation.P4InfoDigest,
		"pipeline": observation.PipelineDigest, "profile": observation.ProfileDigest,
		"capacity": observation.CapacityDigest,
	} {
		if !digestRE.MatchString(digest) {
			return fmt.Errorf("%s digest malformed", name)
		}
	}
	if observation.ObservationDigest != ComputeCapabilityObservationDigest(observation) {
		return errors.New("observation digest mismatch")
	}
	if observation.AssignmentGeneration < 1 || observation.ApplicationGeneration < 1 ||
		observation.ObservedAtUnixMS < 1 || observation.ExpiresAtUnixMS <= observation.ObservedAtUnixMS ||
		observation.ExpiresAtUnixMS-observation.ObservedAtUnixMS > assignmentLeaseMaxMS {
		return errors.New("generation/time/freshness window invalid")
	}
	switch observation.Freshness {
	case "not-observed", "fresh", "stale", "gap":
	default:
		return errors.New("freshness enum invalid")
	}
	if observation.HighPriorityQueueDepth > 128 || observation.TelemetryQueueDepth > 64 ||
		observation.ObservationQueueDepth > 8 || observation.SourceWALBytes > 1<<30 ||
		observation.InputWALBytes > 2<<30 || observation.ResultWALBytes > 2<<30 {
		return errors.New("queue/WAL resource observation exceeds profile")
	}
	return nil
}

func validateTarget(t Target) error {
	if !endpointRE.MatchString(t.P4RuntimeEndpoint) {
		return fmt.Errorf("target: bad p4runtime_endpoint (must be https://host:port)")
	}
	if t.DeviceID < 1 {
		return errors.New("target: device_id >= 1 required")
	}
	if t.DisplayName == "" || len(t.DisplayName) > 256 || t.Role == "" || len(t.Role) > 64 {
		return errors.New("target: role required")
	}
	if !identityRE.MatchString(t.CredentialRef) {
		return errors.New("target: bad credential_ref")
	}
	if !identityRE.MatchString(t.TLS.IdentityRef) || t.TLS.IdentityRef != t.CredentialRef ||
		t.TLS.ServerName == "" || len(t.TLS.ServerName) > 253 || !regexp.MustCompile(`^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$`).MatchString(t.TLS.ServerName) {
		return errors.New("target: exact TLS server_name/identity_ref required")
	}
	if t.Scope == "" || len(t.Scope) > 256 || !digestRE.MatchString(t.DesiredProfileDigest) ||
		!identityRE.MatchString(t.TraceID) || !identityRE.MatchString(t.IdempotencyKey) {
		return errors.New("target: scope/profile/trace/idempotency identity malformed")
	}
	if err := t.Actor.Validate(); err != nil {
		return fmt.Errorf("target: actor: %w", err)
	}
	return nil
}

func validateLifecycleAuthorization(t Target, auth LifecycleAuthorization) error {
	return validateLifecycleMutation(t.Actor, auth, t.Scope, "TARGET_REGISTERED", t.TraceID)
}

func validateLifecycleMutation(actor security.Actor, auth LifecycleAuthorization, scope, reasonCode, traceID string) error {
	if err := actor.Validate(); err != nil {
		return err
	}
	if scope == "" || auth.Scope != scope || !auth.PlatformAdmin || !auth.StepUpFresh || !auth.CSRFVerified {
		return errors.New("target: lifecycle mutation requires exact scoped Platform Admin, CSRF, and fresh step-up")
	}
	if !reasonRE.MatchString(reasonCode) || !identityRE.MatchString(traceID) {
		return errors.New("target: lifecycle reason/trace malformed")
	}
	return nil
}

func appendLifecycleEvent(ctx context.Context, tx *db.Tx, targetID, previousStatus, newStatus, scope string,
	actor security.Actor, reasonCode, traceID string, occurredAtUnixMS int64) error {
	if occurredAtUnixMS < 1 || !reasonRE.MatchString(reasonCode) {
		return errors.New("target: lifecycle event time/reason invalid")
	}
	sum := sha256.Sum256([]byte(targetID + "|" + previousStatus + "|" + newStatus + "|" + traceID))
	eventID := "tle-" + hex.EncodeToString(sum[:16])
	_, err := tx.Exec(ctx, `INSERT INTO target_lifecycle_events(event_id,target_id,previous_status,
		new_status,scope,actor_ref,actor_issuer,actor_subject,reason_code,trace_id,occurred_at_unix_ms)
		VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)`, eventID, targetID, nullableStatus(previousStatus),
		newStatus, scope, actor.String(), actor.Issuer, actor.Subject, reasonCode, traceID, occurredAtUnixMS)
	return err
}

func nullableStatus(status string) any {
	if status == "" {
		return nil
	}
	return status
}
