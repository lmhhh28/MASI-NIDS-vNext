package api

import (
	"encoding/json"
	"io"
	"net"
	"net/http"
	"strings"
	"time"

	"masi-nids/control-go/internal/security"
)

// testLoginACR is the ACR the test-only login route advertises to callers. It
// must be listed in cfg.OIDCStepUpACRValues (set in the e2e config) so that
// SessionStore.Create populates LastStepUp and R2/R3 step-up checks pass.
const testLoginACR = "urn:masi:acr:phishing-resistant"

const (
	browserTestLoginIssuer  = "https://idp.example"
	browserTestLoginSubject = "operator-a"
)

// testLoginRequest is the body of POST /oidc/test-login. It mints an
// authenticated session with caller-supplied actor identity and (when the
// requested ACR is in the store allowlist) a fresh phishing-resistant step-up.
type testLoginRequest struct {
	Issuer  string `json:"issuer"`
	Subject string `json:"subject"`
}

// handleTestLogin mints a session for a caller-supplied identity. It is the
// test-profile substitute for a real OIDC code exchange: it mints IDENTITY
// ONLY. Authorization still flows through the RoleScopeMapping loaded at
// startup — a default-deny mapping still denies every mutation. The route is
// registered only when deps.TestLogin is set (test runtime profile); production
// never wires it and config enforces test↔test-fake / production↔production-mtls
// cannot be mixed.
func handleTestLogin(store *SessionStore, secure bool, allowedOrigin string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// This route exists solely for the explicit loopback test profile. Require
		// both a loopback transport peer and browser same-origin signals so a page
		// on another origin cannot mint a privileged fixture session against a
		// developer's local process.
		if !isLoopbackRequest(r) ||
			r.Header.Get("Origin") == "" || !sameOrigin(r.Header.Get("Origin"), allowedOrigin) ||
			r.Header.Get("Sec-Fetch-Site") != "same-origin" {
			WriteError(w, http.StatusForbidden, "TEST_LOGIN_ORIGIN_REJECTED")
			return
		}
		if mediaType := strings.ToLower(strings.TrimSpace(strings.Split(r.Header.Get("Content-Type"), ";")[0])); mediaType != "application/json" {
			WriteError(w, http.StatusUnsupportedMediaType, "CONTENT_TYPE_REJECTED")
			return
		}
		var req testLoginRequest
		dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&req); err != nil {
			WriteError(w, http.StatusBadRequest, "BAD_REQUEST")
			return
		}
		if err := dec.Decode(&struct{}{}); err != io.EOF {
			WriteError(w, http.StatusBadRequest, "BAD_REQUEST")
			return
		}
		if req.Issuer == "" || req.Subject == "" {
			WriteError(w, http.StatusBadRequest, "MISSING_IDENTITY")
			return
		}
		actor := security.Actor{Issuer: req.Issuer, Subject: req.Subject}
		if err := actor.Validate(); err != nil {
			WriteError(w, http.StatusBadRequest, "IDENTITY_MALFORMED")
			return
		}
		s, err := mintTestSession(store, actor)
		if err != nil {
			WriteError(w, http.StatusServiceUnavailable, "SESSION_BOUND")
			return
		}
		SetSessionCookie(w, s, secure, store.cookieName)
		actorRef := actor.String()
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"actor_ref": actorRef})
	}
}

// handleBrowserTestLogin is the human-facing counterpart to handleTestLogin.
// It is wired only when the test runtime explicitly enables TestLogin and has
// no real OIDC client. The identity is fixed (never caller-controlled), the
// transport peer must be loopback, and cross-site navigations are rejected.
// Production therefore cannot use this path, while a developer can follow the
// ordinary same-origin login CTA without manually extracting an HttpOnly cookie.
func handleBrowserTestLogin(store *SessionStore, secure bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		fetchSite := r.Header.Get("Sec-Fetch-Site")
		if !isLoopbackRequest(r) || (fetchSite != "" && fetchSite != "none" && fetchSite != "same-origin") {
			WriteError(w, http.StatusForbidden, "TEST_LOGIN_ORIGIN_REJECTED")
			return
		}
		if r.URL.RawQuery != "" || r.ContentLength > 0 {
			WriteError(w, http.StatusBadRequest, "BAD_REQUEST")
			return
		}
		actor := security.Actor{Issuer: browserTestLoginIssuer, Subject: browserTestLoginSubject}
		s, err := mintTestSession(store, actor)
		if err != nil {
			WriteError(w, http.StatusServiceUnavailable, "SESSION_BOUND")
			return
		}
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Referrer-Policy", "same-origin")
		SetSessionCookie(w, s, secure, store.cookieName)
		http.Redirect(w, r, "/", http.StatusSeeOther)
	}
}

func mintTestSession(store *SessionStore, actor security.Actor) (*Session, error) {
	now := time.Now()
	return store.Create(actorClaims{
		Issuer:   actor.Issuer,
		Subject:  actor.Subject,
		Audience: audienceClaim{"masi-web"},
		IssuedAt: now.Unix(),
		AuthTime: now.Unix(),
		Acr:      testLoginACR,
		Amr:      []string{"passkey"},
	}, string(security.StepUpPasskey))
}

func isLoopbackRequest(r *http.Request) bool {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	ip := net.ParseIP(host)
	return err == nil && ip != nil && ip.IsLoopback()
}
