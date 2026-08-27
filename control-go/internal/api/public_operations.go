package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/firewall"
	"masi-nids/control-go/internal/governance"
	"masi-nids/control-go/internal/security"
)

func writeRawJSON(w http.ResponseWriter, status int, raw []byte) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "private, no-store")
	w.WriteHeader(status)
	_, _ = w.Write(raw)
}

func writeScopedQueryError(w http.ResponseWriter, err error, notFoundReason string) {
	if errors.Is(err, pgx.ErrNoRows) {
		WriteError(w, http.StatusNotFound, notFoundReason)
		return
	}
	WriteError(w, http.StatusInternalServerError, "INTERNAL_QUERY_FAILED")
}

func scopedActorAndScopes(w http.ResponseWriter, r *http.Request, deps Deps) (*Session, []string, bool) {
	actor, scopes, err := readAuthorization(r, deps)
	if err != nil {
		WriteError(w, http.StatusForbidden, "SCOPE_DENIED")
		return nil, nil, false
	}
	return actor, scopes, true
}

func handleGetEffectProposal(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		err := deps.Pool.QueryRow(r.Context(), `SELECT jsonb_build_object('proposal_id',p.proposal_id,
		 'proposal_digest',p.proposal_digest,'scope',p.scope,'risk_level',p.risk_level,'effect_kind',p.effect_kind,
		 'target_set_digest',p.target_set_digest,'target_ids',p.target_ids,'policy_digest',p.policy_digest,
		 'evidence_refs',p.evidence_refs,'expires_at_unix_ms',p.expires_at_unix_ms,'note',p.note,
		 'created_at_unix_ms',p.created_at_unix_ms,'actor_ref',p.actor_ref,'reason_code',p.reason_code,
		 'governance_status',CASE WHEN p.superseded_by_proposal_id IS NOT NULL THEN 'superseded'
		   WHEN EXISTS(SELECT 1 FROM effect_decisions d WHERE d.proposal_id=p.proposal_id AND d.decision='approve') THEN 'approved'
		   WHEN EXISTS(SELECT 1 FROM effect_decisions d WHERE d.proposal_id=p.proposal_id AND d.decision='reject') THEN 'rejected'
		   WHEN p.expires_at_unix_ms<=$2 THEN 'expired' ELSE 'pending' END,
		 'superseded_by_proposal_id',p.superseded_by_proposal_id,'superseded_at_unix_ms',p.superseded_at_unix_ms,
		 'supersede_reason_code',p.supersede_reason_code)
		 FROM effect_proposals p WHERE p.proposal_id=$1 AND p.scope=ANY($3)`,
			chi.URLParam(r, "proposalID"), time.Now().UnixMilli(), scopes).Scan(&raw)
		if err != nil {
			writeScopedQueryError(w, err, "PROPOSAL_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleGetEffectDecision(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		err := deps.Pool.QueryRow(r.Context(), `SELECT jsonb_build_object('decision_id',d.decision_id,
		 'proposal_id',d.proposal_id,'proposal_digest',d.proposal_digest,'risk_level',d.risk_level,
		 'decision',d.decision,'authz_context',d.authz_context,'decision_digest',d.decision_digest,
		 'expires_at_unix_ms',d.expires_at_unix_ms,'created_at_unix_ms',d.created_at_unix_ms,
		 'reason_code',d.reason_code,'trace_id',d.trace_id) FROM effect_decisions d
		 JOIN effect_proposals p ON p.proposal_id=d.proposal_id WHERE d.decision_id=$1 AND p.scope=ANY($2)`,
			chi.URLParam(r, "decisionID"), scopes).Scan(&raw)
		if err != nil {
			writeScopedQueryError(w, err, "DECISION_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleGetEffectIntent(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		err := deps.Pool.QueryRow(r.Context(), `SELECT jsonb_build_object('effect_intent_id',i.effect_intent_id,
		 'operation_id',i.operation_id,'proposal_id',i.proposal_id,'decision_id',i.decision_id,'target_id',i.target_id,
		 'fleet_operation_id',i.fleet_operation_id,'effect_digest',i.effect_digest,'effect_kind',i.effect_kind,
		 'risk_level',i.risk_level,'deadline_unix_ms',i.deadline_unix_ms,'claim_state',i.claim_state,
		 'gate_open',i.gate_open,'reason_code',i.reason_code,'trace_id',i.trace_id,'effect_payload',i.effect_payload,
		 'latest_attempt',(SELECT to_jsonb(a)-'actor_ref' FROM effect_attempts a WHERE a.intent_id=i.effect_intent_id
		   ORDER BY a.attempt_number DESC LIMIT 1)) FROM effect_intents i
		 JOIN effect_proposals p ON p.proposal_id=i.proposal_id WHERE i.effect_intent_id=$1 AND p.scope=ANY($2)`,
			chi.URLParam(r, "intentID"), scopes).Scan(&raw)
		if err != nil {
			writeScopedQueryError(w, err, "INTENT_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleGetEffectOperation(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		err := deps.Pool.QueryRow(r.Context(), `SELECT jsonb_build_object('operation_id',i.operation_id,
		 'effect_intent_id',i.effect_intent_id,'proposal_id',i.proposal_id,'decision_id',i.decision_id,
		 'target_id',i.target_id,'effect_kind',i.effect_kind,'effect_digest',i.effect_digest,
		 'claim_state',i.claim_state,'deadline_unix_ms',i.deadline_unix_ms,'reason_code',i.reason_code,
		 'acknowledgement',(SELECT to_jsonb(a)-'last_error' FROM effect_acknowledgements a WHERE a.operation_id=i.operation_id),
		 'latest_attempt',(SELECT to_jsonb(e) FROM effect_attempts e WHERE e.intent_id=i.effect_intent_id
		   ORDER BY e.attempt_number DESC LIMIT 1)) FROM effect_intents i
		 JOIN effect_proposals p ON p.proposal_id=i.proposal_id WHERE i.operation_id=$1 AND p.scope=ANY($2)`,
			chi.URLParam(r, "operationID"), scopes).Scan(&raw)
		if err != nil {
			writeScopedQueryError(w, err, "EFFECT_OPERATION_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleGetEffectAttempts(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		err := deps.Pool.QueryRow(r.Context(), `SELECT COALESCE((SELECT jsonb_agg(to_jsonb(a) ORDER BY a.attempt_number)
		 FROM effect_attempts a WHERE a.intent_id=i.effect_intent_id),'[]')
		 FROM effect_intents i JOIN effect_proposals p ON p.proposal_id=i.proposal_id
		 WHERE i.operation_id=$1 AND p.scope=ANY($2)`,
			chi.URLParam(r, "operationID"), scopes).Scan(&raw)
		if err != nil {
			writeScopedQueryError(w, err, "EFFECT_ATTEMPTS_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleGetEffectReadback(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		err := deps.Pool.QueryRow(r.Context(), `SELECT jsonb_build_object('operation_id',i.operation_id,
		 'attempts',COALESCE((SELECT jsonb_agg(jsonb_build_object('attempt_id',a.attempt_id,'status',a.status,
		  'readback_digest',a.readback_digest,'readback_manifest_digest',a.readback_manifest_digest,
		  'expected_entries',a.expected_entries,'observed_entries',a.observed_entries,
		  'mismatched_entries',a.mismatched_entries,'reason_code',a.reason_code) ORDER BY a.attempt_number)
		  FROM effect_attempts a WHERE a.intent_id=i.effect_intent_id),'[]'),
		 'entries',COALESCE((SELECT jsonb_agg(to_jsonb(e) ORDER BY e.entry_index)
		  FROM effect_attempt_readback_entries e WHERE e.intent_id=i.effect_intent_id),'[]'),
		 'bounded_capture',(SELECT to_jsonb(c) FROM bounded_capture_results c WHERE c.effect_intent_id=i.effect_intent_id))
		 FROM effect_intents i JOIN effect_proposals p ON p.proposal_id=i.proposal_id
		 WHERE i.operation_id=$1 AND p.scope=ANY($2)`, chi.URLParam(r, "operationID"), scopes).Scan(&raw)
		if err != nil {
			writeScopedQueryError(w, err, "EFFECT_READBACK_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleGetTarget(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		err := deps.Pool.QueryRow(r.Context(), `SELECT jsonb_build_object('target_id',t.target_id,
		 'display_name',t.display_name,'p4runtime_endpoint',t.p4runtime_endpoint,'device_id',t.device_id,
		 'role',t.role,'status',t.status,'desired_profile_digest',t.desired_profile_digest,'scope',t.scope,
		 'provenance',t.provenance,'assignment',(SELECT to_jsonb(a)-'actor_ref'-'actor_issuer'-'actor_subject'
		  FROM target_assignments a WHERE a.target_id=t.target_id ORDER BY a.assignment_generation DESC LIMIT 1),
		 'observation',(SELECT to_jsonb(c) FROM target_capability_observations c WHERE c.target_id=t.target_id),
		 'lifecycle',(SELECT COALESCE(jsonb_agg(to_jsonb(e)-'actor_ref'-'actor_issuer'-'actor_subject'
		  ORDER BY e.occurred_at_unix_ms,e.event_id),'[]') FROM target_lifecycle_events e WHERE e.target_id=t.target_id))
		 FROM targets t WHERE t.target_id=$1 AND t.scope=ANY($2)`, chi.URLParam(r, "targetID"), scopes).Scan(&raw)
		if err != nil {
			writeScopedQueryError(w, err, "TARGET_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleGetTargetObservation(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		err := deps.Pool.QueryRow(r.Context(), `SELECT to_jsonb(c) FROM target_capability_observations c
		 JOIN targets t ON t.target_id=c.target_id WHERE c.target_id=$1 AND t.scope=ANY($2)`,
			chi.URLParam(r, "targetID"), scopes).Scan(&raw)
		if err != nil {
			writeScopedQueryError(w, err, "TARGET_OBSERVATION_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleGetTargetOperation(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		err := deps.Pool.QueryRow(r.Context(), `SELECT result FROM (
		 SELECT e.event_id AS operation_id,e.target_id,e.scope,to_jsonb(e)-'actor_ref'-'actor_issuer'-'actor_subject' AS result
		 FROM target_lifecycle_events e
		 UNION ALL
		 SELECT a.lease_id AS operation_id,a.target_id,t.scope,jsonb_build_object('operation_id',a.lease_id,
		  'operation_kind','target-assignment','target_id',a.target_id,'assignment_generation',a.assignment_generation,
		  'incarnation_id',a.incarnation_id,'edge_workload_ref',a.edge_workload_ref,'issued_at_unix_ms',a.issued_at_unix_ms,
		  'expires_at_unix_ms',a.expires_at_unix_ms,'revoked_at_unix_ms',a.revoked_at_unix_ms,
		  'election_floor',a.election_floor,'election_ceiling',a.election_ceiling,'actor_runtime_epoch',a.actor_runtime_epoch,
		  'application_generation',a.application_generation,'trace_id',a.trace_id) AS result
		 FROM target_assignments a JOIN targets t ON t.target_id=a.target_id) operations
		 WHERE operation_id=$1 AND target_id=$2 AND scope=ANY($3)`,
			chi.URLParam(r, "operationID"), chi.URLParam(r, "targetID"), scopes).Scan(&raw)
		if err != nil {
			writeScopedQueryError(w, err, "TARGET_OPERATION_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleGetTargetCandidateDiff(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		err := deps.Pool.QueryRow(r.Context(), `SELECT jsonb_build_object('candidate',jsonb_build_object(
		 'target_id',t.target_id,'display_name',t.display_name,'p4runtime_endpoint',t.p4runtime_endpoint,
		 'device_id',t.device_id,'role',t.role,'desired_profile_digest',t.desired_profile_digest,
		 'scope',t.scope,'provenance',t.provenance),'conflicting_active_targets',COALESCE((SELECT jsonb_agg(
		 jsonb_build_object('target_id',a.target_id,'endpoint',a.p4runtime_endpoint,'device_id',a.device_id,'role',a.role))
			 FROM targets a WHERE a.status='active' AND a.scope=ANY($2) AND a.target_id<>t.target_id AND
		 (a.p4runtime_endpoint=t.p4runtime_endpoint OR (a.device_id=t.device_id AND a.role=t.role))),'[]'))
		 FROM targets t WHERE t.target_id=$1 AND t.status='candidate' AND t.scope=ANY($2)`,
			chi.URLParam(r, "targetID"), scopes).Scan(&raw)
		if err != nil {
			writeScopedQueryError(w, err, "TARGET_CANDIDATE_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleGetFleetOperation(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		err := deps.Pool.QueryRow(r.Context(), `SELECT jsonb_build_object('fleet_operation_id',f.fleet_operation_id,
		 'target_set_digest',f.target_set_digest,'operation_digest',f.operation_digest,
		 'completed_vector_digest',f.completed_vector_digest,'wave_count',f.wave_count,'waves',f.waves,
		 'parent_intent_id',f.parent_intent_id,'aggregate_status',f.aggregate_status,'reason_code',f.reason_code,
		 'trace_id',f.trace_id,'children',COALESCE((SELECT jsonb_agg(jsonb_build_object('target_id',c.target_id,
		  'effect_digest',c.effect_digest,'child_intent_id',c.child_intent_id,'wave_index',c.wave_index,
		  'gate_open',c.gate_open,'status',c.status,'reason_code',c.reason_code,
		  'claim_state',i.claim_state,'latest_attempt',(SELECT to_jsonb(a) FROM effect_attempts a
		    WHERE a.intent_id=i.effect_intent_id ORDER BY a.attempt_number DESC LIMIT 1)) ORDER BY c.wave_index,c.target_id)
		  FROM fleet_child_intents c JOIN effect_intents i ON i.effect_intent_id=c.child_intent_id
		  WHERE c.fleet_operation_id=f.fleet_operation_id),'[]')) FROM fleet_operations f
		 WHERE f.fleet_operation_id=$1 AND f.scope=ANY($2)`, chi.URLParam(r, "fleetID"), scopes).Scan(&raw)
		if err != nil {
			writeScopedQueryError(w, err, "FLEET_OPERATION_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleGetFirewallRevision(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		err := deps.Pool.QueryRow(r.Context(), `SELECT jsonb_build_object('revision_id',revision_id,
		 'revision_digest',revision_digest,'target_id',target_id,'default_action',default_action,
		 'rules',rules,'scope',scope,'reason_code',reason_code) FROM firewall_revisions
		 WHERE revision_id=$1 AND scope=ANY($2)`, chi.URLParam(r, "revisionID"), scopes).Scan(&raw)
		if err != nil {
			writeScopedQueryError(w, err, "FIREWALL_REVISION_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleInspectFirewallRevision(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		session, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		if deps.FirewallCompiler == nil {
			WriteError(w, http.StatusServiceUnavailable, "FIREWALL_COMPILER_UNAVAILABLE")
			return
		}
		var revisionID, revisionDigest, targetID, defaultAction, scope string
		var rulesRaw, currentRulesRaw []byte
		var currentRevisionID, previousRevisionID, selectorState string
		var activeBank int
		err := deps.Pool.QueryRow(r.Context(), `SELECT r.revision_id,r.revision_digest,r.target_id,
		 r.default_action,r.rules,r.scope,COALESCE(b.current_revision_id,''),COALESCE(b.previous_revision_id,''),
		 COALESCE(b.active_bank,-1),COALESCE(b.selector_state,'unbound'),COALESCE(c.rules,'[]'::jsonb)
		 FROM firewall_revisions r
		 LEFT JOIN firewall_bindings b ON b.target_id=r.target_id
		 LEFT JOIN firewall_revisions c ON c.revision_id=b.current_revision_id
		 WHERE r.revision_id=$1 AND r.scope=ANY($2)`, chi.URLParam(r, "revisionID"), scopes).
			Scan(&revisionID, &revisionDigest, &targetID, &defaultAction, &rulesRaw, &scope,
				&currentRevisionID, &previousRevisionID, &activeBank, &selectorState, &currentRulesRaw)
		if err != nil {
			writeScopedQueryError(w, err, "FIREWALL_INSPECTION_NOT_FOUND")
			return
		}
		var rules []firewall.Rule
		if err := json.Unmarshal(rulesRaw, &rules); err != nil {
			WriteError(w, http.StatusInternalServerError, "FIREWALL_REVISION_CORRUPT")
			return
		}
		var incarnationID, edgeWorkloadRef, actorRuntimeEpoch, observedActorRuntimeEpoch string
		var p4InfoDigest, pipelineDigest, capacityDigest string
		var assignmentGeneration, applicationGeneration, electionFloor int64
		var assignmentExpires, capabilityExpires int64
		var capacityAvailable bool
		err = deps.Pool.QueryRow(r.Context(), `SELECT a.incarnation_id,a.assignment_generation,a.edge_workload_ref,
		 a.actor_runtime_epoch,a.application_generation,a.election_floor,a.expires_at_unix_ms,
		 c.p4info_digest,c.pipeline_digest,c.capacity_digest,c.capacity_available,c.actor_runtime_epoch,c.expires_at_unix_ms
		 FROM targets t JOIN LATERAL (SELECT * FROM target_assignments x WHERE x.target_id=t.target_id
		  AND x.revoked_at_unix_ms IS NULL ORDER BY x.assignment_generation DESC LIMIT 1) a ON true
		 JOIN target_capability_observations c ON c.target_id=t.target_id
		 WHERE t.target_id=$1 AND t.status='active' AND t.scope=$2`, targetID, scope).
			Scan(&incarnationID, &assignmentGeneration, &edgeWorkloadRef, &actorRuntimeEpoch,
				&applicationGeneration, &electionFloor, &assignmentExpires, &p4InfoDigest, &pipelineDigest,
				&capacityDigest, &capacityAvailable, &observedActorRuntimeEpoch, &capabilityExpires)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				WriteError(w, http.StatusConflict, "FIREWALL_COMPILE_HOLD")
			} else {
				WriteError(w, http.StatusInternalServerError, "INTERNAL_QUERY_FAILED")
			}
			return
		}
		now := time.Now()
		if assignmentExpires <= now.UnixMilli() || capabilityExpires <= now.UnixMilli() || !capacityAvailable ||
			actorRuntimeEpoch == "" || actorRuntimeEpoch != observedActorRuntimeEpoch || electionFloor < 1 {
			WriteError(w, http.StatusConflict, "FIREWALL_COMPILE_HOLD")
			return
		}
		actor := security.Actor{Issuer: session.Actor.Issuer, Subject: session.Actor.Subject}
		bucket := now.UnixMilli() / 10_000
		identitySeed := revisionDigest + ":" + targetID + ":" + actor.String() + ":" +
			incarnationID + ":" + strconv.FormatInt(assignmentGeneration, 10) + ":" + strconv.FormatInt(bucket, 10)
		intent := governance.Intent{
			EffectIntentID: "fw-compile-intent-" + shortID(identitySeed),
			OperationID:    "fw-compile-operation-" + shortID(identitySeed),
			TargetID:       targetID,
			Fence: governance.Fence{TargetControlIncarnationID: incarnationID,
				TargetAssignmentGeneration: assignmentGeneration, EdgeWorkloadRef: edgeWorkloadRef,
				ActorRuntimeEpoch: actorRuntimeEpoch, ApplicationGeneration: applicationGeneration,
				ElectionIDLow: uint64(electionFloor), P4InfoDigest: p4InfoDigest,
				PipelineDigest: pipelineDigest, CapacityDigest: capacityDigest},
			Payload: governance.EffectPayload{SchemaVersion: "p4-effect-payload/v1",
				Operation: "baseline-activate", PolicyRevisionDigest: revisionDigest,
				DefaultAction: defaultAction, BaselineRules: json.RawMessage(rulesRaw), OverlayRules: json.RawMessage(`[]`)},
			AuthorizationDigest: digestText("firewall-compile:" + actor.String() + ":" + scope + ":" + revisionDigest),
			EffectKind:          governance.KindFirewallBaselineActivate, RiskLevel: security.R3,
			RequiredWriteAtomicity: "CONTINUE_ON_ERROR", DeadlineUnixMS: (bucket + 2) * 10_000,
			Actor: actor, TraceID: "trace-fw-compile-" + shortID(identitySeed),
		}
		intent.EffectDigest = governance.ComputeEffectDigest(intent)
		if intent.EffectDigest == "" {
			WriteError(w, http.StatusInternalServerError, "FIREWALL_COMPILE_INTENT_INVALID")
			return
		}
		compiled, err := deps.FirewallCompiler.PreflightEffect(r.Context(), intent)
		if err != nil {
			var rejected *governance.PreflightRejectedError
			if errors.As(err, &rejected) {
				WriteError(w, http.StatusConflict, "FIREWALL_COMPILE_HOLD")
			} else {
				WriteError(w, http.StatusServiceUnavailable, "FIREWALL_COMPILER_UNAVAILABLE")
			}
			return
		}
		logicalEntries := 0
		for _, rule := range rules {
			if rule.Enabled {
				logicalEntries++
			}
		}
		var currentRules any
		if err := json.Unmarshal(currentRulesRaw, &currentRules); err != nil {
			WriteError(w, http.StatusInternalServerError, "FIREWALL_BINDING_CORRUPT")
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"revision_id": revisionID, "revision_digest": revisionDigest, "target_id": targetID,
			"validation": "valid-normalized", "logical_entry_count": logicalEntries,
			"profile_capacity": 4096, "capacity_remaining": 4096 - logicalEntries,
			"same_priority_overlap_count": 0, "current_revision_id": currentRevisionID,
			"previous_revision_id": previousRevisionID, "active_bank": activeBank, "selector_state": selectorState,
			"logical_diff":            map[string]any{"desired_rules": rules, "current_rules": currentRules},
			"physical_compile_status": compiled.Result, "physical_plan_digest": compiled.PlanDigest,
			"physical_entry_count": compiled.PhysicalEntries, "preflight_expires_at_unix_ms": compiled.ExpiresAtUnixMS,
			"compile_fence": map[string]any{"target_control_incarnation_id": incarnationID,
				"target_assignment_generation": assignmentGeneration, "actor_runtime_epoch": actorRuntimeEpoch,
				"application_generation": applicationGeneration, "p4info_digest": p4InfoDigest,
				"pipeline_digest": pipelineDigest, "capacity_digest": capacityDigest},
		})
	}
}

func handleListFirewallBindings(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "firewall-binding", `SELECT count(*) FROM firewall_bindings b
		JOIN targets t ON t.target_id=b.target_id WHERE t.scope=ANY($1)`,
		`SELECT jsonb_build_object('target_id',b.target_id,'current_revision_id',b.current_revision_id,
		 'previous_revision_id',b.previous_revision_id,'active_bank',b.active_bank,'selector_state',b.selector_state,
		 'operation_id',b.operation_id,'cas_digest',b.cas_digest,'binding_version',b.binding_version),b.target_id
		 FROM firewall_bindings b JOIN targets t ON t.target_id=b.target_id WHERE t.scope=ANY($1)
		 AND ($3::text='' OR b.target_id>$3) ORDER BY b.target_id LIMIT $2`)
}

func handleGetFirewallActivation(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		err := deps.Pool.QueryRow(r.Context(), `SELECT jsonb_build_object('activation',to_jsonb(a),
		 'intent',jsonb_build_object('effect_intent_id',i.effect_intent_id,'claim_state',i.claim_state,
		 'effect_digest',i.effect_digest,'reason_code',i.reason_code),
		 'attempts',COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.attempt_number) FROM effect_attempts x
		  WHERE x.intent_id=i.effect_intent_id),'[]')) FROM firewall_activations a
		 JOIN effect_intents i ON i.operation_id=a.operation_id JOIN effect_proposals p ON p.proposal_id=i.proposal_id
		 WHERE a.operation_id=$1 AND p.scope=ANY($2)`, chi.URLParam(r, "operationID"), scopes).Scan(&raw)
		if err != nil {
			writeScopedQueryError(w, err, "FIREWALL_ACTIVATION_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleGetModelRolloutGroup(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		out, err := deps.ModelRollout.LoadRolloutGroup(r.Context(), chi.URLParam(r, "groupID"), scopes)
		if err != nil {
			writeScopedQueryError(w, err, "MODEL_ROLLOUT_GROUP_NOT_FOUND")
			return
		}
		writeJSON(w, http.StatusOK, out)
	}
}

func handleGetModelIncarnation(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		if err := deps.Pool.QueryRow(r.Context(), `SELECT jsonb_build_object('incarnation_id',i.incarnation_id,
		 'source',i.source,'rotated_at_unix_ms',i.rotated_at_unix_ms,'replaced_incarnation_id',i.replaced_incarnation_id,
		 'writer_enabled',s.writer_enabled,'trace_id',i.trace_id) FROM model_control_state s
		 JOIN model_control_incarnations i ON i.incarnation_id=s.active_incarnation_id
		 WHERE s.singleton AND EXISTS(SELECT 1 FROM shard_bindings b WHERE b.scope=ANY($1))`, scopes).Scan(&raw); err != nil {
			writeScopedQueryError(w, err, "MODEL_INCARNATION_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleListModelPools(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "model-pool", `SELECT count(*) FROM logical_pools l
		WHERE EXISTS(SELECT 1 FROM shard_bindings s WHERE s.logical_pool_id=l.logical_pool_id AND s.scope=ANY($1))`,
		`SELECT jsonb_build_object('logical_pool_id',l.logical_pool_id,'current_generation',l.current_generation,
		 'availability_profile',l.availability_profile,'runtime_profile',l.runtime_profile),l.logical_pool_id
		 FROM logical_pools l WHERE EXISTS(SELECT 1 FROM shard_bindings s WHERE s.logical_pool_id=l.logical_pool_id
		 AND s.scope=ANY($1)) AND ($3::text='' OR l.logical_pool_id>$3) ORDER BY l.logical_pool_id LIMIT $2`)
}

func handleGetModelPool(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		err := deps.Pool.QueryRow(r.Context(), `SELECT jsonb_build_object('pool',to_jsonb(l)-'actor_ref',
			 'generations',COALESCE((SELECT jsonb_agg(to_jsonb(g) ORDER BY g.pool_generation) FROM pool_generations g
			  WHERE g.logical_pool_id=l.logical_pool_id AND (EXISTS(SELECT 1 FROM shard_bindings visible
			   WHERE visible.logical_pool_id=g.logical_pool_id AND visible.scope=ANY($2)
			   AND (visible.current_generation=g.pool_generation OR visible.previous_generation=g.pool_generation))
			   OR EXISTS(SELECT 1 FROM model_rollout_operations op WHERE op.logical_pool_id=g.logical_pool_id
			   AND op.target_generation=g.pool_generation AND op.scope=ANY($2)))),'[]'),
		 'observations',COALESCE((SELECT jsonb_agg(to_jsonb(o) ORDER BY o.pool_generation) FROM model_pool_observations_current o
		  WHERE o.logical_pool_id=l.logical_pool_id AND o.scope=ANY($2)),'[]'),
		 'shards',COALESCE((SELECT jsonb_agg(to_jsonb(s) ORDER BY s.shard_id) FROM shard_bindings s
		  WHERE s.logical_pool_id=l.logical_pool_id AND s.scope=ANY($2)),'[]')) FROM logical_pools l
		 WHERE l.logical_pool_id=$1 AND EXISTS(SELECT 1 FROM shard_bindings s WHERE s.logical_pool_id=l.logical_pool_id
		 AND s.scope=ANY($2))`, chi.URLParam(r, "poolID"), scopes).Scan(&raw)
		if err != nil {
			writeScopedQueryError(w, err, "MODEL_POOL_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleGetModelOperation(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		if err := deps.Pool.QueryRow(r.Context(), `SELECT to_jsonb(o)-'actor_ref' FROM model_rollout_operations o
		 WHERE o.operation_id=$1 AND o.scope=ANY($2)`, chi.URLParam(r, "operationID"), scopes).Scan(&raw); err != nil {
			writeScopedQueryError(w, err, "MODEL_OPERATION_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleGetPluginStatDefinition(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		if err := deps.Pool.QueryRow(r.Context(), `SELECT to_jsonb(d) FROM plugin_statistics_definitions d
		 WHERE d.definition_id=$1 AND d.scope=ANY($2) ORDER BY d.plugin_revision DESC LIMIT 1`,
			chi.URLParam(r, "definitionID"), scopes).Scan(&raw); err != nil {
			writeScopedQueryError(w, err, "STAT_DEFINITION_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleGetPluginStatDefinitionHistory(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		if err := deps.Pool.QueryRow(r.Context(), `SELECT jsonb_build_object(
		 'definition',to_jsonb(d),
		 'current',(SELECT to_jsonb(c) FROM plugin_statistics_current c WHERE c.definition_id=d.definition_id),
		 'history',COALESCE((SELECT jsonb_agg(x.entry ORDER BY x.history_seq DESC) FROM (
		   SELECT h.history_seq,jsonb_build_object('history_seq',h.history_seq,
		    'binding_generation',h.binding_generation,'artifact_id',h.artifact_id,'run_id',h.run_id,
		    'quality',h.quality,'recorded_at',h.recorded_at,'artifact_digest',a.artifact_digest,
		    'status',a.status,'artifact',a.artifact,'bytes',a.bytes,'truncated_rows',a.truncated_rows,
		    'truncated_series',a.truncated_series) AS entry
		   FROM plugin_statistics_history h JOIN plugin_statistic_artifacts a ON a.artifact_id=h.artifact_id
		   WHERE h.definition_id=d.definition_id ORDER BY h.history_seq DESC LIMIT 200) x),'[]'))
		 FROM plugin_statistics_definitions d WHERE d.definition_id=$1 AND d.scope=ANY($2)`,
			chi.URLParam(r, "definitionID"), scopes).Scan(&raw); err != nil {
			writeScopedQueryError(w, err, "STAT_DEFINITION_HISTORY_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleGetPluginStatRun(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		if err := deps.Pool.QueryRow(r.Context(), `SELECT jsonb_build_object('run',to_jsonb(r)-'actor_ref'-'actor_issuer'-'actor_subject'-'claim_owner'-'claim_lease_id',
		 'artifact',(SELECT to_jsonb(a) FROM plugin_statistic_artifacts a WHERE a.run_id=r.run_id))
		 FROM plugin_statistic_runs r WHERE r.run_id=$1 AND r.scope=ANY($2)`, chi.URLParam(r, "runID"), scopes).Scan(&raw); err != nil {
			writeScopedQueryError(w, err, "STAT_RUN_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleGetPluginStatSchedule(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		if err := deps.Pool.QueryRow(r.Context(), `SELECT to_jsonb(s)-'actor_ref'-'actor_issuer'-'actor_subject'
		 FROM plugin_statistic_schedules s WHERE s.schedule_id=$1 AND s.scope=ANY($2)
		 ORDER BY s.schedule_revision DESC LIMIT 1`, chi.URLParam(r, "scheduleID"), scopes).Scan(&raw); err != nil {
			writeScopedQueryError(w, err, "STAT_SCHEDULE_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

func handleGetPluginStatScheduleHistory(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, scopes, ok := scopedActorAndScopes(w, r, deps)
		if !ok {
			return
		}
		var raw []byte
		if err := deps.Pool.QueryRow(r.Context(), `SELECT COALESCE((SELECT jsonb_agg(to_jsonb(x)-'actor_ref'-'actor_issuer'-'actor_subject'
		 ORDER BY x.schedule_revision) FROM plugin_statistic_schedules x
		 WHERE x.schedule_id=s.schedule_id AND x.scope=ANY($2)),'[]') FROM plugin_statistic_schedules s
		 WHERE s.schedule_id=$1 AND s.scope=ANY($2) LIMIT 1`, chi.URLParam(r, "scheduleID"), scopes).Scan(&raw); err != nil {
			writeScopedQueryError(w, err, "STAT_SCHEDULE_HISTORY_NOT_FOUND")
			return
		}
		writeRawJSON(w, http.StatusOK, raw)
	}
}

// ---- mutation handlers for the public operation surfaces ----

func handleSupersedeProposal(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			ReplacementProposalID string `json:"replacement_proposal_id"`
			Scope                 string `json:"scope"`
			TargetSetDigest       string `json:"target_set_digest"`
			ReasonCode            string `json:"reason_code"`
			IdempotencyKey        string `json:"idempotency_key"`
		}
		if decodeStrictJSON(w, r, 8*1024, &body) != nil || body.ReplacementProposalID == "" ||
			!validReasonCode(body.ReasonCode) || body.IdempotencyKey == "" {
			WriteError(w, 400, "PROPOSAL_SUPERSEDE_MALFORMED")
			return
		}
		session, err := sessionFromContext(r.Context())
		if err != nil {
			WriteError(w, 401, "UNAUTHENTICATED")
			return
		}
		actor := security.Actor{Issuer: session.Actor.Issuer, Subject: session.Actor.Subject}
		var kind string
		if err := deps.Pool.QueryRow(r.Context(), `SELECT effect_kind FROM effect_proposals WHERE proposal_id=$1 AND scope=$2`,
			chi.URLParam(r, "proposalID"), body.Scope).Scan(&kind); err != nil {
			WriteError(w, 404, "PROPOSAL_NOT_FOUND")
			return
		}
		authorized := false
		for _, level := range []security.AuthzContextLevel{security.LevelAnalyst, security.LevelOperator, security.LevelScopedOperator, security.LevelPlatformAdmin} {
			if _, err := deps.Mapping.AuthorizeScope(actor, level, body.Scope, kind, body.TargetSetDigest); err == nil {
				authorized = true
				break
			}
		}
		if !authorized {
			WriteError(w, 403, "PROPOSAL_SUPERSEDE_DENIED")
			return
		}
		if err := deps.Proposals.Supersede(r.Context(), chi.URLParam(r, "proposalID"), body.ReplacementProposalID, actor, body.ReasonCode); err != nil {
			WriteError(w, 409, "PROPOSAL_SUPERSEDE_REJECTED")
			return
		}
		writeJSON(w, 200, map[string]string{"proposal_id": chi.URLParam(r, "proposalID"), "superseded_by_proposal_id": body.ReplacementProposalID, "status": "superseded"})
	}
}

func handleCreateEffectIntent(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			ProposalID      string `json:"proposal_id"`
			DecisionID      string `json:"decision_id"`
			EffectIntentID  string `json:"effect_intent_id"`
			OperationID     string `json:"operation_id"`
			Scope           string `json:"scope"`
			TargetSetDigest string `json:"target_set_digest"`
			IdempotencyKey  string `json:"idempotency_key"`
			DeadlineUnixMS  int64  `json:"deadline_unix_ms"`
		}
		if decodeStrictJSON(w, r, 16*1024, &body) != nil || body.ProposalID == "" || body.DecisionID == "" || body.EffectIntentID == "" ||
			body.OperationID == "" || body.IdempotencyKey == "" || body.DeadlineUnixMS <= time.Now().UnixMilli() {
			WriteError(w, 400, "INTENT_MALFORMED")
			return
		}
		var targetID, kind, risk, decisionDigest, decisionIssuer, decisionSubject string
		var targetIDs []string
		err := deps.Pool.QueryRow(r.Context(), `SELECT p.target_ids,p.target_ids[1],p.effect_kind,p.risk_level,
		 d.decision_digest,d.actor_issuer,d.actor_subject FROM effect_proposals p JOIN effect_decisions d
		 ON d.proposal_id=p.proposal_id WHERE p.proposal_id=$1 AND p.scope=$2 AND p.target_set_digest=$3
		 AND p.superseded_by_proposal_id IS NULL AND d.decision_id=$4 AND d.decision='approve'`, body.ProposalID, body.Scope, body.TargetSetDigest, body.DecisionID).
			Scan(&targetIDs, &targetID, &kind, &risk, &decisionDigest, &decisionIssuer, &decisionSubject)
		if err != nil || len(targetIDs) != 1 || (governance.EffectKind(kind) != governance.KindFirewallBaselineActivate && governance.EffectKind(kind) != governance.KindFirewallRollback) {
			WriteError(w, 409, "INTENT_POLICY_UNAVAILABLE")
			return
		}
		operator, err := authorizeOperatorMutation(r, deps, body.Scope, kind, body.TargetSetDigest, security.RiskLevel(risk))
		if err != nil || operator.Actor.Issuer != decisionIssuer || operator.Actor.Subject != decisionSubject {
			WriteError(w, 403, "INTENT_DENIED")
			return
		}
		token, err := deps.Preflight.Preflight(r.Context(), body.ProposalID)
		if err != nil {
			WriteError(w, 409, "INTENT_PREFLIGHT_HOLD")
			return
		}
		out, err := deps.Intents.Create(r.Context(), body.ProposalID, body.DecisionID, *token, governance.Intent{EffectIntentID: body.EffectIntentID,
			OperationID: body.OperationID, TargetID: targetID, Fence: token.Fence, AuthorizationDigest: decisionDigest,
			EffectKind: governance.EffectKind(kind), RiskLevel: security.RiskLevel(risk), RequiredWriteAtomicity: "CONTINUE_ON_ERROR",
			DeadlineUnixMS: body.DeadlineUnixMS, Actor: operator.Actor, TraceID: token.TraceID})
		if err != nil {
			WriteError(w, 409, "INTENT_REJECTED")
			return
		}
		writeJSON(w, http.StatusCreated, out)
	}
}

func handleReconcileEffectOperation(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Scope           string `json:"scope"`
			TargetSetDigest string `json:"target_set_digest"`
			IdempotencyKey  string `json:"idempotency_key"`
		}
		if decodeStrictJSON(w, r, 4096, &body) != nil || body.IdempotencyKey == "" {
			WriteError(w, 400, "RECONCILE_MALFORMED")
			return
		}
		var intentID, kind, risk string
		if err := deps.Pool.QueryRow(r.Context(), `SELECT i.effect_intent_id,i.effect_kind,i.risk_level FROM effect_intents i
		 JOIN effect_proposals p ON p.proposal_id=i.proposal_id WHERE i.operation_id=$1 AND p.scope=$2 AND p.target_set_digest=$3`,
			chi.URLParam(r, "operationID"), body.Scope, body.TargetSetDigest).Scan(&intentID, &kind, &risk); err != nil {
			WriteError(w, 404, "EFFECT_OPERATION_NOT_FOUND")
			return
		}
		if _, err := authorizeOperatorMutation(r, deps, body.Scope, kind, body.TargetSetDigest, security.RiskLevel(risk)); err != nil {
			WriteError(w, 403, "RECONCILE_DENIED")
			return
		}
		out, err := deps.Reconcile.ReconcileUnknown(r.Context(), intentID)
		if err != nil {
			WriteError(w, 409, "RECONCILE_PENDING")
			return
		}
		writeJSON(w, 200, out)
	}
}
