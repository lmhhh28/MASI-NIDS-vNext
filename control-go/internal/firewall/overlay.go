package firewall

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/netip"
	"strings"
	"time"

	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/governance"
	"masi-nids/control-go/internal/security"
)

// OverlayService manages response overlays. An overlay uses the ordinary R0/R1/R2
// governance path + a durable TTL delete intent. It MUST NOT modify the baseline
// selector (ADR-0014). Overlay TTL expiry is PG-deadline-driven: when
// expires_at_unix_ms passes, a durable delete intent is created and Edge
// delete/readback/CAS converges. No Go memory timer is the source of truth.
type OverlayService struct {
	pool *db.Pool
	now  func() time.Time
}

func NewOverlayService(pool *db.Pool) *OverlayService {
	return &OverlayService{pool: pool, now: time.Now}
}

// Create atomically persists the response-overlay fact plus its ordinary
// upsert intent and pre-authorized durable TTL-delete intent. Both remain on the
// sole effect_intents queue; no timer or second dispatcher is introduced.
func (s *OverlayService) Create(ctx context.Context, ov Overlay) error {
	now := s.now()
	if ov.OverlayRuleID == "" || ov.TargetID == "" {
		return fmt.Errorf("firewall: overlay id and target required")
	}
	if err := validateOverlayRule(ov.Rule, ov.ExpiresAtUnixMS, now); err != nil {
		return fmt.Errorf("firewall: overlay rule: %w", err)
	}
	if ov.Scope == "" || ov.OperationID == "" || ov.UpsertIntent.EffectIntentID == "" ||
		ov.UpsertIntent.OperationID != ov.OperationID || ov.DeleteIntent.EffectIntentID == "" ||
		ov.DeleteIntent.OperationID == "" || ov.DeleteIntent.DeadlineUnixMS <= ov.ExpiresAtUnixMS ||
		ov.DeleteIntent.DeadlineUnixMS-ov.ExpiresAtUnixMS > int64(time.Hour/time.Millisecond) ||
		ov.UpsertIntent.EffectIntentID == ov.DeleteIntent.EffectIntentID ||
		ov.UpsertIntent.OperationID == ov.DeleteIntent.OperationID {
		return fmt.Errorf("firewall: overlay requires distinct upsert and bounded durable delete intents")
	}
	if err := ov.Actor.Validate(); err != nil {
		return err
	}
	ruleJSON, err := marshalRule(ov.Rule)
	if err != nil {
		return fmt.Errorf("firewall: marshal overlay rule: %w", err)
	}
	var overlayBody map[string]any
	if err := json.Unmarshal(ruleJSON, &overlayBody); err != nil {
		return err
	}
	overlayBody["expires_at_unix_ms"] = ov.ExpiresAtUnixMS
	edgeRuleJSON, err := json.Marshal(overlayBody)
	if err != nil {
		return err
	}
	upsertIntent, err := prepareOverlayIntent(ov.UpsertIntent, ov, edgeRuleJSON, "overlay-upsert")
	if err != nil {
		return err
	}
	deleteIntent, err := prepareOverlayIntent(ov.DeleteIntent, ov, edgeRuleJSON, "overlay-delete")
	if err != nil {
		return err
	}
	if upsertIntent.ProposalID != deleteIntent.ProposalID || upsertIntent.DecisionID != deleteIntent.DecisionID ||
		upsertIntent.AuthorizationDigest != deleteIntent.AuthorizationDigest || upsertIntent.RiskLevel != deleteIntent.RiskLevel ||
		upsertIntent.Fence != deleteIntent.Fence {
		return fmt.Errorf("firewall: overlay upsert/delete governance fence mismatch")
	}
	err = s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		var proposalScope, policyDigest, proposalRisk, decisionDigest, decisionActorIssuer, decisionActorSubject string
		var proposalExpiry, decisionExpiry int64
		if err := tx.QueryRow(ctx, `SELECT p.scope,p.policy_digest,p.risk_level,p.expires_at_unix_ms,
		 d.decision_digest,d.actor_issuer,d.actor_subject,d.expires_at_unix_ms
		 FROM effect_proposals p JOIN effect_decisions d ON d.proposal_id=p.proposal_id
		 WHERE p.proposal_id=$1 AND d.decision_id=$2 AND d.decision='approve'
		 AND p.effect_kind='firewall-overlay' AND $3=ANY(p.target_ids) FOR UPDATE`,
			upsertIntent.ProposalID, upsertIntent.DecisionID, ov.TargetID).Scan(&proposalScope, &policyDigest,
			&proposalRisk, &proposalExpiry, &decisionDigest, &decisionActorIssuer, &decisionActorSubject,
			&decisionExpiry); err != nil {
			return err
		}
		if proposalScope != ov.Scope || policyDigest != ov.Rule.CanonicalRuleDigest ||
			proposalRisk != string(upsertIntent.RiskLevel) || decisionDigest != upsertIntent.AuthorizationDigest ||
			decisionActorIssuer != ov.Actor.Issuer || decisionActorSubject != ov.Actor.Subject ||
			proposalExpiry <= now.UnixMilli() || decisionExpiry <= now.UnixMilli() ||
			upsertIntent.DeadlineUnixMS <= now.UnixMilli() || upsertIntent.DeadlineUnixMS > proposalExpiry ||
			upsertIntent.DeadlineUnixMS > decisionExpiry {
			return fmt.Errorf("firewall: overlay governance authorization stale/mismatched")
		}
		var targetCurrent bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM targets t JOIN target_assignments a ON a.target_id=t.target_id
		 JOIN target_capability_observations c ON c.target_id=t.target_id
		 WHERE t.target_id=$1 AND t.status='active' AND a.assignment_generation=$2
		 AND a.assignment_generation=(SELECT max(x.assignment_generation) FROM target_assignments x WHERE x.target_id=$1)
		 AND a.incarnation_id=$3 AND a.edge_workload_ref=$4 AND a.actor_runtime_epoch=$5
		 AND a.application_generation=$6 AND a.election_floor=$7 AND a.revoked_at_unix_ms IS NULL
		 AND a.expires_at_unix_ms>$8 AND c.assignment_generation=$2
		 AND c.target_control_incarnation_id=$3 AND c.actor_runtime_epoch=$5 AND c.application_generation=$6
		 AND c.p4info_digest=$9 AND c.pipeline_digest=$10 AND c.capacity_digest=$11
		 AND c.capacity_available AND c.expires_at_unix_ms>$8)`, ov.TargetID,
			upsertIntent.Fence.TargetAssignmentGeneration, upsertIntent.Fence.TargetControlIncarnationID,
			upsertIntent.Fence.EdgeWorkloadRef, upsertIntent.Fence.ActorRuntimeEpoch,
			upsertIntent.Fence.ApplicationGeneration, upsertIntent.Fence.ElectionIDLow, now.UnixMilli(),
			upsertIntent.Fence.P4InfoDigest, upsertIntent.Fence.PipelineDigest,
			upsertIntent.Fence.CapacityDigest).Scan(&targetCurrent); err != nil {
			return err
		}
		if !targetCurrent {
			return fmt.Errorf("firewall: overlay target/assignment/capability drifted")
		}
		if err := insertOverlayIntent(ctx, tx, upsertIntent, 0, "OVERLAY_UPSERT_CREATED"); err != nil {
			return err
		}
		if err := insertOverlayIntent(ctx, tx, deleteIntent, ov.ExpiresAtUnixMS, "OVERLAY_DELETE_SCHEDULED"); err != nil {
			return err
		}
		tag, err := tx.Exec(ctx, `
				INSERT INTO firewall_overlays (overlay_rule_id, target_id, rule, expires_at_unix_ms,
				    deleted, upsert_intent_id,delete_intent_id, operation_id, actor_ref, trace_id, reason_code, scope,
				    delete_effect_digest,delete_decision_id)
				VALUES ($1,$2,$3,$4,false,$5,$6,$7,$8,$9,$10,$11,$12,$13)
				ON CONFLICT(overlay_rule_id) DO NOTHING`,
			ov.OverlayRuleID, ov.TargetID, ruleJSON, ov.ExpiresAtUnixMS,
			upsertIntent.EffectIntentID, deleteIntent.EffectIntentID, ov.OperationID, ov.Actor.String(), ov.TraceID,
			"OVERLAY_CREATED", ov.Scope, deleteIntent.EffectDigest, deleteIntent.DecisionID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			var storedTarget, storedDigest, storedUpsert, storedDelete string
			if err := tx.QueryRow(ctx, `SELECT target_id,rule->>'canonical_rule_digest',upsert_intent_id,
			 delete_intent_id FROM firewall_overlays WHERE overlay_rule_id=$1`, ov.OverlayRuleID).
				Scan(&storedTarget, &storedDigest, &storedUpsert, &storedDelete); err != nil {
				return err
			}
			if storedTarget != ov.TargetID || storedDigest != ov.Rule.CanonicalRuleDigest ||
				storedUpsert != upsertIntent.EffectIntentID || storedDelete != deleteIntent.EffectIntentID {
				return fmt.Errorf("firewall: overlay immutable identity conflict")
			}
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("firewall: create overlay: %w", err)
	}
	return nil
}

func prepareOverlayIntent(input governance.Intent, ov Overlay, edgeRuleJSON []byte, operation string) (governance.Intent, error) {
	intent := input
	intent.TargetID = ov.TargetID
	intent.Actor = ov.Actor
	intent.IsFleetParent = false
	if intent.RequiredWriteAtomicity == "" {
		intent.RequiredWriteAtomicity = "CONTINUE_ON_ERROR"
	}
	if intent.EffectKind != governance.KindFirewallOverlay ||
		(intent.RiskLevel != "R1" && intent.RiskLevel != "R2") {
		return governance.Intent{}, fmt.Errorf("firewall: overlay intent kind/risk must be firewall-overlay R1/R2")
	}
	if err := governance.ValidateFence(intent.Fence); err != nil {
		return governance.Intent{}, err
	}
	intent.Payload = governance.EffectPayload{SchemaVersion: "p4-effect-payload/v1", Operation: operation,
		PolicyRevisionDigest: ov.Rule.CanonicalRuleDigest, BaselineRules: json.RawMessage(`[]`),
		OverlayRules: append(append(json.RawMessage(`[`), edgeRuleJSON...), ']')}
	if err := governance.ValidateEffectPayload(intent.Payload); err != nil {
		return governance.Intent{}, err
	}
	computed := governance.ComputeEffectDigest(intent)
	if computed == "" || input.EffectDigest != "" && input.EffectDigest != computed {
		return governance.Intent{}, fmt.Errorf("firewall: overlay %s effect digest mismatch", operation)
	}
	intent.EffectDigest = computed
	return intent, nil
}

func insertOverlayIntent(ctx context.Context, tx *db.Tx, intent governance.Intent, notBefore int64, reason string) error {
	fenceJSON, err := json.Marshal(intent.Fence)
	if err != nil {
		return err
	}
	payloadJSON, err := json.Marshal(intent.Payload)
	if err != nil {
		return err
	}
	tag, err := tx.Exec(ctx, `INSERT INTO effect_intents(
		 effect_intent_id,operation_id,proposal_id,proposal_digest,decision_id,target_id,is_fleet_parent,fence,effect_digest,
		 authorization_digest,effect_kind,risk_level,required_write_atomicity,deadline_unix_ms,claim_state,
		 actor_ref,trace_id,reason_code,actor_issuer,actor_subject,gate_open,not_before_unix_ms,effect_payload)
		 VALUES($1,$2,$3,(SELECT proposal_digest FROM effect_proposals WHERE proposal_id=$3),$4,$5,false,$6,$7,$8,$9,$10,$11,$12,'unclaimed',$13,$14,$15,$16,$17,true,$18,$19)
		 ON CONFLICT DO NOTHING`, intent.EffectIntentID, intent.OperationID, intent.ProposalID, intent.DecisionID,
		intent.TargetID, fenceJSON, intent.EffectDigest, intent.AuthorizationDigest, string(intent.EffectKind),
		string(intent.RiskLevel), intent.RequiredWriteAtomicity, intent.DeadlineUnixMS, intent.Actor.String(),
		intent.TraceID, reason, intent.Actor.Issuer, intent.Actor.Subject, notBefore, payloadJSON)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		var storedID, storedDigest string
		if err := tx.QueryRow(ctx, `SELECT effect_intent_id,effect_digest FROM effect_intents WHERE operation_id=$1`,
			intent.OperationID).Scan(&storedID, &storedDigest); err != nil {
			return err
		}
		if storedID != intent.EffectIntentID || storedDigest != intent.EffectDigest {
			return fmt.Errorf("firewall: overlay intent immutable identity conflict")
		}
	}
	return nil
}

// ExpireOverlays projects already-created durable delete intents: expired
// pending rows remain available to the ordinary effect dispatcher, while an
// exact finalized delete intent atomically marks its overlay deleted. The
// baseline selector is never modified.
func (s *OverlayService) ExpireOverlays(ctx context.Context) (int, error) {
	now := s.now().UnixMilli()
	finalized, err := s.pool.Pool.Exec(ctx, `UPDATE firewall_overlays o SET deleted=true
	 FROM effect_intents i WHERE o.delete_intent_id=i.effect_intent_id AND o.deleted=false
	 AND o.expires_at_unix_ms<=$1 AND i.claim_state='finalized'`, now)
	if err != nil {
		return 0, fmt.Errorf("firewall: finalize expired overlays: %w", err)
	}
	rows, err := s.pool.Pool.Query(ctx, `
			SELECT o.overlay_rule_id, o.target_id, o.operation_id
			FROM firewall_overlays o JOIN effect_intents i ON i.effect_intent_id=o.delete_intent_id
			WHERE o.deleted = false AND o.expires_at_unix_ms <= $1
			  AND i.claim_state <> 'finalized'`, now)
	if err != nil {
		return 0, fmt.Errorf("firewall: query expired overlays: %w", err)
	}
	defer rows.Close()
	count := int(finalized.RowsAffected())
	for rows.Next() {
		var id, targetID, opID string
		if err := rows.Scan(&id, &targetID, &opID); err != nil {
			return count, fmt.Errorf("firewall: scan expired overlay: %w", err)
		}
		_ = id
		_ = targetID
		_ = opID
		count++
	}
	return count, rows.Err()
}

// MarkDeleted records that the delete intent's Edge delete/readback/CAS has
// converged. Called after the effect dispatcher finalizes the delete intent.
func (s *OverlayService) MarkDeleted(ctx context.Context, overlayRuleID string) error {
	tag, err := s.pool.Pool.Exec(ctx, `
			UPDATE firewall_overlays o SET deleted = true
			FROM effect_intents i
			WHERE o.overlay_rule_id = $1 AND i.effect_intent_id=o.delete_intent_id
			  AND i.claim_state='finalized' AND o.deleted=false`, overlayRuleID)
	if err != nil {
		return fmt.Errorf("firewall: mark overlay deleted: %w", err)
	}
	if tag.RowsAffected() != 1 {
		return fmt.Errorf("firewall: delete intent not finalized or overlay absent")
	}
	return nil
}

func marshalRule(r OverlayRule) ([]byte, error) {
	return marshalJSON(r)
}

func validateOverlayRule(rule OverlayRule, expiresAtUnixMS int64, now time.Time) error {
	if rule.RuleID == "" || len(rule.RuleID) > 128 || rule.RuleRevision < 1 ||
		rule.StagePrecedence != "before-baseline" || !rule.Enabled {
		return fmt.Errorf("overlay identity/revision/precedence/enabled invalid")
	}
	source, sourceErr := netip.ParseAddr(rule.SourceIPv4)
	destination, destinationErr := netip.ParseAddr(rule.DestinationIPv4)
	if sourceErr != nil || destinationErr != nil || !source.Is4() || !destination.Is4() {
		return fmt.Errorf("overlay requires exact IPv4 source/destination")
	}
	if rule.Protocol != 6 && rule.Protocol != 17 {
		return fmt.Errorf("overlay protocol must be TCP(6) or UDP(17)")
	}
	if rule.Action != "permit-and-continue" && rule.Action != "drop" {
		return fmt.Errorf("overlay action unsupported")
	}
	expires, err := time.Parse(time.RFC3339, rule.ExpiresAt)
	if err != nil || expires.UnixMilli() != expiresAtUnixMS || expiresAtUnixMS <= now.UnixMilli() ||
		expiresAtUnixMS-now.UnixMilli() > int64(24*time.Hour/time.Millisecond) {
		return fmt.Errorf("overlay expiry must be exact RFC3339, future, and <=24h")
	}
	if rule.ActorRef == "" || len(rule.ActorRef) > 128 || rule.ReasonCode == "" || len(rule.ReasonCode) > 64 {
		return fmt.Errorf("overlay actor/reason required and bounded")
	}
	if !security.ValidDigest(rule.CanonicalRuleDigest) {
		return fmt.Errorf("overlay canonical digest malformed")
	}
	if _, err := hex.DecodeString(strings.TrimPrefix(rule.CanonicalRuleDigest, "sha256:")); err != nil ||
		rule.CanonicalRuleDigest != ComputeOverlayRuleDigest(rule) {
		return fmt.Errorf("overlay canonical digest mismatch")
	}
	return nil
}

func ComputeOverlayRuleDigest(rule OverlayRule) string {
	h := sha256.New()
	fmt.Fprintf(h, "%s|%d|%s|%s|%s|%d|%d|%d|%s|%t|%s",
		rule.RuleID, rule.RuleRevision, rule.StagePrecedence, rule.SourceIPv4,
		rule.DestinationIPv4, rule.Protocol, rule.SourcePort, rule.DestinationPort,
		rule.Action, rule.Enabled, rule.ExpiresAt)
	return "sha256:" + hex.EncodeToString(h.Sum(nil))
}
