// Package security implements Go Control Core's authorization foundation:
// stable actor identity (exact (iss, sub), not email/display — SEC-002),
// versioned role/scope mapping (least-privilege, default-deny, recorded to
// every Decision), and the OIDC BFF scaffolding (Go does authorization; the
// reverse proxy only provides verified identity context).
package security

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"regexp"
)

// Actor is the stable human identity. The stable comparison key is exact
// (Issuer, Subject) — never email or display name. R2 maker-checker compares
// stable actors; proposer != approver for R2 (ADR-0001 §2).
type Actor struct {
	Issuer  string `json:"iss"`
	Subject string `json:"sub"`
}

// identityRE is the contracts `#/$defs/identity` pattern that every persisted
// actor_ref MUST match: `^[A-Za-z0-9][A-Za-z0-9._:-]*$`. Note it forbids "/",
// so a raw issuer URL (https://...) cannot be used verbatim as actor_ref.
var identityRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]*$`)

// String returns the stable actor_ref used in persisted records and audit. It
// hashes the complete length-delimited (iss,sub) tuple, including issuer scheme
// and path. This avoids the realm/path collisions caused by host-only keys while
// still satisfying the contract identity grammar.
func (a Actor) String() string {
	h := sha256.New()
	fmt.Fprintf(h, "%d:%s|%d:%s", len(a.Issuer), a.Issuer, len(a.Subject), a.Subject)
	return "actor:" + hex.EncodeToString(h.Sum(nil))
}

// ParseActor intentionally refuses to reverse an opaque actor_ref. Authorization
// code must reconstruct Actor from separately persisted issuer/subject columns;
// guessing an issuer from a display key would break exact maker-checker checks.
func ParseActor(s string) (Actor, error) {
	if !identityRE.MatchString(s) {
		return Actor{}, fmt.Errorf("security: malformed actor_ref %q", s)
	}
	return Actor{}, errors.New("security: opaque actor_ref is not reversible; persisted issuer/subject required")
}

// Equal reports whether two actors are the same stable identity.
func (a Actor) Equal(b Actor) bool { return a.Issuer == b.Issuer && a.Subject == b.Subject }

var (
	issRE = regexp.MustCompile(`^https?://[A-Za-z0-9._:/-]+$`)
	subRE = regexp.MustCompile(`^[A-Za-z0-9._:/-]{1,255}$`)
)

// Validate checks the actor identity is well-formed.
func (a Actor) Validate() error {
	if !issRE.MatchString(a.Issuer) {
		return errors.New("security: actor issuer malformed")
	}
	if !subRE.MatchString(a.Subject) {
		return errors.New("security: actor subject malformed")
	}
	return nil
}

// AuthzContextLevel is the verified authorization context level for a decision.
type AuthzContextLevel string

const (
	LevelOperator       AuthzContextLevel = "operator"
	LevelScopedOperator AuthzContextLevel = "scoped-operator"
	LevelPlatformAdmin  AuthzContextLevel = "platform-admin"
	LevelAnalyst        AuthzContextLevel = "analyst"
	LevelAuditor        AuthzContextLevel = "auditor"
)

// StepUpType is the phishing-resistant step-up mechanism (R2/R3 require
// WebAuthn/FIDO2/passkey/hardware-key within 5 minutes — ADR-0001 §2).
type StepUpType string

const (
	StepUpNone        StepUpType = "none"
	StepUpWebAuthn    StepUpType = "webauthn-fido2"
	StepUpPasskey     StepUpType = "passkey"
	StepUpHardwareKey StepUpType = "hardware-key"
)

// IsPhishingResistant reports whether the step-up type is phishing-resistant.
func (s StepUpType) IsPhishingResistant() bool {
	switch s {
	case StepUpWebAuthn, StepUpPasskey, StepUpHardwareKey:
		return true
	}
	return false
}

// AuthzContext is the authorization context bound to a Decision. R2 requires
// phishing-resistant step-up with age <= 300s; R3 checker also requires it;
// R1 allows same-Operator proposer/approver (ADR-0001 §2, SEC-002).
type AuthzContext struct {
	StepUpType        StepUpType        `json:"step_up_type"`
	StepUpAgeMS       int               `json:"step_up_age_ms"`
	PhishingResistant bool              `json:"phishing_resistant"`
	RoleMappingDigest string            `json:"role_mapping_version"` // digest of the versioned mapping
	Level             AuthzContextLevel `json:"authz_context_level"`
}

// R2StepUpFresh reports whether the step-up is phishing-resistant and fresh
// (age <= 300000ms = 5 minutes).
func (a AuthzContext) R2StepUpFresh() bool {
	return a.PhishingResistant && a.StepUpType.IsPhishingResistant() &&
		a.StepUpAgeMS >= 0 && a.StepUpAgeMS <= 300000
}

// CanonicalStepUp maps accepted OIDC AMR spellings to the closed project enum.
func CanonicalStepUp(amr []string) StepUpType {
	for _, method := range amr {
		switch method {
		case "webauthn", "webauthn-fido2", "fido2":
			return StepUpWebAuthn
		case "passkey":
			return StepUpPasskey
		case "hwk", "hardware-key":
			return StepUpHardwareKey
		}
	}
	return StepUpNone
}
