package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/a2a"
	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/firewall"
	"masi-nids/control-go/internal/governance"
	"masi-nids/control-go/internal/model"
	"masi-nids/control-go/internal/plugin"
	"masi-nids/control-go/internal/pluginstat"
	"masi-nids/control-go/internal/ruleobs"
	"masi-nids/control-go/internal/security"
	"masi-nids/control-go/internal/target"
)

// Deps carries the wired subdomain services the API projects. Handlers ONLY
// use public service methods + read queries; no handler ever opens a
// transaction (services own transaction boundaries).
type Deps struct {
	Pool           *db.Pool
	Proposals      *governance.ProposalService
	Decisions      *governance.DecisionService
	Intents        *governance.IntentService
	Mapping        *security.RoleScopeMapping
	Cursor         *CursorCodec
	Secure         bool
	RequestTimeout time.Duration
	AcceptMutation func() bool
	// TestLogin enables test-only session bootstrap routes. POST /oidc/test-login
	// supports harness-supplied identities; when no OIDC client is configured,
	// GET /oidc/login mints one fixed loopback-only browser fixture identity.
	// Production never wires either behavior. Authorization still flows through Mapping.
	TestLogin bool

	// Wired subdomain services (constructed with the real pool at startup). The
	// read projections above currently query the pool directly; mutations and
	// future handlers consume these services. Holding them here makes the full
	// subdomain set part of the live process and available to the API layer.
	FirewallRevisions  *firewall.RevisionService
	FirewallOverlays   *firewall.OverlayService
	FirewallActivation *firewall.ActivationService
	TargetRegistry     *target.RegistryService
	FleetCoordinator   *target.FleetCoordinator
	Assignment         *target.AssignmentService
	ModelRevisions     *model.RevisionService
	ModelIncarnation   *model.IncarnationService
	ModelRollout       *model.RolloutService
	PluginCatalog      *plugin.CatalogService
	PluginBinding      *plugin.BindingService
	PluginStat         *pluginstat.Service
	RuleObs            *ruleobs.Projector
	Preflight          *governance.PreflightService
	FirewallCompiler   governance.EdgeEffectPreflightClient
	Dispatcher         *governance.Dispatcher
	Reconcile          *governance.ReconcileService
	Capture            *governance.CaptureService
	Analysis           *a2a.Client
	Invalidations      InvalidationPublisher
}

// Router assembles the same-origin /api surface + /events SSE + OIDC.
func Router(deps Deps, oidc *OIDCClient, store *SessionStore, hub *Hub, allowedOrigin string) http.Handler {
	r := chi.NewRouter()

	// OIDC (no session required to start login; callback verifies). A test-profile
	// process without an OIDC client exposes a fixed, loopback-only browser
	// bootstrap so the human-facing CTA can enter the same bounded fixture session
	// used by the browser harness. Production retains the normal OIDC-only path.
	login := handleLogin(oidc, store, deps.Secure)
	if deps.TestLogin && oidc == nil {
		login = handleBrowserTestLogin(store, deps.Secure)
	}
	r.With(requestDeadline(deps.RequestTimeout)).Get("/oidc/login", login)
	r.With(requestDeadline(deps.RequestTimeout)).Get("/oidc/callback", handleCallback(oidc, store, deps.Secure))
	// Test-only session minting. Registered solely in the test runtime profile
	// (deps.TestLogin); production never wires it. Mints identity only —
	// authorization still flows through the RoleScopeMapping.
	if deps.TestLogin {
		r.With(requestDeadline(deps.RequestTimeout)).Post("/oidc/test-login", handleTestLogin(store, deps.Secure, allowedOrigin))
	}
	r.With(requestDeadline(deps.RequestTimeout), requireMutationGuard(store, allowedOrigin)).Post("/oidc/logout", handleLogout(store, deps.Secure))

	// Session bootstrap: the SPA reads its CSRF token + actor here (the ONLY
	// credential in the browser is the HttpOnly cookie).
	r.With(requestDeadline(deps.RequestTimeout)).Get("/api/session", handleSession(store))

	// Read projections (session required; server-side scope filtering).
	r.Group(func(r chi.Router) {
		r.Use(requestDeadline(deps.RequestTimeout))
		r.Use(requireSession(store))
		r.Get("/api/dashboard", handleGetDashboard(deps))
		r.Get("/api/events", handleListEvents(deps))
		r.Get("/api/events/{id}", handleGetEvent(deps))
		r.Get("/api/incidents", handleListIncidents(deps))
		r.Get("/api/evidence", handleListEvidence(deps))
		r.Get("/api/evidence/captures", handleListBoundedCaptures(deps))
		r.Get("/api/effects/proposals", handleListProposals(deps))
		r.Get("/api/effects/proposals/{proposalID}", handleGetEffectProposal(deps))
		r.Get("/api/effects/decisions", handleListDecisions(deps))
		r.Get("/api/effects/decisions/{decisionID}", handleGetEffectDecision(deps))
		r.Get("/api/effects/intents", handleListIntents(deps))
		r.Get("/api/effects/intents/{intentID}", handleGetEffectIntent(deps))
		r.Get("/api/effects/operations/{operationID}", handleGetEffectOperation(deps))
		r.Get("/api/effects/operations/{operationID}/attempts", handleGetEffectAttempts(deps))
		r.Get("/api/effects/operations/{operationID}/readback", handleGetEffectReadback(deps))
		r.Get("/api/targets", handleListTargets(deps))
		r.Get("/api/targets/{targetID}", handleGetTarget(deps))
		r.Get("/api/targets/{targetID}/observation", handleGetTargetObservation(deps))
		r.Get("/api/targets/{targetID}/operations/{operationID}", handleGetTargetOperation(deps))
		r.Get("/api/targets/candidates/{targetID}/diff", handleGetTargetCandidateDiff(deps))
		r.Get("/api/fleet/operations", handleListFleetOperations(deps))
		r.Get("/api/fleet/operations/{fleetID}", handleGetFleetOperation(deps))
		r.Get("/api/firewall/revisions", handleListFirewallRevisions(deps))
		r.Get("/api/firewall/revisions/{revisionID}", handleGetFirewallRevision(deps))
		r.Get("/api/firewall/revisions/{revisionID}/inspection", handleInspectFirewallRevision(deps))
		r.Get("/api/firewall/bindings", handleListFirewallBindings(deps))
		r.Get("/api/firewall/activations/{operationID}", handleGetFirewallActivation(deps))
		r.Get("/api/rule-effectiveness", handleListRuleEffectiveness(deps))
		r.Get("/api/models/revisions", handleListModelRevisions(deps))
		r.Get("/api/models/bindings", handleListModelBindings(deps))
		r.Get("/api/models/rollout-groups", handleListModelRolloutGroups(deps))
		r.Get("/api/models/rollout-groups/{groupID}", handleGetModelRolloutGroup(deps))
		r.Get("/api/models/incarnations/current", handleGetModelIncarnation(deps))
		r.Get("/api/models/pools", handleListModelPools(deps))
		r.Get("/api/models/pools/{poolID}", handleGetModelPool(deps))
		r.Get("/api/models/operations/{operationID}", handleGetModelOperation(deps))
		r.Get("/api/plugins", handleListPlugins(deps))
		r.Get("/api/plugins/statistics/definitions", handleListPluginStatDefinitions(deps))
		r.Get("/api/plugins/statistics/definitions/{definitionID}", handleGetPluginStatDefinition(deps))
		r.Get("/api/plugins/statistics/definitions/{definitionID}/history", handleGetPluginStatDefinitionHistory(deps))
		r.Get("/api/plugins/statistics/runs", handleListPluginStatRuns(deps))
		r.Get("/api/plugins/statistics/runs/{runID}", handleGetPluginStatRun(deps))
		r.Get("/api/plugins/statistics/current", handleListPluginStatsCurrent(deps))
		r.Get("/api/plugins/statistics/schedules", handleListPluginStatSchedules(deps))
		r.Get("/api/plugins/statistics/schedules/{scheduleID}", handleGetPluginStatSchedule(deps))
		r.Get("/api/plugins/statistics/schedules/{scheduleID}/history", handleGetPluginStatScheduleHistory(deps))
		r.Get("/api/plugins/statistics/artifacts/{artifactID}", handleGetPluginStatArtifact(deps))
		r.Get("/api/analysis/tasks", handleListAnalysisTasks(deps))
		r.Get("/api/analysis/artifacts", handleListAnalysisArtifacts(deps))
		r.Get("/api/analysis/artifacts/{artifactID}", handleGetAnalysisArtifact(deps))
		r.Get("/api/audit", handleListAudit(deps))
	})

	// Mutations: session + Origin + CSRF + server-side scope authorization.
	r.Group(func(r chi.Router) {
		r.Use(requestDeadline(deps.RequestTimeout))
		r.Use(requireMutationAdmission(deps.AcceptMutation))
		r.Use(requireMutationGuard(store, allowedOrigin))
		r.Use(requireMutationIdempotency(deps.Pool))
		r.Post("/api/effects/proposals", handleCreateProposal(deps))
		r.Post("/api/effects/proposals/{proposalID}/supersede", handleSupersedeProposal(deps))
		r.Post("/api/effects/decisions", handleRecordDecision(deps))
		r.Post("/api/effects/intents", handleCreateEffectIntent(deps))
		r.Post("/api/effects/operations/{operationID}/reconcile", handleReconcileEffectOperation(deps))
		r.Post("/api/evidence/captures", handleRegisterBoundedCapture(deps))
		r.Post("/api/evidence/captures/{captureID}/intents", handleCreateBoundedCaptureIntent(deps))
		r.Post("/api/plugins/statistics/runs", handleStartPluginStatRun(deps))
		r.Post("/api/targets", handleRegisterTarget(deps))
		r.Post("/api/targets/candidates/import", handleImportTargetCandidate(deps))
		r.Post("/api/targets/{targetID}/verify", handleVerifyTarget(deps))
		r.Post("/api/targets/{targetID}/activate", handleTargetLifecycle(deps, true))
		r.Post("/api/targets/{targetID}/assign", handleAssignTarget(deps))
		r.Post("/api/targets/{targetID}/drain", handleTargetOperationalLifecycle(deps, target.StatusDraining))
		r.Post("/api/targets/{targetID}/disable", handleTargetOperationalLifecycle(deps, target.StatusDisabled))
		r.Post("/api/targets/{targetID}/quarantine", handleTargetOperationalLifecycle(deps, target.StatusQuarantined))
		r.Post("/api/targets/{targetID}/retire", handleTargetLifecycle(deps, false))
		r.Post("/api/fleet/operations", handleCreateFleetOperation(deps))
		r.Post("/api/fleet/operations/{fleetID}/advance", handleAdvanceFleetWave(deps))
		r.Post("/api/fleet/operations/{fleetID}/rollback", handleRollbackFleetOperation(deps))
		r.Post("/api/firewall/revisions", handleCreateFirewallRevision(deps))
		r.Post("/api/firewall/revisions/{revisionID}/activation-proposals", handleCreateFirewallActivationProposal(deps, false))
		r.Post("/api/firewall/activations/{operationID}/rollback-proposals", handleCreateFirewallActivationProposal(deps, true))
		r.Post("/api/firewall/activation-proposals/{proposalID}/decisions", handleFirewallProposalDecision(deps))
		r.Post("/api/firewall/activations", handlePrepareFirewallActivation(deps))
		r.Post("/api/firewall/overlays", handleCreateFirewallOverlay(deps))
		r.Post("/api/models/revisions", handleRegisterModelRevision(deps))
		r.Post("/api/models/revisions/{revisionID}/revoke", handleRevokeModelRevision(deps))
		r.Post("/api/models/rollout-groups", handleCreateModelRolloutGroup(deps))
		r.Post("/api/models/rollout-groups/{groupID}/advance", handleAdvanceModelRolloutGroup(deps))
		r.Post("/api/models/rollout-groups/{groupID}/rollback", handleCreateModelRollbackGroup(deps))
		r.Post("/api/models/operations/{operationID}/recover", handleRecoverModelOperation(deps))
		r.Post("/api/models/incarnations/rotate", handleRotateModelIncarnation(deps))
		r.Post("/api/models/incarnations/{incarnationID}/enable-writer", handleEnableModelWriter(deps))
		r.Post("/api/plugins", handleRegisterPluginManifest(deps))
		r.Post("/api/plugins/{pluginID}/qualifications", handleQualifyPlugin(deps))
		r.Post("/api/plugins/{pluginID}/bindings", handleActivatePluginBinding(deps))
		r.Post("/api/plugins/{pluginID}/drain", handlePluginBindingLifecycle(deps, "drain"))
		r.Post("/api/plugins/{pluginID}/revoke", handlePluginBindingLifecycle(deps, "revoke"))
		r.Post("/api/plugins/{pluginID}/rollback", handlePluginBindingLifecycle(deps, "rollback"))
		r.Post("/api/plugins/statistics/definitions", handleRegisterPluginStatDefinition(deps))
		r.Post("/api/plugins/statistics/schedules", handleCreatePluginStatSchedule(deps))
		r.Post("/api/plugins/statistics/schedules/{scheduleID}/revise", handleRevisePluginStatSchedule(deps, false))
		r.Post("/api/plugins/statistics/schedules/{scheduleID}/disable", handleRevisePluginStatSchedule(deps, true))
		r.Post("/api/analysis/tasks", handleSubmitAnalysisTask(deps))
		r.Post("/api/analysis/tasks/{taskID}/poll", handlePollAnalysisTask(deps))
	})

	// SSE (read-only stream; session + origin verified inside).
	r.Get("/events", hub.ServeEvents(store, deps.Mapping, allowedOrigin))
	return r
}

func requestDeadline(timeout time.Duration) func(http.Handler) http.Handler {
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			ctx, cancel := context.WithTimeout(r.Context(), timeout)
			defer cancel()
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

func requireMutationAdmission(accept func() bool) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if accept != nil && !accept() {
				WriteError(w, http.StatusServiceUnavailable, "PROCESS_NOT_ACCEPTING_MUTATIONS")
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

func requireSession(st *SessionStore) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			s, ok := st.SessionFromRequest(r)
			if !ok {
				WriteError(w, http.StatusUnauthorized, "UNAUTHENTICATED")
				return
			}
			ctx := context.WithValue(r.Context(), sessionKey{}, s)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

// ---- OIDC handlers ----

func handleLogin(oidc *OIDCClient, store *SessionStore, secure bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if oidc == nil {
			WriteError(w, http.StatusServiceUnavailable, "OIDC_NOT_CONFIGURED")
			return
		}
		state, err := randomState()
		if err != nil {
			WriteError(w, http.StatusInternalServerError, "STATE_MINT_ERROR")
			return
		}
		nonce, err := randomB64(24)
		if err != nil {
			WriteError(w, http.StatusInternalServerError, "NONCE_MINT_ERROR")
			return
		}
		redirect, verifier, err := oidc.AuthCodeURL(state, nonce)
		if err != nil {
			WriteError(w, http.StatusInternalServerError, "AUTH_URL_ERROR")
			return
		}
		if err := store.BeginOIDC(OIDCTransaction{
			State: state, Nonce: nonce, Verifier: verifier, ExpiresAt: time.Now().Add(10 * time.Minute),
		}); err != nil {
			WriteError(w, http.StatusServiceUnavailable, "OIDC_TRANSACTION_BOUND")
			return
		}
		// The browser carries only the one-time state. Nonce and PKCE verifier stay
		// in the bounded server-side transaction store.
		http.SetCookie(w, &http.Cookie{
			Name: "masi_oidc_tx", Value: state, Path: "/oidc",
			HttpOnly: true, Secure: secure, SameSite: http.SameSiteLaxMode, MaxAge: 600,
		})
		http.Redirect(w, r, redirect, http.StatusSeeOther)
	}
}

func handleCallback(oidc *OIDCClient, store *SessionStore, secure bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if oidc == nil {
			WriteError(w, http.StatusServiceUnavailable, "OIDC_NOT_CONFIGURED")
			return
		}
		tx, err := r.Cookie("masi_oidc_tx")
		if err != nil {
			WriteError(w, http.StatusUnauthorized, "MISSING_TX_COOKIE")
			return
		}
		state := tx.Value
		if r.URL.Query().Get("state") != state || !stateValid(state) {
			WriteError(w, http.StatusUnauthorized, "STATE_MISMATCH")
			return
		}
		code := r.URL.Query().Get("code")
		if code == "" {
			WriteError(w, http.StatusUnauthorized, "MISSING_CODE")
			return
		}
		preAuth, err := store.ConsumeOIDC(state)
		if err != nil {
			WriteError(w, http.StatusUnauthorized, "OIDC_TRANSACTION_INVALID")
			return
		}
		idToken, err := oidc.Exchange(r.Context(), code, preAuth.Verifier)
		if err != nil {
			WriteError(w, http.StatusUnauthorized, "TOKEN_EXCHANGE_FAILED")
			return
		}
		claims, err := oidc.VerifyIDToken(r.Context(), idToken, preAuth.Nonce)
		if err != nil {
			WriteError(w, http.StatusUnauthorized, "ID_TOKEN_REJECTED")
			return
		}
		stepUp := security.CanonicalStepUp(claims.Amr)
		s, err := store.Create(*claims, string(stepUp))
		if err != nil {
			WriteError(w, http.StatusServiceUnavailable, "SESSION_BOUND")
			return
		}
		// Clear the tx cookie and set the session.
		http.SetCookie(w, &http.Cookie{Name: "masi_oidc_tx", Value: "", Path: "/oidc", Secure: secure, HttpOnly: true, SameSite: http.SameSiteLaxMode, MaxAge: -1})
		SetSessionCookie(w, s, secure, store.cookieName)
		http.Redirect(w, r, "/", http.StatusFound)
	}
}

func handleLogout(store *SessionStore, secure bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if s, ok := store.SessionFromRequest(r); ok {
			store.Drop(s.ID)
		}
		ClearSessionCookie(w, secure, store.cookieName)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "logged_out"})
	}
}

func handleSession(store *SessionStore) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		s, ok := store.SessionFromRequest(r)
		if !ok {
			WriteError(w, http.StatusUnauthorized, "UNAUTHENTICATED")
			return
		}
		actor := security.Actor{Issuer: s.Actor.Issuer, Subject: s.Actor.Subject}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"schema_version": "masi-web-projection/v1",
			"actor_ref":      actor.String(),
			"csrf_token":     s.CSRFToken,
			"expires_at":     s.ExpiresAt.Unix(),
			"step_up":        s.StepUpType,
		})
	}
}

// ---- Read projections ----

func readAuthorization(r *http.Request, deps Deps) (*Session, []string, error) {
	s, err := sessionFromContext(r.Context())
	if err != nil {
		return nil, nil, err
	}
	if deps.Mapping == nil {
		return nil, nil, errors.New("authorization mapping unavailable")
	}
	actor := security.Actor{Issuer: s.Actor.Issuer, Subject: s.Actor.Subject}
	if err := actor.Validate(); err != nil {
		return nil, nil, err
	}
	scopes, err := deps.Mapping.ScopeIDs(actor)
	if err != nil {
		return nil, nil, err
	}
	return s, scopes, nil
}

func actorRefOf(s *Session) string {
	return security.Actor{Issuer: s.Actor.Issuer, Subject: s.Actor.Subject}.String()
}

// scopedJSONList centralizes the fail-closed SQL scope predicate and bounded,
// authenticated keyset cursor contract for read projections. countSQL uses $1
// for the authorized scope array. itemSQL must use $1 for scopes, $2 for the
// page limit and $3 for the exclusive stable string key, and return (JSON,key).
// Cursor kind+scope binding prevents cross-resource replay; no OFFSET/deep-page
// scan is used.
func scopedJSONList(deps Deps, kind, countSQL, itemSQL string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		s, scopes, err := readAuthorization(r, deps)
		if err != nil {
			WriteError(w, http.StatusForbidden, "SCOPE_DENIED")
			return
		}
		pageSize, err := ParsePageSize(r)
		if err != nil {
			WriteError(w, http.StatusBadRequest, "PAGE_SIZE_OUT_OF_RANGE")
			return
		}
		if deps.Cursor == nil {
			WriteError(w, http.StatusServiceUnavailable, "CURSOR_CODEC_UNAVAILABLE")
			return
		}
		cur, err := deps.Cursor.Decode(r.URL.Query().Get("cursor"))
		if err != nil {
			WriteError(w, http.StatusBadRequest, "CURSOR_MALFORMED")
			return
		}
		scopeHash := digestText(strings.Join(scopes, "\x00"))
		if cur.Kind != "" && (cur.Kind != kind || cur.ScopeHash != scopeHash || !cur.Forward || cur.LastID != 0) {
			WriteError(w, http.StatusBadRequest, "CURSOR_SCOPE_MISMATCH")
			return
		}
		var total int64
		if err := deps.Pool.Pool.QueryRow(r.Context(), countSQL, scopes).Scan(&total); err != nil {
			WriteError(w, http.StatusInternalServerError, "COUNT_FAILED")
			return
		}
		rows, err := deps.Pool.Pool.Query(r.Context(), itemSQL, scopes, pageSize+1, cur.LastKey)
		if err != nil {
			WriteError(w, http.StatusInternalServerError, "QUERY_FAILED")
			return
		}
		defer rows.Close()
		items := make([]any, 0, pageSize+1)
		lastKey := ""
		for rows.Next() {
			var raw []byte
			var key string
			if err := rows.Scan(&raw, &key); err != nil || key == "" || len(key) > 128 {
				WriteError(w, http.StatusInternalServerError, "SCAN_FAILED")
				return
			}
			var item any
			if err := json.Unmarshal(raw, &item); err != nil {
				WriteError(w, http.StatusInternalServerError, "PROJECTION_INVALID")
				return
			}
			items = append(items, item)
			if len(items) == pageSize {
				lastKey = key
			}
		}
		if err := rows.Err(); err != nil {
			WriteError(w, http.StatusInternalServerError, "QUERY_FAILED")
			return
		}
		p := NewProjection(kind, ProjectionCurrent, pageSize, total, 1, actorRefOf(s), strings.Join(scopes, ","))
		p.Items = items
		if len(items) == pageSize+1 {
			items = items[:pageSize]
			p.Items = items
			p.Cursor, err = deps.Cursor.Encode(Cursor{LastKey: lastKey, Forward: true, Generation: 1,
				Kind: kind, ScopeHash: scopeHash})
			if err != nil {
				WriteError(w, http.StatusInternalServerError, "CURSOR_ENCODE_FAILED")
				return
			}
		}
		WriteProjection(w, p)
	}
}

func handleListIncidents(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "incident",
		`SELECT count(*) FROM incidents WHERE scope=ANY($1)`,
		`SELECT jsonb_build_object('incident_id',incident_id,'severity',severity,'status',status,
		 'created_at_unix_ms',(extract(epoch from created_at)*1000)::bigint)
			,incident_id FROM incidents WHERE scope=ANY($1)
			 AND ($3::text='' OR incident_id>$3) ORDER BY incident_id LIMIT $2`)
}

func handleListEvidence(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "evidence",
		`SELECT count(*) FROM evidence_refs WHERE scope=ANY($1)`,
		`SELECT jsonb_build_object('evidence_id',evidence_id,'kind',kind,
		 'reference_digest',reference_digest,'source',source,
		 'created_at_unix_ms',(extract(epoch from created_at)*1000)::bigint)
			,evidence_id FROM evidence_refs WHERE scope=ANY($1)
			 AND ($3::text='' OR evidence_id>$3) ORDER BY evidence_id LIMIT $2`)
}

func handleListFleetOperations(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "fleet-operation",
		`SELECT count(*) FROM fleet_operations WHERE scope=ANY($1)`,
		`SELECT jsonb_build_object('fleet_operation_id',fo.fleet_operation_id,
		 'target_set_digest',fo.target_set_digest,'wave_count',fo.wave_count,'waves',fo.waves,
		 'parent_intent_id',fo.parent_intent_id,'aggregate_status',fo.aggregate_status,
		 'child_intents',COALESCE((SELECT jsonb_agg(jsonb_build_object(
		   'target_id',fc.target_id,'intent_id',fc.child_intent_id,'wave_index',fc.wave_index,
		   'status',fc.status,'reason_code',fc.reason_code,'gate_open',fc.gate_open)
		   ORDER BY fc.wave_index,fc.target_id) FROM fleet_child_intents fc
		   WHERE fc.fleet_operation_id=fo.fleet_operation_id),'[]'::jsonb),
		 'created_at_unix_ms',fo.created_at_unix_ms),fo.fleet_operation_id
		 FROM fleet_operations fo WHERE fo.scope=ANY($1)
		 AND ($3::text='' OR fo.fleet_operation_id>$3)
		 ORDER BY fo.fleet_operation_id LIMIT $2`)
}

func handleListModelRevisions(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "model-revision",
		`SELECT count(*) FROM model_revisions WHERE scope=ANY($1)`,
		`SELECT jsonb_build_object('model_revision_id',model_revision_id,
		 'model_revision_digest',model_revision_digest,'qualification_status',qualification_status,
		 'reader_runtime_profile',reader_runtime_profile),model_revision_id
			 FROM model_revisions WHERE scope=ANY($1) AND ($3::text='' OR model_revision_id>$3)
			 ORDER BY model_revision_id LIMIT $2`)
}

func handleListModelBindings(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "model-binding",
		`SELECT count(*) FROM shard_bindings WHERE scope=ANY($1)`,
		`SELECT jsonb_build_object('shard_id',shard_id,'logical_pool_id',logical_pool_id,
		 'current_generation',current_generation,'current_binding_generation',current_binding_generation,
		 'current_revision_id',current_revision_id,'previous_generation',previous_generation,
		 'previous_binding_generation',previous_binding_generation,'previous_revision_id',previous_revision_id,
		 'route_epoch',route_epoch,'resume_state',resume_state,'loaded',loaded,'ready',ready),shard_id
		 FROM shard_bindings WHERE scope=ANY($1) AND ($3::text='' OR shard_id>$3) ORDER BY shard_id LIMIT $2`)
}

func handleListModelRolloutGroups(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "model-rollout-group",
		`SELECT count(*) FROM model_rollout_groups WHERE scope=ANY($1)`,
		`SELECT jsonb_build_object('group_id',g.group_id,'logical_pool_id',g.logical_pool_id,
		 'target_generation',g.target_generation,'target_revision_id',g.target_revision_id,
		 'ordered_shards',g.ordered_shards,'status',g.status,'next_index',g.next_index,
		 'shards',COALESCE((SELECT jsonb_agg(jsonb_build_object('shard_index',s.shard_index,
		   'shard_id',s.shard_id,'operation_id',s.operation_id,'status',s.status,
		   'current_generation',s.current_generation,'route_epoch',s.route_epoch,
		   'reason_code',s.reason_code) ORDER BY s.shard_index)
			   FROM model_rollout_group_shards s WHERE s.group_id=g.group_id),'[]'::jsonb)),g.group_id
		 FROM model_rollout_groups g WHERE g.scope=ANY($1)
		 AND ($3::text='' OR g.group_id>$3) ORDER BY g.group_id LIMIT $2`)
}

func handleListPlugins(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "plugin",
		`SELECT count(DISTINCT plugin_id) FROM plugin_manifests WHERE scope=ANY($1)`,
		`SELECT jsonb_build_object('plugin_id',plugin_id,'manifest_id',manifest_id,
		 'manifest_revision',manifest_revision,'manifest_digest',manifest_digest,'kind',kind,
		 'runtime_profile',runtime_profile),plugin_id FROM (
		   SELECT DISTINCT ON (plugin_id) * FROM plugin_manifests
		   WHERE scope=ANY($1) ORDER BY plugin_id,manifest_revision DESC
		 ) p WHERE ($3::text='' OR plugin_id>$3) ORDER BY plugin_id LIMIT $2`)
}

func handleListPluginStatDefinitions(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "plugin-statistics-definition",
		`SELECT count(*) FROM plugin_statistics_definitions WHERE scope=ANY($1)`,
		`SELECT jsonb_build_object('definition_id',definition_id,'definition_digest',definition_digest,
		 'plugin_id',plugin_id,'binding_generation',binding_generation,'producer_kind',producer_kind,
		 'display_hint',display_hint,'revoked',revoked),definition_id
			 FROM plugin_statistics_definitions WHERE scope=ANY($1)
			 AND ($3::text='' OR definition_id>$3) ORDER BY definition_id LIMIT $2`)
}

func handleListPluginStatRuns(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "plugin-statistics-run",
		`SELECT count(*) FROM plugin_statistic_runs WHERE scope=ANY($1)`,
		`SELECT jsonb_build_object('run_id',run_id,'definition_id',definition_id,'status',status,
		 'binding_generation',binding_generation,'started_at_unix_ms',started_at_unix_ms,
		 'finished_at_unix_ms',finished_at_unix_ms,'artifact_id',artifact_id),run_id
			 FROM plugin_statistic_runs WHERE scope=ANY($1) AND ($3::text='' OR run_id>$3)
			 ORDER BY run_id LIMIT $2`)
}

func handleListPluginStatSchedules(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "plugin-statistics-schedule",
		`SELECT count(*) FROM (SELECT DISTINCT ON(schedule_id) schedule_id FROM plugin_statistic_schedules
		 WHERE scope=ANY($1) ORDER BY schedule_id,schedule_revision DESC) latest`,
		`SELECT jsonb_build_object('schedule_id',schedule_id,'schedule_revision',schedule_revision,
		 'definition_id',definition_id,'definition_digest',definition_digest,
		 'interval_seconds',interval_seconds,'disabled',disabled,'data_class',data_class,
		 'target_set_digest',target_set_digest,'reason_code',reason_code),schedule_id
		 FROM (SELECT DISTINCT ON(schedule_id) * FROM plugin_statistic_schedules
		 WHERE scope=ANY($1) ORDER BY schedule_id,schedule_revision DESC) latest
		 WHERE ($3::text='' OR schedule_id>$3) ORDER BY schedule_id LIMIT $2`)
}

func handleListEvents(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		s, scopes, err := readAuthorization(r, deps)
		if err != nil {
			WriteError(w, http.StatusForbidden, "SCOPE_DENIED")
			return
		}
		pageSize, err := ParsePageSize(r)
		if err != nil {
			WriteError(w, http.StatusBadRequest, "PAGE_SIZE_OUT_OF_RANGE")
			return
		}
		if deps.Cursor == nil {
			WriteError(w, http.StatusServiceUnavailable, "CURSOR_CODEC_UNAVAILABLE")
			return
		}
		cur, err := deps.Cursor.Decode(r.URL.Query().Get("cursor"))
		if err != nil {
			WriteError(w, http.StatusBadRequest, "CURSOR_MALFORMED")
			return
		}
		scopeHash := digestText(strings.Join(scopes, "\x00"))
		if cur.Kind != "" && (cur.Kind != "event" || cur.ScopeHash != scopeHash) {
			WriteError(w, http.StatusBadRequest, "CURSOR_SCOPE_MISMATCH")
			return
		}
		var total int64
		if err := deps.Pool.Pool.QueryRow(r.Context(), `SELECT count(*) FROM events WHERE scope = ANY($1)`, scopes).Scan(&total); err != nil {
			WriteError(w, http.StatusInternalServerError, "COUNT_FAILED")
			return
		}
		rows, err := deps.Pool.Pool.Query(r.Context(), `
					SELECT event_id, shard_id, model_control_incarnation_id, route_epoch,
					       commit_status,quality,reason_code,decision,predicted_label,
					       out_of_distribution,abstain,execution_status,
					       (EXTRACT(EPOCH FROM event_time)*1000)::bigint
					FROM events
					WHERE scope = ANY($1)
					  AND ($2::bigint = 0 OR (EXTRACT(EPOCH FROM event_time)*1000)::bigint < $2
					       OR ((EXTRACT(EPOCH FROM event_time)*1000)::bigint = $2 AND event_id < $3))
					ORDER BY event_time DESC,event_id DESC LIMIT $4`, scopes, cur.LastID, cur.LastKey, pageSize+1)
		if err != nil {
			WriteError(w, http.StatusInternalServerError, "QUERY_FAILED")
			return
		}
		defer rows.Close()
		items := []any{}
		var lastUnixMS int64
		var lastEventID string
		for rows.Next() {
			var id, shard, inc, commit, quality, reason, decision, execution string
			var epoch, label, ts int64
			var ood, abstain bool
			if err := rows.Scan(&id, &shard, &inc, &epoch, &commit, &quality, &reason, &decision,
				&label, &ood, &abstain, &execution, &ts); err != nil {
				WriteError(w, http.StatusInternalServerError, "SCAN_FAILED")
				return
			}
			items = append(items, map[string]any{
				"event_id": id, "shard_id": shard,
				"model_control_incarnation_id": inc, "route_epoch": epoch,
				"commit_status": commit, "event_time_unix_ms": ts,
				"quality": quality, "reason_code": reason, "decision": decision,
				"predicted_label": label, "out_of_distribution": ood, "abstain": abstain,
				"execution_status": execution,
			})
			if len(items) == pageSize {
				lastUnixMS = ts
				lastEventID = id
			}
		}
		next := ""
		if len(items) == pageSize+1 {
			items = items[:pageSize]
			next, err = deps.Cursor.Encode(Cursor{LastID: lastUnixMS, LastKey: lastEventID,
				Forward: false, Kind: "event", ScopeHash: scopeHash})
			if err != nil {
				WriteError(w, http.StatusInternalServerError, "CURSOR_ENCODE_FAILED")
				return
			}
		}
		actorRef := security.Actor{Issuer: s.Actor.Issuer, Subject: s.Actor.Subject}.String()
		p := NewProjection("event", ProjectionCurrent, pageSize, total, 1, actorRef, strings.Join(scopes, ","))
		p.Items = items
		p.Cursor = next
		WriteProjection(w, p)
	}
}

func handleGetEvent(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		s, scopes, err := readAuthorization(r, deps)
		if err != nil {
			WriteError(w, http.StatusForbidden, "SCOPE_DENIED")
			return
		}
		id := chi.URLParam(r, "id")
		if id == "" || len(id) > 128 {
			WriteError(w, http.StatusBadRequest, "EVENT_ID_MALFORMED")
			return
		}
		var shard, inc, commit, quality, reason, decision, execution string
		var epoch, label, ts int64
		var ood, abstain bool
		err = deps.Pool.Pool.QueryRow(r.Context(), `
					SELECT shard_id, model_control_incarnation_id, route_epoch, commit_status,quality,reason_code,
					       decision,predicted_label,out_of_distribution,abstain,execution_status,
					       (extract(epoch from event_time)*1000)::bigint
				FROM events WHERE event_id = $1 AND scope = ANY($2)`, id, scopes).
			Scan(&shard, &inc, &epoch, &commit, &quality, &reason, &decision, &label, &ood, &abstain, &execution, &ts)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				WriteError(w, http.StatusNotFound, "EVENT_NOT_FOUND")
				return
			}
			WriteError(w, http.StatusInternalServerError, "QUERY_FAILED")
			return
		}
		actorRef := security.Actor{Issuer: s.Actor.Issuer, Subject: s.Actor.Subject}.String()
		p := NewProjection("event", ProjectionCurrent, 1, 1, 1, actorRef, strings.Join(scopes, ","))
		p.ResourceID = id
		p.Items = []any{map[string]any{
			"event_id": id, "shard_id": shard,
			"model_control_incarnation_id": inc, "route_epoch": epoch,
			"commit_status": commit, "event_time_unix_ms": ts,
			"quality": quality, "reason_code": reason, "decision": decision,
			"predicted_label": label, "out_of_distribution": ood, "abstain": abstain,
			"execution_status": execution,
		}}
		WriteProjection(w, p)
	}
}

func handleListProposals(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "proposal",
		`SELECT count(*) FROM effect_proposals WHERE scope=ANY($1)`,
		`SELECT jsonb_build_object('proposal_id',proposal_id,'risk_level',risk_level,
		 'effect_kind',effect_kind,'reason_code',reason_code,
		 'created_at_unix_ms',(extract(epoch from created_at)*1000)::bigint),proposal_id
		 FROM effect_proposals WHERE scope=ANY($1)
		 AND ($3::text='' OR proposal_id>$3) ORDER BY proposal_id LIMIT $2`)
}

func handleListDecisions(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "decision",
		`SELECT count(*) FROM effect_decisions d JOIN effect_proposals p ON p.proposal_id=d.proposal_id
		 WHERE p.scope=ANY($1)`,
		`SELECT jsonb_build_object('decision_id',d.decision_id,'proposal_id',d.proposal_id,
		 'proposal_digest',d.proposal_digest,'risk_level',d.risk_level,'decision',d.decision,
		 'decision_digest',d.decision_digest,'expires_at_unix_ms',d.expires_at_unix_ms,
		 'created_at_unix_ms',d.created_at_unix_ms,'reason_code',d.reason_code),d.decision_id
		 FROM effect_decisions d JOIN effect_proposals p ON p.proposal_id=d.proposal_id
		 WHERE p.scope=ANY($1) AND ($3::text='' OR d.decision_id>$3)
		 ORDER BY d.decision_id LIMIT $2`)
}

func handleListIntents(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "intent",
		`SELECT count(*) FROM effect_intents i JOIN effect_proposals p ON p.proposal_id=i.proposal_id WHERE p.scope=ANY($1)`,
		`SELECT jsonb_build_object('effect_intent_id',i.effect_intent_id,'operation_id',i.operation_id,
		 'target_id',i.target_id,'claim_state',i.claim_state,'deadline_unix_ms',i.deadline_unix_ms,
		 'reason_code',i.reason_code),i.effect_intent_id
		 FROM effect_intents i JOIN effect_proposals p ON p.proposal_id=i.proposal_id
		 WHERE p.scope=ANY($1) AND ($3::text='' OR i.effect_intent_id>$3)
		 ORDER BY i.effect_intent_id LIMIT $2`)
}

func handleListTargets(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "target",
		`SELECT count(*) FROM targets WHERE scope=ANY($1)`,
		`SELECT jsonb_build_object(
			 'target_id',t.target_id,'display_name',t.display_name,'p4runtime_endpoint',t.p4runtime_endpoint,
			 'lifecycle',t.status,'device_id',t.device_id,'role',t.role,
			 'desired_profile_digest',t.desired_profile_digest,'provenance',t.provenance,
			 'assignment_generation',a.assignment_generation,'lease_expires_at_unix_ms',a.expires_at_unix_ms,
			 'lease_state',CASE WHEN a.target_id IS NULL THEN 'unassigned' WHEN a.revoked_at_unix_ms IS NOT NULL THEN 'revoked'
			   WHEN a.expires_at_unix_ms <= (extract(epoch from clock_timestamp())*1000)::bigint THEN 'expired' ELSE 'assigned' END,
			 'application_generation',o.application_generation,'observed_profile_digest',o.profile_digest,
			 'p4info_digest',o.p4info_digest,'freshness',COALESCE(o.freshness,'not-observed'),
			 'profile_alignment',CASE WHEN o.target_id IS NULL THEN 'not-observed'
			   WHEN o.profile_digest=t.desired_profile_digest THEN 'exact' ELSE 'drift' END,
			 'p4_connected',COALESCE(o.p4_connected,false),'primary_actor',COALESCE(o.primary_actor,false),
			 'assignment',CASE WHEN a.target_id IS NULL THEN NULL ELSE jsonb_build_object(
			   'assignment_generation',a.assignment_generation,'incarnation_id',a.incarnation_id,
			   'edge_workload_ref',a.edge_workload_ref,'lease_id',a.lease_id,
			   'issued_at_unix_ms',a.issued_at_unix_ms,'expires_at_unix_ms',a.expires_at_unix_ms,
			   'election_floor',a.election_floor,'election_ceiling',a.election_ceiling,
			   'actor_runtime_epoch',a.actor_runtime_epoch,'application_generation',a.application_generation,
			   'actor_ref',a.actor_ref,'revoked_at_unix_ms',a.revoked_at_unix_ms) END,
			 'observation',CASE WHEN o.target_id IS NULL THEN NULL ELSE jsonb_build_object(
			   'observation_id',o.observation_id,'observation_digest',o.observation_digest,
			   'target_control_incarnation_id',o.target_control_incarnation_id,
			   'assignment_generation',o.assignment_generation,'actor_runtime_epoch',o.actor_runtime_epoch,
			   'application_generation',o.application_generation,'profile_digest',o.profile_digest,
			   'p4info_digest',o.p4info_digest,'pipeline_digest',o.pipeline_digest,
			   'capacity_digest',o.capacity_digest,'capacity_available',o.capacity_available,
			   'lease_valid',o.lease_valid,'p4_connected',o.p4_connected,'primary_actor',o.primary_actor,
			   'pipeline_exact',o.pipeline_exact,'freshness',o.freshness,'reason_code',o.reason_code,
			   'last_successful_read_unix_ms',o.last_successful_read_unix_ms,
			   'observed_at_unix_ms',o.observed_at_unix_ms,'expires_at_unix_ms',o.expires_at_unix_ms) END,
			 'latest_lifecycle_audit',CASE WHEN l.event_id IS NULL THEN NULL ELSE jsonb_build_object(
			   'event_id',l.event_id,'previous_status',l.previous_status,'new_status',l.new_status,
			   'actor_ref',l.actor_ref,'reason_code',l.reason_code,'trace_id',l.trace_id,
			   'occurred_at_unix_ms',l.occurred_at_unix_ms) END),t.target_id
			 FROM targets t
			 LEFT JOIN LATERAL (SELECT x.* FROM target_assignments x WHERE x.target_id=t.target_id
			   ORDER BY x.assignment_generation DESC LIMIT 1) a ON true
			 LEFT JOIN target_capability_observations o ON o.target_id=t.target_id
			 LEFT JOIN LATERAL (SELECT x.* FROM target_lifecycle_events x WHERE x.target_id=t.target_id
			   ORDER BY x.occurred_at_unix_ms DESC,x.event_id DESC LIMIT 1) l ON true
			 WHERE t.scope=ANY($1) AND ($3::text='' OR t.target_id>$3) ORDER BY t.target_id LIMIT $2`)
}

func handleListBoundedCaptures(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "bounded-capture",
		`SELECT count(*) FROM bounded_capture_requests WHERE scope=ANY($1)`,
		`SELECT jsonb_build_object('capture_id',r.capture_id,'target_id',r.target_id,'capture_digest',r.capture_digest,
		 'duration_ms',r.duration_ms,'sample_limit',r.sample_limit,'byte_limit',r.byte_limit,
		 'expires_at_unix_ms',r.expires_at_unix_ms,'state',r.state,'effect_intent_id',r.effect_intent_id,
		 'evidence_id',x.evidence_id,'content_digest',x.content_digest,'observed_samples',x.observed_samples,
		 'observed_bytes',x.observed_bytes,'truncated',x.truncated,'gap',x.gap),r.capture_id
		 FROM bounded_capture_requests r LEFT JOIN bounded_capture_results x ON x.capture_id=r.capture_id
		 WHERE r.scope=ANY($1) AND ($3::text='' OR r.capture_id>$3) ORDER BY r.capture_id LIMIT $2`)
}

func handleListFirewallRevisions(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "firewall-revision",
		`SELECT count(*) FROM firewall_revisions WHERE scope=ANY($1)`,
		`SELECT jsonb_build_object('revision_id',revision_id,'revision_digest',revision_digest,
		 'target_id',target_id,'default_action',default_action,'scope',scope,
		 'created_at_unix_ms',(extract(epoch from created_at)*1000)::bigint),revision_id
		 FROM firewall_revisions WHERE scope=ANY($1)
		 AND ($3::text='' OR revision_id>$3) ORDER BY revision_id LIMIT $2`)
}

func handleListRuleEffectiveness(deps Deps) http.HandlerFunc {
	// Never expose digest/match/IP/five-tuple fields to the browser.
	return scopedJSONList(deps, "rule-effectiveness",
		`SELECT count(*) FROM rule_observations ro JOIN rule_observation_epochs e ON e.epoch_id=ro.epoch_id
		 JOIN effect_intents i ON i.effect_intent_id=e.effect_intent_id
		 JOIN effect_proposals p ON p.proposal_id=i.proposal_id WHERE p.scope=ANY($1)`,
		`SELECT jsonb_build_object(
		 'epoch_id',e.epoch_id,'rule_id',e.rule_id,'target_id',e.target_id,
		 'quality_status',ro.quality_status,'rate',ro.rate,'coverage',ro.coverage,
		 'outcome_status',ro.outcome_status,
		 'installation',jsonb_build_object(
		   'status',e.installation_readback,'observation_epoch',e.observation_epoch,
		   'reset_epoch',e.reset_epoch,'readback',jsonb_build_object(
		     'status',a.status,'expected_entries',a.expected_entries,
		     'observed_entries',a.observed_entries,'mismatched_entries',a.mismatched_entries,
		     'active_bank',a.active_bank)),
		 'dataplane',jsonb_build_object(
		   'formula','direct_delta/eligible_delta','direct_packets',ro.direct_delta_packets,
		   'direct_bytes',ro.direct_delta_bytes,'eligible_packets',ro.eligible_delta_packets,
		   'packet_match_ratio',ro.rate,'quality',ro.quality_status,
		   'quality_reasons',ro.quality_reasons,'coverage',ro.coverage,
		   'read_completed_at_unix_ms',ro.read_completed_at_unix_ms),
		 'outcome',jsonb_build_object(
		   'status',ro.outcome_status,'expected',ro.outcome_expected,'actual',ro.outcome_actual)
		 ),e.epoch_id
		 FROM rule_observations ro JOIN rule_observation_epochs e ON e.epoch_id=ro.epoch_id
		 JOIN effect_intents i ON i.effect_intent_id=e.effect_intent_id
		 JOIN effect_proposals p ON p.proposal_id=i.proposal_id
		 LEFT JOIN LATERAL (
		   SELECT ea.status,ea.expected_entries,ea.observed_entries,ea.mismatched_entries,ea.active_bank
		   FROM effect_attempts ea WHERE ea.intent_id=e.effect_intent_id
		     AND ea.operation_id=e.operation_id AND ea.status='applied'
		   ORDER BY ea.attempt_number DESC LIMIT 1
		 ) a ON true WHERE p.scope=ANY($1)
		 AND ($3::text='' OR e.epoch_id>$3) ORDER BY e.epoch_id LIMIT $2`)
}

func handleListPluginStatsCurrent(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "plugin-statistics-current",
		`SELECT count(*) FROM plugin_statistics_current WHERE scope=ANY($1)`,
		`SELECT jsonb_build_object('definition_id',definition_id,'binding_generation',binding_generation,
		 'artifact_id',artifact_id,'run_id',run_id,'quality',quality,
		 'updated_at_unix_ms',(extract(epoch from updated_at)*1000)::bigint),definition_id
		 FROM plugin_statistics_current WHERE scope=ANY($1) AND ($3::text='' OR definition_id>$3)
		 ORDER BY definition_id LIMIT $2`)
}

func handleListAnalysisTasks(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "analysis-task",
		`SELECT count(*) FROM analysis_task_requests WHERE scope=ANY($1)`,
		`SELECT jsonb_build_object('task_id',task_id,'plugin_id',plugin_id,'binding_generation',binding_generation,
		 'status',status,'poll_count',poll_count,'deadline_unix_ms',deadline_unix_ms,
		 'remote_task_id',remote_task_id,'updated_at_unix_ms',updated_at_unix_ms),task_id
		 FROM analysis_task_requests WHERE scope=ANY($1) AND ($3::text='' OR task_id>$3)
		 ORDER BY task_id LIMIT $2`)
}

func handleListAnalysisArtifacts(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "analysis-artifact",
		`SELECT count(*) FROM analysis_artifacts WHERE scope=ANY($1)`,
		`SELECT jsonb_build_object('artifact_id',artifact_id,'task_id',task_id,'plugin_id',plugin_id,
		 'binding_generation',binding_generation,'artifact_digest',artifact_digest,'media_type',media_type,
		 'analysis_outcome',analysis_outcome,'non_executable',non_executable,
		 'deployment_eligible',deployment_eligible,'created_at_unix_ms',created_at_unix_ms),artifact_id
		 FROM analysis_artifacts WHERE scope=ANY($1) AND ($3::text='' OR artifact_id>$3)
		 ORDER BY artifact_id LIMIT $2`)
}

func handleListAudit(deps Deps) http.HandlerFunc {
	return scopedJSONList(deps, "audit",
		`SELECT count(*) FROM (
		 SELECT audit_id FROM plugin_audit_events WHERE scope=ANY($1)
		 UNION ALL SELECT audit_id FROM mcp_access_audit WHERE scope=ANY($1)
		 UNION ALL SELECT evidence_id FROM evidence_refs WHERE scope=ANY($1) AND kind='audit'
		) audit_facts`,
		`SELECT item,audit_key FROM (
		 SELECT jsonb_build_object('audit_id',audit_id,'category','plugin-lifecycle','subject_id',plugin_id,
		   'action',action,'outcome','recorded','reason_code',reason_code,'actor_ref',actor_ref,
		   'trace_id',trace_id,'created_at_unix_ms',(extract(epoch FROM created_at)*1000)::bigint) AS item,
		   audit_id AS audit_key FROM plugin_audit_events WHERE scope=ANY($1)
		 UNION ALL
		 SELECT jsonb_build_object('audit_id',audit_id,'category','mcp-readonly','subject_id',plugin_id,
		   'action',method,'outcome',outcome,'reason_code',reason_code,'trace_id',trace_id,
		   'created_at_unix_ms',created_at_unix_ms),audit_id
		   FROM mcp_access_audit WHERE scope=ANY($1)
		 UNION ALL
		 SELECT jsonb_build_object('audit_id',evidence_id,'category','evidence','subject_id',source,
		   'action',kind,'outcome','recorded','reason_code','AUDIT_EVIDENCE','trace_id',trace_id,
		   'created_at_unix_ms',(extract(epoch FROM created_at)*1000)::bigint),evidence_id
		   FROM evidence_refs WHERE scope=ANY($1) AND kind='audit'
		) audit_facts WHERE ($3::text='' OR audit_key>$3) ORDER BY audit_key LIMIT $2`)
}

func handleGetAnalysisArtifact(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		actor, scopes, err := readAuthorization(r, deps)
		_ = actor
		if err != nil {
			WriteError(w, http.StatusForbidden, "ANALYSIS_ARTIFACT_DENIED")
			return
		}
		artifactID := chi.URLParam(r, "artifactID")
		var raw []byte
		if err := deps.Pool.Pool.QueryRow(r.Context(), `SELECT jsonb_build_object(
			'artifact_id',artifact_id,'task_id',task_id,'plugin_id',plugin_id,'binding_generation',binding_generation,
			'artifact_digest',artifact_digest,'media_type',media_type,'analysis_outcome',analysis_outcome,
			'non_executable',non_executable,'deployment_eligible',deployment_eligible,'body',body)
			FROM analysis_artifacts WHERE artifact_id=$1 AND scope=ANY($2)`, artifactID, scopes).Scan(&raw); err != nil {
			WriteError(w, http.StatusNotFound, "ANALYSIS_ARTIFACT_NOT_FOUND")
			return
		}
		w.Header().Set("Cache-Control", "private, no-store")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(raw)
	}
}

// ---- Mutations (governed) ----

func handleCreateProposal(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		s, err := sessionFromContext(r.Context())
		if err != nil {
			WriteError(w, http.StatusUnauthorized, "UNAUTHENTICATED")
			return
		}
		var body struct {
			EffectKind      string   `json:"effect_kind"`
			TargetIDs       []string `json:"target_ids"`
			Scope           string   `json:"scope"`
			PolicyDigest    string   `json:"policy_digest"`
			RiskLevel       string   `json:"risk_level"`
			EvidenceRefs    []string `json:"evidence_refs"`
			Note            string   `json:"note"`
			IdempotencyKey  string   `json:"idempotency_key"`
			TraceID         string   `json:"trace_id"`
			ExpiresAtUnixMS int64    `json:"expires_at_unix_ms"`
		}
		if err := decodeStrictJSON(w, r, 32*1024, &body); err != nil {
			WriteError(w, http.StatusBadRequest, "BODY_MALFORMED")
			return
		}
		if len(body.Note) > 2048 {
			WriteError(w, http.StatusBadRequest, "NOTE_OVER_2KIB")
			return
		}
		if len(body.TargetIDs) == 0 || len(body.TargetIDs) > 128 {
			WriteError(w, http.StatusBadRequest, "TARGET_SET_OUT_OF_RANGE")
			return
		}
		if body.IdempotencyKey == "" || len(body.IdempotencyKey) > 128 {
			WriteError(w, http.StatusBadRequest, "IDEMPOTENCY_KEY_REQUIRED")
			return
		}
		nowMS := time.Now().UnixMilli()
		if body.ExpiresAtUnixMS <= nowMS || body.ExpiresAtUnixMS > nowMS+int64(24*time.Hour/time.Millisecond) {
			WriteError(w, http.StatusBadRequest, "EXPIRY_OUT_OF_RANGE")
			return
		}
		seenTargets := make(map[string]struct{}, len(body.TargetIDs))
		for _, id := range body.TargetIDs {
			if id == "" || len(id) > 128 {
				WriteError(w, http.StatusBadRequest, "TARGET_ID_MALFORMED")
				return
			}
			if _, exists := seenTargets[id]; exists {
				WriteError(w, http.StatusBadRequest, "TARGET_ID_DUPLICATED")
				return
			}
			seenTargets[id] = struct{}{}
		}
		if len(body.EvidenceRefs) > 128 {
			WriteError(w, http.StatusBadRequest, "EVIDENCE_REFS_OUT_OF_RANGE")
			return
		}
		seenEvidence := make(map[string]struct{}, len(body.EvidenceRefs))
		for _, ref := range body.EvidenceRefs {
			if ref == "" || len(ref) > 256 {
				WriteError(w, http.StatusBadRequest, "EVIDENCE_REF_MALFORMED")
				return
			}
			if _, exists := seenEvidence[ref]; exists {
				WriteError(w, http.StatusBadRequest, "EVIDENCE_REF_DUPLICATED")
				return
			}
			seenEvidence[ref] = struct{}{}
		}
		if body.Scope == "" {
			WriteError(w, http.StatusBadRequest, "SCOPE_REQUIRED")
			return
		}
		actor := security.Actor{Issuer: s.Actor.Issuer, Subject: s.Actor.Subject}
		if err := actor.Validate(); err != nil {
			WriteError(w, http.StatusForbidden, "ACTOR_MALFORMED")
			return
		}
		risk := security.RiskLevel(body.RiskLevel)
		if risk != security.R0 && risk != security.R1 && risk != security.R2 && risk != security.R3 {
			WriteError(w, http.StatusBadRequest, "RISK_LEVEL_INVALID")
			return
		}
		kind := governance.EffectKind(body.EffectKind)
		if !validEffectKind(kind) {
			WriteError(w, http.StatusBadRequest, "EFFECT_KIND_INVALID")
			return
		}
		if !validDigest(body.PolicyDigest) {
			WriteError(w, http.StatusBadRequest, "POLICY_DIGEST_INVALID")
			return
		}
		targetDigest := targetSetDigest(body.TargetIDs)
		level := security.LevelAnalyst
		authorized := false
		if (risk == security.R0 || risk == security.R1) && deps.Mapping != nil {
			for _, candidate := range []security.AuthzContextLevel{security.LevelOperator, security.LevelScopedOperator} {
				if _, err := deps.Mapping.AuthorizeScope(actor, candidate, body.Scope, body.EffectKind, targetDigest); err == nil {
					level = candidate
					authorized = true
					break
				}
			}
			if risk == security.R0 {
				if kind != governance.KindBoundedCapture || len(body.TargetIDs) != 1 {
					WriteError(w, http.StatusBadRequest, "R0_ONLY_BOUNDED_CAPTURE")
					return
				}
				var exact bool
				if err := deps.Pool.Pool.QueryRow(r.Context(), `SELECT EXISTS(SELECT 1 FROM bounded_capture_requests
					WHERE target_id=$1 AND scope=$2 AND capture_digest=$3 AND state='planned'
					  AND expires_at_unix_ms>=$4)`, body.TargetIDs[0], body.Scope, body.PolicyDigest,
					body.ExpiresAtUnixMS).Scan(&exact); err != nil || !exact {
					WriteError(w, http.StatusConflict, "BOUNDED_CAPTURE_POLICY_NOT_FROZEN")
					return
				}
			}
		}
		if risk == security.R3 {
			level = security.LevelPlatformAdmin
			stepUp := security.StepUpType(s.StepUpType)
			age := timeSinceMS(s.LastStepUp)
			if !stepUp.IsPhishingResistant() || age < 0 || age > 300000 {
				WriteError(w, http.StatusForbidden, "R3_MAKER_STEP_UP_REQUIRED")
				return
			}
		}
		if deps.Mapping == nil {
			WriteError(w, http.StatusForbidden, "SCOPE_DENIED")
			return
		}
		if !authorized {
			_, err := deps.Mapping.AuthorizeScope(actor, level, body.Scope, body.EffectKind, targetDigest)
			authorized = err == nil
		}
		if !authorized {
			WriteError(w, http.StatusForbidden, "SCOPE_DENIED")
			return
		}
		p := governance.Proposal{
			ProposalID:      "prop-" + shortID(actor.String()+":"+body.IdempotencyKey),
			Actor:           actor,
			ActorLevel:      level,
			Scope:           body.Scope,
			EffectKind:      kind,
			TargetSetDigest: targetDigest,
			TargetIDs:       append([]string(nil), body.TargetIDs...),
			PolicyDigest:    body.PolicyDigest,
			EvidenceRefs:    append([]string(nil), body.EvidenceRefs...),
			RiskLevel:       risk,
			ExpiresAtUnixMS: body.ExpiresAtUnixMS,
			Note:            body.Note,
			TraceID:         body.TraceID,
			IdempotencyKey:  body.IdempotencyKey,
		}
		out, err := deps.Proposals.Create(r.Context(), p)
		if err != nil {
			WriteError(w, http.StatusBadRequest, "PROPOSAL_REJECTED")
			return
		}
		publishInvalidation(deps, ResProposal, p.ProposalID, p.Scope, p.CreatedAtUnixMS)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(out)
	}
}

func handleRecordDecision(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			ProposalID     string `json:"proposal_id"`
			Decision       string `json:"decision"`
			ReasonCode     string `json:"reason_code"`
			IdempotencyKey string `json:"idempotency_key"`
		}
		if err := decodeStrictJSON(w, r, 8*1024, &body); err != nil || body.IdempotencyKey == "" {
			WriteError(w, http.StatusBadRequest, "BODY_MALFORMED")
			return
		}
		if body.ProposalID == "" || len(body.ProposalID) > 128 {
			WriteError(w, http.StatusBadRequest, "PROPOSAL_ID_MALFORMED")
			return
		}
		if body.Decision != "approve" && body.Decision != "reject" {
			WriteError(w, http.StatusBadRequest, "DECISION_INVALID")
			return
		}
		if !validReasonCode(body.ReasonCode) {
			WriteError(w, http.StatusBadRequest, "REASON_CODE_REQUIRED")
			return
		}
		recordDecision(w, r, deps, body.ProposalID, body.Decision == "approve", body.ReasonCode, http.StatusCreated)
	}
}

func recordDecision(w http.ResponseWriter, r *http.Request, deps Deps, proposalID string, approved bool, reasonCode string, statusCode int) {
	s, err := sessionFromContext(r.Context())
	if err != nil {
		WriteError(w, http.StatusUnauthorized, "UNAUTHENTICATED")
		return
	}
	if deps.Decisions == nil || deps.Mapping == nil {
		WriteError(w, http.StatusServiceUnavailable, "DECISION_SERVICE_UNAVAILABLE")
		return
	}
	actor := security.Actor{Issuer: s.Actor.Issuer, Subject: s.Actor.Subject}
	age := int(timeSinceMS(s.LastStepUp))
	stepUp := security.StepUpType(s.StepUpType)
	authz := governance.AuthzContext{StepUpType: stepUp, StepUpAgeMS: age, PhishingResistant: stepUp.IsPhishingResistant()}
	var out *governance.Decision
	if approved {
		out, err = deps.Decisions.ApproveWithReason(r.Context(), proposalID, actor, authz, reasonCode)
	} else {
		out, err = deps.Decisions.RejectWithReason(r.Context(), proposalID, actor, authz, reasonCode)
	}
	if err != nil {
		WriteError(w, http.StatusForbidden, "DECISION_REJECTED")
		return
	}
	var scope string
	if deps.Pool != nil && deps.Pool.Pool.QueryRow(r.Context(),
		`SELECT scope FROM effect_proposals WHERE proposal_id=$1`, proposalID).Scan(&scope) == nil {
		publishInvalidation(deps, ResDecision, out.DecisionID, scope, out.CreatedAtUnixMS)
		publishInvalidation(deps, ResProposal, proposalID, scope, out.CreatedAtUnixMS)
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(statusCode)
	_ = json.NewEncoder(w).Encode(out)
}

func handleStartPluginStatRun(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		s, err := sessionFromContext(r.Context())
		if err != nil {
			WriteError(w, http.StatusUnauthorized, "UNAUTHENTICATED")
			return
		}
		var body struct {
			DefinitionID      string `json:"definition_id"`
			DefinitionDigest  string `json:"definition_digest"`
			IdempotencyKey    string `json:"idempotency_key"`
			Scope             string `json:"scope"`
			DataClass         string `json:"data_class"`
			TargetSetDigest   string `json:"target_set_digest"`
			WindowStartUnixMS int64  `json:"window_start_unix_ms"`
			WindowEndUnixMS   int64  `json:"window_end_unix_ms"`
			TraceID           string `json:"trace_id"`
		}
		if err := decodeStrictJSON(w, r, 16*1024, &body); err != nil {
			WriteError(w, http.StatusBadRequest, "BODY_MALFORMED")
			return
		}
		if deps.PluginStat == nil || deps.Mapping == nil || !validDigest(body.DefinitionDigest) || !validDigest(body.TargetSetDigest) || body.IdempotencyKey == "" || len(body.IdempotencyKey) > 128 {
			WriteError(w, http.StatusBadRequest, "STATISTICS_RUN_MALFORMED")
			return
		}
		actor := security.Actor{Issuer: s.Actor.Issuer, Subject: s.Actor.Subject}
		authorized := false
		for _, level := range []security.AuthzContextLevel{security.LevelAnalyst, security.LevelOperator, security.LevelScopedOperator} {
			_, sourceErr := deps.Mapping.AuthorizeScope(actor, level, body.Scope, "source-read", body.TargetSetDigest)
			_, runErr := deps.Mapping.AuthorizeScope(actor, level, body.Scope, "plugin.statistics.run", body.TargetSetDigest)
			if sourceErr == nil && runErr == nil {
				authorized = true
				break
			}
		}
		if !authorized {
			WriteError(w, http.StatusForbidden, "STATISTICS_RUN_SCOPE_DENIED")
			return
		}
		runID := "statrun-" + shortID(actor.String()+":"+body.IdempotencyKey)
		out, err := deps.PluginStat.StartOnDemand(r.Context(), pluginstat.OnDemandRequest{RunID: runID, DefinitionID: body.DefinitionID, DefinitionDigest: body.DefinitionDigest, IdempotencyKey: body.IdempotencyKey, Scope: body.Scope, DataClass: body.DataClass, TargetSetDigest: body.TargetSetDigest, WindowStartUnixMS: body.WindowStartUnixMS, WindowEndUnixMS: body.WindowEndUnixMS, TraceID: body.TraceID}, actor, pluginstat.RunAuthorization{Scope: body.Scope, DataClass: body.DataClass, TargetSetDigest: body.TargetSetDigest, SourceRead: true, StatisticsRun: true})
		if err != nil {
			WriteError(w, http.StatusBadRequest, "STATISTICS_RUN_REJECTED")
			return
		}
		publishInvalidation(deps, ResPluginRun, out, body.Scope, time.Now().UnixMilli())
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]string{"run_id": out, "status": "pending"})
	}
}

func decodeStrictJSON(w http.ResponseWriter, r *http.Request, maxBytes int64, dst any) error {
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxBytes))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return err
	}
	if err := dec.Decode(&struct{}{}); err != io.EOF {
		return errors.New("api: trailing JSON content")
	}
	return nil
}

func validReasonCode(reason string) bool {
	if len(reason) < 1 || len(reason) > 64 || reason[0] < 'A' || reason[0] > 'Z' {
		return false
	}
	for _, r := range reason[1:] {
		if (r < 'A' || r > 'Z') && (r < '0' || r > '9') && r != '_' {
			return false
		}
	}
	return true
}

func validEffectKind(kind governance.EffectKind) bool {
	switch kind {
	case governance.KindFirewallBaselineActivate, governance.KindFirewallOverlay,
		governance.KindFirewallRollback, governance.KindTargetAssignment,
		governance.KindFleetOperation, governance.KindModelRollout,
		governance.KindModelRollback, governance.KindModelRecovery,
		governance.KindPluginActivate, governance.KindPluginDrain,
		governance.KindPluginRevoke, governance.KindPluginStatisticsRun,
		governance.KindRuleObservationEpoch, governance.KindBoundedCapture:
		return true
	default:
		return false
	}
}

func validDigest(s string) bool {
	return security.ValidDigest(s)
}

func shortID(seed string) string {
	return fmt.Sprintf("%x", sha256Sum16(seed))
}

func sha256Sum16(s string) []byte {
	h := sha256.Sum256([]byte(s))
	return h[:8]
}

// timeSinceMS returns the milliseconds elapsed since t (0 when t is zero).
func timeSinceMS(t time.Time) int64 {
	if t.IsZero() {
		return 1 << 62 // no step-up recorded: effectively stale
	}
	return time.Since(t).Milliseconds()
}

// targetSetDigest derives the frozen target-set digest (sorted ids).
func targetSetDigest(ids []string) string {
	sorted := append([]string(nil), ids...)
	for i := 1; i < len(sorted); i++ {
		for j := i; j > 0 && sorted[j] < sorted[j-1]; j-- {
			sorted[j], sorted[j-1] = sorted[j-1], sorted[j]
		}
	}
	return "sha256:" + hex.EncodeToString(sha256Sum32(strings.Join(sorted, ",")))
}

func sha256Sum32(s string) []byte {
	h := sha256.Sum256([]byte(s))
	return h[:]
}

func digestText(value string) string {
	return "sha256:" + hex.EncodeToString(sha256Sum32(value))
}
