package pluginstat

import (
	"encoding/json"
	"math"
	"os"
	"strings"
	"testing"
)

func validDefinition() Definition {
	d := Definition{
		DefinitionID:           "statdef-1",
		PluginID:               "plugin-1",
		PluginRevision:         "0123456789abcdef0123456789abcdef01234567",
		PluginKind:             KindPureTransform,
		BindingGeneration:      1,
		HostProjectionRefs:     []string{"event-count-projection"},
		MetricKind:             MetricSum,
		Temporality:            TemporalCumulative,
		DimensionLabels:        []string{"shard"},
		SeriesCardinalityLimit: 100,
		DeadlineMS:             5000,
		DisplayHint:            DisplayMetricCard,
	}
	d.DefinitionDigest = ComputeDefinitionDigest(d)
	return d
}

func TestValidateDefinitionAccepts(t *testing.T) {
	if err := ValidateDefinition(validDefinition()); err != nil {
		t.Fatalf("valid definition rejected: %v", err)
	}
	ro := validDefinition()
	ro.PluginKind = KindReadOnlyTool
	ro.ExternalSourceCapabilityRefs = []string{"approved-ext-cap"}
	ro.DefinitionDigest = ComputeDefinitionDigest(ro)
	if err := ValidateDefinition(ro); err != nil {
		t.Fatalf("read-only-tool with one approved external capability rejected: %v", err)
	}
}

func TestPostCommitHookIsExplicitAndBounded(t *testing.T) {
	service := NewService(nil)
	called := 0
	service.SetPostCommitHook(func(token ClaimToken, artifact Artifact) {
		called++
		if token.RunID != "run-1" || artifact.ArtifactID != "artifact-1" {
			t.Fatalf("unexpected hook identity: token=%+v artifact=%+v", token, artifact)
		}
	})
	service.notifyRunCommitted(ClaimToken{RunID: "run-1"}, Artifact{ArtifactID: "artifact-1"})
	if called != 1 {
		t.Fatalf("post-commit hook count=%d", called)
	}
}

func TestDigestSentinelIsRejectedAcrossStatisticsValidators(t *testing.T) {
	if digestRE.MatchString("sha256:" + strings.Repeat("0", 64)) {
		t.Fatal("all-zero statistics digest accepted")
	}
	definition := validDefinition()
	definition.DefinitionDigest = "sha256:" + strings.Repeat("0", 64)
	if err := ValidateDefinition(definition); err == nil {
		t.Fatal("all-zero definition digest accepted")
	}
}

func TestExternalSourceProvenanceExactCapabilityFence(t *testing.T) {
	d := "sha256:" + strings.Repeat("a", 64)
	provenance := &ExternalSourceProvenance{CapabilityID: "approved-ext-cap", RequestDigest: d,
		ObservedAtUnixMS: 1, ResponseDigest: d, ETagOrVersion: "etag-1", Status: "complete"}
	if err := validateExternalProvenance(KindReadOnlyTool, []string{"approved-ext-cap"}, provenance); err != nil {
		t.Fatalf("exact approved external provenance rejected: %v", err)
	}
	if err := validateExternalProvenance(KindReadOnlyTool, []string{"other-cap"}, provenance); err == nil {
		t.Fatal("wrong external capability identity must be rejected")
	}
	if err := validateExternalProvenance(KindPureTransform, nil, provenance); err == nil {
		t.Fatal("pure-transform external provenance must be rejected")
	}
	if err := validateExternalProvenance(KindReadOnlyTool, []string{"approved-ext-cap"}, nil); err == nil {
		t.Fatal("approved external capability without provenance must be rejected")
	}
}

func TestValidateDefinitionRejections(t *testing.T) {
	cases := []struct {
		name string
		mut  func(*Definition)
	}{
		{"unknown kind", func(d *Definition) { d.PluginKind = PluginKind("daemon") }},
		{"unknown metric kind", func(d *Definition) { d.MetricKind = MetricKind("counter") }},
		{"unknown temporality", func(d *Definition) { d.Temporality = Temporality("epoch") }},
		{"unknown display hint", func(d *Definition) { d.DisplayHint = DisplayHint("gauge-3d") }},
		{"external cap on pure-transform", func(d *Definition) {
			d.ExternalSourceCapabilityRefs = []string{"some-cap"}
		}},
		{"endpoint in projection ref", func(d *Definition) {
			d.HostProjectionRefs = []string{"https://evil.example/projection"}
		}},
		{"credential-ish projection ref", func(d *Definition) {
			d.HostProjectionRefs = []string{"api_key=abcdef123456"}
		}},
		{"SQL in projection ref", func(d *Definition) {
			d.HostProjectionRefs = []string{"select x from events"}
		}},
		{"template dimension label", func(d *Definition) {
			d.DimensionLabels = []string{"{{user}}"}
		}},
		{"cardinality over max", func(d *Definition) { d.SeriesCardinalityLimit = 10001 }},
		{"deadline over max", func(d *Definition) { d.DeadlineMS = 600001 }},
		{"no host projection", func(d *Definition) { d.HostProjectionRefs = nil }},
		{"two external caps", func(d *Definition) {
			d.PluginKind = KindReadOnlyTool
			d.ExternalSourceCapabilityRefs = []string{"cap-a", "cap-b"}
		}},
	}
	for _, c := range cases {
		d := validDefinition()
		c.mut(&d)
		if err := ValidateDefinition(d); err == nil {
			t.Fatalf("%s: must be rejected", c.name)
		}
	}
}

func validArtifact() Artifact {
	return FinalizeArtifact(Artifact{
		ArtifactID:       "art-1",
		RunID:            "run-1",
		DefinitionID:     "statdef-1",
		DefinitionDigest: "sha256:" + strings.Repeat("a", 64),
		Status:           RunSucceeded,
		Quality:          QualityValid,
		Metrics:          []Metric{{MetricID: "m-1", MetricKind: MetricSum, Temporality: TemporalCumulative, Value: 42, Unit: "events"}},
		Series: []Series{{SeriesID: "s-1", Labels: map[string]string{"class": "alert"}, Points: []Point{
			{TimestampUnixMS: 1_700_000_000_000, Value: 1.5},
		}}},
		Truncation: Truncation{ReasonCode: "NONE"},
		Provenance: Provenance{PluginID: "plugin-1", PluginRevision: "manifest-1:1", ComputedAtUnixMS: 1_700_000_000_001,
			DefinitionID: "statdef-1", DefinitionDigest: "sha256:" + strings.Repeat("a", 64), RunID: "run-1", BindingGeneration: 1},
		ActorRef: "plugin-1", ReasonCode: "SUCCEEDED", TraceID: "trace-1",
	})
}

func TestValidateArtifactAccepts(t *testing.T) {
	if err := ValidateArtifact(validArtifact()); err != nil {
		t.Fatalf("valid artifact rejected: %v", err)
	}
}

func TestValidateArtifactRejections(t *testing.T) {
	clone := func() Artifact { a := validArtifact(); return a }
	cases := []struct {
		name string
		mut  func(*Artifact)
	}{
		{"NaN metric", func(a *Artifact) { a.Metrics[0].Value = math.NaN() }},
		{"+Inf point", func(a *Artifact) { a.Series[0].Points[0].Value = math.Inf(1) }},
		{"script payload in unit", func(a *Artifact) { a.Metrics[0].Unit = "<script>alert(1)</script>" }},
		{"javascript URL", func(a *Artifact) { a.Metrics[0].Unit = "javascript:alert(1)" }},
		{"echarts payload", func(a *Artifact) { a.Metrics[0].Unit = "echarts.init(document)" }},
		{"vega payload", func(a *Artifact) { a.Metrics[0].Unit = "vega.embed(#vis)" }},
		{"quality valid on failed run", func(a *Artifact) { a.Status = RunFailed; a.Quality = QualityValid }},
		{"unknown quality", func(a *Artifact) { a.Quality = Quality("perfect") }},
		{"too many metrics", func(a *Artifact) {
			a.Metrics = make([]Metric, MaxMetrics+1)
		}},
		{"too many series", func(a *Artifact) {
			a.Series = make([]Series, MaxSeries+1)
		}},
		{"malformed artifact digest", func(a *Artifact) { a.ArtifactDigest = "md5:x" }},
	}
	for _, c := range cases {
		a := clone()
		c.mut(&a)
		if err := ValidateArtifact(a); err == nil {
			t.Fatalf("%s: must be rejected", c.name)
		}
	}
}

func TestComputeFrozenInputDigestStable(t *testing.T) {
	b := InputBundle{
		SchemaVersion: "masi-plugin-statistics/v1", RecordType: "input-bundle", RecordID: "bundle-1",
		RunID: "run-1", RequestDigest: "sha256:" + strings.Repeat("b", 64),
		PluginID: "plugin.fixture", PluginRevision: "revision-1", ConfigDigest: "sha256:" + strings.Repeat("c", 64),
		BindingGeneration: 1, DefinitionID: "statdef-1", DefinitionRevision: "statdef-1",
		DefinitionDigest: "sha256:" + strings.Repeat("a", 64), SourceRevision: "rev-1",
		SourceProfileDigest: "sha256:" + strings.Repeat("d", 64), SourceGeneration: 1,
		SourceEpoch: "epoch-1", SourceSequenceStart: 1, SourceSequenceEnd: 2,
		Coverage: 1, Quality: string(QualityValid), Scope: "tenant:test", DataClassRef: "data-class.internal",
		WindowStartUnixMS: 1_700_000_000_000, WindowEndUnixMS: 1_700_000_300_000,
		AsOfUnixMS: 1_700_000_300_000, BytesLimit: 2 * 1024 * 1024, CardinalityLimit: 10000,
		DeadlineMS: 5000, ActorRef: "control-plugin-statistics", ReasonCode: "INPUT_FROZEN", TraceID: "trace-1",
		Rows: []map[string]any{
			{"b": 2, "a": "x"},
			{"a": "y", "b": 1},
		},
	}
	d1 := ComputeFrozenInputDigest(b)
	d2 := ComputeFrozenInputDigest(b)
	if d1 != d2 {
		t.Fatal("frozen digest must be deterministic")
	}
	if !strings.HasPrefix(d1, "sha256:") || len(d1) != 71 {
		t.Fatalf("digest malformed: %s", d1)
	}
	// Row key order must not matter (canonical ordering).
	b.Rows[0]["c"] = 3
	if ComputeFrozenInputDigest(b) == d1 {
		t.Fatal("content change must change the digest")
	}
}

func TestInputBundleGoldenDigestsMatchRustContract(t *testing.T) {
	raw, err := os.ReadFile("../../../contracts/plugin/statistics/v1/golden/input-bundle-v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var bundle InputBundle
	if err := json.Unmarshal(raw, &bundle); err != nil {
		t.Fatal(err)
	}
	if observed := ComputeFrozenInputDigest(bundle); observed != bundle.FrozenInputDigest {
		t.Fatalf("semantic frozen digest=%s want=%s", observed, bundle.FrozenInputDigest)
	}
	if observed := ComputeInputBundleDigest(bundle); observed != bundle.BundleDigest {
		t.Fatalf("full bundle digest=%s want=%s", observed, bundle.BundleDigest)
	}
}

func TestIsSameIdempotencyConflict(t *testing.T) {
	if !IsSameIdempotencyConflict("k1", "sha256:"+strings.Repeat("1", 64), "k1", "sha256:"+strings.Repeat("2", 64)) {
		t.Fatal("same key different digest is a conflict")
	}
	if IsSameIdempotencyConflict("k1", "sha256:"+strings.Repeat("1", 64), "k1", "sha256:"+strings.Repeat("1", 64)) {
		t.Fatal("same key same digest is idempotent, not a conflict")
	}
	if IsSameIdempotencyConflict("k1", "d1", "k2", "d2") {
		t.Fatal("different keys never conflict")
	}
}

func TestGoldenDefinitionFieldsMatchContract(t *testing.T) {
	// The closed display union must match the frozen golden vector
	// plugin-statistics-definition-v1 (digest pinned by the contract
	// validator); verify the Go enum against the contract file itself.
	raw, err := readContract()
	if err != nil {
		t.Skipf("contract file unavailable: %v", err)
	}
	var schema struct {
		Defs struct {
			DisplayHint struct {
				Enum []string `json:"enum"`
			} `json:"display_hint"`
			MetricKind struct {
				Enum []string `json:"enum"`
			} `json:"metric_kind"`
		} `json:"$defs"`
	}
	if err := json.Unmarshal(raw, &schema); err != nil {
		t.Fatalf("parse contract: %v", err)
	}
	goDisplays := map[DisplayHint]bool{
		DisplayMetricCard: true, DisplayStatus: true, DisplayTimeseries: true,
		DisplayBar: true, DisplayHeatmap: true, DisplayTable: true,
		DisplayText: true, DisplayEvidenceList: true,
	}
	if len(schema.Defs.DisplayHint.Enum) != len(goDisplays) {
		t.Fatalf("display union drift: contract %d vs go %d", len(schema.Defs.DisplayHint.Enum), len(goDisplays))
	}
	for _, e := range schema.Defs.DisplayHint.Enum {
		if !goDisplays[DisplayHint(e)] {
			t.Fatalf("contract display hint %q missing in Go enum", e)
		}
	}
	goKinds := map[MetricKind]bool{MetricGauge: true, MetricSum: true, MetricHistogram: true}
	for _, e := range schema.Defs.MetricKind.Enum {
		if !goKinds[MetricKind(e)] {
			t.Fatalf("contract metric kind %q missing in Go enum", e)
		}
	}
}

func readContract() ([]byte, error) {
	return os.ReadFile("../../../contracts/plugin/statistics/v1/schema.json")
}
