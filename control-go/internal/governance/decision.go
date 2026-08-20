package governance

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

// DecisionService appends authorization decisions. A decision binds the exact
// proposal_digest and enforces R0-R3 maker-checker via security.CanApprove.
// One terminal decision per proposal (unique constraint); a second approve is
// a unique violation, not a silent overwrite (ADR-0001 §2, ADR-0004).
type DecisionService struct {
	pool    *db.Pool
	mapping *security.RoleScopeMapping
	now     func() time.Time
}

func NewDecisionService(pool *db.Pool, mapping *security.RoleScopeMapping) *DecisionService {
	return &DecisionService{pool: pool, mapping: mapping, now: time.Now}
}

// Approve records an approval after enforcing maker-checker. The approver MUST
// differ from the proposer for R2/R3, with fresh phishing-resistant step-up.
// Platform Admin/service-account/Agent/Frontend-BFF cannot replace a human
// approver (SEC-002). A stale/expired/scope-drift authorization is rejected
// with ZERO Edge RPC (ADR-0001 §2).
func (s *DecisionService) Approve(ctx context.Context, proposalID string, approver security.Actor, authz AuthzContext) (*Decision, error) {
	return s.decide(ctx, proposalID, approver, authz, true, "APPROVED", nil)
}

// Reject records a rejection. Maker-checker is still enforced (a reject is a
// terminal decision; the proposer cannot self-reject-silently to bypass audit).
func (s *DecisionService) Reject(ctx context.Context, proposalID string, approver security.Actor, authz AuthzContext) (*Decision, error) {
	return s.decide(ctx, proposalID, approver, authz, false, "REJECTED", nil)
}

func (s *DecisionService) ApproveWithReason(ctx context.Context, proposalID string, approver security.Actor, authz AuthzContext, reasonCode string) (*Decision, error) {
	return s.decide(ctx, proposalID, approver, authz, true, reasonCode, nil)
}

func (s *DecisionService) RejectWithReason(ctx context.Context, proposalID string, approver security.Actor, authz AuthzContext, reasonCode string) (*Decision, error) {
	return s.decide(ctx, proposalID, approver, authz, false, reasonCode, nil)
}

// DecisionAtomicCallback runs only inside the short serializable transaction
// that appends (or recalls) the terminal Decision. It must perform database work
// only: external calls/waits are forbidden. inserted is true only when this
// transaction appended the Decision.
type DecisionAtomicCallback func(context.Context, *db.Tx, *Decision, bool) error

// ApproveWithReasonAtomic is reserved for facts that must be formed in the same
// transaction as approval (notably fleet parent + complete child vector).
func (s *DecisionService) ApproveWithReasonAtomic(ctx context.Context, proposalID string,
	approver security.Actor, authz AuthzContext, reasonCode string, callback DecisionAtomicCallback) (*Decision, error) {
	if callback == nil {
		return nil, errors.New("governance: atomic decision callback required")
	}
	return s.decide(ctx, proposalID, approver, authz, true, reasonCode, callback)
}

var reasonCodeRE = regexp.MustCompile(`^[A-Z][A-Z0-9_]{0,63}$`)

func (s *DecisionService) decide(ctx context.Context, proposalID string, approver security.Actor, authz AuthzContext, approved bool, reasonCode string, callback DecisionAtomicCallback) (*Decision, error) {
	if err := approver.Validate(); err != nil {
		return nil, fmt.Errorf("governance: approver: %w", err)
	}
	if !reasonCodeRE.MatchString(reasonCode) {
		return nil, errors.New("governance: controlled reason_code required")
	}
	// Load the proposal (read-only; no claim, no Write).
	var p Proposal
	var actorIssuer, actorSubject string
	err := s.pool.Pool.QueryRow(ctx, `
			SELECT proposal_id, proposal_digest, actor_issuer, actor_subject, actor_level,
			       scope, risk_level, effect_kind, target_set_digest, policy_digest,
			       expires_at_unix_ms, trace_id
				FROM effect_proposals WHERE proposal_id = $1 AND superseded_by_proposal_id IS NULL`, proposalID).
		Scan(&p.ProposalID, &p.ProposalDigest, &actorIssuer, &actorSubject, &p.ActorLevel,
			&p.Scope, &p.RiskLevel,
			&p.EffectKind, &p.TargetSetDigest, &p.PolicyDigest, &p.ExpiresAtUnixMS, &p.TraceID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, fmt.Errorf("governance: proposal %s not found", proposalID)
		}
		return nil, fmt.Errorf("governance: load proposal: %w", err)
	}
	proposer := security.Actor{Issuer: actorIssuer, Subject: actorSubject}
	if err := proposer.Validate(); err != nil {
		return nil, fmt.Errorf("governance: persisted proposer invalid: %w", err)
	}
	// Stale proposal: expired -> HOLD, zero Edge RPC.
	if s.now().UnixMilli() > p.ExpiresAtUnixMS {
		return nil, fmt.Errorf("governance: proposal expired (HOLD, zero Edge RPC)")
	}
	// Revalidate the exact resource scope at decision time. Session possession or
	// route visibility never grants mutation authority, and a mapping change only
	// affects future decisions.
	if s.mapping == nil {
		return nil, fmt.Errorf("governance: authorization mapping unavailable (default-deny)")
	}
	levels := []security.AuthzContextLevel{authz.Level}
	if authz.Level == "" {
		levels = []security.AuthzContextLevel{security.LevelOperator, security.LevelScopedOperator}
	}
	authorized := false
	for _, level := range levels {
		if _, err := s.mapping.AuthorizeScope(approver, level, p.Scope, string(p.EffectKind), p.TargetSetDigest); err == nil {
			authz.Level = level
			authorized = true
			break
		}
	}
	if !authorized {
		return nil, fmt.Errorf("governance: exact decision scope denied")
	}
	authz.RoleMappingDigest = s.mapping.Digest
	if !approved && authz.Level != security.LevelOperator && authz.Level != security.LevelScopedOperator {
		return nil, fmt.Errorf("governance: reject requires operator/scoped-operator")
	}
	// Maker-checker (R0-R3). This is the approval gate.
	decision := "approve"
	if !approved {
		decision = "reject"
	}
	// For approve, enforce CanApprove; for reject, any authorized actor may reject.
	if approved {
		if err := security.CanApprove(p.RiskLevel, proposer, p.ActorLevel, approver, authz); err != nil {
			return nil, fmt.Errorf("governance: maker-checker: %w", err)
		}
	}
	d := Decision{
		DecisionID:      "dec-" + shortID(p.ProposalID+":"+approver.String()+":"+decision),
		ProposalID:      p.ProposalID,
		ProposalDigest:  p.ProposalDigest,
		Actor:           approver,
		RiskLevel:       p.RiskLevel,
		Approved:        approved,
		Authz:           authz,
		DecisionDigest:  computeDecisionDigest(p.ProposalDigest, approver, decision, reasonCode, authz),
		ReasonCode:      reasonCode,
		ExpiresAtUnixMS: s.now().Add(15 * time.Minute).UnixMilli(),
		CreatedAtUnixMS: s.now().UnixMilli(),
		TraceID:         p.TraceID,
	}
	authzJSON, _ := json.Marshal(authz)
	err = s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		tag, err := tx.Exec(ctx, `
			INSERT INTO effect_decisions (
			 decision_id,proposal_id,proposal_digest,actor_ref,risk_level,decision,authz_context,
			 decision_digest,expires_at_unix_ms,created_at_unix_ms,trace_id,reason_code,actor_issuer,actor_subject)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
			ON CONFLICT (proposal_id) DO NOTHING`, d.DecisionID, d.ProposalID, d.ProposalDigest,
			d.Actor.String(), string(d.RiskLevel), decision, authzJSON, d.DecisionDigest,
			d.ExpiresAtUnixMS, d.CreatedAtUnixMS, d.TraceID, d.ReasonCode, d.Actor.Issuer, d.Actor.Subject)
		if err != nil {
			return err
		}
		inserted := tag.RowsAffected() == 1
		if !inserted {
			var existingDecision, existingIssuer, existingSubject, existingRisk string
			var existingAuthz []byte
			if err := tx.QueryRow(ctx, `SELECT decision_id,decision,actor_issuer,actor_subject,
			 risk_level,authz_context,decision_digest,expires_at_unix_ms,created_at_unix_ms,trace_id,proposal_digest,reason_code
			 FROM effect_decisions WHERE proposal_id=$1 FOR UPDATE`, proposalID).Scan(&d.DecisionID, &existingDecision,
				&existingIssuer, &existingSubject, &existingRisk, &existingAuthz, &d.DecisionDigest,
				&d.ExpiresAtUnixMS, &d.CreatedAtUnixMS, &d.TraceID, &d.ProposalDigest, &d.ReasonCode); err != nil {
				return fmt.Errorf("governance: recall terminal decision: %w", err)
			}
			d.Actor = security.Actor{Issuer: existingIssuer, Subject: existingSubject}
			if err := d.Actor.Validate(); err != nil {
				return fmt.Errorf("governance: persisted decision actor invalid: %w", err)
			}
			d.RiskLevel = RiskLevel(existingRisk)
			d.Approved = existingDecision == "approve"
			if err := json.Unmarshal(existingAuthz, &d.Authz); err != nil {
				return fmt.Errorf("governance: parse persisted authz: %w", err)
			}
		}
		if callback != nil {
			return callback(ctx, tx, &d, inserted)
		}
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("governance: append decision transaction: %w", err)
	}
	return &d, nil
}

func computeDecisionDigest(proposalDigest string, approver security.Actor, decision, reasonCode string, authz AuthzContext) string {
	h := sha256.New()
	fmt.Fprintf(h, "%s|%s|%s|%s|%s|%d|%t", proposalDigest, approver.String(), decision, reasonCode,
		authz.StepUpType, authz.StepUpAgeMS, authz.PhishingResistant)
	return "sha256:" + hex.EncodeToString(h.Sum(nil))
}

func shortID(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:16])
}
