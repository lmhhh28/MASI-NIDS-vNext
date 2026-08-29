package ruleobs

import (
	"errors"
	"fmt"
	"math"

	"masi-nids/control-go/internal/security"
)

type nonZeroDigestPattern struct{}

func (nonZeroDigestPattern) MatchString(value string) bool { return security.ValidDigest(value) }

var digestPattern nonZeroDigestPattern

// ComputeDerived is the pure rule-observation formula (golden
// control-rule-formula-status-0001; cross-language runners Go/Rust/TS/Python
// must derive identical values).
//
//	prev, cur — consecutive cumulative samples within the SAME epoch; cur must
//	             carry Sequence > prev.Sequence and gap/order statuses of the
//	             cur sample.
//
// Formula: direct_delta/eligible_delta. Zero-traffic, no-hit, reset, gap,
// stale and not-measurable are NEVER encoded as zero rate/failure:
//   - eligible_delta == 0 (with direct_delta == 0) -> no-eligible-traffic
//     (rate stays 0 but the status carries the semantics);
//   - reset-or-wrap gap status -> reset;
//   - gap -> gap;
//   - duplicate/out-of-order/conflict -> invalid with the matching reason.
func ComputeDerived(prev, cur CounterSample) (Derived, error) {
	if cur.Sequence <= prev.Sequence {
		return Derived{}, fmt.Errorf("ruleobs: sample sequence must advance (prev=%d cur=%d)", prev.Sequence, cur.Sequence)
	}
	d := Derived{Reasons: []QualityReason{ReasonNone}}
	if cur.CoveragePPM == 0 || cur.CoveragePPM > 1_000_000 {
		return Derived{}, fmt.Errorf("ruleobs: sampling coverage ppm must be 1..1000000")
	}
	// Counter deltas: cumulative monotonic since reset epoch. A decrease is a
	// reset-or-wrap signal, never a negative delta.
	dpu := cur.Packets - prev.Packets
	dbu := cur.Bytes - prev.Bytes
	deu := cur.EligiblePackets - prev.EligiblePackets
	if cur.Packets < prev.Packets || cur.Bytes < prev.Bytes || cur.EligiblePackets < prev.EligiblePackets {
		return Derived{Quality: QualityReset, Reasons: []QualityReason{ReasonReset}}, nil
	}
	d.DirectDeltaPackets = dpu
	d.DirectDeltaBytes = dbu
	d.EligibleDeltaPackets = deu

	switch cur.GapStatus {
	case "reset-or-wrap":
		return Derived{Quality: QualityReset, Reasons: []QualityReason{ReasonReset, ReasonWrap}}, nil
	case "ambiguous":
		return Derived{Quality: QualityGap, Reasons: []QualityReason{ReasonGap}}, nil
	case "gap":
		return Derived{Quality: QualityGap, Reasons: []QualityReason{ReasonGap}}, nil
	case "none":
	default:
		return Derived{}, fmt.Errorf("ruleobs: unknown gap_status %q", cur.GapStatus)
	}
	switch cur.OrderStatus {
	case "in-order":
		d.Coverage = float64(cur.CoveragePPM) / 1_000_000
	case "duplicate":
		return Derived{Quality: QualityInvalid, Reasons: []QualityReason{ReasonDuplicate}}, nil
	case "out-of-order":
		return Derived{Quality: QualityInvalid, Reasons: []QualityReason{ReasonOutOfOrder}}, nil
	case "conflict":
		return Derived{Quality: QualityInvalid, Reasons: []QualityReason{ReasonIdentityDrift}}, nil
	default:
		return Derived{}, fmt.Errorf("ruleobs: unknown order_status %q", cur.OrderStatus)
	}

	if d.EligibleDeltaPackets == 0 {
		if d.DirectDeltaPackets == 0 {
			// No eligible traffic AND no hits: NOT 0% effectiveness — a
			// distinct no-eligible-traffic status (golden zero_traffic_not_zero).
			return Derived{DirectDeltaPackets: 0, DirectDeltaBytes: 0, EligibleDeltaPackets: 0,
				Rate: 0, Coverage: d.Coverage, Quality: QualityNoEligibleTraffic,
				Reasons: []QualityReason{ReasonNoEligibleTraffic}}, nil
		}
		// Hits without eligible denominator: not measurable (denominator lost).
		return Derived{DirectDeltaPackets: d.DirectDeltaPackets, DirectDeltaBytes: d.DirectDeltaBytes,
			EligibleDeltaPackets: 0, Rate: 0, Coverage: d.Coverage,
			Quality: QualityNotMeasurable, Reasons: []QualityReason{ReasonNoEligibleTraffic}}, nil
	}
	d.Rate = float64(d.DirectDeltaPackets) / float64(d.EligibleDeltaPackets)
	if math.IsNaN(d.Rate) || math.IsInf(d.Rate, 0) {
		return Derived{Quality: QualityNotMeasurable, Reasons: []QualityReason{ReasonCounterUnsupported}}, nil
	}
	if d.Rate > 1 {
		// Numerator above denominator: counter drift — never >100%.
		return Derived{DirectDeltaPackets: d.DirectDeltaPackets, DirectDeltaBytes: d.DirectDeltaBytes,
			EligibleDeltaPackets: d.EligibleDeltaPackets, Rate: 1, Coverage: d.Coverage,
			Quality: QualityInvalid, Reasons: []QualityReason{ReasonIdentityDrift}}, nil
	}
	if d.Coverage < 1 {
		d.Quality = QualityPartial
		d.Reasons = []QualityReason{ReasonSamplingPartial}
	} else {
		d.Quality = QualityValid
	}
	return d, nil
}

// StaleDerived is the derived result for a rule whose samples expired without
// refresh (stale != 0% and != failure).
func StaleDerived() Derived {
	return Derived{Quality: QualityStale, Reasons: []QualityReason{ReasonExpired}}
}

// NotMeasurableDerived is the derived result when the counter is unsupported.
func NotMeasurableDerived() Derived {
	return Derived{Quality: QualityNotMeasurable, Reasons: []QualityReason{ReasonCounterUnsupported}}
}

// EpochCrossed reports whether a sample's epoch key is stale relative to the
// stored epoch. Late samples never cross the boundary (golden
// late_sample_cross_epoch_rejected).
func EpochCrossed(stored, sample EpochKey) bool {
	return sample.ObservationEpoch < stored.ObservationEpoch ||
		sample.ResetEpoch < stored.ResetEpoch
}

// ValidateEpochForCreation enforces that an epoch row may be created only
// after the owning effect operation's exact entry-installation readback.
func ValidateEpochForCreation(r ObservationEpochRecord) error {
	if r.EpochID == "" || r.EntityID == "" || r.RuleID == "" || r.TargetID == "" {
		return errors.New("ruleobs: epoch_id/entity_id/rule_id/target_id required")
	}
	if r.EffectIntentID == "" || r.OperationID == "" {
		return errors.New("ruleobs: effect_intent_id/operation_id required")
	}
	if !digestPattern.MatchString(r.CanonicalEntryDigest) || !digestPattern.MatchString(r.MatchPriorityActionDigest) {
		return errors.New("ruleobs: canonical entry/action digests malformed")
	}
	if r.InstallationReadback != ReadbackExact {
		return fmt.Errorf("ruleobs: epoch creation requires exact installation readback (got %s)", r.InstallationReadback)
	}
	if r.EpochKey.ObservationEpoch < 1 || r.EpochKey.ResetEpoch < 1 {
		return errors.New("ruleobs: observation_epoch/reset_epoch >= 1 required")
	}
	return nil
}
