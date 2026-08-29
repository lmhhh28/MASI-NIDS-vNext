package governance

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

// IntentService is the only constructor for ordinary claimable effect intents.
// It consumes a short-lived read-only preflight token but revalidates every
// authorization/target/assignment/capability fence in the insertion transaction.
type IntentService struct {
	pool *db.Pool
	now  func() time.Time
}

func NewIntentService(pool *db.Pool) *IntentService { return &IntentService{pool: pool, now: time.Now} }

func (s *IntentService) Create(ctx context.Context, proposalID, decisionID string, token PreflightToken, intent Intent) (*Intent, error) {
	if proposalID == "" || decisionID == "" || intent.EffectIntentID == "" || intent.OperationID == "" || intent.TargetID == "" ||
		intent.IsFleetParent || !token.Valid(s.now()) {
		return nil, errors.New("governance: malformed/expired intent or preflight token")
	}
	if intent.RequiredWriteAtomicity != "CONTINUE_ON_ERROR" && intent.RequiredWriteAtomicity != "DATAPLANE_ATOMIC" {
		return nil, errors.New("governance: invalid write atomicity")
	}
	if err := ValidateFence(intent.Fence); err != nil {
		return nil, err
	}
	if !validSHA256(intent.AuthorizationDigest) {
		return nil, errors.New("governance: malformed authorization digest")
	}
	if intent.DeadlineUnixMS <= s.now().UnixMilli() {
		return nil, errors.New("governance: intent deadline expired")
	}
	if err := intent.Actor.Validate(); err != nil {
		return nil, err
	}
	intent.ProposalID = proposalID
	intent.DecisionID = decisionID
	intent.ClaimState = ClaimUnclaimed
	providedEffectDigest := intent.EffectDigest
	fenceJSON, err := json.Marshal(intent.Fence)
	if err != nil {
		return nil, err
	}
	err = s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		var proposalDigest, targetSetDigest, policyDigest, effectKind, risk, decisionDigest, decision, actorIssuer, actorSubject, evidenceJSON, proposalScope string
		var targetIDs []string
		var proposalExpiry, decisionExpiry int64
		if err := tx.QueryRow(ctx, `SELECT p.proposal_digest,p.target_set_digest,p.policy_digest,p.effect_kind,p.risk_level,
			 p.target_ids,p.expires_at_unix_ms,d.decision_digest,d.decision,d.expires_at_unix_ms,d.actor_issuer,d.actor_subject,p.evidence_refs::text,p.scope
			 FROM effect_proposals p JOIN effect_decisions d ON d.proposal_id=p.proposal_id
			 WHERE p.proposal_id=$1 AND p.superseded_by_proposal_id IS NULL AND d.decision_id=$2 FOR UPDATE`, proposalID, decisionID).Scan(
			&proposalDigest, &targetSetDigest, &policyDigest, &effectKind, &risk, &targetIDs, &proposalExpiry,
			&decisionDigest, &decision, &decisionExpiry, &actorIssuer, &actorSubject, &evidenceJSON, &proposalScope); err != nil {
			return err
		}
		nowMS := s.now().UnixMilli()
		if decision != "approve" || proposalExpiry <= nowMS || decisionExpiry <= nowMS || intent.DeadlineUnixMS > proposalExpiry || intent.DeadlineUnixMS > decisionExpiry {
			return errors.New("governance: authorization/deadline stale")
		}
		evidenceSum := sha256.Sum256([]byte(evidenceJSON))
		evidenceDigest := "sha256:" + hex.EncodeToString(evidenceSum[:])
		if proposalDigest != token.ProposalDigest || targetSetDigest != token.TargetSetDigest || policyDigest != token.PolicyDigest || token.EvidenceDigest != evidenceDigest ||
			intent.TargetID != token.TargetID || intent.Fence != token.Fence || string(intent.EffectKind) != effectKind || string(intent.RiskLevel) != risk ||
			intent.AuthorizationDigest != decisionDigest || intent.Actor.Issuer != actorIssuer || intent.Actor.Subject != actorSubject ||
			intent.TraceID != token.TraceID {
			return errors.New("governance: intent/preflight/decision fence mismatch")
		}
		found := false
		for _, id := range targetIDs {
			if id == intent.TargetID {
				found = true
				break
			}
		}
		if !found {
			return errors.New("governance: intent target outside frozen set")
		}
		payload, err := loadEffectPayloadTx(ctx, tx, EffectKind(effectKind), policyDigest, intent.TargetID,
			proposalScope, intent.EffectIntentID, intent.OperationID)
		if err != nil {
			return err
		}
		intent.Payload = payload
		intent.EffectDigest = ComputeEffectDigest(intent)
		if providedEffectDigest != "" && providedEffectDigest != intent.EffectDigest {
			return errors.New("governance: caller effect digest does not bind canonical payload")
		}
		payloadJSON, err := json.Marshal(payload)
		if err != nil {
			return err
		}
		var current bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM targets t JOIN target_assignments a ON a.target_id=t.target_id
			 JOIN target_capability_observations c ON c.target_id=t.target_id
			 WHERE t.target_id=$1 AND t.status='active' AND a.assignment_generation=$2
			 AND a.assignment_generation=(SELECT max(x.assignment_generation) FROM target_assignments x WHERE x.target_id=$1)
			 AND a.incarnation_id=$3 AND a.application_generation=$4 AND a.revoked_at_unix_ms IS NULL AND a.expires_at_unix_ms>$5
			 AND c.assignment_generation=$2 AND c.target_control_incarnation_id=$3 AND c.application_generation=$4
			 AND a.actor_runtime_epoch=$6 AND c.actor_runtime_epoch=$6
			 AND c.p4info_digest=$7 AND c.pipeline_digest=$8 AND c.capacity_digest=$9
			 AND c.capacity_available AND c.expires_at_unix_ms>$5)`, intent.TargetID, intent.Fence.TargetAssignmentGeneration,
			intent.Fence.TargetControlIncarnationID, intent.Fence.ApplicationGeneration, nowMS,
			intent.Fence.ActorRuntimeEpoch, intent.Fence.P4InfoDigest, intent.Fence.PipelineDigest,
			intent.Fence.CapacityDigest).Scan(&current); err != nil {
			return err
		}
		if !current {
			return errors.New("governance: target/assignment/capability drifted")
		}
		gateOpen := EffectKind(effectKind) != KindFirewallBaselineActivate && EffectKind(effectKind) != KindFirewallRollback
		tag, err := tx.Exec(ctx, `INSERT INTO effect_intents(effect_intent_id,operation_id,proposal_id,proposal_digest,decision_id,target_id,
				 is_fleet_parent,fence,effect_digest,authorization_digest,effect_kind,risk_level,required_write_atomicity,
				 deadline_unix_ms,claim_state,actor_ref,trace_id,reason_code,actor_issuer,actor_subject,gate_open,effect_payload)
					 VALUES($1,$2,$3,$4,$5,$6,false,$7,$8,$9,$10,$11,$12,$13,'unclaimed',$14,$15,'INTENT_CREATED',$16,$17,$18,$19)
				 ON CONFLICT DO NOTHING`, intent.EffectIntentID, intent.OperationID, proposalID, proposalDigest, decisionID, intent.TargetID, fenceJSON,
			intent.EffectDigest, intent.AuthorizationDigest, string(intent.EffectKind), string(intent.RiskLevel), intent.RequiredWriteAtomicity,
			intent.DeadlineUnixMS, intent.Actor.String(), intent.TraceID, intent.Actor.Issuer, intent.Actor.Subject, gateOpen, payloadJSON)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			var existingID, existingDigest string
			if err := tx.QueryRow(ctx, `SELECT effect_intent_id,effect_digest FROM effect_intents WHERE operation_id=$1`, intent.OperationID).Scan(&existingID, &existingDigest); err != nil {
				return err
			}
			if existingDigest != intent.EffectDigest {
				return errors.New("governance: operation digest conflict")
			}
			intent.EffectIntentID = existingID
		}
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("governance: create intent: %w", err)
	}
	return &intent, nil
}

func loadEffectPayloadTx(ctx context.Context, tx *db.Tx, kind EffectKind, policyDigest, targetID, scope, intentID, operationID string) (EffectPayload, error) {
	payload := EffectPayload{
		SchemaVersion: "p4-effect-payload/v1", PolicyRevisionDigest: policyDigest,
		BaselineRules: json.RawMessage(`[]`), OverlayRules: json.RawMessage(`[]`),
	}
	switch kind {
	case KindFirewallBaselineActivate, KindFirewallRollback:
		var rules []byte
		if err := tx.QueryRow(ctx, `SELECT default_action,rules FROM firewall_revisions
			WHERE revision_digest=$1 AND target_id=$2 AND scope=$3 FOR SHARE`, policyDigest, targetID, scope).
			Scan(&payload.DefaultAction, &rules); err != nil {
			return EffectPayload{}, fmt.Errorf("governance: exact firewall revision unavailable: %w", err)
		}
		payload.Operation = "baseline-activate"
		payload.BaselineRules = append(json.RawMessage(nil), rules...)
	case KindFirewallOverlay:
		var rule []byte
		var expiresAt int64
		var deleteIntentID *string
		if err := tx.QueryRow(ctx, `SELECT rule,expires_at_unix_ms,delete_intent_id FROM firewall_overlays
			WHERE target_id=$1 AND scope=$2 AND rule->>'canonical_rule_digest'=$3 AND NOT deleted
			ORDER BY created_at DESC LIMIT 1 FOR SHARE`, targetID, scope, policyDigest).
			Scan(&rule, &expiresAt, &deleteIntentID); err != nil {
			return EffectPayload{}, fmt.Errorf("governance: exact overlay rule unavailable: %w", err)
		}
		payload.Operation = "overlay-upsert"
		if deleteIntentID != nil && *deleteIntentID == intentID {
			payload.Operation = "overlay-delete"
		}
		var overlayBody map[string]any
		if err := json.Unmarshal(rule, &overlayBody); err != nil {
			return EffectPayload{}, err
		}
		overlayBody["expires_at_unix_ms"] = expiresAt
		edgeRule, err := json.Marshal(overlayBody)
		if err != nil {
			return EffectPayload{}, err
		}
		payload.OverlayRules = append(json.RawMessage(`[`), edgeRule...)
		payload.OverlayRules = append(payload.OverlayRules, ']')
	case KindBoundedCapture:
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,0))`, "bounded-capture:"+targetID); err != nil {
			return EffectPayload{}, err
		}
		var raw []byte
		var state string
		var existingIntent, existingOperation *string
		if err := tx.QueryRow(ctx, `SELECT spec,state,effect_intent_id,operation_id FROM bounded_capture_requests
			WHERE capture_digest=$1 AND target_id=$2 AND scope=$3 AND expires_at_unix_ms>$4 FOR UPDATE`,
			policyDigest, targetID, scope, time.Now().UnixMilli()).Scan(&raw, &state, &existingIntent, &existingOperation); err != nil {
			return EffectPayload{}, fmt.Errorf("governance: exact bounded capture request unavailable: %w", err)
		}
		var spec BoundedCaptureSpec
		if err := json.Unmarshal(raw, &spec); err != nil || normalizeCaptureSpec(&spec) != nil ||
			spec.CaptureDigest != policyDigest || ComputeCaptureDigest(spec) != policyDigest {
			return EffectPayload{}, errors.New("governance: persisted bounded capture envelope invalid")
		}
		if state != "planned" && (state != "authorized" || existingIntent == nil || existingOperation == nil ||
			*existingIntent != intentID || *existingOperation != operationID) {
			return EffectPayload{}, errors.New("governance: bounded capture request already consumed")
		}
		var active int
		if err := tx.QueryRow(ctx, `SELECT count(*) FROM bounded_capture_requests
			WHERE target_id=$1 AND state IN ('authorized','claimed','executing','unknown')
			  AND expires_at_unix_ms>$2 AND capture_id<>$3`, targetID, time.Now().UnixMilli(), spec.CaptureID).Scan(&active); err != nil {
			return EffectPayload{}, err
		}
		if active >= spec.MaxConcurrentOnTarget {
			return EffectPayload{}, errors.New("governance: bounded capture concurrency limit reached")
		}
		if _, err := tx.Exec(ctx, `UPDATE bounded_capture_requests SET state='authorized',effect_intent_id=$1,
			operation_id=$2,reason_code='CAPTURE_AUTHORIZED' WHERE capture_id=$3`, intentID, operationID, spec.CaptureID); err != nil {
			return EffectPayload{}, err
		}
		payload.Operation = "capture-start"
		payload.BoundedCapture = &spec
	default:
		return EffectPayload{}, fmt.Errorf("governance: effect kind %s is not a P4 effect intent", kind)
	}
	if err := ValidateEffectPayload(payload); err != nil {
		return EffectPayload{}, err
	}
	return payload, nil
}

// LoadCanonicalEffectPayloadTx exposes the Go-owned policy-to-effect adapter to
// the atomic fleet coordinator. Callers must already be inside the Decision +
// full-child-vector transaction; browser-supplied payloads are never trusted.
func LoadCanonicalEffectPayloadTx(ctx context.Context, tx *db.Tx, kind EffectKind,
	policyDigest, targetID, scope, intentID, operationID string) (EffectPayload, error) {
	return loadEffectPayloadTx(ctx, tx, kind, policyDigest, targetID, scope, intentID, operationID)
}

func ValidateEffectPayload(payload EffectPayload) error {
	if payload.SchemaVersion != "p4-effect-payload/v1" || !validSHA256(payload.PolicyRevisionDigest) {
		return errors.New("governance: effect payload schema/policy digest malformed")
	}
	if payload.Operation != "baseline-activate" && payload.Operation != "overlay-upsert" && payload.Operation != "overlay-delete" && payload.Operation != "capture-start" {
		return errors.New("governance: effect payload operation unsupported")
	}
	if !json.Valid(payload.BaselineRules) || !json.Valid(payload.OverlayRules) ||
		len(payload.BaselineRules)+len(payload.OverlayRules) > 4*1024*1024 {
		return errors.New("governance: effect payload rules malformed or oversized")
	}
	var baseline, overlays []json.RawMessage
	if err := json.Unmarshal(payload.BaselineRules, &baseline); err != nil || len(baseline) > 4096 {
		return errors.New("governance: baseline effect rule vector invalid")
	}
	if err := json.Unmarshal(payload.OverlayRules, &overlays); err != nil || len(overlays) > 1024 {
		return errors.New("governance: overlay effect rule vector invalid")
	}
	if payload.Operation == "capture-start" {
		if len(baseline) != 0 || len(overlays) != 0 || payload.DefaultAction != "" || payload.BoundedCapture == nil {
			return errors.New("governance: capture payload cannot contain firewall fields")
		}
		spec := *payload.BoundedCapture
		if err := normalizeCaptureSpec(&spec); err != nil || spec.CaptureDigest != payload.PolicyRevisionDigest ||
			ComputeCaptureDigest(spec) != payload.PolicyRevisionDigest {
			return errors.New("governance: bounded capture payload digest invalid")
		}
	} else if payload.BoundedCapture != nil {
		return errors.New("governance: firewall payload cannot contain bounded capture")
	} else if payload.Operation == "baseline-activate" {
		if payload.DefaultAction != "permit-and-continue" && payload.DefaultAction != "drop" || len(overlays) != 0 {
			return errors.New("governance: baseline payload default/overlay conflict")
		}
	} else if len(baseline) != 0 || len(overlays) != 1 {
		return errors.New("governance: overlay payload must carry exactly one rule")
	}
	return nil
}

func ComputeEffectDigest(intent Intent) string {
	edgeIntent, err := ToEdgeEffectIntent(intent)
	if err != nil {
		return ""
	}
	return edgeIntent.EffectDigest
}

func ValidateFence(f Fence) error {
	if f.TargetControlIncarnationID == "" || f.TargetAssignmentGeneration < 1 || f.ApplicationGeneration < 1 ||
		f.EdgeWorkloadRef == "" || len(f.EdgeWorkloadRef) > 128 || f.ActorRuntimeEpoch == "" || len(f.ActorRuntimeEpoch) > 128 || f.ElectionIDLow < 1 ||
		!validSHA256(f.P4InfoDigest) || !validSHA256(f.PipelineDigest) || !validSHA256(f.CapacityDigest) {
		return errors.New("governance: malformed effect fence")
	}
	return nil
}

func validSHA256(s string) bool {
	return security.ValidDigest(s)
}
