package api

import (
	"encoding/json"
	"errors"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"

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
	return adminMutation{Actor: actor, Session: s, Scope: scope, TargetSet: targetSet}, nil
}

func authorizeOperatorMutation(r *http.Request, deps Deps, scope, action, targetSet string, risk security.RiskLevel) (adminMutation, error) {
	s, err := sessionFromContext(r.Context())
	if err != nil || deps.Mapping == nil || scope == "" || !validDigest(targetSet) ||
		(risk != security.R1 && risk != security.R2 && risk != security.R3) {
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
			return adminMutation{Actor: actor, Session: s, Scope: scope, TargetSet: targetSet}, nil
		}
	}
	return adminMutation{}, errors.New("operator mutation denied")
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
			CurrentWave     int    `json:"current_wave"`
			NextWave        int    `json:"next_wave"`
			Scope           string `json:"scope"`
			TargetSetDigest string `json:"target_set_digest"`
			IdempotencyKey  string `json:"idempotency_key"`
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
		if _, err := authorizeOperatorMutation(r, deps, scope, effectKind, targetDigest, security.RiskLevel(risk)); err != nil {
			WriteError(w, http.StatusForbidden, "FLEET_WAVE_DENIED")
			return
		}
		if err := deps.FleetCoordinator.AdvanceWaveGate(r.Context(), fleetID, body.CurrentWave, body.NextWave); err != nil {
			WriteError(w, http.StatusConflict, "FLEET_WAVE_REJECTED")
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"fleet_operation_id": fleetID, "opened_wave": body.NextWave})
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
			err = deps.TargetRegistry.Retire(r.Context(), targetID, admin.Actor, auth, body.ReasonCode, body.TraceID)
		}
		if err != nil {
			WriteError(w, http.StatusConflict, "TARGET_LIFECYCLE_REJECTED")
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"target_id": targetID, "status": map[bool]string{true: "active", false: "retired"}[activate]})
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
