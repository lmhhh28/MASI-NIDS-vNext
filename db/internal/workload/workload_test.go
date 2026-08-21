package workload

import (
	"context"
	"testing"
	"time"
)

func TestCapacityRequiresExactMatrix(t *testing.T) {
	t.Parallel()
	if _, err := RunCapacity(context.Background(), nil, []int{0, 128, 4096}); err == nil {
		t.Fatal("non-exact capacity matrix accepted")
	}
}

func TestFormalSoakDurationCannotBeShortened(t *testing.T) {
	t.Parallel()
	_, err := RunSoak(context.Background(), nil, SoakConfig{
		Formal: true, PhaseDuration: time.Second, SampleEvery: time.Second, Warmup: 60 * time.Second,
	}, nil)
	if err == nil {
		t.Fatal("short formal soak accepted")
	}
}

func TestSoakBoundsAreFailClosed(t *testing.T) {
	t.Parallel()
	for _, cfg := range []SoakConfig{
		{PhaseDuration: 0, SampleEvery: time.Second},
		{PhaseDuration: time.Second, SampleEvery: 500 * time.Millisecond},
		{PhaseDuration: 901 * time.Second, SampleEvery: time.Second},
		{PhaseDuration: time.Second, SampleEvery: time.Second, Warmup: 61 * time.Second},
	} {
		if _, err := RunSoak(context.Background(), nil, cfg, nil); err == nil {
			t.Fatalf("invalid soak config accepted: %+v", cfg)
		}
	}
}

func TestLatencyDeltaIsBounded(t *testing.T) {
	t.Parallel()
	c := newCounters()
	before := c.snapshot()
	for _, elapsed := range []time.Duration{time.Millisecond, 2 * time.Millisecond, 8 * time.Millisecond} {
		c.record(elapsed, nil)
	}
	after := c.snapshot()
	if after.ops-before.ops != 3 || after.errs != 0 || after.max != 8000 {
		t.Fatalf("unexpected bounded counters: %+v", after)
	}
}
