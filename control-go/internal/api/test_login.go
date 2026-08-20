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
		host, _, err := net.SplitHostPort(r.RemoteAddr)
		if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() ||
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
		now := time.Now()
		claims := actorClaims{
			Issuer:   req.Issuer,
			Subject:  req.Subject,
			Audience: audienceClaim{"masi-web"},
			IssuedAt: now.Unix(),
			AuthTime: now.Unix(),
			Acr:      testLoginACR,
			Amr:      []string{"passkey"},
		}
		s, err := store.Create(claims, string(security.StepUpPasskey))
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
