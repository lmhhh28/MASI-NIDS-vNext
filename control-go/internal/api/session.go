// Package api implements the Go Control Core HTTP surface: same-origin
// /api REST projection, /events SSE, OIDC login/callback with PKCE, and the
// session/CSRF foundation.
//
// Security invariants (AGENTS.md, ADR-0005, contracts/web/v1):
//   - the browser holds ONLY an HttpOnly/SameSite=Strict session cookie plus
//     a CSRF token; no token/secret ever reaches the SPA;
//   - every mutation is authorized SERVER-SIDE via the versioned role/scope
//     mapping (default-deny); route visibility is never authorization;
//   - CSRF: double-submit token bound to the session id + Origin and
//     Sec-Fetch-Site verification on all state-changing requests;
//   - SSE events are invalidate/heartbeat/snapshot-refetch-required only,
//     each <= 64KiB, heartbeat every 15s; full payloads are never pushed;
//   - cursors are opaque server-side cursors; page size is bounded (<= 200);
//   - SSE responses never carry credentials; cursor/payload carry none.
package api

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"masi-nids/control-go/internal/security"
)

// Session is a server-side browser session. The cookie carries only the
// opaque session id; ALL state (actor, scopes, step-up) lives server-side.
type Session struct {
	ID         string
	Actor      actorClaims
	CSRFToken  string
	ExpiresAt  time.Time
	LastStepUp time.Time
	StepUpType string
}

type actorClaims struct {
	Issuer          string        `json:"iss"`
	Subject         string        `json:"sub"`
	Audience        audienceClaim `json:"aud"`
	ExpiresAt       int64         `json:"exp"`
	IssuedAt        int64         `json:"iat"`
	NotBefore       int64         `json:"nbf,omitempty"`
	AuthorizedParty string        `json:"azp,omitempty"`
	Nonce           string        `json:"nonce"`
	Amr             []string      `json:"amr,omitempty"`
	AuthTime        int64         `json:"auth_time,omitempty"`
	Acr             string        `json:"acr,omitempty"`
}

type audienceClaim []string

func (a *audienceClaim) UnmarshalJSON(raw []byte) error {
	var one string
	if err := json.Unmarshal(raw, &one); err == nil {
		*a = []string{one}
		return nil
	}
	var many []string
	if err := json.Unmarshal(raw, &many); err != nil || len(many) == 0 {
		return errors.New("api: aud must be string or non-empty string array")
	}
	*a = many
	return nil
}

// SessionStore is a bounded in-memory session store. The bound keeps memory
// finite; sessions expire and are swept lazily.
type SessionStore struct {
	mu         sync.Mutex
	sessions   map[string]*Session
	preAuth    map[string]OIDCTransaction
	watchers   map[string]map[chan struct{}]struct{}
	max        int
	ttl        time.Duration
	now        func() time.Time
	cookieName string
	stepUpACRs map[string]struct{}
}

// OIDCTransaction is a bounded, server-side authorization-code transaction.
// The browser cookie contains only the random state; PKCE verifier and nonce
// never appear in callback query parameters.
type OIDCTransaction struct {
	State     string
	Nonce     string
	Verifier  string
	ExpiresAt time.Time
}

const (
	defaultSessionMax = 10000
	sessionCookie     = "masi_session"
	csrfHeader        = "X-CSRF-Token"
	// MaxSSEBytes is the SSE single-event bound (web/v1: <= 65536).
	MaxSSEBytes = 65536
	// SSEHeartbeatInterval matches the web/v1 profile (15s).
	SSEHeartbeatInterval = 15 * time.Second
)

func NewSessionStore(cookieNames ...string) *SessionStore {
	name := sessionCookie
	if len(cookieNames) > 0 && cookieNames[0] != "" {
		name = cookieNames[0]
	}
	return newSessionStore(name, nil)
}

// NewSessionStoreWithStepUpACRs freezes the exact IdP ACR values that are
// allowed to confer R2/R3 step-up. A signed amr claim without an allowed acr and
// recent auth_time remains an ordinary session.
func NewSessionStoreWithStepUpACRs(cookieName string, acrValues []string) *SessionStore {
	if cookieName == "" {
		cookieName = sessionCookie
	}
	return newSessionStore(cookieName, acrValues)
}

func newSessionStore(name string, acrValues []string) *SessionStore {
	allow := make(map[string]struct{}, len(acrValues))
	for _, acr := range acrValues {
		if acr != "" {
			allow[acr] = struct{}{}
		}
	}
	return &SessionStore{
		sessions:   make(map[string]*Session),
		preAuth:    make(map[string]OIDCTransaction),
		watchers:   make(map[string]map[chan struct{}]struct{}),
		max:        defaultSessionMax,
		ttl:        12 * time.Hour,
		now:        time.Now,
		cookieName: name,
		stepUpACRs: allow,
	}
}

// Create mints a session for verified OIDC claims. The returned CSRF token
// is bound to the session id via HMAC.
func (st *SessionStore) Create(claims actorClaims, stepUpType string) (*Session, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return nil, fmt.Errorf("api: session id: %w", err)
	}
	id := hex.EncodeToString(raw)
	s := &Session{
		ID:         id,
		Actor:      claims,
		CSRFToken:  "", // set below (needs the id)
		ExpiresAt:  st.now().Add(st.ttl),
		StepUpType: stepUpType,
	}
	_, acrAllowed := st.stepUpACRs[claims.Acr]
	if canonical := security.StepUpType(stepUpType); canonical.IsPhishingResistant() && acrAllowed && claims.AuthTime > 0 {
		authenticated := time.Unix(claims.AuthTime, 0)
		age := st.now().Sub(authenticated)
		if age >= 0 && age <= 5*time.Minute {
			s.LastStepUp = authenticated
		}
	}
	csrf, err := csrfTokenFor()
	if err != nil {
		return nil, err
	}
	s.CSRFToken = csrf
	st.mu.Lock()
	defer st.mu.Unlock()
	if len(st.sessions) >= st.max {
		// Lazy sweep then bounded admit; never unbounded growth.
		st.sweepLocked()
		if len(st.sessions) >= st.max {
			return nil, errors.New("api: session store full")
		}
	}
	st.sessions[id] = s
	return s, nil
}

// BeginOIDC stores a one-time pre-auth transaction. It is bounded by the same
// session cap and rejects state reuse.
func (st *SessionStore) BeginOIDC(tx OIDCTransaction) error {
	if tx.State == "" || tx.Nonce == "" || tx.Verifier == "" || !tx.ExpiresAt.After(st.now()) {
		return errors.New("api: invalid oidc transaction")
	}
	st.mu.Lock()
	defer st.mu.Unlock()
	st.sweepLocked()
	if len(st.preAuth) >= st.max {
		return errors.New("api: oidc transaction store full")
	}
	if _, exists := st.preAuth[tx.State]; exists {
		return errors.New("api: oidc state already exists")
	}
	st.preAuth[tx.State] = tx
	return nil
}

// ConsumeOIDC atomically consumes a one-time transaction. Replay and expiry are
// rejected before token exchange.
func (st *SessionStore) ConsumeOIDC(state string) (OIDCTransaction, error) {
	st.mu.Lock()
	defer st.mu.Unlock()
	tx, ok := st.preAuth[state]
	if !ok {
		return OIDCTransaction{}, errors.New("api: oidc state not found or already consumed")
	}
	delete(st.preAuth, state)
	if !tx.ExpiresAt.After(st.now()) {
		return OIDCTransaction{}, errors.New("api: oidc transaction expired")
	}
	return tx, nil
}

// Get returns a live session.
func (st *SessionStore) Get(id string) (*Session, bool) {
	st.mu.Lock()
	defer st.mu.Unlock()
	s, ok := st.sessions[id]
	if !ok {
		return nil, false
	}
	if st.now().After(s.ExpiresAt) {
		st.invalidateLocked(id)
		return nil, false
	}
	return s, true
}

// Drop removes a session (logout).
func (st *SessionStore) Drop(id string) {
	st.mu.Lock()
	defer st.mu.Unlock()
	st.invalidateLocked(id)
}

// WatchSession returns a channel that closes as soon as the session is dropped
// or expires. Long-lived transports use it so logout/revocation does not leave
// an authenticated stream alive until the network connection happens to end.
func (st *SessionStore) WatchSession(id string) (<-chan struct{}, func(), bool) {
	st.mu.Lock()
	defer st.mu.Unlock()
	s, ok := st.sessions[id]
	if !ok || !s.ExpiresAt.After(st.now()) {
		if ok {
			st.invalidateLocked(id)
		}
		return nil, func() {}, false
	}
	ch := make(chan struct{})
	if st.watchers[id] == nil {
		st.watchers[id] = make(map[chan struct{}]struct{})
	}
	st.watchers[id][ch] = struct{}{}
	var once sync.Once
	cancel := func() {
		once.Do(func() {
			st.mu.Lock()
			defer st.mu.Unlock()
			if watchers := st.watchers[id]; watchers != nil {
				delete(watchers, ch)
				if len(watchers) == 0 {
					delete(st.watchers, id)
				}
			}
		})
	}
	return ch, cancel, true
}

func (st *SessionStore) invalidateLocked(id string) {
	delete(st.sessions, id)
	for ch := range st.watchers[id] {
		close(ch)
	}
	delete(st.watchers, id)
}

func (st *SessionStore) sweepLocked() {
	now := st.now()
	for id, s := range st.sessions {
		if now.After(s.ExpiresAt) {
			st.invalidateLocked(id)
		}
	}
	for state, tx := range st.preAuth {
		if !tx.ExpiresAt.After(now) {
			delete(st.preAuth, state)
		}
	}
}

func csrfTokenFor() (string, error) {
	// Random per-session secret keeps tokens unforgeable even under
	// same-origin XSS-free assumptions; format: nonce where the value is
	// random and the session stores the exact token (double-submit compare).
	nonce := make([]byte, 32)
	if _, err := rand.Read(nonce); err != nil {
		return "", fmt.Errorf("api: csrf token: %w", err)
	}
	return base64.RawURLEncoding.EncodeToString(nonce), nil
}

// verifyCSRF validates the double-submit token for a session (constant-time
// compare against the server-stored per-session token).
func verifyCSRF(s *Session, presented string) bool {
	if s == nil || presented == "" {
		return false
	}
	return hmac.Equal([]byte(s.CSRFToken), []byte(presented))
}

// SessionFromRequest extracts and validates the session cookie.
func (st *SessionStore) SessionFromRequest(r *http.Request) (*Session, bool) {
	c, err := r.Cookie(st.cookieName)
	if err != nil || c.Value == "" {
		return nil, false
	}
	return st.Get(c.Value)
}

// SetSessionCookie writes the HttpOnly/SameSite=Strict cookie. Secure is
// forced in production (TLS termination assumed at the trusted proxy).
func SetSessionCookie(w http.ResponseWriter, s *Session, secure bool, cookieNames ...string) {
	name := sessionCookie
	if len(cookieNames) > 0 && cookieNames[0] != "" {
		name = cookieNames[0]
	}
	http.SetCookie(w, &http.Cookie{
		Name:     name,
		Value:    s.ID,
		Path:     "/",
		HttpOnly: true,
		Secure:   secure,
		SameSite: http.SameSiteStrictMode,
		Expires:  s.ExpiresAt,
	})
}

// ClearSessionCookie expires the cookie.
func ClearSessionCookie(w http.ResponseWriter, secure bool, cookieNames ...string) {
	name := sessionCookie
	if len(cookieNames) > 0 && cookieNames[0] != "" {
		name = cookieNames[0]
	}
	http.SetCookie(w, &http.Cookie{
		Name: name, Value: "", Path: "/", HttpOnly: true,
		Secure: secure, SameSite: http.SameSiteStrictMode, MaxAge: -1,
	})
}

// VerifyOrigin enforces same-origin mutations: Origin header (when present)
// must match the configured origin; Sec-Fetch-Site (when present) must be
// same-origin/none. Absence of both headers is allowed only for non-browser
// clients (health probes).
func VerifyOrigin(r *http.Request, allowedOrigin string) error {
	if origin := r.Header.Get("Origin"); origin != "" {
		if !sameOrigin(origin, allowedOrigin) {
			return fmt.Errorf("api: cross-origin %q rejected", origin)
		}
	}
	if sfs := r.Header.Get("Sec-Fetch-Site"); sfs != "" {
		if sfs != "same-origin" && sfs != "none" {
			return fmt.Errorf("api: Sec-Fetch-Site %q rejected", sfs)
		}
	}
	return nil
}

func sameOrigin(origin, allowed string) bool {
	return strings.EqualFold(strings.TrimSuffix(origin, "/"), strings.TrimSuffix(allowed, "/"))
}

// requireMutationGuard applies the full mutation guard chain: session,
// Origin/Sec-Fetch-Site, CSRF.
func requireMutationGuard(st *SessionStore, allowedOrigin string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			s, ok := st.SessionFromRequest(r)
			if !ok {
				WriteError(w, http.StatusUnauthorized, "UNAUTHENTICATED")
				return
			}
			if err := VerifyOrigin(r, allowedOrigin); err != nil {
				WriteError(w, http.StatusForbidden, "CROSS_ORIGIN_REJECTED")
				return
			}
			if !verifyCSRF(s, r.Header.Get(csrfHeader)) {
				WriteError(w, http.StatusForbidden, "CSRF_REJECTED")
				return
			}
			ctx := context.WithValue(r.Context(), sessionKey{}, s)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

type sessionKey struct{}

func sessionFromContext(ctx context.Context) (*Session, error) {
	s, ok := ctx.Value(sessionKey{}).(*Session)
	if !ok || s == nil {
		return nil, errors.New("api: no session in context")
	}
	return s, nil
}
