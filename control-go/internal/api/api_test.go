package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/go-chi/chi/v5"

	"masi-nids/control-go/internal/security"
)

func newTestStore() *SessionStore {
	st := NewSessionStore()
	st.ttl = time.Hour
	return st
}

func testSession(t *testing.T, st *SessionStore) *Session {
	t.Helper()
	s, err := st.Create(actorClaims{Issuer: "https://idp.example", Subject: "user-1", Audience: audienceClaim{"masi-web"}}, "none")
	if err != nil {
		t.Fatalf("create session: %v", err)
	}
	return s
}

func TestSessionCookieFlags(t *testing.T) {
	st := newTestStore()
	s := testSession(t, st)
	rec := httptest.NewRecorder()
	SetSessionCookie(rec, s, true)
	c := rec.Header().Get("Set-Cookie")
	for _, want := range []string{"HttpOnly", "SameSite=Strict", "Secure"} {
		if !strings.Contains(c, want) {
			t.Fatalf("cookie missing %s: %s", want, c)
		}
	}
	// The cookie value is the opaque session id only — no claims, no tokens.
	if !strings.Contains(c, "masi_session="+s.ID) {
		t.Fatalf("cookie must carry only the session id: %s", c)
	}
}

func TestValidateRollbackShardSelectionBindsOriginalGroup(t *testing.T) {
	original := []byte(`["shard-a","shard-b","shard-c"]`)
	requested := []string{"shard-c", "shard-a"}
	if err := validateRollbackShardSelection(original, requested, targetSetDigest(requested)); err != nil {
		t.Fatalf("valid rollback subset rejected: %v", err)
	}

	for name, tc := range map[string]struct {
		original  []byte
		requested []string
		digest    string
	}{
		"outside-original-group": {original: original, requested: []string{"shard-a", "shard-x"}, digest: targetSetDigest([]string{"shard-a", "shard-x"})},
		"duplicate-request":      {original: original, requested: []string{"shard-a", "shard-a"}, digest: targetSetDigest([]string{"shard-a", "shard-a"})},
		"wrong-digest":           {original: original, requested: requested, digest: targetSetDigest([]string{"shard-a"})},
		"invalid-original":       {original: []byte(`{"not":"an array"}`), requested: requested, digest: targetSetDigest(requested)},
		"duplicate-original":     {original: []byte(`["shard-a","shard-a"]`), requested: []string{"shard-a"}, digest: targetSetDigest([]string{"shard-a"})},
	} {
		t.Run(name, func(t *testing.T) {
			if err := validateRollbackShardSelection(tc.original, tc.requested, tc.digest); err == nil {
				t.Fatal("unsafe rollback shard selection accepted")
			}
		})
	}
}

func TestClearSessionCookiePreservesSecurityAttributes(t *testing.T) {
	rec := httptest.NewRecorder()
	ClearSessionCookie(rec, true)
	c := rec.Header().Get("Set-Cookie")
	for _, want := range []string{"HttpOnly", "SameSite=Strict", "Secure", "Max-Age=0"} {
		if !strings.Contains(c, want) {
			t.Fatalf("clear cookie missing %s: %s", want, c)
		}
	}
}

func TestBrowserTestLoginMintsFixedLoopbackSession(t *testing.T) {
	st := newTestStore()
	req := httptest.NewRequest(http.MethodGet, "/oidc/login", nil)
	req.RemoteAddr = "127.0.0.1:43123"
	req.Header.Set("Sec-Fetch-Site", "same-origin")
	rec := httptest.NewRecorder()

	handleBrowserTestLogin(st, false).ServeHTTP(rec, req)
	if rec.Code != http.StatusSeeOther || rec.Header().Get("Location") != "/" {
		t.Fatalf("browser test login did not redirect: status=%d location=%q", rec.Code, rec.Header().Get("Location"))
	}
	var sessionCookie *http.Cookie
	for _, cookie := range rec.Result().Cookies() {
		if cookie.Name == "masi_session" {
			sessionCookie = cookie
			break
		}
	}
	if sessionCookie == nil || !sessionCookie.HttpOnly || sessionCookie.SameSite != http.SameSiteStrictMode {
		t.Fatalf("browser test login returned an invalid session cookie: %+v", sessionCookie)
	}
	session, ok := st.Get(sessionCookie.Value)
	if !ok || session.Actor.Issuer != browserTestLoginIssuer || session.Actor.Subject != browserTestLoginSubject {
		t.Fatalf("browser test login minted the wrong fixed identity: %+v", session)
	}
}

func TestBrowserTestLoginRejectsCrossSiteOrNonLoopback(t *testing.T) {
	for name, tc := range map[string]struct {
		remoteAddr string
		fetchSite  string
	}{
		"cross-site":   {remoteAddr: "127.0.0.1:43123", fetchSite: "cross-site"},
		"non-loopback": {remoteAddr: "192.0.2.10:43123", fetchSite: "same-origin"},
	} {
		t.Run(name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "/oidc/login", nil)
			req.RemoteAddr = tc.remoteAddr
			req.Header.Set("Sec-Fetch-Site", tc.fetchSite)
			rec := httptest.NewRecorder()
			handleBrowserTestLogin(newTestStore(), false).ServeHTTP(rec, req)
			if rec.Code != http.StatusForbidden {
				t.Fatalf("unsafe browser test login returned %d", rec.Code)
			}
		})
	}
}

func TestRouterUsesBrowserFixtureOnlyForExplicitTestProfile(t *testing.T) {
	for name, tc := range map[string]struct {
		enabled    bool
		wantStatus int
	}{
		"test-profile":     {enabled: true, wantStatus: http.StatusSeeOther},
		"non-test-no-oidc": {enabled: false, wantStatus: http.StatusServiceUnavailable},
	} {
		t.Run(name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "/oidc/login", nil)
			req.RemoteAddr = "127.0.0.1:43123"
			req.Header.Set("Sec-Fetch-Site", "same-origin")
			rec := httptest.NewRecorder()
			Router(
				Deps{TestLogin: tc.enabled, RequestTimeout: time.Second},
				nil,
				newTestStore(),
				NewHub(),
				"http://127.0.0.1:4189",
			).ServeHTTP(rec, req)
			if rec.Code != tc.wantStatus {
				t.Fatalf("unexpected login status: got=%d want=%d", rec.Code, tc.wantStatus)
			}
		})
	}
}

func TestMutationGuardRejectsMissingCSRF(t *testing.T) {
	st := newTestStore()
	s := testSession(t, st)
	called := false
	h := requireMutationGuard(st, "https://ctrl.example")(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
	}))
	req := httptest.NewRequest(http.MethodPost, "/api/proposals", nil)
	req.AddCookie(&http.Cookie{Name: "masi_session", Value: s.ID})
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if called {
		t.Fatal("mutation must not reach handler without CSRF")
	}
	if rec.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", rec.Code)
	}
}

func TestMutationGuardAcceptsValidCSRFAndOrigin(t *testing.T) {
	st := newTestStore()
	s := testSession(t, st)
	called := false
	h := requireMutationGuard(st, "https://ctrl.example")(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, err := sessionFromContext(r.Context()); err != nil {
			t.Fatalf("session missing in context: %v", err)
		}
		called = true
	}))
	req := httptest.NewRequest(http.MethodPost, "/api/proposals", nil)
	req.AddCookie(&http.Cookie{Name: "masi_session", Value: s.ID})
	req.Header.Set("X-CSRF-Token", s.CSRFToken)
	req.Header.Set("Origin", "https://ctrl.example")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if !called {
		t.Fatalf("valid mutation blocked: %d", rec.Code)
	}
}

func TestMutationGuardRejectsCrossOrigin(t *testing.T) {
	st := newTestStore()
	s := testSession(t, st)
	h := requireMutationGuard(st, "https://ctrl.example")(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	req := httptest.NewRequest(http.MethodPost, "/api/proposals", nil)
	req.AddCookie(&http.Cookie{Name: "masi_session", Value: s.ID})
	req.Header.Set("X-CSRF-Token", s.CSRFToken)
	req.Header.Set("Origin", "https://evil.example")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("cross-origin must be 403, got %d", rec.Code)
	}
	req2 := httptest.NewRequest(http.MethodPost, "/api/proposals", nil)
	req2.AddCookie(&http.Cookie{Name: "masi_session", Value: s.ID})
	req2.Header.Set("X-CSRF-Token", s.CSRFToken)
	req2.Header.Set("Sec-Fetch-Site", "cross-site")
	rec2 := httptest.NewRecorder()
	h.ServeHTTP(rec2, req2)
	if rec2.Code != http.StatusForbidden {
		t.Fatalf("cross-site fetch must be 403, got %d", rec2.Code)
	}
}

func TestSessionExpiryAndDrop(t *testing.T) {
	st := newTestStore()
	s := testSession(t, st)
	if _, ok := st.Get(s.ID); !ok {
		t.Fatal("session must be live")
	}
	st.Drop(s.ID)
	if _, ok := st.Get(s.ID); ok {
		t.Fatal("dropped session must be gone")
	}
	// Expired session rejected.
	s2, _ := st.Create(actorClaims{Issuer: "https://idp.example", Subject: "u2"}, "none")
	st.mu.Lock()
	st.sessions[s2.ID].ExpiresAt = time.Now().Add(-time.Second)
	st.mu.Unlock()
	if _, ok := st.Get(s2.ID); ok {
		t.Fatal("expired session must be rejected")
	}
}

func TestSessionStepUpRequiresAllowedACRAndRecentAuthTime(t *testing.T) {
	const acr = "urn:masi:acr:phishing-resistant"
	st := NewSessionStoreWithStepUpACRs("masi_session", []string{acr})
	now := time.Now()
	st.now = func() time.Time { return now }
	claims := actorClaims{Issuer: "https://idp.example", Subject: "u-stepup", IssuedAt: now.Unix(),
		AuthTime: now.Add(-time.Minute).Unix(), Acr: acr, Amr: []string{"webauthn"}}
	s, err := st.Create(claims, string(security.StepUpWebAuthn))
	if err != nil || s.LastStepUp.IsZero() {
		t.Fatalf("valid explicit step-up rejected: session=%+v err=%v", s, err)
	}
	claims.Acr = "urn:unapproved"
	s, err = st.Create(claims, string(security.StepUpWebAuthn))
	if err != nil || !s.LastStepUp.IsZero() {
		t.Fatalf("unapproved ACR must not confer step-up: session=%+v err=%v", s, err)
	}
	claims.Acr = acr
	claims.AuthTime = 0
	s, err = st.Create(claims, string(security.StepUpWebAuthn))
	if err != nil || !s.LastStepUp.IsZero() {
		t.Fatalf("missing auth_time must not confer step-up: session=%+v err=%v", s, err)
	}
}

func TestCursorRoundTripAndBounds(t *testing.T) {
	codec, err := NewCursorCodec([]byte(strings.Repeat("k", 32)))
	if err != nil {
		t.Fatal(err)
	}
	c := Cursor{LastID: 42, Forward: false, Kind: "event", ScopeHash: "sha256:" + strings.Repeat("a", 64)}
	enc, err := codec.Encode(c)
	if err != nil {
		t.Fatal(err)
	}
	dec, err := codec.Decode(enc)
	if err != nil || dec.LastID != 42 || dec.Forward {
		t.Fatalf("round trip failed: %+v err=%v", dec, err)
	}
	if _, err := codec.Decode("!!!not-base64!!!"); err == nil {
		t.Fatal("malformed cursor must be rejected")
	}
	tampered := enc[:len(enc)-1] + "A"
	if _, err := codec.Decode(tampered); err == nil {
		t.Fatal("tampered cursor must be rejected")
	}
	first, err := codec.Decode("")
	if err != nil || !first.Forward {
		t.Fatal("empty cursor is the first page")
	}
}

func TestParsePageSizeBounded(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/api/events", nil)
	if n, err := ParsePageSize(req); err != nil || n != DefaultPageSize {
		t.Fatalf("default page size: %d %v", n, err)
	}
	req = httptest.NewRequest(http.MethodGet, "/api/events?page_size=200", nil)
	if n, err := ParsePageSize(req); err != nil || n != 200 {
		t.Fatalf("max page size: %d %v", n, err)
	}
	req = httptest.NewRequest(http.MethodGet, "/api/events?page_size=201", nil)
	if _, err := ParsePageSize(req); err == nil {
		t.Fatal("page_size > 200 must be rejected")
	}
	req = httptest.NewRequest(http.MethodGet, "/api/events?page_size=0", nil)
	if _, err := ParsePageSize(req); err == nil {
		t.Fatal("page_size < 1 must be rejected")
	}
}

func TestSSEEventBounded(t *testing.T) {
	ev := finalizeSSEEvent(SSEEvent{EventType: SSEInvalidate, ResourceKind: ResEvent, ResourceID: "evt-1",
		Cursor: "sse:1:1", Sequence: 1, Generation: 1, ProducedAtUnixMS: 1,
		DataTimeUnixMS: 1, EmittedAtUnixMS: 1, ScopeID: "scope-a"})
	payload, err := json.Marshal(ev)
	if err != nil {
		t.Fatal(err)
	}
	if len(payload) > MaxSSEBytes {
		t.Fatalf("identity-only event exceeds bound: %d", len(payload))
	}
	// A generation change forces snapshot refetch, never a payload push.
	if SSEInvalidate == SSEHeartbeat {
		t.Fatal("event types must be distinct")
	}
}

func TestHubSubscribePublishUnsubscribe(t *testing.T) {
	h := NewHub()
	ch, err := h.Subscribe([]string{"scope-a"})
	if err != nil {
		t.Fatal(err)
	}
	if err := h.PublishScoped(ResProposal, "prop-1", "scope-a", 1, 1); err != nil {
		t.Fatal(err)
	}
	select {
	case ev := <-ch:
		if ev.EventType != SSEInvalidate || ev.ResourceKind != ResProposal || ev.ResourceID != "prop-1" {
			t.Fatalf("unexpected event: %+v", ev)
		}
	default:
		t.Fatal("event must be delivered")
	}
	h.Unsubscribe(ch)
	// Publishing after unsubscribe must not panic.
	if err := h.PublishScoped(ResEvent, "evt-9", "scope-a", 1, 1); err != nil {
		t.Fatal(err)
	}
}

func TestHubBoundedSubscribers(t *testing.T) {
	h := NewHub()
	h.max = 2
	for i := 0; i < 2; i++ {
		if _, err := h.Subscribe([]string{"scope-a"}); err != nil {
			t.Fatalf("subscribe %d: %v", i, err)
		}
	}
	if _, err := h.Subscribe([]string{"scope-a"}); err == nil {
		t.Fatal("subscriber bound must be enforced")
	}
}

func TestHubFiltersInvalidationsByAuthorizedScope(t *testing.T) {
	h := NewHub()
	a, err := h.Subscribe([]string{"scope-a"})
	if err != nil {
		t.Fatal(err)
	}
	b, err := h.Subscribe([]string{"scope-b"})
	if err != nil {
		t.Fatal(err)
	}
	defer h.Unsubscribe(a)
	defer h.Unsubscribe(b)
	if err := h.PublishScoped(ResEvent, "evt-a", "scope-a", 1, 1); err != nil {
		t.Fatal(err)
	}
	select {
	case ev := <-a:
		if ev.ResourceID != "evt-a" || ev.Sequence != 1 || ev.Generation == 0 || ev.Cursor == "" || ev.PayloadDigest == "" {
			t.Fatalf("incomplete scoped event: %+v", ev)
		}
	default:
		t.Fatal("authorized scope did not receive invalidation")
	}
	select {
	case ev := <-b:
		t.Fatalf("cross-scope invalidation leaked: %+v", ev)
	default:
	}
	if err := h.PublishScoped(ResEvent, "evt-b", "scope-b", 1, 1); err != nil {
		t.Fatal(err)
	}
	if err := h.PublishScoped(ResEvent, "evt-a-2", "scope-a", 1, 1); err != nil {
		t.Fatal(err)
	}
	if ev := <-b; ev.ResourceID != "evt-b" || ev.Sequence != 1 {
		t.Fatalf("scope-b sequence includes unauthorized events: %+v", ev)
	}
	if ev := <-a; ev.ResourceID != "evt-a-2" || ev.Sequence != 2 {
		t.Fatalf("scope-a sequence is not subscriber-contiguous: %+v", ev)
	}

	multi, err := h.Subscribe([]string{"scope-a", "scope-b"})
	if err != nil {
		t.Fatal(err)
	}
	defer h.Unsubscribe(multi)
	if err := h.PublishScoped(ResEvent, "evt-a-3", "scope-a", 1, 1); err != nil {
		t.Fatal(err)
	}
	if err := h.PublishScoped(ResEvent, "evt-b-2", "scope-b", 1, 1); err != nil {
		t.Fatal(err)
	}
	if first, second := <-multi, <-multi; first.Sequence != 1 || second.Sequence != 2 {
		t.Fatalf("multi-scope subscriber sequence is not contiguous: first=%+v second=%+v", first, second)
	}
}

func TestSessionWatcherClosesOnLogout(t *testing.T) {
	st := newTestStore()
	s := testSession(t, st)
	revoked, cancel, ok := st.WatchSession(s.ID)
	if !ok {
		t.Fatal("live session watcher rejected")
	}
	defer cancel()
	st.Drop(s.ID)
	select {
	case <-revoked:
	default:
		t.Fatal("logout must close long-lived session transports immediately")
	}
}

func TestSessionWatcherClosesAtExpiryWithoutAnotherRequest(t *testing.T) {
	st := NewSessionStore()
	st.ttl = 25 * time.Millisecond
	session, err := st.Create(actorClaims{Issuer: "https://idp.example", Subject: "expiry-user"}, "")
	if err != nil {
		t.Fatal(err)
	}
	expired, cancel, ok := st.WatchSession(session.ID)
	if !ok {
		t.Fatal("live session watcher rejected")
	}
	defer cancel()
	select {
	case <-expired:
	case <-time.After(time.Second):
		t.Fatal("session watcher stayed open after absolute expiry")
	}
	if _, ok := st.Get(session.ID); ok {
		t.Fatal("expired watched session remained in the store")
	}
}

func TestStateNonceLifecycle(t *testing.T) {
	state, err := randomState()
	if err != nil {
		t.Fatal(err)
	}
	if !stateValid(state) {
		t.Fatal("fresh state must be valid")
	}
	if stateValid("garbage") {
		t.Fatal("malformed state must be invalid")
	}
	if stateValid("aaaa.!!!!!") {
		t.Fatal("undecodable state must be invalid")
	}
}

func TestWriteErrorBounded(t *testing.T) {
	rec := httptest.NewRecorder()
	WriteError(rec, http.StatusBadRequest, strings.Repeat("x", 200))
	var body map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if len(body["error"]) > 64 {
		t.Fatalf("error reason must be bounded: %d", len(body["error"]))
	}
}

func TestStrictJSONRejectsTrailingValue(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/test", strings.NewReader(`{"reason_code":"APPROVED"}{"extra":true}`))
	rec := httptest.NewRecorder()
	var body struct {
		ReasonCode string `json:"reason_code"`
	}
	if err := decodeStrictJSON(rec, req, 1024, &body); err == nil {
		t.Fatal("a second JSON value must be rejected")
	}
	if !validReasonCode("APPROVED_BY_POLICY") || validReasonCode("free form") || validReasonCode("lowercase") {
		t.Fatal("reason code union/format validation drift")
	}
}

func TestRouterOpenAPIPathsRegistered(t *testing.T) {
	h := Router(Deps{}, nil, NewSessionStore(), NewHub(), "https://ctrl.example")
	routes, ok := h.(chi.Routes)
	if !ok {
		t.Fatal("router does not expose chi routes")
	}
	seen := map[string]bool{}
	if err := chi.Walk(routes, func(method, route string, _ http.Handler, _ ...func(http.Handler) http.Handler) error {
		seen[method+" "+route] = true
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		"GET /api/dashboard", "GET /api/events", "GET /api/incidents", "GET /api/evidence", "GET /api/effects/proposals",
		"GET /api/effects/decisions", "POST /api/effects/proposals", "POST /api/effects/decisions", "GET /api/effects/intents",
		"GET /api/firewall/revisions", "GET /api/targets", "GET /api/fleet/operations", "GET /api/rule-effectiveness",
		"GET /api/models/revisions", "GET /api/models/bindings", "GET /api/models/rollout-groups", "GET /api/plugins",
		"GET /api/plugins/statistics/definitions", "GET /api/plugins/statistics/runs", "POST /api/plugins/statistics/runs",
		"GET /api/plugins/statistics/current", "GET /api/plugins/statistics/artifacts/{artifactID}",
		"GET /api/analysis/artifacts",
		"GET /api/audit",
		"POST /api/targets", "POST /api/targets/{targetID}/activate", "POST /api/targets/{targetID}/retire",
		"POST /api/firewall/revisions", "POST /api/firewall/activations",
		"POST /api/firewall/overlays",
		"POST /api/models/revisions", "POST /api/models/revisions/{revisionID}/revoke",
		"POST /api/models/rollout-groups", "POST /api/models/rollout-groups/{groupID}/advance",
		"POST /api/plugins", "POST /api/plugins/{pluginID}/qualifications", "POST /api/plugins/{pluginID}/bindings",
		"POST /api/plugins/{pluginID}/drain", "POST /api/plugins/{pluginID}/revoke", "POST /api/plugins/{pluginID}/rollback",
		"POST /api/plugins/statistics/definitions", "POST /api/plugins/statistics/schedules",
		"GET /api/plugins/statistics/schedules",
		"GET /events", "GET /oidc/callback",
	} {
		if !seen[want] {
			t.Errorf("OpenAPI route missing: %s", want)
		}
	}
}
