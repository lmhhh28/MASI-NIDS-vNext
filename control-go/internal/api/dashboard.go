package api

import (
	"encoding/json"
	"fmt"
	"net/http"
)

const dashboardListLimit = 8

// handleGetDashboard returns one PostgreSQL statement snapshot. The browser
// never has to assemble Overview state from independently timed list requests,
// and every collection in the response has a hard contract bound.
func handleGetDashboard(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		session, scopes, err := readAuthorization(r, deps)
		if err != nil || len(scopes) == 0 || len(scopes) > 256 {
			WriteError(w, http.StatusForbidden, "SCOPE_DENIED")
			return
		}
		if deps.Pool == nil || deps.Pool.Pool == nil {
			WriteError(w, http.StatusServiceUnavailable, "DASHBOARD_STORE_UNAVAILABLE")
			return
		}

		var snapshotUnixMS int64
		var countsRaw, alertsRaw, operationsRaw, targetsRaw []byte
		err = deps.Pool.Pool.QueryRow(r.Context(), `
			SELECT
			  (extract(epoch FROM statement_timestamp()) * 1000)::bigint,
			  jsonb_build_object(
			    'events_24h', (SELECT count(*) FROM events e
			      WHERE e.scope=ANY($1) AND e.event_time >= statement_timestamp() - interval '24 hours'),
			    'alerts_24h', (SELECT count(*) FROM events e
			      WHERE e.scope=ANY($1) AND e.event_time >= statement_timestamp() - interval '24 hours'
			        AND e.decision='alert'),
			    'degraded_events_24h', (SELECT count(*) FROM events e
			      WHERE e.scope=ANY($1) AND e.event_time >= statement_timestamp() - interval '24 hours'
			        AND e.quality IN ('partial','gap','stale','invalid','reset','not-covered','not-measurable')),
			    'open_incidents', (SELECT count(*) FROM incidents i
			      WHERE i.scope=ANY($1) AND i.status NOT IN ('closed','resolved')),
			    'targets_total', (SELECT count(*) FROM targets t WHERE t.scope=ANY($1)),
			    'targets_active', (SELECT count(*) FROM targets t WHERE t.scope=ANY($1) AND t.status='active'),
			    'targets_attention', (SELECT count(*) FROM targets t
			      WHERE t.scope=ANY($1) AND t.status IN ('draining','disabled','quarantined')),
			    'pending_approvals', (SELECT count(*) FROM effect_proposals p
			      WHERE p.scope=ANY($1) AND p.expires_at_unix_ms > (extract(epoch FROM statement_timestamp()) * 1000)::bigint
			        AND p.superseded_by_proposal_id IS NULL
			        AND NOT EXISTS (SELECT 1 FROM effect_decisions d WHERE d.proposal_id=p.proposal_id)),
			    'active_effects', (SELECT count(*) FROM effect_intents i
			      JOIN effect_proposals p ON p.proposal_id=i.proposal_id
			      WHERE p.scope=ANY($1) AND NOT i.is_fleet_parent AND i.claim_state <> 'finalized'),
			    'unknown_effects', (SELECT count(*) FROM effect_intents i
			      JOIN effect_proposals p ON p.proposal_id=i.proposal_id
			      WHERE p.scope=ANY($1) AND NOT i.is_fleet_parent AND i.claim_state='unknown'),
			    'model_shards_ready', (SELECT count(*) FROM shard_bindings s
			      WHERE s.scope=ANY($1) AND s.resume_state='current' AND s.loaded AND s.ready),
			    'model_shards_unavailable', (SELECT count(*) FROM shard_bindings s
			      WHERE s.scope=ANY($1) AND (s.resume_state <> 'current' OR NOT s.loaded OR NOT s.ready)),
			    'plugins_active', (SELECT count(*) FROM plugin_bindings b
			      WHERE b.activation_state='active' AND EXISTS (
			        SELECT 1 FROM plugin_manifests m WHERE m.plugin_id=b.plugin_id AND m.scope=ANY($1))),
			    'analysis_attention', (SELECT count(*) FROM analysis_task_requests a
			      WHERE a.scope=ANY($1) AND a.status IN ('limited','insufficient_evidence','failed','fenced'))
			  ),
			  COALESCE((SELECT jsonb_agg(x.item ORDER BY x.event_time_unix_ms DESC, x.event_id DESC)
			    FROM (SELECT e.event_id,
			      (extract(epoch FROM e.event_time) * 1000)::bigint AS event_time_unix_ms,
			      jsonb_build_object('event_id',e.event_id,'shard_id',e.shard_id,
			        'decision',e.decision,'quality',e.quality,'predicted_label',e.predicted_label,
			        'event_time_unix_ms',(extract(epoch FROM e.event_time) * 1000)::bigint) AS item
			      FROM events e WHERE e.scope=ANY($1) AND e.decision IN ('alert','abstain')
			      ORDER BY e.event_time DESC,e.event_id DESC LIMIT $2) x), '[]'::jsonb),
			  COALESCE((SELECT jsonb_agg(x.item ORDER BY x.created_at DESC, x.effect_intent_id)
			    FROM (SELECT i.effect_intent_id,i.created_at,
			      jsonb_build_object('effect_intent_id',i.effect_intent_id,'operation_id',i.operation_id,
			        'target_id',i.target_id,'effect_kind',i.effect_kind,'risk_level',i.risk_level,
			        'claim_state',i.claim_state,'deadline_unix_ms',i.deadline_unix_ms,
			        'reason_code',i.reason_code) AS item
			      FROM effect_intents i JOIN effect_proposals p ON p.proposal_id=i.proposal_id
			      WHERE p.scope=ANY($1) AND NOT i.is_fleet_parent AND i.claim_state <> 'finalized'
			      ORDER BY i.created_at DESC,i.effect_intent_id LIMIT $2) x), '[]'::jsonb),
			  COALESCE((SELECT jsonb_agg(x.item ORDER BY x.attention_rank, x.target_id)
			    FROM (SELECT t.target_id,
			      CASE WHEN t.status IN ('draining','disabled','quarantined') THEN 0 ELSE 1 END AS attention_rank,
			      jsonb_build_object('target_id',t.target_id,'display_name',t.display_name,
			        'lifecycle',t.status,'assignment_generation',a.assignment_generation,
			        'lease_expires_at_unix_ms',a.expires_at_unix_ms) AS item
			      FROM targets t LEFT JOIN LATERAL (
			        SELECT ta.assignment_generation,ta.expires_at_unix_ms FROM target_assignments ta
			        WHERE ta.target_id=t.target_id ORDER BY ta.assignment_generation DESC LIMIT 1
			      ) a ON true
			      WHERE t.scope=ANY($1)
			      ORDER BY attention_rank,t.target_id LIMIT $2) x), '[]'::jsonb)
		`, scopes, dashboardListLimit).Scan(
			&snapshotUnixMS, &countsRaw, &alertsRaw, &operationsRaw, &targetsRaw,
		)
		if err != nil {
			WriteError(w, http.StatusInternalServerError, "DASHBOARD_QUERY_FAILED")
			return
		}

		var attention struct {
			DegradedEvents24H      int64 `json:"degraded_events_24h"`
			TargetsAttention       int64 `json:"targets_attention"`
			PendingApprovals       int64 `json:"pending_approvals"`
			UnknownEffects         int64 `json:"unknown_effects"`
			ModelShardsUnavailable int64 `json:"model_shards_unavailable"`
			AnalysisAttention      int64 `json:"analysis_attention"`
		}
		if err := json.Unmarshal(countsRaw, &attention); err != nil {
			WriteError(w, http.StatusInternalServerError, "DASHBOARD_PROJECTION_INVALID")
			return
		}
		state := "ready"
		reason := "DASHBOARD_READY"
		if attention.UnknownEffects > 0 || attention.ModelShardsUnavailable > 0 {
			state = "hold"
			reason = "DASHBOARD_HOLD"
		} else if attention.DegradedEvents24H > 0 || attention.TargetsAttention > 0 ||
			attention.PendingApprovals > 0 || attention.AnalysisAttention > 0 {
			state = "partial"
			reason = "DASHBOARD_ATTENTION"
		}

		writeJSON(w, http.StatusOK, map[string]any{
			"schema_version":    "masi-web-dashboard/v1",
			"snapshot_id":       fmt.Sprintf("dashboard-%d", snapshotUnixMS),
			"snapshot_unix_ms":  snapshotUnixMS,
			"generation":        snapshotUnixMS,
			"state":             state,
			"actor_ref":         actorRefOf(session),
			"authorized_scopes": scopes,
			"counts":            json.RawMessage(countsRaw),
			"recent_alerts":     json.RawMessage(alertsRaw),
			"active_operations": json.RawMessage(operationsRaw),
			"target_health":     json.RawMessage(targetsRaw),
			"reason_code":       reason,
		})
	}
}
