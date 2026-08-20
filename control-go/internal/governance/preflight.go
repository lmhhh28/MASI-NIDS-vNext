package governance

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/db"
)

// PreflightToken is a read-only, short-lived token binding the proposal/
// evidence/target/generation/P4Info/pipeline/capacity/policy digests observed
// at preflight time. It has a ≤10s total deadline and ≤30s validity.
//
// A preflight token MUST NOT: claim an Intent, obtain a writer reservation,
// send a P4 Write, modify/delete an entity, clear a counter, change scheduler/
// checkpoint, or hold a DB connection (ADR-0004 §4). It is produced by a
// transaction-free read and consumed by the later short Decision/Intent txn.
type PreflightToken struct {
	ProposalDigest    string `json:"proposal_digest"`
	TargetID          string `json:"target_id"`
	TargetSetDigest   string `json:"target_set_digest"`
	Fence             Fence  `json:"fence"`
	PolicyDigest      string `json:"policy_digest"`
	EvidenceDigest    string `json:"evidence_digest"`
	CapacityAvailable bool   `json:"capacity_available"`
	ObservedAtUnixMS  int64  `json:"observed_at_unix_ms"`
	IssuedAtUnixMS    int64  `json:"issued_at_unix_ms"`
	ExpiresAtUnixMS   int64  `json:"expires_at_unix_ms"`
	TraceID           string `json:"trace_id"`
}

const (
	preflightMaxDeadline = 10 * time.Second
	preflightMaxValidity = 30 * time.Second
)

// PreflightService performs transaction-free, read-only preflight checks. It
// reads current facts (target-control, assignment, application-gen, pipeline/
// P4Info, capacity, policy) and binds them into a short-lived token. It does
// NOT claim, write, or hold a connection beyond the read.
type PreflightService struct {
	pool *db.Pool
	now  func() time.Time
}

func NewPreflightService(pool *db.Pool) *PreflightService {
	return &PreflightService{pool: pool, now: time.Now}
}

// Preflight validates the proposal's preconditions against current durable
// facts and returns a read-only token. On any drift (expired, wrong generation,
// P4Info drift, capacity exceeded) it returns an error WITHOUT claiming or
// writing anything (zero Edge RPC).
func (s *PreflightService) Preflight(ctx context.Context, proposalID string) (*PreflightToken, error) {
	return s.preflight(ctx, proposalID, false)
}

func (s *PreflightService) preflight(ctx context.Context, proposalID string, allowExpiredProposal bool) (*PreflightToken, error) {
	deadlineCtx, cancel := context.WithTimeout(ctx, preflightMaxDeadline)
	defer cancel()

	var proposalDigest, targetSetDigest, policyDigest, traceID, targetID, evidenceJSON, effectKind, scope string
	var targetStatus, incarnationID, edgeWorkloadRef, actorRuntimeEpoch, observedActorRuntimeEpoch, p4InfoDigest, pipelineDigest, capacityDigest string
	var proposalExpires, assignmentGeneration, applicationGeneration int64
	var assignmentExpires, capabilityExpires, electionFloor int64
	var capacityAvailable bool
	now := s.now()
	err := s.pool.Pool.QueryRow(deadlineCtx, `
			SELECT p.proposal_digest, p.target_set_digest, p.policy_digest, p.trace_id,
				       p.expires_at_unix_ms, p.target_ids[1], p.evidence_refs::text,p.effect_kind,p.scope,
				       t.status, a.incarnation_id, a.assignment_generation,a.edge_workload_ref,a.actor_runtime_epoch,
				       a.application_generation, a.expires_at_unix_ms,a.election_floor,
				       c.p4info_digest, c.pipeline_digest, c.capacity_digest,
				       c.capacity_available, c.expires_at_unix_ms,c.actor_runtime_epoch
			FROM effect_proposals p
			JOIN targets t ON cardinality(p.target_ids)=1 AND t.target_id=p.target_ids[1]
			JOIN LATERAL (
			    SELECT * FROM target_assignments x
			    WHERE x.target_id=t.target_id
			    ORDER BY x.assignment_generation DESC LIMIT 1
			) a ON a.revoked_at_unix_ms IS NULL
			JOIN target_capability_observations c ON c.target_id=t.target_id
				WHERE p.proposal_id = $1 AND p.superseded_by_proposal_id IS NULL`, proposalID).
		Scan(&proposalDigest, &targetSetDigest, &policyDigest, &traceID,
			&proposalExpires, &targetID, &evidenceJSON, &effectKind, &scope, &targetStatus,
			&incarnationID, &assignmentGeneration, &edgeWorkloadRef, &actorRuntimeEpoch, &applicationGeneration,
			&assignmentExpires, &electionFloor, &p4InfoDigest, &pipelineDigest, &capacityDigest,
			&capacityAvailable, &capabilityExpires, &observedActorRuntimeEpoch)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, fmt.Errorf("preflight: proposal %s not found", proposalID)
		}
		return nil, fmt.Errorf("preflight: read proposal: %w", err)
	}
	if (!allowExpiredProposal && proposalExpires <= now.UnixMilli()) || targetStatus != "active" ||
		assignmentExpires <= now.UnixMilli() || capabilityExpires <= now.UnixMilli() || !capacityAvailable ||
		actorRuntimeEpoch == "" || actorRuntimeEpoch != observedActorRuntimeEpoch || electionFloor < 1 {
		return nil, fmt.Errorf("preflight: stale target/assignment/capacity facts (HOLD, zero Edge RPC)")
	}
	var policyExact bool
	switch EffectKind(effectKind) {
	case KindFirewallBaselineActivate, KindFirewallRollback:
		err = s.pool.Pool.QueryRow(deadlineCtx, `SELECT EXISTS(SELECT 1 FROM firewall_revisions
			WHERE revision_digest=$1 AND target_id=$2 AND scope=$3)`, policyDigest, targetID, scope).Scan(&policyExact)
	case KindFirewallOverlay:
		err = s.pool.Pool.QueryRow(deadlineCtx, `SELECT EXISTS(SELECT 1 FROM firewall_overlays
			WHERE target_id=$1 AND scope=$2 AND rule->>'canonical_rule_digest'=$3 AND NOT deleted)`,
			targetID, scope, policyDigest).Scan(&policyExact)
	case KindBoundedCapture:
		err = s.pool.Pool.QueryRow(deadlineCtx, `SELECT EXISTS(
			SELECT 1 FROM bounded_capture_requests r
			WHERE r.target_id=$1 AND r.scope=$2 AND r.capture_digest=$3
			  AND r.state IN ('planned','authorized','claimed','executing','unknown')
			  AND r.expires_at_unix_ms>$4
			  AND (SELECT count(*) FROM bounded_capture_requests c
			       WHERE c.target_id=r.target_id AND c.state IN ('authorized','claimed','executing','unknown')
			         AND c.expires_at_unix_ms>$4) <= r.max_concurrent_on_target)`,
			targetID, scope, policyDigest, now.UnixMilli()).Scan(&policyExact)
	default:
		return nil, fmt.Errorf("preflight: effect kind %s is not a P4 effect", effectKind)
	}
	if err != nil {
		return nil, fmt.Errorf("preflight: validate policy fact: %w", err)
	}
	if !policyExact {
		return nil, fmt.Errorf("preflight: policy digest/target/scope not current (HOLD, zero Edge RPC)")
	}
	evidenceHash := sha256.Sum256([]byte(evidenceJSON))
	token := &PreflightToken{
		ProposalDigest:  proposalDigest,
		TargetSetDigest: targetSetDigest,
		PolicyDigest:    policyDigest,
		TargetID:        targetID,
		Fence: Fence{
			TargetControlIncarnationID: incarnationID,
			TargetAssignmentGeneration: assignmentGeneration,
			EdgeWorkloadRef:            edgeWorkloadRef,
			ActorRuntimeEpoch:          actorRuntimeEpoch,
			ApplicationGeneration:      applicationGeneration,
			ElectionIDLow:              uint64(electionFloor),
			P4InfoDigest:               p4InfoDigest,
			PipelineDigest:             pipelineDigest,
			CapacityDigest:             capacityDigest,
		},
		EvidenceDigest:    "sha256:" + hex.EncodeToString(evidenceHash[:]),
		TraceID:           traceID,
		ObservedAtUnixMS:  now.UnixMilli(),
		IssuedAtUnixMS:    now.UnixMilli(),
		ExpiresAtUnixMS:   now.Add(preflightMaxValidity).UnixMilli(),
		CapacityAvailable: capacityAvailable,
	}
	return token, nil
}

// ValidateIntent repeats every mutable precondition immediately before claim.
// Normal effects require an unexpired proposal and decision. A durable
// overlay-delete intent may outlive the original authorization TTL because the
// approved overlay body bound its expiry; only that time check is relaxed. Its
// target/assignment/P4Info/capacity/policy facts are still revalidated.
func (s *PreflightService) ValidateIntent(ctx context.Context, intent Intent) error {
	if s == nil || s.pool == nil || intent.IsFleetParent || intent.DeadlineUnixMS <= s.now().UnixMilli() {
		return errors.New("preflight: intent unavailable, parent, or expired")
	}
	allowExpiredAuthorization := intent.Payload.Operation == "overlay-delete"
	token, err := s.preflight(ctx, intent.ProposalID, allowExpiredAuthorization)
	if err != nil {
		return err
	}
	if token.TargetID != intent.TargetID || token.PolicyDigest != intent.Payload.PolicyRevisionDigest ||
		token.Fence.TargetControlIncarnationID != intent.Fence.TargetControlIncarnationID ||
		token.Fence.TargetAssignmentGeneration != intent.Fence.TargetAssignmentGeneration ||
		token.Fence.EdgeWorkloadRef != intent.Fence.EdgeWorkloadRef ||
		token.Fence.ActorRuntimeEpoch != intent.Fence.ActorRuntimeEpoch ||
		token.Fence.ApplicationGeneration != intent.Fence.ApplicationGeneration ||
		token.Fence.P4InfoDigest != intent.Fence.P4InfoDigest || token.Fence.PipelineDigest != intent.Fence.PipelineDigest ||
		token.Fence.CapacityDigest != intent.Fence.CapacityDigest || token.Fence.ElectionIDLow != intent.Fence.ElectionIDLow {
		return errors.New("preflight: intent fence drift (HOLD, zero Edge RPC)")
	}
	nowMS := s.now().UnixMilli()
	var authorizationExact bool
	if err := s.pool.Pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM effect_decisions d
		 JOIN effect_proposals p ON p.proposal_id=d.proposal_id
		 WHERE d.decision_id=$1 AND d.proposal_id=$2 AND d.decision='approve'
		   AND d.decision_digest=$3 AND p.effect_kind=$4 AND p.risk_level=$5
		   AND d.actor_issuer=$6 AND d.actor_subject=$7
		   AND ($8 OR (d.expires_at_unix_ms>$9 AND p.expires_at_unix_ms>$9)))`,
		intent.DecisionID, intent.ProposalID, intent.AuthorizationDigest, string(intent.EffectKind),
		string(intent.RiskLevel), intent.Actor.Issuer, intent.Actor.Subject,
		allowExpiredAuthorization, nowMS).Scan(&authorizationExact); err != nil {
		return err
	}
	if !authorizationExact {
		return errors.New("preflight: authorization stale/mismatched (HOLD, zero Edge RPC)")
	}
	if allowExpiredAuthorization {
		var deleteReady bool
		if err := s.pool.Pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM firewall_overlays
		 WHERE delete_intent_id=$1 AND target_id=$2 AND NOT deleted AND expires_at_unix_ms<=$3
		 AND rule->>'canonical_rule_digest'=$4)`, intent.EffectIntentID, intent.TargetID, nowMS,
			intent.Payload.PolicyRevisionDigest).Scan(&deleteReady); err != nil {
			return err
		}
		if !deleteReady {
			return errors.New("preflight: overlay delete is early/stale (HOLD, zero Edge RPC)")
		}
	}
	return nil
}

// MarshalJSON serializes the token for binding into the Intent txn.
func (t *PreflightToken) MarshalJSON() ([]byte, error) {
	type alias PreflightToken
	return json.Marshal((*alias)(t))
}

// Valid reports whether the token is still within its validity window.
func (t *PreflightToken) Valid(now time.Time) bool {
	return now.UnixMilli() >= t.IssuedAtUnixMS && now.UnixMilli() < t.ExpiresAtUnixMS
}
