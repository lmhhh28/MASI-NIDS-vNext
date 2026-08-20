package api

import (
	"encoding/json"
	"net/http"
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
	Issuer string `json:"issuer"`
	Subject string `json:"subject"`
	// ACR selects the step-up allowlist entry. Defaults to testLoginACR.
	ACR string `json:"acr"`
	// StepUpType overrides the canonical step-up enum; defaults to passkey.
	StepUp string `json:"step_up_type"`
}

// handleTestLogin mints a session for a caller-supplied identity. It is the
// test-profile substitute for a real OIDC code exchange: it mints IDENTITY
// ONLY. Authorization still flows through the RoleScopeMapping loaded at
// startup — a default-deny mapping still denies every mutation. The route is
// registered only when deps.TestLogin is set (test runtime profile); production
// never wires it and config enforces test↔test-fake / production↔production-mtls
// cannot be mixed.
func handleTestLogin(store *SessionStore, secure bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var req testLoginRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			WriteError(w, http.StatusBadRequest, "BAD_REQUEST")
			return
		}
		if req.Issuer == "" || req.Subject == "" {
			WriteError(w, http.StatusBadRequest, "MISSING_IDENTITY")
			return
		}
		acr := req.ACR
		if acr == "" {
			acr = testLoginACR
		}
		stepUp := req.StepUp
		if stepUp == "" {
			stepUp = string(security.StepUpPasskey)
		}
		// Reject non-phishing-resistant step-up requests explicitly so a test
		// cannot silently mint an R2/R3-ineligible session and then assert the
		// wrong rejection reason.
		if !security.StepUpType(stepUp).IsPhishingResistant() {
			WriteError(w, http.StatusBadRequest, "STEP_UP_NOT_PHISHING_RESISTANT")
			return
		}
		now := time.Now()
		claims := actorClaims{
			Issuer:   req.Issuer,
			Subject:  req.Subject,
			Audience: audienceClaim{"masi-web"},
			IssuedAt: now.Unix(),
			AuthTime: now.Unix(),
			Acr:      acr,
			Amr:      []string{"passkey"},
		}
		s, err := store.Create(claims, stepUp)
		if err != nil {
			WriteError(w, http.StatusServiceUnavailable, "SESSION_BOUND")
			return
		}
		SetSessionCookie(w, s, secure, store.cookieName)
		actorRef := security.Actor{Issuer: claims.Issuer, Subject: claims.Subject}.String()
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"actor_ref": actorRef})
	}
}