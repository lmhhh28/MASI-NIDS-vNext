package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/a2a"
	"masi-nids/control-go/internal/firewall"
	"masi-nids/control-go/internal/governance"
	"masi-nids/control-go/internal/model"
	"masi-nids/control-go/internal/plugin"
	"masi-nids/control-go/internal/pluginstat"
	"masi-nids/control-go/internal/security"
	"masi-nids/control-go/internal/target"
)

type adminMutation struct {
	Actor     security.Actor
	Session   *Session
	Scope     string
	TargetSet string
	Level     security.AuthzContextLevel
}

// authorizeAdminMutation is the common fail-closed gate for platform-control
// writes. The route middleware has already proved session, Origin, and CSRF;
// this additionally binds the exact scope/action/target set and fresh phishing-
// resistant step-up.
func authorizeAdminMutation(r *http.Request, deps Deps, scope, action, targetSet string) (adminMutation, error) {
	s, err := sessionFromContext(r.Context())
	if err != nil || deps.Mapping == nil || scope == "" || !validDigest(targetSet) {
		return adminMutation{}, errors.New("admin mutation identity unavailable")
	}
	actor := security.Actor{Issuer: s.Actor.Issuer, Subject: s.Actor.Subject}
	if err := actor.Validate(); err != nil {
		return adminMutation{}, err
	}
	stepUp := security.StepUpType(s.StepUpType)
	age := timeSinceMS(s.LastStepUp)
	if !stepUp.IsPhishingResistant() || age < 0 || age > 300_000 {
		return adminMutation{}, errors.New("fresh phishing-resistant step-up required")
	}
	if _, err := deps.Mapping.AuthorizeScope(actor, security.LevelPlatformAdmin, scope, action, targetSet); err != nil {
		return adminMutation{}, err
	}
	return adminMutation{Actor: actor, Session: s, Scope: scope, TargetSet: targetSet, Level: security.LevelPlatformAdmin}, nil
}

func authorizeOperatorMutation(r *http.Request, deps Deps, scope, action, targetSet string, risk security.RiskLevel) (adminMutation, error) {
	s, err := sessionFromContext(r.Context())
	if err != nil || deps.Mapping == nil || scope == "" || !validDigest(targetSet) ||
		(risk != security.R0 && risk != security.R1 && risk != security.R2 && risk != security.R3) {
		return adminMutation{}, errors.New("operator mutation identity unavailable")
	}
	actor := security.Actor{Issuer: s.Actor.Issuer, Subject: s.Actor.Subject}
	if err := actor.Validate(); err != nil {
		return adminMutation{}, err
	}
	if risk == security.R2 || risk == security.R3 {
		stepUp := security.StepUpType(s.StepUpType)
		age := timeSinceMS(s.LastStepUp)
		if !stepUp.IsPhishingResistant() || age < 0 || age > 300_000 {
			return adminMutation{}, errors.New("R2 requires fresh phishing-resistant step-up")
		}
	}
	for _, level := range []security.AuthzContextLevel{security.LevelOperator, security.LevelScopedOperator} {
		if _, err := deps.Mapping.AuthorizeScope(actor, level, scope, action, targetSet); err == nil {
			return adminMutation{Actor: actor, Session: s, Scope: scope, TargetSet: targetSet, Level: level}, nil
		}
	}
	return adminMutation{}, errors.New("operator mutation denied")
}

func handleRegisterBoundedCapture(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			TargetID        string                        `json:"target_id"`
			Scope           string                        `json:"scope"`
			TargetSetDigest string                        `json:"target_set_digest"`
			Spec            governance.BoundedCaptureSpec `json:"spec"`
			TraceID         string                        `json:"trace_id"`
			IdempotencyKey  string                        `json:"idempotency_key"`
		}
		if decodeStrictJSON(w, r, 32*1024, &body) != nil || deps.Capture == nil ||
			body.TargetID == "" || body.IdempotencyKey == "" {
			WriteError(w, http.StatusBadRequest, "BOUNDED_CAPTURE_MALFORMED")
			return
		}
		if targetSetDigest([]string{body.TargetID}) != body.TargetSetDigest {
			WriteError(w, http.StatusConflict, "BOUNDED_CAPTURE_TARGET_SET_MISMATCH")
			return
		}
		operator, err := authorizeOperatorMutation(r, deps, body.Scope, string(governance.KindBoundedCapture),
			body.TargetSetDigest, security.R0)
		if err != nil {
			WriteError(w, http.StatusForbidden, "BOUNDED_CAPTURE_DENIED")
			return
		}
		out, err := deps.Capture.Register(r.Context(), body.Spec, body.TargetID, body.Scope, operator.Actor, body.TraceID)
		if err != nil {
			WriteError(w, http.StatusConflict, "BOUNDED_CAPTURE_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, map[string]any{"capture": out})
	}
}

func authorizeAnalysisMutation(r *http.Request, deps Deps, scope, targetSetDigest string) (security.Actor, error) {
	session, err := sessionFromContext(r.Context())
	if err != nil || deps.Mapping == nil || scope == "" || !validDigest(targetSetDigest) {
		return security.Actor{}, errors.New("analysis identity unavailable")
	}
	actor := security.Actor{Issuer: session.Actor.Issuer, Subject: session.Actor.Subject}
	if err := actor.Validate(); err != nil {
		return security.Actor{}, err
	}
	for _, level := range []security.AuthzContextLevel{security.LevelAnalyst, security.LevelOperator, security.LevelScopedOperator} {
		if _, err := deps.Mapping.AuthorizeScope(actor, level, scope, "analysis-task", targetSetDigest); err == nil {
			return actor, nil
		}
	}
	return security.Actor{}, errors.New("analysis scope denied")
}

func handleSubmitAnalysisTask(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Input          a2a.InputBundle `json:"input"`
			IdempotencyKey string          `json:"idempotency_key"`
		}
		if decodeStrictJSON(w, r, 64*1024, &body) != nil || deps.Analysis == nil || body.IdempotencyKey == "" ||
			(body.Input.IdempotencyKey != "" && body.Input.IdempotencyKey != body.IdempotencyKey) {
			WriteError(w, http.StatusBadRequest, "ANALYSIS_TASK_MALFORMED")
			return
		}
		body.Input.IdempotencyKey = body.IdempotencyKey
		actor, err := authorizeAnalysisMutation(r, deps, body.Input.Scope, body.Input.TargetSetDigest)
		if err != nil {
			WriteError(w, http.StatusForbidden, "ANALYSIS_TASK_DENIED")
			return
		}
		out, err := deps.Analysis.Submit(r.Context(), body.Input, actor)
		if err != nil {
			if out != nil && out.Status == "unknown" {
				writeJSON(w, http.StatusAccepted, out)
				return
			}
			WriteError(w, http.StatusConflict, "ANALYSIS_TASK_REJECTED")
			return
		}
		writeJSON(w, http.StatusAccepted, out)
	}
}

func handlePollAnalysisTask(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Scope           string `json:"scope"`
			TargetSetDigest string `json:"target_set_digest"`
			IdempotencyKey  string `json:"idempotency_key"`
		}
		taskID := chi.URLParam(r, "taskID")
		if decodeStrictJSON(w, r, 8*1024, &body) != nil || deps.Analysis == nil || taskID == "" || body.IdempotencyKey == "" {
			WriteError(w, http.StatusBadRequest, "ANALYSIS_POLL_MALFORMED")
			return
		}
		var scope, targetSetDigest string
		if err := deps.Pool.Pool.QueryRow(r.Context(), `SELECT scope,input_bundle->>'target_set_digest'
			FROM analysis_task_requests WHERE task_id=$1`, taskID).Scan(&scope, &targetSetDigest); err != nil {
			WriteError(w, http.StatusNotFound, "ANALYSIS_TASK_NOT_FOUND")
			return
		}
		if body.Scope != scope || body.TargetSetDigest != targetSetDigest {
			WriteError(w, http.StatusConflict, "ANALYSIS_POLL_SCOPE_DRIFT")
			return
		}
		actor, err := authorizeAnalysisMutation(r, deps, scope, targetSetDigest)
		if err != nil {
			WriteError(w, http.StatusForbidden, "ANALYSIS_POLL_DENIED")
			return
		}
		out, err := deps.Analysis.Poll(r.Context(), taskID, actor)
		if err != nil {
			WriteError(w, http.StatusConflict, "ANALYSIS_POLL_REJECTED")
			return
		}
		writeJSON(w, http.StatusOK, out)
	}
}

func handleCreateBoundedCaptureIntent(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			ProposalID      string `json:"proposal_id"`
			DecisionID      string `json:"decision_id"`
			EffectIntentID  string `json:"effect_intent_id"`
			OperationID     string `json:"operation_id"`
			Scope           string `json:"scope"`
			TargetSetDigest string `json:"target_set_digest"`
			DeadlineUnixMS  int64  `json:"deadline_unix_ms"`
			IdempotencyKey  string `json:"idempotency_key"`
		}
		captureID := chi.URLParam(r, "captureID")
		if decodeStrictJSON(w, r, 16*1024, &body) != nil || captureID == "" || body.ProposalID == "" ||
			body.DecisionID == "" || body.EffectIntentID == "" || body.OperationID == "" ||
			body.IdempotencyKey == "" || deps.Intents == nil || deps.Preflight == nil {
			WriteError(w, http.StatusBadRequest, "BOUNDED_CAPTURE_INTENT_MALFORMED")
			return
		}
		var targetID, scope, captureDigest, state, decisionDigest string
		var expiresAt int64
		if err := deps.Pool.Pool.QueryRow(r.Context(), `SELECT target_id,scope,capture_digest,state,expires_at_unix_ms
			FROM bounded_capture_requests WHERE capture_id=$1`, captureID).
			Scan(&targetID, &scope, &captureDigest, &state, &expiresAt); err != nil {
			WriteError(w, http.StatusNotFound, "BOUNDED_CAPTURE_NOT_FOUND")
			return
		}
		if scope != body.Scope || targetSetDigest([]string{targetID}) != body.TargetSetDigest ||
			(state != "planned" && state != "authorized") || body.DeadlineUnixMS <= time.Now().UnixMilli() ||
			body.DeadlineUnixMS > expiresAt {
			WriteError(w, http.StatusConflict, "BOUNDED_CAPTURE_INTENT_DRIFT")
			return
		}
		operator, err := authorizeOperatorMutation(r, deps, scope, string(governance.KindBoundedCapture),
			body.TargetSetDigest, security.R0)
		if err != nil {
			WriteError(w, http.StatusForbidden, "BOUNDED_CAPTURE_INTENT_DENIED")
			return
		}
		if err := deps.Pool.Pool.QueryRow(r.Context(), `SELECT decision_digest FROM effect_decisions
			WHERE decision_id=$1 AND proposal_id=$2 AND decision='approve' AND actor_issuer=$3 AND actor_subject=$4`,
			body.DecisionID, body.ProposalID, operator.Actor.Issuer, operator.Actor.Subject).Scan(&decisionDigest); err != nil {
			WriteError(w, http.StatusConflict, "BOUNDED_CAPTURE_DECISION_STALE")
			return
		}
		token, err := deps.Preflight.Preflight(r.Context(), body.ProposalID)
		if err != nil || token.TargetID != targetID || token.PolicyDigest != captureDigest {
			WriteError(w, http.StatusConflict, "BOUNDED_CAPTURE_PREFLIGHT_HOLD")
			return
		}
		out, err := deps.Intents.Create(r.Context(), body.ProposalID, body.DecisionID, *token, governance.Intent{
			EffectIntentID: body.EffectIntentID, OperationID: body.OperationID, TargetID: targetID,
			Fence: token.Fence, AuthorizationDigest: decisionDigest, EffectKind: governance.KindBoundedCapture,
			RiskLevel: security.R0, RequiredWriteAtomicity: "CONTINUE_ON_ERROR", DeadlineUnixMS: body.DeadlineUnixMS,
			Actor: operator.Actor, TraceID: token.TraceID,
		})
		if err != nil {
			WriteError(w, http.StatusConflict, "BOUNDED_CAPTURE_INTENT_REJECTED")
			return
		}
		writeJSON(w, http.StatusAccepted, out)
	}
}

func handleCreateFleetOperation(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			ProposalID     string                `json:"proposal_id"`
			DecisionReason string                `json:"decision_reason"`
			Fleet          target.FleetOperation `json:"fleet"`
			IdempotencyKey string                `json:"idempotency_key"`
		}
		if decodeStrictJSON(w, r, 4*1024*1024, &body) != nil || deps.FleetCoordinator == nil ||
			deps.Decisions == nil || body.ProposalID == "" || !validReasonCode(body.DecisionReason) ||
			body.IdempotencyKey == "" {
			WriteError(w, http.StatusBadRequest, "FLEET_OPERATION_MALFORMED")
			return
		}
		s, err := sessionFromContext(r.Context())
		if err != nil {
			WriteError(w, http.StatusUnauthorized, "UNAUTHENTICATED")
			return
		}
		actor := security.Actor{Issuer: s.Actor.Issuer, Subject: s.Actor.Subject}
		stepUp := security.StepUpType(s.StepUpType)
		authz := governance.AuthzContext{StepUpType: stepUp, StepUpAgeMS: int(timeSinceMS(s.LastStepUp)),
			PhishingResistant: stepUp.IsPhishingResistant()}
		fleet, decision, err := deps.FleetCoordinator.CreateApprovedFleetOperation(r.Context(), deps.Decisions,
			body.ProposalID, actor, authz, body.DecisionReason, body.Fleet)
		if err != nil {
			WriteError(w, http.StatusConflict, "FLEET_OPERATION_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, map[string]any{"fleet": fleet, "decision": decision})
	}
}

func handleAdvanceFleetWave(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			CurrentWave           int    `json:"current_wave"`
			NextWave              int    `json:"next_wave"`
			Scope                 string `json:"scope"`
			TargetSetDigest       string `json:"target_set_digest"`
			CompletedVectorDigest string `json:"completed_vector_digest,omitempty"`
			GateProposalID        string `json:"gate_proposal_id,omitempty"`
			DecisionReason        string `json:"decision_reason,omitempty"`
			IdempotencyKey        string `json:"idempotency_key"`
		}
		fleetID := chi.URLParam(r, "fleetID")
		if decodeStrictJSON(w, r, 8*1024, &body) != nil || fleetID == "" || body.IdempotencyKey == "" ||
			deps.FleetCoordinator == nil {
			WriteError(w, http.StatusBadRequest, "FLEET_WAVE_MALFORMED")
			return
		}
		var scope, targetDigest, effectKind, risk string
		if err := deps.Pool.Pool.QueryRow(r.Context(), `SELECT fo.scope,fo.target_set_digest,p.effect_kind,p.risk_level
		 FROM fleet_operations fo JOIN effect_intents i ON i.effect_intent_id=fo.parent_intent_id
		 JOIN effect_proposals p ON p.proposal_id=i.proposal_id WHERE fo.fleet_operation_id=$1`, fleetID).
			Scan(&scope, &targetDigest, &effectKind, &risk); err != nil {
			WriteError(w, http.StatusNotFound, "FLEET_OPERATION_NOT_FOUND")
			return
		}
		if scope != body.Scope || targetDigest != body.TargetSetDigest {
			WriteError(w, http.StatusConflict, "FLEET_OPERATION_DRIFT")
			return
		}
		operator, err := authorizeOperatorMutation(r, deps, scope, effectKind, targetDigest, security.RiskLevel(risk))
		if err != nil {
			WriteError(w, http.StatusForbidden, "FLEET_WAVE_DENIED")
			return
		}
		if body.GateProposalID != "" {
			if !validDigest(body.CompletedVectorDigest) || !validReasonCode(body.DecisionReason) || deps.Decisions == nil {
				WriteError(w, http.StatusBadRequest, "FLEET_MANUAL_GATE_MALFORMED")
				return
			}
			stepUp := security.StepUpType(operator.Session.StepUpType)
			authz := governance.AuthzContext{StepUpType: stepUp,
				StepUpAgeMS:       int(timeSinceMS(operator.Session.LastStepUp)),
				PhishingResistant: stepUp.IsPhishingResistant()}
			if _, err := deps.FleetCoordinator.AuthorizeManualGate(r.Context(), deps.Decisions,
				body.GateProposalID, operator.Actor, authz, body.DecisionReason, fleetID,
				body.CurrentWave, body.NextWave, body.CompletedVectorDigest); err != nil {
				WriteError(w, http.StatusConflict, "FLEET_MANUAL_GATE_REJECTED")
				return
			}
		}
		if err := deps.FleetCoordinator.AdvanceWaveGate(r.Context(), fleetID, body.CurrentWave, body.NextWave); err != nil {
			WriteError(w, http.StatusConflict, "FLEET_WAVE_REJECTED")
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"fleet_operation_id": fleetID, "opened_wave": body.NextWave})
	}
}

func handleRollbackFleetOperation(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			ProposalID     string                `json:"proposal_id"`
			DecisionReason string                `json:"decision_reason"`
			Rollback       target.FleetOperation `json:"rollback"`
			IdempotencyKey string                `json:"idempotency_key"`
		}
		originalID := chi.URLParam(r, "fleetID")
		if decodeStrictJSON(w, r, 4*1024*1024, &body) != nil || originalID == "" || body.ProposalID == "" ||
			!validReasonCode(body.DecisionReason) || body.IdempotencyKey == "" ||
			deps.FleetCoordinator == nil || deps.Decisions == nil {
			WriteError(w, http.StatusBadRequest, "FLEET_ROLLBACK_MALFORMED")
			return
		}
		s, err := sessionFromContext(r.Context())
		if err != nil {
			WriteError(w, http.StatusUnauthorized, "UNAUTHENTICATED")
			return
		}
		actor := security.Actor{Issuer: s.Actor.Issuer, Subject: s.Actor.Subject}
		stepUp := security.StepUpType(s.StepUpType)
		authz := governance.AuthzContext{StepUpType: stepUp, StepUpAgeMS: int(timeSinceMS(s.LastStepUp)),
			PhishingResistant: stepUp.IsPhishingResistant()}
		fleet, decision, err := deps.FleetCoordinator.CreateApprovedRollback(r.Context(), deps.Decisions,
			originalID, body.ProposalID, actor, authz, body.DecisionReason, body.Rollback)
		if err != nil {
			WriteError(w, http.StatusConflict, "FLEET_ROLLBACK_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, map[string]any{"fleet": fleet, "decision": decision})
	}
}

func handleRegisterTarget(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			DisplayName          string             `json:"display_name"`
			P4RuntimeEndpoint    string             `json:"p4runtime_endpoint"`
			DeviceID             int64              `json:"device_id"`
			Role                 string             `json:"role"`
			DesiredProfileDigest string             `json:"desired_profile_digest"`
			CredentialRef        string             `json:"credential_ref"`
			Scope                string             `json:"scope"`
			TLS                  target.TLSIdentity `json:"p4runtime_tls"`
			TargetSetDigest      string             `json:"target_set_digest"`
			IdempotencyKey       string             `json:"idempotency_key"`
			TraceID              string             `json:"trace_id"`
		}
		if decodeStrictJSON(w, r, 32*1024, &body) != nil || deps.TargetRegistry == nil {
			WriteError(w, http.StatusBadRequest, "TARGET_REGISTRATION_MALFORMED")
			return
		}
		admin, err := authorizeAdminMutation(r, deps, body.Scope, "target-lifecycle", body.TargetSetDigest)
		if err != nil {
			WriteError(w, http.StatusForbidden, "TARGET_REGISTRATION_DENIED")
			return
		}
		out, err := deps.TargetRegistry.RegisterTarget(r.Context(), target.Target{
			DisplayName: body.DisplayName, P4RuntimeEndpoint: body.P4RuntimeEndpoint,
			DeviceID: body.DeviceID, Role: body.Role, DesiredProfileDigest: body.DesiredProfileDigest,
			CredentialRef: body.CredentialRef, Scope: body.Scope, TLS: body.TLS, Actor: admin.Actor,
			TraceID: body.TraceID, IdempotencyKey: body.IdempotencyKey,
		}, target.LifecycleAuthorization{Scope: body.Scope, PlatformAdmin: true, StepUpFresh: true, CSRFVerified: true})
		if err != nil {
			WriteError(w, http.StatusBadRequest, "TARGET_REGISTRATION_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, out)
	}
}

func handleTargetLifecycle(deps Deps, activate bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Scope           string `json:"scope"`
			TargetSetDigest string `json:"target_set_digest"`
			ReasonCode      string `json:"reason_code"`
			TraceID         string `json:"trace_id"`
			IdempotencyKey  string `json:"idempotency_key"`
		}
		targetID := chi.URLParam(r, "targetID")
		if decodeStrictJSON(w, r, 8*1024, &body) != nil || targetID == "" ||
			!validReasonCode(body.ReasonCode) || body.IdempotencyKey == "" || deps.TargetRegistry == nil {
			WriteError(w, http.StatusBadRequest, "TARGET_LIFECYCLE_MALFORMED")
			return
		}
		admin, err := authorizeAdminMutation(r, deps, body.Scope, "target-lifecycle", body.TargetSetDigest)
		if err != nil {
			WriteError(w, http.StatusForbidden, "TARGET_LIFECYCLE_DENIED")
			return
		}
		auth := target.LifecycleAuthorization{Scope: body.Scope, PlatformAdmin: true, StepUpFresh: true, CSRFVerified: true}
		if activate {
			err = deps.TargetRegistry.Activate(r.Context(), targetID, admin.Actor, auth, body.ReasonCode, body.TraceID)
		} else {
			var generation int64
			var revoked *int64
			assignmentErr := deps.Pool.QueryRow(r.Context(), `SELECT assignment_generation,revoked_at_unix_ms
					FROM target_assignments WHERE target_id=$1 ORDER BY assignment_generation DESC LIMIT 1`, targetID).
				Scan(&generation, &revoked)
			if assignmentErr == nil && revoked == nil {
				if err = deps.Assignment.Revoke(r.Context(), targetID, generation, time.Now().UnixMilli()); err != nil {
					WriteError(w, http.StatusConflict, "TARGET_ASSIGNMENT_REVOKE_REJECTED")
					return
				}
			} else if assignmentErr != nil && !errors.Is(assignmentErr, pgx.ErrNoRows) {
				WriteError(w, http.StatusInternalServerError, "TARGET_ASSIGNMENT_QUERY_FAILED")
				return
			}
			err = deps.TargetRegistry.Retire(r.Context(), targetID, admin.Actor, auth, body.ReasonCode, body.TraceID)
		}
		if err != nil {
			WriteError(w, http.StatusConflict, "TARGET_LIFECYCLE_REJECTED")
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"target_id": targetID,
			"status":       map[bool]string{true: "active", false: "retired"}[activate],
			"operation_id": lifecycleOperationID(r, deps, targetID, body.TraceID)})
	}
}

func handleCreateFirewallRevision(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Revision        firewall.Revision `json:"revision"`
			TargetSetDigest string            `json:"target_set_digest"`
			TraceID         string            `json:"trace_id"`
			IdempotencyKey  string            `json:"idempotency_key"`
		}
		if decodeStrictJSON(w, r, 2*1024*1024, &body) != nil || deps.FirewallRevisions == nil || body.IdempotencyKey == "" {
			WriteError(w, http.StatusBadRequest, "FIREWALL_REVISION_MALFORMED")
			return
		}
		admin, err := authorizeAdminMutation(r, deps, body.Revision.Scope, "firewall-baseline-activate", body.TargetSetDigest)
		if err != nil {
			WriteError(w, http.StatusForbidden, "FIREWALL_REVISION_DENIED")
			return
		}
		body.Revision.ActorRef = admin.Actor.String()
		out, err := deps.FirewallRevisions.Create(r.Context(), body.Revision)
		if err != nil {
			WriteError(w, http.StatusBadRequest, "FIREWALL_REVISION_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, out)
	}
}

func handlePrepareFirewallActivation(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			TargetID        string `json:"target_id"`
			RevisionID      string `json:"revision_id"`
			OperationID     string `json:"operation_id"`
			Scope           string `json:"scope"`
			TargetSetDigest string `json:"target_set_digest"`
			IdempotencyKey  string `json:"idempotency_key"`
		}
		if decodeStrictJSON(w, r, 8*1024, &body) != nil || deps.FirewallActivation == nil || body.IdempotencyKey == "" {
			WriteError(w, http.StatusBadRequest, "FIREWALL_ACTIVATION_MALFORMED")
			return
		}
		if _, err := authorizeAdminMutation(r, deps, body.Scope, "firewall-baseline-activate", body.TargetSetDigest); err != nil {
			WriteError(w, http.StatusForbidden, "FIREWALL_ACTIVATION_DENIED")
			return
		}
		out, err := deps.FirewallActivation.Activate(r.Context(), body.TargetID, body.RevisionID, body.OperationID)
		if err != nil {
			WriteError(w, http.StatusConflict, "FIREWALL_ACTIVATION_REJECTED")
			return
		}
		writeJSON(w, http.StatusAccepted, out)
	}
}

func handleCreateFirewallOverlay(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Overlay         firewall.Overlay `json:"overlay"`
			TargetSetDigest string           `json:"target_set_digest"`
			IdempotencyKey  string           `json:"idempotency_key"`
		}
		if decodeStrictJSON(w, r, 128*1024, &body) != nil || deps.FirewallOverlays == nil ||
			body.IdempotencyKey == "" || body.Overlay.UpsertIntent.RiskLevel != body.Overlay.DeleteIntent.RiskLevel {
			WriteError(w, http.StatusBadRequest, "FIREWALL_OVERLAY_MALFORMED")
			return
		}
		operator, err := authorizeOperatorMutation(r, deps, body.Overlay.Scope, "firewall-overlay",
			body.TargetSetDigest, body.Overlay.UpsertIntent.RiskLevel)
		if err != nil {
			WriteError(w, http.StatusForbidden, "FIREWALL_OVERLAY_DENIED")
			return
		}
		body.Overlay.Actor = operator.Actor
		body.Overlay.Rule.ActorRef = operator.Actor.String()
		if err := deps.FirewallOverlays.Create(r.Context(), body.Overlay); err != nil {
			WriteError(w, http.StatusConflict, "FIREWALL_OVERLAY_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, map[string]any{"overlay_rule_id": body.Overlay.OverlayRuleID,
			"upsert_operation_id": body.Overlay.UpsertIntent.OperationID,
			"delete_operation_id": body.Overlay.DeleteIntent.OperationID, "status": "pending"})
	}
}

func handleRegisterModelRevision(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Revision        model.ModelRevision `json:"revision"`
			TargetSetDigest string              `json:"target_set_digest"`
			IdempotencyKey  string              `json:"idempotency_key"`
		}
		if decodeStrictJSON(w, r, 32*1024, &body) != nil || deps.ModelRevisions == nil || body.IdempotencyKey == "" {
			WriteError(w, http.StatusBadRequest, "MODEL_REVISION_MALFORMED")
			return
		}
		admin, err := authorizeAdminMutation(r, deps, body.Revision.Scope, "model-rollout", body.TargetSetDigest)
		if err != nil {
			WriteError(w, http.StatusForbidden, "MODEL_REVISION_DENIED")
			return
		}
		body.Revision.Actor = admin.Actor
		out, err := deps.ModelRevisions.RegisterRevision(r.Context(), body.Revision)
		if err != nil {
			WriteError(w, http.StatusBadRequest, "MODEL_REVISION_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, out)
	}
}

func handleRevokeModelRevision(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Scope           string `json:"scope"`
			TargetSetDigest string `json:"target_set_digest"`
			ReasonCode      string `json:"reason_code"`
			TraceID         string `json:"trace_id"`
			IdempotencyKey  string `json:"idempotency_key"`
		}
		id := chi.URLParam(r, "revisionID")
		if decodeStrictJSON(w, r, 8*1024, &body) != nil || id == "" || !validReasonCode(body.ReasonCode) ||
			body.IdempotencyKey == "" || deps.ModelRevisions == nil {
			WriteError(w, http.StatusBadRequest, "MODEL_REVOKE_MALFORMED")
			return
		}
		admin, err := authorizeAdminMutation(r, deps, body.Scope, "model-rollout", body.TargetSetDigest)
		if err != nil {
			WriteError(w, http.StatusForbidden, "MODEL_REVOKE_DENIED")
			return
		}
		if err := deps.ModelRevisions.Revoke(r.Context(), id, admin.Actor, body.ReasonCode, body.TraceID); err != nil {
			WriteError(w, http.StatusConflict, "MODEL_REVOKE_REJECTED")
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"model_revision_id": id, "qualification_status": "revoked"})
	}
}

type rolloutGroupBody struct {
	GroupID                   string   `json:"group_id"`
	OrderedShards             []string `json:"ordered_shards"`
	LogicalPoolID             string   `json:"logical_pool_id"`
	TargetGeneration          int64    `json:"target_generation"`
	TargetRevisionID          string   `json:"target_revision_id"`
	Scope                     string   `json:"scope"`
	TargetSetDigest           string   `json:"target_set_digest"`
	ReasonCode                string   `json:"reason_code"`
	TraceID                   string   `json:"trace_id"`
	IdempotencyKey            string   `json:"idempotency_key"`
	WireProfile               string   `json:"wire_profile"`
	WireProfileDigest         string   `json:"wire_profile_digest"`
	RuntimeProfileDigest      string   `json:"runtime_profile_digest"`
	OptimizationProfileDigest string   `json:"optimization_profile_digest"`
	AvailabilityProfile       string   `json:"availability_profile"`
	MinReadyReplicas          int      `json:"min_ready_replicas"`
	ModelControlIncarnationID string   `json:"model_control_incarnation_id"`
}

func handleCreateModelRolloutGroup(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body rolloutGroupBody
		if decodeStrictJSON(w, r, 64*1024, &body) != nil || deps.ModelRollout == nil || body.IdempotencyKey == "" ||
			targetSetDigest(body.OrderedShards) != body.TargetSetDigest || !validReasonCode(body.ReasonCode) {
			WriteError(w, http.StatusBadRequest, "MODEL_ROLLOUT_GROUP_MALFORMED")
			return
		}
		admin, err := authorizeAdminMutation(r, deps, body.Scope, "model-rollout", body.TargetSetDigest)
		if err != nil {
			WriteError(w, http.StatusForbidden, "MODEL_ROLLOUT_GROUP_DENIED")
			return
		}
		template := model.RolloutRequest{LogicalPoolID: body.LogicalPoolID, TargetGeneration: body.TargetGeneration,
			TargetRevisionID: body.TargetRevisionID, Kind: model.OpRollout, ActorRef: admin.Actor.String(),
			ReasonCode: body.ReasonCode, TraceID: body.TraceID, Scope: body.Scope,
			WireProfile: body.WireProfile, WireProfileDigest: body.WireProfileDigest,
			RuntimeProfileDigest: body.RuntimeProfileDigest, OptimizationProfileDigest: body.OptimizationProfileDigest,
			AvailabilityProfile: body.AvailabilityProfile, MinReadyReplicas: body.MinReadyReplicas,
			ModelControlIncarnationID: body.ModelControlIncarnationID}
		out, err := deps.ModelRollout.CreateRolloutGroup(r.Context(), model.RolloutGroupRequest{
			GroupID: body.GroupID, OrderedShards: body.OrderedShards, Template: template})
		if err != nil {
			WriteError(w, http.StatusConflict, "MODEL_ROLLOUT_GROUP_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, out)
	}
}

func handleAdvanceModelRolloutGroup(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Scope           string `json:"scope"`
			TargetSetDigest string `json:"target_set_digest"`
			IdempotencyKey  string `json:"idempotency_key"`
		}
		groupID := chi.URLParam(r, "groupID")
		if decodeStrictJSON(w, r, 8*1024, &body) != nil || groupID == "" || body.IdempotencyKey == "" || deps.ModelRollout == nil {
			WriteError(w, http.StatusBadRequest, "MODEL_ROLLOUT_ADVANCE_MALFORMED")
			return
		}
		var persistedScope string
		var shardsJSON []byte
		if err := deps.Pool.Pool.QueryRow(r.Context(), `SELECT scope,ordered_shards FROM model_rollout_groups WHERE group_id=$1`, groupID).Scan(&persistedScope, &shardsJSON); err != nil {
			WriteError(w, http.StatusNotFound, "MODEL_ROLLOUT_GROUP_NOT_FOUND")
			return
		}
		var shards []string
		if json.Unmarshal(shardsJSON, &shards) != nil || persistedScope != body.Scope || targetSetDigest(shards) != body.TargetSetDigest {
			WriteError(w, http.StatusConflict, "MODEL_ROLLOUT_GROUP_DRIFT")
			return
		}
		if _, err := authorizeAdminMutation(r, deps, body.Scope, "model-rollout", body.TargetSetDigest); err != nil {
			WriteError(w, http.StatusForbidden, "MODEL_ROLLOUT_ADVANCE_DENIED")
			return
		}
		out, err := deps.ModelRollout.AdvanceRolloutGroup(r.Context(), groupID)
		if err != nil && out == nil {
			WriteError(w, http.StatusConflict, "MODEL_ROLLOUT_ADVANCE_REJECTED")
			return
		}
		writeJSON(w, http.StatusAccepted, out)
	}
}

func handleRegisterPluginManifest(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Manifest        plugin.Manifest `json:"manifest"`
			TargetSetDigest string          `json:"target_set_digest"`
			TraceID         string          `json:"trace_id"`
			IdempotencyKey  string          `json:"idempotency_key"`
		}
		if decodeStrictJSON(w, r, 128*1024, &body) != nil || deps.PluginCatalog == nil || body.IdempotencyKey == "" {
			WriteError(w, http.StatusBadRequest, "PLUGIN_MANIFEST_MALFORMED")
			return
		}
		admin, err := authorizeAdminMutation(r, deps, body.Manifest.Scope, "plugin-activate", body.TargetSetDigest)
		if err != nil {
			WriteError(w, http.StatusForbidden, "PLUGIN_MANIFEST_DENIED")
			return
		}
		out, err := deps.PluginCatalog.Register(r.Context(), body.Manifest, admin.Actor, body.TraceID)
		if err != nil {
			WriteError(w, http.StatusBadRequest, "PLUGIN_MANIFEST_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, out)
	}
}

func handleQualifyPlugin(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			ManifestID       string                     `json:"manifest_id"`
			ManifestRevision int                        `json:"manifest_revision"`
			Status           plugin.QualificationStatus `json:"qualification_status"`
			Scope            string                     `json:"scope"`
			TargetSetDigest  string                     `json:"target_set_digest"`
			TraceID          string                     `json:"trace_id"`
			IdempotencyKey   string                     `json:"idempotency_key"`
		}
		pluginID := chi.URLParam(r, "pluginID")
		if decodeStrictJSON(w, r, 16*1024, &body) != nil || pluginID == "" || body.IdempotencyKey == "" || deps.PluginCatalog == nil {
			WriteError(w, http.StatusBadRequest, "PLUGIN_QUALIFICATION_MALFORMED")
			return
		}
		admin, err := authorizeAdminMutation(r, deps, body.Scope, "plugin-activate", body.TargetSetDigest)
		if err != nil {
			WriteError(w, http.StatusForbidden, "PLUGIN_QUALIFICATION_DENIED")
			return
		}
		if err := deps.PluginCatalog.Qualify(r.Context(), pluginID, body.ManifestID, body.ManifestRevision, body.Status, admin.Actor, body.TraceID); err != nil {
			WriteError(w, http.StatusConflict, "PLUGIN_QUALIFICATION_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, map[string]any{"plugin_id": pluginID, "qualification_status": body.Status})
	}
}

func handleActivatePluginBinding(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Binding         plugin.Binding `json:"binding"`
			TargetSetDigest string         `json:"target_set_digest"`
			TraceID         string         `json:"trace_id"`
			IdempotencyKey  string         `json:"idempotency_key"`
		}
		pluginID := chi.URLParam(r, "pluginID")
		if decodeStrictJSON(w, r, 32*1024, &body) != nil || pluginID == "" || body.Binding.PluginID != pluginID ||
			body.IdempotencyKey == "" || deps.PluginBinding == nil {
			WriteError(w, http.StatusBadRequest, "PLUGIN_BINDING_MALFORMED")
			return
		}
		admin, err := authorizeAdminMutation(r, deps, body.Binding.Scope, "plugin-activate", body.TargetSetDigest)
		if err != nil {
			WriteError(w, http.StatusForbidden, "PLUGIN_BINDING_DENIED")
			return
		}
		out, err := deps.PluginBinding.Activate(r.Context(), body.Binding, admin.Actor, body.TraceID)
		if err != nil {
			WriteError(w, http.StatusConflict, "PLUGIN_BINDING_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, out)
	}
}

func handlePluginBindingLifecycle(deps Deps, action string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Scope                     string `json:"scope"`
			TargetSetDigest           string `json:"target_set_digest"`
			TraceID                   string `json:"trace_id"`
			IdempotencyKey            string `json:"idempotency_key"`
			PreviousBindingGeneration int    `json:"previous_binding_generation,omitempty"`
		}
		pluginID := chi.URLParam(r, "pluginID")
		if decodeStrictJSON(w, r, 8*1024, &body) != nil || pluginID == "" || body.IdempotencyKey == "" || deps.PluginBinding == nil {
			WriteError(w, http.StatusBadRequest, "PLUGIN_LIFECYCLE_MALFORMED")
			return
		}
		authorizationAction := "plugin-" + action
		if action == "rollback" {
			authorizationAction = "plugin-activate"
		}
		admin, err := authorizeAdminMutation(r, deps, body.Scope, authorizationAction, body.TargetSetDigest)
		if err != nil {
			WriteError(w, http.StatusForbidden, "PLUGIN_LIFECYCLE_DENIED")
			return
		}
		switch action {
		case "drain":
			err = deps.PluginBinding.Drain(r.Context(), pluginID, admin.Actor, body.TraceID)
		case "revoke":
			err = deps.PluginBinding.Revoke(r.Context(), pluginID, admin.Actor, body.TraceID)
		case "rollback":
			err = deps.PluginBinding.Rollback(r.Context(), pluginID, body.PreviousBindingGeneration, admin.Actor, body.TraceID)
		default:
			err = errors.New("unsupported plugin lifecycle")
		}
		if err != nil {
			WriteError(w, http.StatusConflict, "PLUGIN_LIFECYCLE_REJECTED")
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"plugin_id": pluginID, "status": action})
	}
}

func handleRegisterPluginStatDefinition(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Definition       pluginstat.Definition `json:"definition"`
			ManifestID       string                `json:"manifest_id"`
			ManifestRevision int                   `json:"manifest_revision"`
			Scope            string                `json:"scope"`
			DataClass        string                `json:"data_class"`
			TargetSetDigest  string                `json:"target_set_digest"`
			IdempotencyKey   string                `json:"idempotency_key"`
		}
		if decodeStrictJSON(w, r, 64*1024, &body) != nil || deps.PluginStat == nil || body.IdempotencyKey == "" {
			WriteError(w, http.StatusBadRequest, "STATISTICS_DEFINITION_MALFORMED")
			return
		}
		if _, err := authorizeAdminMutation(r, deps, body.Scope, "plugin.statistics.manage", body.TargetSetDigest); err != nil {
			WriteError(w, http.StatusForbidden, "STATISTICS_DEFINITION_DENIED")
			return
		}
		if err := deps.PluginStat.RegisterDefinition(r.Context(), body.Definition, body.ManifestID, body.ManifestRevision, body.Scope, body.DataClass); err != nil {
			WriteError(w, http.StatusConflict, "STATISTICS_DEFINITION_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, map[string]string{"definition_id": body.Definition.DefinitionID, "definition_digest": body.Definition.DefinitionDigest})
	}
}

func handleCreatePluginStatSchedule(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			ScheduleID       string `json:"schedule_id"`
			DefinitionID     string `json:"definition_id"`
			DefinitionDigest string `json:"definition_digest"`
			IntervalSeconds  int    `json:"interval_seconds"`
			Disabled         bool   `json:"disabled"`
			Scope            string `json:"scope"`
			DataClass        string `json:"data_class"`
			TargetSetDigest  string `json:"target_set_digest"`
			TraceID          string `json:"trace_id"`
			IdempotencyKey   string `json:"idempotency_key"`
		}
		if decodeStrictJSON(w, r, 16*1024, &body) != nil || deps.PluginStat == nil || body.IdempotencyKey == "" {
			WriteError(w, http.StatusBadRequest, "STATISTICS_SCHEDULE_MALFORMED")
			return
		}
		admin, err := authorizeAdminMutation(r, deps, body.Scope, "plugin.statistics.manage", body.TargetSetDigest)
		if err != nil {
			WriteError(w, http.StatusForbidden, "STATISTICS_SCHEDULE_DENIED")
			return
		}
		revision, err := deps.PluginStat.CreateScheduleRevision(r.Context(), body.ScheduleID, body.DefinitionID,
			body.DefinitionDigest, body.IntervalSeconds, body.Disabled, admin.Actor,
			pluginstat.ScheduleAuthorization{Scope: body.Scope, DataClass: body.DataClass, PlatformAdmin: true,
				CSRFVerified: true, StepUpFresh: true, TargetSetDigest: body.TargetSetDigest,
				SourceRead: true, StatisticsRun: true}, body.TraceID)
		if err != nil {
			WriteError(w, http.StatusConflict, "STATISTICS_SCHEDULE_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, map[string]any{"schedule_id": body.ScheduleID, "schedule_revision": revision, "disabled": body.Disabled})
	}
}

func handleGetPluginStatArtifact(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		s, scopes, err := readAuthorization(r, deps)
		if err != nil {
			WriteError(w, http.StatusForbidden, "SCOPE_DENIED")
			return
		}
		_ = s
		id := chi.URLParam(r, "artifactID")
		if id == "" || len(id) > 128 {
			WriteError(w, http.StatusBadRequest, "ARTIFACT_ID_MALFORMED")
			return
		}
		var raw []byte
		err = deps.Pool.Pool.QueryRow(r.Context(), `SELECT a.artifact FROM plugin_statistic_artifacts a
		 JOIN plugin_statistic_runs r ON r.run_id=a.run_id WHERE a.artifact_id=$1 AND r.scope=ANY($2)`, id, scopes).Scan(&raw)
		if errors.Is(err, pgx.ErrNoRows) {
			WriteError(w, http.StatusNotFound, "ARTIFACT_NOT_FOUND")
			return
		}
		if err != nil {
			WriteError(w, http.StatusInternalServerError, "ARTIFACT_QUERY_FAILED")
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		_, _ = w.Write(raw)
	}
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
