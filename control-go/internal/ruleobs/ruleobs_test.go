package ruleobs

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// goldenRuleFormula mirrors the frozen cross-language golden
// contracts/golden/control/rule-formula-status-v1.json (digest pinned by the
// contract validator). The test loads the file when present and verifies the
// Go runner derives the identical values.
type goldenRuleFormula struct {
	Input struct {
		Samples []struct {
			OffsetMs        int64  `json:"offset_ms"`
			Packets         uint64 `json:"packets"`
			Bytes           uint64 `json:"bytes"`
			EligiblePackets uint64 `json:"eligible_packets"`
			ActionPackets   uint64 `json:"action_packets"`
		} `json:"samples"`
	} `json:"input"`
	Expected struct {
		DirectDeltaPackets   uint64  `json:"direct_delta_packets"`
		DirectDeltaBytes     uint64  `json:"direct_delta_bytes"`
		EligibleDeltaPackets uint64  `json:"eligible_delta_packets"`
		Rate                 float64 `json:"rate"`
		Coverage             float64 `json:"coverage"`
		Quality              string  `json:"quality"`
		ResetQuality         string  `json:"reset_quality"`
		GapQuality           string  `json:"gap_quality"`
		StaleQuality         string  `json:"stale_quality"`
		NotMeasurableQuality string  `json:"not_measurable_quality"`
	} `json:"expected"`
}

func goldenPath() string {
	// internal/ruleobs -> control-go -> contracts
	return filepath.Join("..", "..", "..", "contracts", "golden", "control", "rule-formula-status-v1.json")
}

func TestGoldenRuleFormula(t *testing.T) {
	raw, err := os.ReadFile(goldenPath())
	if err != nil {
		t.Fatalf("read golden: %v", err)
	}
	var g goldenRuleFormula
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatalf("parse golden: %v", err)
	}
	if len(g.Input.Samples) != 2 {
		t.Fatalf("golden must carry 2 samples, got %d", len(g.Input.Samples))
	}
	base := time.UnixMilli(1_700_000_000_000)
	mk := func(i int, order, gap string) CounterSample {
		s := g.Input.Samples[i]
		return CounterSample{
			Sequence:        int64(i + 1),
			ReadCompletedAt: base.Add(time.Duration(s.OffsetMs) * time.Millisecond),
			Packets:         s.Packets, Bytes: s.Bytes,
			EligiblePackets: s.EligiblePackets, ActionPackets: s.ActionPackets,
			OrderStatus: order, GapStatus: gap,
			CoveragePPM: 1_000_000,
		}
	}
	d, err := ComputeDerived(mk(0, "in-order", "none"), mk(1, "in-order", "none"))
	if err != nil {
		t.Fatalf("golden compute: %v", err)
	}
	if d.DirectDeltaPackets != g.Expected.DirectDeltaPackets {
		t.Fatalf("direct_delta_packets: got %d want %d", d.DirectDeltaPackets, g.Expected.DirectDeltaPackets)
	}
	if d.DirectDeltaBytes != g.Expected.DirectDeltaBytes {
		t.Fatalf("direct_delta_bytes: got %d want %d", d.DirectDeltaBytes, g.Expected.DirectDeltaBytes)
	}
	if d.EligibleDeltaPackets != g.Expected.EligibleDeltaPackets {
		t.Fatalf("eligible_delta_packets: got %d want %d", d.EligibleDeltaPackets, g.Expected.EligibleDeltaPackets)
	}
	if d.Rate != g.Expected.Rate {
		t.Fatalf("rate: got %.17g want %.17g", d.Rate, g.Expected.Rate)
	}
	if d.Coverage != g.Expected.Coverage {
		t.Fatalf("coverage: got %v want %v", d.Coverage, g.Expected.Coverage)
	}
	if string(d.Quality) != g.Expected.Quality {
		t.Fatalf("quality: got %s want %s", d.Quality, g.Expected.Quality)
	}
	// Derived special statuses must never be valid/zero.
	if string(StaleDerived().Quality) != g.Expected.StaleQuality {
		t.Fatalf("stale: got %s want %s", StaleDerived().Quality, g.Expected.StaleQuality)
	}
	if string(NotMeasurableDerived().Quality) != g.Expected.NotMeasurableQuality {
		t.Fatalf("not-measurable: got %s want %s", NotMeasurableDerived().Quality, g.Expected.NotMeasurableQuality)
	}
	// Reset via reset-or-wrap gap status.
	dr, err := ComputeDerived(mk(0, "in-order", "none"), mk(1, "in-order", "reset-or-wrap"))
	if err != nil {
		t.Fatalf("reset compute: %v", err)
	}
	if string(dr.Quality) != g.Expected.ResetQuality {
		t.Fatalf("reset quality: got %s want %s", dr.Quality, g.Expected.ResetQuality)
	}
	// Gap via gap status.
	dg, err := ComputeDerived(mk(0, "in-order", "none"), mk(1, "in-order", "gap"))
	if err != nil {
		t.Fatalf("gap compute: %v", err)
	}
	if string(dg.Quality) != g.Expected.GapQuality {
		t.Fatalf("gap quality: got %s want %s", dg.Quality, g.Expected.GapQuality)
	}
}

func TestComputeDerivedZeroTrafficNotZero(t *testing.T) {
	now := time.UnixMilli(1_700_000_000_000)
	prev := CounterSample{Sequence: 1, ReadCompletedAt: now, Packets: 10, Bytes: 100, EligiblePackets: 50, OrderStatus: "in-order", GapStatus: "none", CoveragePPM: 1_000_000}
	cur := CounterSample{Sequence: 2, ReadCompletedAt: now.Add(5 * time.Second), Packets: 10, Bytes: 100, EligiblePackets: 50, OrderStatus: "in-order", GapStatus: "none", CoveragePPM: 1_000_000}
	d, err := ComputeDerived(prev, cur)
	if err != nil {
		t.Fatal(err)
	}
	if d.Quality != QualityNoEligibleTraffic {
		t.Fatalf("zero traffic must be no-eligible-traffic, got %s", d.Quality)
	}
	if d.Rate != 0 {
		t.Fatalf("no-eligible-traffic rate must not claim a percentage, got %v", d.Rate)
	}
}

func TestComputeDerivedCounterDecreaseIsReset(t *testing.T) {
	now := time.UnixMilli(1_700_000_000_000)
	prev := CounterSample{Sequence: 1, ReadCompletedAt: now, Packets: 200, Bytes: 2000, EligiblePackets: 900, OrderStatus: "in-order", GapStatus: "none", CoveragePPM: 1_000_000}
	cur := CounterSample{Sequence: 2, ReadCompletedAt: now.Add(5 * time.Second), Packets: 5, Bytes: 300, EligiblePackets: 950, OrderStatus: "in-order", GapStatus: "none", CoveragePPM: 1_000_000}
	d, err := ComputeDerived(prev, cur)
	if err != nil {
		t.Fatal(err)
	}
	if d.Quality != QualityReset {
		t.Fatalf("counter decrease must be reset, got %s", d.Quality)
	}
	if d.DirectDeltaPackets != 0 {
		t.Fatalf("reset must not derive a negative delta, got %d", d.DirectDeltaPackets)
	}
}

func TestComputeDerivedRejectsNonAdvancing(t *testing.T) {
	now := time.UnixMilli(1_700_000_000_000)
	prev := CounterSample{Sequence: 5, ReadCompletedAt: now, Packets: 1, Bytes: 1, EligiblePackets: 1, OrderStatus: "in-order", GapStatus: "none", CoveragePPM: 1_000_000}
	cur := prev
	if _, err := ComputeDerived(prev, cur); err == nil {
		t.Fatal("non-advancing sequence must be rejected")
	}
}

func TestEpochCrossed(t *testing.T) {
	stored := EpochKey{ObservationEpoch: 3, ResetEpoch: 2}
	if EpochCrossed(stored, EpochKey{ObservationEpoch: 3, ResetEpoch: 2}) {
		t.Fatal("same epoch key must not be crossed")
	}
	if EpochCrossed(stored, EpochKey{ObservationEpoch: 4, ResetEpoch: 2}) {
		t.Fatal("newer observation epoch must not be treated as late")
	}
	if !EpochCrossed(stored, EpochKey{ObservationEpoch: 4, ResetEpoch: 1}) {
		t.Fatal("older reset epoch is a pre-reset late sample and must be rejected")
	}
	if !EpochCrossed(stored, EpochKey{ObservationEpoch: 2, ResetEpoch: 5}) {
		t.Fatal("older observation epoch must be crossed/rejected")
	}
	if !EpochCrossed(stored, EpochKey{ObservationEpoch: 3, ResetEpoch: 1}) {
		t.Fatal("older reset epoch must be crossed/rejected")
	}
}

func TestValidateEpochForCreationRequiresExactReadback(t *testing.T) {
	good := ObservationEpochRecord{
		EpochID: "ep-1", EffectIntentID: "it-1", OperationID: "op-1",
		EntityID: "en-1", RuleID: "r-1", TargetID: "tgt-1",
		CanonicalEntryDigest:      "sha256:" + repeat("a", 64),
		MatchPriorityActionDigest: "sha256:" + repeat("b", 64),
		EpochKey:                  EpochKey{ObservationEpoch: 1, ResetEpoch: 1},
		InstallationReadback:      ReadbackExact,
	}
	if err := ValidateEpochForCreation(good); err != nil {
		t.Fatalf("exact readback epoch rejected: %v", err)
	}
	bad := good
	bad.InstallationReadback = ReadbackMismatch
	if err := ValidateEpochForCreation(bad); err == nil {
		t.Fatal("non-exact installation readback must forbid epoch creation")
	}
	bad2 := good
	bad2.EpochKey.ObservationEpoch = 0
	if err := ValidateEpochForCreation(bad2); err == nil {
		t.Fatal("observation_epoch >= 1 required")
	}
}

func TestOutcomeEnumsClosed(t *testing.T) {
	if !validOutcome(OutcomeObserved, ExpectedDropped, ActualDropped) {
		t.Fatal("valid outcome tuple rejected")
	}
	if validOutcome(OutcomeStatus("unknown"), ExpectedDropped, ActualDropped) {
		t.Fatal("effect unknown must not enter rule outcome namespace")
	}
	if validOutcome(OutcomeObserved, OutcomeExpected("executed"), ActualDropped) {
		t.Fatal("unknown expected outcome must be rejected")
	}
	if validOutcome(OutcomeObserved, ExpectedDropped, OutcomeActual("success")) {
		t.Fatal("unknown actual outcome must be rejected")
	}
}

func repeat(s string, n int) string {
	out := make([]byte, 0, n*len(s))
	for i := 0; i < n; i++ {
		out = append(out, s...)
	}
	return string(out)
}
