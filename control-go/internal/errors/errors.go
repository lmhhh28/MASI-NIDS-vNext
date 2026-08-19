// Package errors defines the stable error namespace for Go Control Core.
// Every error carries a stable Reason code matching the contracts
// `^reason_code` pattern (`^[A-Z][A-Z0-9_]{0,63}$`) and a status namespace
// distinct per domain (CONTRACT-003: no single generic "status" string mixing
// HOLD/stale/unknown/failed/unavailable).
package errors

import (
	"errors"
	"fmt"
)

// Status is the domain status namespace. Each domain (effect, model, firewall,
// target, plugin-statistics, rule-observation, ui-transport) uses a distinct
// status enum; they are never collapsed into one generic string.
type Status string

const (
	// EffectStatusNamespace — effect intent/attempt lifecycle.
	EffectApplied     Status = "applied"
	EffectHold        Status = "hold"
	EffectUnknown     Status = "unknown"
	EffectReconciling Status = "reconciling"
	EffectUnclaimed   Status = "unclaimed"
	EffectClaimed     Status = "claimed"
	EffectFinalized   Status = "finalized"

	// ModelStatusNamespace — model binding/rollout.
	ModelCurrent       Status = "current"
	ModelPrevious      Status = "previous"
	ModelResumePending Status = "resume_pending"
	ModelUnavailable   Status = "unavailable"

	// ProjectionStatusNamespace — web/UI transport.
	ProjectionStale    Status = "stale"
	ProjectionObserved Status = "observed"
	ProjectionDesired  Status = "desired"
)

// Error is the stable, machine-readable control-core error. Reason is a stable
// code; StatusNamespace is the domain the status belongs to; Detail is
// human-readable and may be redacted.
type Error struct {
	Reason          string
	StatusNamespace string
	Status          Status
	Detail          string
	Wrapped         error
}

func (e *Error) Error() string {
	if e.Wrapped != nil {
		return fmt.Sprintf("%s: %s: %v", e.Reason, e.Detail, e.Wrapped)
	}
	return fmt.Sprintf("%s: %s", e.Reason, e.Detail)
}

func (e *Error) Unwrap() error { return e.Wrapped }

// New constructs a control-core error with a stable reason code.
func New(reason string, detail string) *Error {
	return &Error{Reason: reason, Detail: detail}
}

// Wrap attaches a stable reason code to an existing error.
func Wrap(err error, reason string, detail string) *Error {
	return &Error{Reason: reason, Detail: detail, Wrapped: err}
}

// WithStatus attaches a domain status to a control-core error.
func (e *Error) WithStatus(ns string, s Status) *Error {
	e.StatusNamespace = ns
	e.Status = s
	return e
}

// As lets callers extract a *Error via errors.As.
var _ interface{ Is(error) bool } = (*Error)(nil)

// Is reports whether target is a *Error with the same Reason.
func (e *Error) Is(target error) bool {
	var t *Error
	if errors.As(target, &t) {
		return e.Reason == t.Reason
	}
	return false
}

// Reason extracts the stable reason code from err, or "" if err is not a
// *Error. Used to record reason_code in persisted facts and audit.
func Reason(err error) string {
	var e *Error
	if errors.As(err, &e) {
		return e.Reason
	}
	return ""
}

// Common stable reason codes (frozen; new codes require a contract revision).
const (
	ReasonCommitted               = "COMMITTED"
	ReasonIdempotent              = "IDEMPOTENT"
	ReasonDigestConflict          = "DIGEST_CONFLICT"
	ReasonLateGeneration          = "LATE_GENERATION"
	ReasonStale                   = "STALE"
	ReasonHold                    = "HOLD"
	ReasonTimeoutUnknown          = "TIMEOUT_UNKNOWN"
	ReasonClaimed                 = "CLAIMED"
	ReasonCasConflict             = "CAS_CONFLICT"
	ReasonCasExpired              = "CAS_EXPIRED"
	ReasonCasFenced               = "CAS_FENCED"
	ReasonIncarnationRotated      = "INCARNATION_ROTATED"
	ReasonUnauthorized            = "UNAUTHORIZED"
	ReasonStepUpStale             = "STEP_UP_STALE"
	ReasonStepUpMissing           = "STEP_UP_MISSING"
	ReasonUnknownMajor            = "UNKNOWN_MAJOR"
	ReasonUnverifiedMinor         = "UNVERIFIED_MINOR"
	ReasonDigestDrift             = "DIGEST_DRIFT"
	ReasonCapacityExceeded        = "CAPACITY_EXCEEDED"
	ReasonP4InfoDrift             = "P4INFO_DRIFT"
	ReasonFleetParentNotClaimable = "FLEET_PARENT_NOT_CLAIMABLE"
	ReasonRunNotInEffectQueue     = "RUN_NOT_IN_EFFECT_QUEUE"
	ReasonSelectorMismatch        = "SELECTOR_MISMATCH"
	ReasonReadbackMismatch        = "READBACK_MISMATCH"
)
