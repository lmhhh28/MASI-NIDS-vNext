package security

import (
	"errors"
	"fmt"
	"regexp"
)

// RiskLevel is the effect risk classification (ADR-0001 §2).
type RiskLevel string

const (
	R0 RiskLevel = "R0" // read-only; no Proposal/Decision/Intent for pure read
	R1 RiskLevel = "R1" // operator may self-approve (same stable actor proposer+approver)
	R2 RiskLevel = "R2" // maker-checker: different scoped Operator, phishing-resistant step-up <=5min
	R3 RiskLevel = "R3" // typed baseline change: Platform Admin maker + different Operator checker
)

// Scope is a bounded authorization scope (target/risk/effect/model/plugin-
// limited). "Has Operator role" cannot approve an arbitrary device/effect —
// the scope binds the exact proposal digest/target-set (SEC-002).
type Scope struct {
	ScopeID         string              `json:"scope_id"`
	TargetSetDigest string              `json:"target_set_digest"`
	EffectKinds     []string            `json:"effect_kinds"`
	DataClass       string              `json:"data_class,omitempty"`
	Levels          []AuthzContextLevel `json:"levels"`
}

// RoleScopeMapping is the versioned, digest-pinned role-to-scope mapping. It is
// least-privilege, default-deny, and recorded to every Decision. IdP group
// names are NOT hardcoded (SEC-002). A mapping change only affects future authz
// after re-validation; it cannot overwrite historical Decisions.
type RoleScopeMapping struct {
	Version     string             `json:"version"`
	Digest      string             `json:"digest"`
	ActorScopes map[string][]Scope `json:"actor_scopes"` // actor_ref -> scopes
	DefaultDeny bool               `json:"default_deny"`
}

var digestRE = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

// Validate checks the mapping is well-formed and default-deny.
func (m *RoleScopeMapping) Validate() error {
	if !m.DefaultDeny {
		return errors.New("security: role mapping must be default-deny")
	}
	if !digestRE.MatchString(m.Digest) {
		return fmt.Errorf("security: role mapping digest malformed: %s", m.Digest)
	}
	if m.Version == "" {
		return errors.New("security: role mapping version required")
	}
	for actorRef, scopes := range m.ActorScopes {
		if !identityRE.MatchString(actorRef) || len(scopes) == 0 || len(scopes) > 256 {
			return fmt.Errorf("security: malformed actor scope entry")
		}
		seen := map[string]struct{}{}
		for _, scope := range scopes {
			if scope.ScopeID == "" || !digestRE.MatchString(scope.TargetSetDigest) || len(scope.EffectKinds) == 0 || len(scope.EffectKinds) > 64 || len(scope.Levels) == 0 {
				return fmt.Errorf("security: malformed scope grant for %s", actorRef)
			}
			if _, ok := seen[scope.ScopeID]; ok {
				return fmt.Errorf("security: duplicate scope %s for %s", scope.ScopeID, actorRef)
			}
			seen[scope.ScopeID] = struct{}{}
			for _, level := range scope.Levels {
				switch level {
				case LevelAnalyst, LevelOperator, LevelScopedOperator, LevelPlatformAdmin, LevelAuditor:
				default:
					return fmt.Errorf("security: unknown level %s", level)
				}
			}
		}
	}
	return nil
}

// Authorize is the default-deny authorization check. It returns the scopes
// granted to the actor for the given level, or an error if none. The check is
// server-side; route visibility is never authorization (SEC-002).
func (m *RoleScopeMapping) Authorize(actor Actor, level AuthzContextLevel) ([]Scope, error) {
	all, ok := m.ActorScopes[actor.String()]
	if !ok || len(all) == 0 {
		return nil, fmt.Errorf("security: %s denied for %s (default-deny)", level, actor.String())
	}
	scopes := make([]Scope, 0, len(all))
	for _, scope := range all {
		for _, granted := range scope.Levels {
			if granted == level {
				scopes = append(scopes, scope)
				break
			}
		}
	}
	if len(scopes) == 0 {
		return nil, fmt.Errorf("security: %s denied for %s (no matching role grant)", level, actor.String())
	}
	return scopes, nil
}

// AuthorizeScope verifies that a role grant covers the exact resource scope,
// target-set digest and effect kind. Empty requested dimensions are ignored only
// for read-only list projections where ScopeID is still mandatory.
func (m *RoleScopeMapping) AuthorizeScope(actor Actor, level AuthzContextLevel, scopeID, effectKind, targetSetDigest string) (Scope, error) {
	scopes, err := m.Authorize(actor, level)
	if err != nil {
		return Scope{}, err
	}
	for _, scope := range scopes {
		if scope.ScopeID != scopeID {
			continue
		}
		if targetSetDigest != "" && scope.TargetSetDigest != targetSetDigest {
			continue
		}
		if effectKind != "" {
			matched := false
			for _, allowed := range scope.EffectKinds {
				if allowed == effectKind {
					matched = true
					break
				}
			}
			if !matched {
				continue
			}
		}
		return scope, nil
	}
	return Scope{}, fmt.Errorf("security: %s denied for exact scope %q", level, scopeID)
}

// ScopeIDs returns the distinct resource scopes granted to an actor across all
// roles. It is used only to build server-side SQL predicates, never to authorize
// a mutation.
func (m *RoleScopeMapping) ScopeIDs(actor Actor) ([]string, error) {
	all, ok := m.ActorScopes[actor.String()]
	if !ok || len(all) == 0 {
		return nil, fmt.Errorf("security: no scopes for %s", actor.String())
	}
	seen := map[string]struct{}{}
	out := make([]string, 0, len(all))
	for _, scope := range all {
		if scope.ScopeID == "" || len(scope.Levels) == 0 {
			continue
		}
		if _, exists := seen[scope.ScopeID]; !exists {
			seen[scope.ScopeID] = struct{}{}
			out = append(out, scope.ScopeID)
		}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("security: no usable scopes for %s", actor.String())
	}
	return out, nil
}

// CanApprove enforces the R0-R3 maker-checker rules:
//   - R0: pure reads create no governance facts; the sole first-release R0
//     mutation (bounded capture) requires an exact scoped Operator decision.
//   - R1: same stable Operator may be proposer and approver.
//   - R2: approver MUST be a different stable actor than proposer, with fresh
//     phishing-resistant step-up.
//   - R3: Platform Admin maker + different Operator checker, both step-up.
//
// Platform Admin/service-account/Agent/Frontend-BFF cannot replace a human
// approver (SEC-002). Self-approval for R2/R3 is rejected.
func CanApprove(risk RiskLevel, proposer Actor, proposerLevel AuthzContextLevel, approver Actor, approverCtx AuthzContext) error {
	switch risk {
	case R0:
		if approverCtx.Level != LevelOperator && approverCtx.Level != LevelScopedOperator {
			return errors.New("security: R0 mutation approver must be operator/scoped-operator")
		}
		return nil
	case R1:
		if !proposer.Equal(approver) {
			return errors.New("security: R1 allows same-Operator proposer/approver only")
		}
		if approverCtx.Level != LevelOperator && approverCtx.Level != LevelScopedOperator {
			return errors.New("security: R1 approver must be operator/scoped-operator")
		}
		if proposerLevel != LevelOperator && proposerLevel != LevelScopedOperator {
			return errors.New("security: R1 proposer must be operator/scoped-operator")
		}
		return nil
	case R2:
		if proposer.Equal(approver) {
			return errors.New("security: R2 self-approval rejected (proposer != approver)")
		}
		if !approverCtx.R2StepUpFresh() {
			return errors.New("security: R2 requires fresh phishing-resistant step-up <=5min")
		}
		if approverCtx.Level != LevelOperator && approverCtx.Level != LevelScopedOperator {
			return errors.New("security: R2 approver must be operator/scoped-operator")
		}
		return nil
	case R3:
		if proposerLevel != LevelPlatformAdmin {
			return errors.New("security: R3 maker must be platform-admin")
		}
		if proposer.Equal(approver) {
			return errors.New("security: R3 self-approval rejected (maker != checker)")
		}
		if !approverCtx.R2StepUpFresh() {
			return errors.New("security: R3 checker requires fresh phishing-resistant step-up")
		}
		if approverCtx.Level != LevelOperator && approverCtx.Level != LevelScopedOperator {
			return errors.New("security: R3 checker must be operator/scoped-operator")
		}
		return nil
	}
	return fmt.Errorf("security: unknown risk level %s", risk)
}
