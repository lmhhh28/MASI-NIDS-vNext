package api

import (
	"errors"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/governance"
	"masi-nids/control-go/internal/model"
	"masi-nids/control-go/internal/pluginstat"
	"masi-nids/control-go/internal/security"
	"masi-nids/control-go/internal/target"
)

type targetMutationBody struct {
	Scope           string `json:"scope"`
	TargetSetDigest string `json:"target_set_digest"`
	ReasonCode      string `json:"reason_code"`
	TraceID         string `json:"trace_id"`
	IdempotencyKey  string `json:"idempotency_key"`
}

func lifecycleOperationID(r *http.Request, deps Deps, targetID, traceID string) string {
	var operationID string
	_ = deps.Pool.QueryRow(r.Context(), `SELECT event_id FROM target_lifecycle_events
		WHERE target_id=$1 AND trace_id=$2 ORDER BY occurred_at_unix_ms DESC,event_id DESC LIMIT 1`,
		targetID, traceID).Scan(&operationID)
	return operationID
}

func handleImportTargetCandidate(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Target          target.Target     `json:"target"`
			Provenance      target.Provenance `json:"provenance"`
			TargetSetDigest string            `json:"target_set_digest"`
			IdempotencyKey  string            `json:"idempotency_key"`
		}
		if decodeStrictJSON(w, r, 32*1024, &body) != nil || deps.TargetRegistry == nil || body.IdempotencyKey == "" {
			WriteError(w, 400, "TARGET_CANDIDATE_IMPORT_MALFORMED")
			return
		}
		admin, err := authorizeAdminMutation(r, deps, body.Target.Scope, "target-lifecycle", body.TargetSetDigest)
		if err != nil {
			WriteError(w, 403, "TARGET_CANDIDATE_IMPORT_DENIED")
			return
		}
		body.Target.Actor, body.Target.IdempotencyKey = admin.Actor, body.IdempotencyKey
		out, err := deps.TargetRegistry.ImportCandidate(r.Context(), body.Target, body.Provenance,
			target.LifecycleAuthorization{Scope: body.Target.Scope, PlatformAdmin: true, StepUpFresh: true, CSRFVerified: true})
		if err != nil {
			WriteError(w, 409, "TARGET_CANDIDATE_IMPORT_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, map[string]any{"target": out,
			"operation_id": lifecycleOperationID(r, deps, out.TargetID, body.Target.TraceID)})
	}
}

func handleVerifyTarget(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body targetMutationBody
		if decodeStrictJSON(w, r, 8*1024, &body) != nil || !validReasonCode(body.ReasonCode) || body.IdempotencyKey == "" {
			WriteError(w, 400, "TARGET_VERIFY_MALFORMED")
			return
		}
		admin, err := authorizeAdminMutation(r, deps, body.Scope, "target-lifecycle", body.TargetSetDigest)
		if err != nil {
			WriteError(w, 403, "TARGET_VERIFY_DENIED")
			return
		}
		targetID := chi.URLParam(r, "targetID")
		if err := deps.TargetRegistry.Verify(r.Context(), targetID, admin.Actor,
			target.LifecycleAuthorization{Scope: body.Scope, PlatformAdmin: true, StepUpFresh: true, CSRFVerified: true},
			body.ReasonCode, body.TraceID); err != nil {
			WriteError(w, 409, "TARGET_VERIFY_REJECTED")
			return
		}
		writeJSON(w, 200, map[string]any{"target_id": targetID, "status": "verified",
			"operation_id": lifecycleOperationID(r, deps, targetID, body.TraceID)})
	}
}

func handleTargetOperationalLifecycle(deps Deps, desired target.TargetStatus) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body targetMutationBody
		if decodeStrictJSON(w, r, 8*1024, &body) != nil || !validReasonCode(body.ReasonCode) || body.IdempotencyKey == "" {
			WriteError(w, 400, "TARGET_LIFECYCLE_MALFORMED")
			return
		}
		admin, err := authorizeAdminMutation(r, deps, body.Scope, "target-lifecycle", body.TargetSetDigest)
		if err != nil {
			WriteError(w, 403, "TARGET_LIFECYCLE_DENIED")
			return
		}
		targetID := chi.URLParam(r, "targetID")
		var generation int64
		var revoked *int64
		err = deps.Pool.QueryRow(r.Context(), `SELECT assignment_generation,revoked_at_unix_ms FROM target_assignments
			WHERE target_id=$1 ORDER BY assignment_generation DESC LIMIT 1`, targetID).Scan(&generation, &revoked)
		if err == nil && revoked == nil {
			if revokeErr := deps.Assignment.Revoke(r.Context(), targetID, generation, time.Now().UnixMilli()); revokeErr != nil {
				WriteError(w, 409, "TARGET_ASSIGNMENT_REVOKE_REJECTED")
				return
			}
		} else if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			WriteError(w, 500, "TARGET_ASSIGNMENT_QUERY_FAILED")
			return
		}
		if err := deps.TargetRegistry.Transition(r.Context(), targetID, desired, admin.Actor,
			target.LifecycleAuthorization{Scope: body.Scope, PlatformAdmin: true, StepUpFresh: true, CSRFVerified: true},
			body.ReasonCode, body.TraceID); err != nil {
			WriteError(w, 409, "TARGET_LIFECYCLE_REJECTED")
			return
		}
		writeJSON(w, 200, map[string]any{"target_id": targetID, "status": desired,
			"operation_id": lifecycleOperationID(r, deps, targetID, body.TraceID)})
	}
}

func handleAssignTarget(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Assignment           target.Assignment `json:"assignment"`
			PriorGeneration      int64             `json:"prior_generation"`
			PriorElectionCeiling int64             `json:"prior_election_ceiling"`
			Scope                string            `json:"scope"`
			TargetSetDigest      string            `json:"target_set_digest"`
			IdempotencyKey       string            `json:"idempotency_key"`
		}
		if decodeStrictJSON(w, r, 16*1024, &body) != nil || body.IdempotencyKey == "" || deps.Assignment == nil {
			WriteError(w, 400, "TARGET_ASSIGNMENT_MALFORMED")
			return
		}
		admin, err := authorizeAdminMutation(r, deps, body.Scope, "target-assignment", body.TargetSetDigest)
		if err != nil {
			WriteError(w, 403, "TARGET_ASSIGNMENT_DENIED")
			return
		}
		body.Assignment.TargetID, body.Assignment.Actor = chi.URLParam(r, "targetID"), admin.Actor
		result, err := deps.Assignment.Handoff(r.Context(), body.Assignment, body.PriorGeneration, body.PriorElectionCeiling)
		if err != nil || result != target.HandoffApplied {
			WriteError(w, 409, "TARGET_ASSIGNMENT_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, map[string]any{"operation_id": body.Assignment.LeaseID,
			"target_id": body.Assignment.TargetID, "assignment_generation": body.Assignment.AssignmentGeneration,
			"result": result})
	}
}

type firewallProposalBody struct {
	ProposalID      string   `json:"proposal_id"`
	TargetSetDigest string   `json:"target_set_digest"`
	EvidenceRefs    []string `json:"evidence_refs"`
	ExpiresAtUnixMS int64    `json:"expires_at_unix_ms"`
	Note            string   `json:"note"`
	TraceID         string   `json:"trace_id"`
	IdempotencyKey  string   `json:"idempotency_key"`
}

func handleCreateFirewallActivationProposal(deps Deps, rollback bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body firewallProposalBody
		if decodeStrictJSON(w, r, 32*1024, &body) != nil || body.ProposalID == "" || body.IdempotencyKey == "" ||
			body.ExpiresAtUnixMS <= time.Now().UnixMilli() {
			WriteError(w, 400, "FIREWALL_PROPOSAL_MALFORMED")
			return
		}
		var revisionID string
		if rollback {
			if err := deps.Pool.QueryRow(r.Context(), `SELECT b.previous_revision_id FROM firewall_activations a
				JOIN firewall_bindings b ON b.target_id=a.target_id WHERE a.operation_id=$1 AND b.previous_revision_id IS NOT NULL`,
				chi.URLParam(r, "operationID")).Scan(&revisionID); err != nil {
				WriteError(w, 409, "FIREWALL_NO_EXACT_PREVIOUS")
				return
			}
		} else {
			revisionID = chi.URLParam(r, "revisionID")
		}
		var targetID, scope, revisionDigest string
		if err := deps.Pool.QueryRow(r.Context(), `SELECT target_id,scope,revision_digest FROM firewall_revisions WHERE revision_id=$1`, revisionID).
			Scan(&targetID, &scope, &revisionDigest); err != nil {
			WriteError(w, 404, "FIREWALL_REVISION_NOT_FOUND")
			return
		}
		kind := governance.KindFirewallBaselineActivate
		if rollback {
			kind = governance.KindFirewallRollback
		}
		admin, err := authorizeAdminMutation(r, deps, scope, string(kind), body.TargetSetDigest)
		if err != nil {
			WriteError(w, 403, "FIREWALL_PROPOSAL_DENIED")
			return
		}
		out, err := deps.Proposals.Create(r.Context(), governance.Proposal{ProposalID: body.ProposalID,
			Actor: admin.Actor, ActorLevel: security.LevelPlatformAdmin, Scope: scope, RiskLevel: security.R3,
			EffectKind: kind, TargetSetDigest: body.TargetSetDigest, TargetIDs: []string{targetID},
			PolicyDigest: revisionDigest, EvidenceRefs: body.EvidenceRefs, ExpiresAtUnixMS: body.ExpiresAtUnixMS,
			Note: body.Note, TraceID: body.TraceID, IdempotencyKey: body.IdempotencyKey})
		if err != nil {
			WriteError(w, 409, "FIREWALL_PROPOSAL_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, out)
	}
}

func handleFirewallProposalDecision(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Decision       string `json:"decision"`
			ReasonCode     string `json:"reason_code"`
			IdempotencyKey string `json:"idempotency_key"`
		}
		if decodeStrictJSON(w, r, 8*1024, &body) != nil || (body.Decision != "approve" && body.Decision != "reject") ||
			!validReasonCode(body.ReasonCode) || body.IdempotencyKey == "" {
			WriteError(w, 400, "FIREWALL_DECISION_MALFORMED")
			return
		}
		recordDecision(w, r, deps, chi.URLParam(r, "proposalID"), body.Decision == "approve", body.ReasonCode, http.StatusCreated)
	}
}

func handleCreateModelRollbackGroup(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			NewGroupID      string               `json:"new_group_id"`
			OrderedShards   []string             `json:"ordered_shards"`
			Template        model.RolloutRequest `json:"template"`
			TargetSetDigest string               `json:"target_set_digest"`
			IdempotencyKey  string               `json:"idempotency_key"`
		}
		if decodeStrictJSON(w, r, 128*1024, &body) != nil || body.NewGroupID == "" || body.IdempotencyKey == "" {
			WriteError(w, 400, "MODEL_ROLLBACK_MALFORMED")
			return
		}
		var originalScope, originalIncarnation string
		if err := deps.Pool.QueryRow(r.Context(), `SELECT scope,model_control_incarnation_id FROM model_rollout_groups WHERE group_id=$1`, chi.URLParam(r, "groupID")).Scan(&originalScope, &originalIncarnation); err != nil {
			WriteError(w, 404, "MODEL_ROLLOUT_GROUP_NOT_FOUND")
			return
		}
		admin, err := authorizeAdminMutation(r, deps, originalScope, "model-rollback", body.TargetSetDigest)
		if err != nil {
			WriteError(w, 403, "MODEL_ROLLBACK_DENIED")
			return
		}
		body.Template.Kind = model.OpRollback
		body.Template.Scope = originalScope
		body.Template.ModelControlIncarnationID = originalIncarnation
		body.Template.ActorRef = admin.Actor.String()
		out, err := deps.ModelRollout.CreateRolloutGroup(r.Context(), model.RolloutGroupRequest{GroupID: body.NewGroupID, OrderedShards: body.OrderedShards, Template: body.Template})
		if err != nil {
			WriteError(w, 409, "MODEL_ROLLBACK_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, out)
	}
}

func handleRecoverModelOperation(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct{ Scope, TargetSetDigest, IdempotencyKey string }
		if decodeStrictJSON(w, r, 4096, &body) != nil || body.IdempotencyKey == "" {
			WriteError(w, 400, "MODEL_RECOVERY_MALFORMED")
			return
		}
		if _, err := authorizeAdminMutation(r, deps, body.Scope, "model-recovery", body.TargetSetDigest); err != nil {
			WriteError(w, 403, "MODEL_RECOVERY_DENIED")
			return
		}
		out, err := deps.ModelRollout.ReconcileOperation(r.Context(), chi.URLParam(r, "operationID"))
		if err != nil {
			WriteError(w, 409, "MODEL_RECOVERY_PENDING")
			return
		}
		writeJSON(w, 200, out)
	}
}

func handleRotateModelIncarnation(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			ReplacedIncarnationID string                  `json:"replaced_incarnation_id"`
			Source                model.IncarnationSource `json:"source"`
			Scope                 string                  `json:"scope"`
			TargetSetDigest       string                  `json:"target_set_digest"`
			TraceID               string                  `json:"trace_id"`
			IdempotencyKey        string                  `json:"idempotency_key"`
		}
		if decodeStrictJSON(w, r, 8192, &body) != nil || body.IdempotencyKey == "" {
			WriteError(w, 400, "MODEL_INCARNATION_MALFORMED")
			return
		}
		admin, err := authorizeAdminMutation(r, deps, body.Scope, "model-recovery", body.TargetSetDigest)
		if err != nil {
			WriteError(w, 403, "MODEL_INCARNATION_DENIED")
			return
		}
		out, err := deps.ModelIncarnation.RotateIncarnation(r.Context(), body.ReplacedIncarnationID, body.Source, admin.Actor.String(), body.TraceID)
		if err != nil {
			WriteError(w, 409, "MODEL_INCARNATION_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, out)
	}
}

func handleEnableModelWriter(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct{ Scope, TargetSetDigest, IdempotencyKey string }
		if decodeStrictJSON(w, r, 4096, &body) != nil || body.IdempotencyKey == "" {
			WriteError(w, 400, "MODEL_WRITER_ENABLE_MALFORMED")
			return
		}
		if _, err := authorizeAdminMutation(r, deps, body.Scope, "model-recovery", body.TargetSetDigest); err != nil {
			WriteError(w, 403, "MODEL_WRITER_ENABLE_DENIED")
			return
		}
		if err := deps.ModelIncarnation.EnableWriter(r.Context(), chi.URLParam(r, "incarnationID")); err != nil {
			WriteError(w, 409, "MODEL_WRITER_ENABLE_REJECTED")
			return
		}
		writeJSON(w, 200, map[string]any{"incarnation_id": chi.URLParam(r, "incarnationID"), "writer_enabled": true})
	}
}

func handleRevisePluginStatSchedule(deps Deps, disabled bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			DefinitionID     string `json:"definition_id"`
			DefinitionDigest string `json:"definition_digest"`
			IntervalSeconds  int    `json:"interval_seconds"`
			Scope            string `json:"scope"`
			DataClass        string `json:"data_class"`
			TargetSetDigest  string `json:"target_set_digest"`
			TraceID          string `json:"trace_id"`
			IdempotencyKey   string `json:"idempotency_key"`
		}
		if decodeStrictJSON(w, r, 16*1024, &body) != nil || body.IdempotencyKey == "" {
			WriteError(w, 400, "STATISTICS_SCHEDULE_REVISION_MALFORMED")
			return
		}
		admin, err := authorizeAdminMutation(r, deps, body.Scope, "plugin.statistics.manage", body.TargetSetDigest)
		if err != nil {
			WriteError(w, 403, "STATISTICS_SCHEDULE_REVISION_DENIED")
			return
		}
		revision, err := deps.PluginStat.CreateScheduleRevision(r.Context(), chi.URLParam(r, "scheduleID"), body.DefinitionID, body.DefinitionDigest,
			body.IntervalSeconds, disabled, admin.Actor, pluginstat.ScheduleAuthorization{Scope: body.Scope, DataClass: body.DataClass,
				PlatformAdmin: true, CSRFVerified: true, StepUpFresh: true, TargetSetDigest: body.TargetSetDigest, SourceRead: true, StatisticsRun: true}, body.TraceID)
		if err != nil {
			WriteError(w, 409, "STATISTICS_SCHEDULE_REVISION_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, map[string]any{"schedule_id": chi.URLParam(r, "scheduleID"), "schedule_revision": revision, "disabled": disabled})
	}
}
