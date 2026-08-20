package governance

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"time"

	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

// ProposalService creates immutable effect proposals. A proposal is never
// executable and never a work queue (no claim_state). R0 pure reads create no
// proposal at all (ADR-0001 §2).
type ProposalService struct {
	pool *db.Pool
	now  func() time.Time
}

var supersedeReasonRE = regexp.MustCompile(`^[A-Z][A-Z0-9_]{0,63}$`)

// Supersede closes an immutable proposal in favor of an already-created exact
// replacement. It never edits either body and cannot supersede a proposal that
// already has a terminal Decision or an Intent.
func (s *ProposalService) Supersede(ctx context.Context, oldProposalID, replacementProposalID string,
	actor security.Actor, reasonCode string) error {
	if oldProposalID == "" || replacementProposalID == "" || oldProposalID == replacementProposalID ||
		!supersedeReasonRE.MatchString(reasonCode) {
		return fmt.Errorf("governance: supersede identity/reason malformed")
	}
	if err := actor.Validate(); err != nil {
		return err
	}
	return s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		var oldScope, oldKind, oldTargetSet, newScope, newKind, newTargetSet string
		var oldTargets, newTargets []string
		if err := tx.QueryRow(ctx, `SELECT scope,effect_kind,target_set_digest,target_ids FROM effect_proposals
			WHERE proposal_id=$1 AND superseded_by_proposal_id IS NULL FOR UPDATE`, oldProposalID).
			Scan(&oldScope, &oldKind, &oldTargetSet, &oldTargets); err != nil {
			return err
		}
		if err := tx.QueryRow(ctx, `SELECT scope,effect_kind,target_set_digest,target_ids FROM effect_proposals
			WHERE proposal_id=$1 AND superseded_by_proposal_id IS NULL FOR SHARE`, replacementProposalID).
			Scan(&newScope, &newKind, &newTargetSet, &newTargets); err != nil {
			return err
		}
		if oldScope != newScope || oldKind != newKind || oldTargetSet != newTargetSet ||
			fmt.Sprint(oldTargets) != fmt.Sprint(newTargets) {
			return fmt.Errorf("governance: replacement scope/effect/target set differs")
		}
		var blocked bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM effect_decisions WHERE proposal_id=$1)
			OR EXISTS(SELECT 1 FROM effect_intents WHERE proposal_id=$1)`, oldProposalID).Scan(&blocked); err != nil {
			return err
		}
		if blocked {
			return fmt.Errorf("governance: decided/intent-backed proposal cannot be superseded")
		}
		tag, err := tx.Exec(ctx, `UPDATE effect_proposals SET superseded_by_proposal_id=$1,
			superseded_at_unix_ms=$2,superseded_by_actor_ref=$3,supersede_reason_code=$4
			WHERE proposal_id=$5 AND superseded_by_proposal_id IS NULL`, replacementProposalID,
			s.now().UnixMilli(), actor.String(), reasonCode, oldProposalID)
		if err != nil || tag.RowsAffected() != 1 {
			return fmt.Errorf("governance: supersede CAS conflict")
		}
		return nil
	})
}

func NewProposalService(pool *db.Pool) *ProposalService {
	return &ProposalService{pool: pool, now: time.Now}
}

// Create validates, computes the immutable canonical proposal_digest, and
// durably persists the proposal in a short transaction. The proposal is
// non-executable; only a later approved Decision can produce a claimable Intent.
func (s *ProposalService) Create(ctx context.Context, p Proposal) (*Proposal, error) {
	if err := validateProposal(p); err != nil {
		return nil, err
	}
	p.TargetIDs = append([]string(nil), p.TargetIDs...)
	sort.Strings(p.TargetIDs)
	p.EvidenceRefs = append([]string(nil), p.EvidenceRefs...)
	sort.Strings(p.EvidenceRefs)
	p.ProposalDigest = computeProposalDigest(p)
	p.CreatedAtUnixMS = s.now().UnixMilli()
	evidenceJSON, _ := json.Marshal(p.EvidenceRefs)
	tag, err := s.pool.Pool.Exec(ctx, `
		INSERT INTO effect_proposals (
				proposal_id, proposal_digest, actor_ref, scope, risk_level,
				effect_kind, target_set_digest, policy_digest, evidence_refs,
				expires_at_unix_ms, note, created_at_unix_ms, trace_id, reason_code,
				actor_issuer, actor_subject, actor_level, target_ids,idempotency_key)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
			ON CONFLICT DO NOTHING`,
		p.ProposalID, p.ProposalDigest, p.Actor.String(), p.Scope, string(p.RiskLevel),
		string(p.EffectKind), p.TargetSetDigest, p.PolicyDigest, evidenceJSON,
		p.ExpiresAtUnixMS, p.Note, p.CreatedAtUnixMS, p.TraceID, "PROPOSED",
		p.Actor.Issuer, p.Actor.Subject, string(p.ActorLevel), p.TargetIDs, p.IdempotencyKey)
	if err != nil {
		return nil, fmt.Errorf("governance: create proposal: %w", err)
	}
	if tag.RowsAffected() == 0 {
		var existingID, existingDigest string
		if err := s.pool.Pool.QueryRow(ctx, `SELECT proposal_id,proposal_digest FROM effect_proposals
		 WHERE actor_ref=$1 AND idempotency_key=$2`, p.Actor.String(), p.IdempotencyKey).Scan(&existingID, &existingDigest); err != nil {
			return nil, fmt.Errorf("governance: proposal conflict: %w", err)
		}
		if existingDigest != p.ProposalDigest {
			return nil, fmt.Errorf("governance: idempotency key reused with different proposal digest")
		}
		p.ProposalID = existingID
		p.ProposalDigest = existingDigest
		if err := s.pool.Pool.QueryRow(ctx, `SELECT created_at_unix_ms FROM effect_proposals WHERE proposal_id=$1`, existingID).
			Scan(&p.CreatedAtUnixMS); err != nil {
			return nil, fmt.Errorf("governance: load canonical proposal: %w", err)
		}
	}
	return &p, nil
}

func validateProposal(p Proposal) error {
	if p.ProposalID == "" || p.Scope == "" || p.IdempotencyKey == "" || len(p.IdempotencyKey) > 128 {
		return fmt.Errorf("governance: proposal_id and scope required")
	}
	if p.RiskLevel != security.R0 && p.RiskLevel != security.R1 &&
		p.RiskLevel != security.R2 && p.RiskLevel != security.R3 {
		return fmt.Errorf("governance: bad risk_level %q", p.RiskLevel)
	}
	if p.RiskLevel == security.R0 && (p.EffectKind != KindBoundedCapture ||
		(p.ActorLevel != security.LevelOperator && p.ActorLevel != security.LevelScopedOperator) || len(p.TargetIDs) != 1) {
		return fmt.Errorf("governance: R0 governance is limited to single-target Operator bounded capture")
	}
	if err := p.Actor.Validate(); err != nil {
		return fmt.Errorf("governance: actor: %w", err)
	}
	if p.ActorLevel != security.LevelAnalyst && p.ActorLevel != security.LevelOperator &&
		p.ActorLevel != security.LevelScopedOperator && p.ActorLevel != security.LevelPlatformAdmin {
		return fmt.Errorf("governance: invalid proposer authorization level %q", p.ActorLevel)
	}
	if len(p.TargetIDs) == 0 || len(p.TargetIDs) > 128 {
		return fmt.Errorf("governance: target_ids must contain 1..128 identities")
	}
	ids := append([]string(nil), p.TargetIDs...)
	sort.Strings(ids)
	for i, id := range ids {
		if id == "" || len(id) > 128 || (i > 0 && ids[i-1] == id) {
			return fmt.Errorf("governance: target_ids malformed or duplicated")
		}
	}
	if p.ExpiresAtUnixMS <= time.Now().UnixMilli() {
		return fmt.Errorf("governance: proposal expiry must be in the future")
	}
	if len(p.Note) > 2048 {
		return fmt.Errorf("governance: note exceeds 2 KiB")
	}
	if len(p.EvidenceRefs) > 128 {
		return fmt.Errorf("governance: too many evidence_refs")
	}
	refs := append([]string(nil), p.EvidenceRefs...)
	sort.Strings(refs)
	for i, ref := range refs {
		if ref == "" || len(ref) > 256 || (i > 0 && refs[i-1] == ref) {
			return fmt.Errorf("governance: evidence_refs malformed or duplicated")
		}
	}
	return nil
}

// computeProposalDigest is the canonical, immutable digest bound to the
// proposal. Any field change produces a different digest (EFFECT-PROPOSAL-IMMUTABLE).
func computeProposalDigest(p Proposal) string {
	h := sha256.New()
	fmt.Fprintf(h, "%s|%s|%s|%s|%s|%s|%s|%s|%d",
		p.Actor.String(), p.ActorLevel, p.Scope, p.RiskLevel, p.EffectKind,
		p.TargetSetDigest, p.PolicyDigest, p.Note, p.ExpiresAtUnixMS)
	ids := append([]string(nil), p.TargetIDs...)
	sort.Strings(ids)
	for _, id := range ids {
		fmt.Fprintf(h, "|target:%s", id)
	}
	refs := append([]string(nil), p.EvidenceRefs...)
	sort.Strings(refs)
	for _, e := range refs {
		fmt.Fprintf(h, "|%s", e)
	}
	return "sha256:" + hex.EncodeToString(h.Sum(nil))
}
