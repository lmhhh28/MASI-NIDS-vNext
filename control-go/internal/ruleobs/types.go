// Package ruleobs is the Go-owned rule observation projector. Per-rule
// latest/rollup/status/outcome reference facts are written ONLY here into
// PostgreSQL; Prometheus/Grafana/Edge memory/logs are never rule fact sources.
//
// Layering invariants (AGENTS.md, contracts/p4/rule-observation/v1, golden
// control-rule-formula-status-0001):
//   - effect execution, exact entry installation readback, per-entry
//     direct-counter dataplane match, and packet/action outcome are four
//     SEPARATE dimensions; counter growth never proves action success;
//   - no-hit, no-eligible-traffic, stale/reset/gap/not-measurable are never
//     collapsed into 0% or failure;
//   - an observation epoch is immutable and created only AFTER the effect's
//     exact installation readback converges;
//   - a late sample never crosses an epoch; rollups never stitch across
//     generation/reset-epoch boundaries;
//   - no per-rule digest/match/IP/five-tuple becomes a Prometheus label.
package ruleobs

import "time"

// QualityStatus is the derived per-rule observation status. "reset" is a
// control-side derived status (counter reset-or-wrap); the wire quality union
// of a single observation carries it via reason RESET + status gap, but the
// Go-owned latest/rollup reference keeps the distinct value so a reset is
// never displayed as 0% or failure.
type QualityStatus string

const (
	QualityValid             QualityStatus = "valid"
	QualityPartial           QualityStatus = "partial"
	QualityGap               QualityStatus = "gap"
	QualityReset             QualityStatus = "reset"
	QualityStale             QualityStatus = "stale"
	QualityNoEligibleTraffic QualityStatus = "no-eligible-traffic"
	QualityNotCovered        QualityStatus = "not-covered"
	QualityNotMeasurable     QualityStatus = "not-measurable"
	QualityInvalid           QualityStatus = "invalid"
)

// QualityReason is a stable, bounded reason code attached to a quality status.
type QualityReason string

const (
	ReasonNone               QualityReason = "NONE"
	ReasonReset              QualityReason = "RESET"
	ReasonWrap               QualityReason = "WRAP"
	ReasonSaturated          QualityReason = "SATURATED"
	ReasonGap                QualityReason = "GAP"
	ReasonDuplicate          QualityReason = "DUPLICATE"
	ReasonOutOfOrder         QualityReason = "OUT_OF_ORDER"
	ReasonIdentityDrift      QualityReason = "IDENTITY_DRIFT"
	ReasonPipelineDrift      QualityReason = "PIPELINE_DRIFT"
	ReasonNoEligibleTraffic  QualityReason = "NO_ELIGIBLE_TRAFFIC"
	ReasonNoPacketOracle     QualityReason = "NO_PACKET_ORACLE"
	ReasonCounterUnsupported QualityReason = "COUNTER_UNSUPPORTED"
	ReasonExpired            QualityReason = "EXPIRED"
	ReasonBaseline           QualityReason = "BASELINE_ESTABLISHED"
	ReasonSamplingPartial    QualityReason = "SAMPLING_PARTIAL"
)

// InstallationReadback is the exact-entry installation dimension.
type InstallationReadback string

const (
	ReadbackExact         InstallationReadback = "exact"
	ReadbackMissing       InstallationReadback = "missing"
	ReadbackMismatch      InstallationReadback = "mismatch"
	ReadbackNotMeasurable InstallationReadback = "not-measurable"
)

// OutcomeStatus / OutcomeExpected / OutcomeActual are the independent
// packet/action outcome dimension.
type OutcomeStatus string

const (
	OutcomeObserved      OutcomeStatus = "observed"
	OutcomeNotObserved   OutcomeStatus = "not-observed"
	OutcomeNotMeasurable OutcomeStatus = "not-measurable"
	OutcomeStale         OutcomeStatus = "stale"
	OutcomeInvalid       OutcomeStatus = "invalid"
)

type OutcomeExpected string

const (
	ExpectedForwarded     OutcomeExpected = "forwarded"
	ExpectedDropped       OutcomeExpected = "dropped"
	ExpectedMirrored      OutcomeExpected = "mirrored"
	ExpectedNotMeasurable OutcomeExpected = "not-measurable"
)

type OutcomeActual string

const (
	ActualForwarded     OutcomeActual = "forwarded"
	ActualDropped       OutcomeActual = "dropped"
	ActualMirrored      OutcomeActual = "mirrored"
	ActualNotObserved   OutcomeActual = "not-observed"
	ActualNotMeasurable OutcomeActual = "not-measurable"
)

func validOutcome(status OutcomeStatus, expected OutcomeExpected, actual OutcomeActual) bool {
	switch status {
	case OutcomeObserved, OutcomeNotObserved, OutcomeNotMeasurable, OutcomeStale, OutcomeInvalid:
	default:
		return false
	}
	switch expected {
	case ExpectedForwarded, ExpectedDropped, ExpectedMirrored, ExpectedNotMeasurable:
	default:
		return false
	}
	switch actual {
	case ActualForwarded, ActualDropped, ActualMirrored, ActualNotObserved, ActualNotMeasurable:
		return true
	default:
		return false
	}
}

// EpochKey identifies the immutable observation epoch. Samples arriving with
// an older key than the stored epoch are late and NEVER cross the boundary.
type EpochKey struct {
	ObservationEpoch int64 `json:"observation_epoch"`
	ResetEpoch       int64 `json:"reset_epoch"`
}

// CounterSample is one cumulative direct/eligible counter sample for a rule.
// Values are CUMULATIVE since the reset epoch; deltas are derived, never
// stored as-is by the counter source.
type CounterSample struct {
	Sequence        int64     `json:"sample_sequence"`
	ReadCompletedAt time.Time `json:"read_completed_at"`
	Packets         uint64    `json:"packets"`
	Bytes           uint64    `json:"bytes"`
	EligiblePackets uint64    `json:"eligible_packets"`
	ActionPackets   uint64    `json:"action_packets"`
	OrderStatus     string    `json:"order_status"` // in-order|duplicate|out-of-order|conflict
	GapStatus       string    `json:"gap_status"`   // none|gap|reset-or-wrap|ambiguous
	CoveragePPM     uint32    `json:"sampling_coverage_ppm"`
}

// Derived is the pure formula result for one sample interval.
type Derived struct {
	DirectDeltaPackets   uint64          `json:"direct_delta_packets"`
	DirectDeltaBytes     uint64          `json:"direct_delta_bytes"`
	EligibleDeltaPackets uint64          `json:"eligible_delta_packets"`
	Rate                 float64         `json:"rate"`
	Coverage             float64         `json:"coverage"`
	Quality              QualityStatus   `json:"quality"`
	Reasons              []QualityReason `json:"reasons"`
}

// ObservationEpochRecord is the immutable epoch row (created only after exact
// installation readback of the owning effect operation).
type ObservationEpochRecord struct {
	EpochID                   string               `json:"epoch_id"`
	EffectIntentID            string               `json:"effect_intent_id"`
	OperationID               string               `json:"operation_id"`
	EntityID                  string               `json:"entity_id"`
	RuleID                    string               `json:"rule_id"`
	TargetID                  string               `json:"target_id"`
	CanonicalEntryDigest      string               `json:"canonical_entry_digest"`
	MatchPriorityActionDigest string               `json:"match_priority_action_digest"`
	EpochKey                  EpochKey             `json:"epoch_key"`
	InstallationReadback      InstallationReadback `json:"installation_readback"`
}

// Window sizes for rollups. Windows are keyed by (window_start, epoch_id,
// rule_id) so they NEVER stitch across generation/reset boundaries.
const (
	Window5mUnixMS int64 = 5 * 60 * 1000
	Window1hUnixMS int64 = 60 * 60 * 1000
)

// RetentionBounds are the bounded retention windows (requirement profile).
const (
	Retention5mWindows = 12 * 24 * 7 // 7 days of 5m windows
	Retention1hWindows = 24 * 90     // DB-RULE-001: 90 days of 1h windows
)
