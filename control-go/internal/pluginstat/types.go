// Package pluginstat is the Go-owned plugin statistics engine: immutable
// definition registration (validated against the host projection/field
// registry), canonical input freeze, the durable NON-EFFECT run ledger with a
// bounded CAS/fenced dispatcher, artifact validation, and the bounded
// current/history projection.
//
// Invariants (contracts/plugin/statistics/v1, AGENTS.md):
//   - a definition may never carry endpoint/credential/SQL/PromQL/JSONPath/
//     MCP-prompt/URL/expression content; runtime registration is rejected;
//   - read-only-tool may reference at most ONE approved external-source
//     capability; pure-transform/analysis-agent definitions reference
//     host-owned projections only;
//   - NaN/Inf values, code/HTML/URL/SVG/JavaScript/ECharts/Vega payloads and
//     unknown display hints are rejected;
//   - plugin_statistic_runs is NOT an effect claim source; the statistics
//     dispatcher is a bounded internal loop over this ledger;
//   - same idempotency key + same digest returns the original run; the same
//     key with a different digest is a conflict;
//   - a late result from an old binding_generation is audit-only.
package pluginstat

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"masi-nids/control-go/internal/security"
)

// MetricKind, Temporality, DisplayHint, Quality, RunStatus mirror the closed
// contract enums; unknown values are rejected fail-closed.
type MetricKind string

const (
	MetricGauge     MetricKind = "gauge"
	MetricSum       MetricKind = "sum"
	MetricHistogram MetricKind = "histogram"
)

type Temporality string

const (
	TemporalCumulative Temporality = "cumulative"
	TemporalDelta      Temporality = "delta"
)

type DisplayHint string

const (
	DisplayMetricCard   DisplayHint = "metric-card"
	DisplayStatus       DisplayHint = "status"
	DisplayTimeseries   DisplayHint = "timeseries"
	DisplayBar          DisplayHint = "bar"
	DisplayHeatmap      DisplayHint = "heatmap"
	DisplayTable        DisplayHint = "table"
	DisplayText         DisplayHint = "text"
	DisplayEvidenceList DisplayHint = "evidence-list"
)

type Quality string

const (
	QualityValid         Quality = "valid"
	QualityPartial       Quality = "partial"
	QualityGap           Quality = "gap"
	QualityStale         Quality = "stale"
	QualityNoData        Quality = "no_data"
	QualityNotMeasurable Quality = "not_measurable"
	QualityInvalid       Quality = "invalid"
)

type RunStatus string

const (
	RunPending   RunStatus = "pending"
	RunRunning   RunStatus = "running"
	RunSucceeded RunStatus = "succeeded"
	RunFailed    RunStatus = "failed"
	RunCancelled RunStatus = "cancelled"
	RunExpired   RunStatus = "expired"
	RunFenced    RunStatus = "fenced"
)

// PluginKind (from plugin/v1) — closed set.
type PluginKind string

const (
	KindAnalysisAgent PluginKind = "analysis-agent"
	KindReadOnlyTool  PluginKind = "read-only-tool"
	KindPureTransform PluginKind = "pure-transform"
)

// Definition is the immutable PluginStatisticsDefinitionV1.
type Definition struct {
	DefinitionID                 string      `json:"definition_id"`
	DefinitionDigest             string      `json:"definition_digest"`
	PluginID                     string      `json:"plugin_id"`
	PluginRevision               string      `json:"plugin_revision"`
	PluginKind                   PluginKind  `json:"plugin_kind"`
	BindingGeneration            int64       `json:"binding_generation"`
	HostProjectionRefs           []string    `json:"host_projection_refs"`
	ExternalSourceCapabilityRefs []string    `json:"external_source_capability_refs"`
	MetricKind                   MetricKind  `json:"metric_kind"`
	Temporality                  Temporality `json:"temporality"`
	Monotonicity                 bool        `json:"monotonicity"`
	DimensionLabels              []string    `json:"dimension_labels"`
	SeriesCardinalityLimit       int         `json:"series_cardinality_limit"`
	DeadlineMS                   int         `json:"deadline_ms"`
	DisplayHint                  DisplayHint `json:"display_hint"`
}

// Definition limits (contracts bounds).
const (
	MaxHostProjectionRefs     = 64
	MaxExternalCapabilityRefs = 1
	MaxDimensionLabels        = 32
	MinSeriesCardinalityLimit = 1
	MaxSeriesCardinalityLimit = 10000
	MaxDimensionLabelLen      = 128
	MinDeadlineMS             = 1
	MaxDeadlineMS             = 10000
	MaxArtifactBytes          = 1024 * 1024
	MaxMetrics                = 32
	MaxSeries                 = 64
	MaxTables                 = 8
	MaxPoints                 = 10000
)

var (
	digestRE   nonZeroDigestPattern
	identityRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)
	// forbiddenDefinitionFields matches any definition field VALUE that smells
	// like an endpoint/credential/query/expression (STATISTICS-DEFINITION-NO-
	// ENDPOINT-CREDENTIAL).
	forbiddenValueRE = regexp.MustCompile(`(?i)\b(https?://|wss?://|postgres(ql)?://|mysql://|mongodb(\+srv)?://|select\s+.+\s+from\s+|insert\s+into\s+|update\s+.+\s+set\s+|delete\s+from\s+|Bearer\s+[A-Za-z0-9._\-]{8,}|-----BEGIN\s|api[_-]?key\s*[:=]|password\s*[:=]|token\s*[:=]|secret\s*[:=]|\$\{.*\}|\{\{.*\}\})`)
	// forbiddenPayloadRE matches executable/render-dangerous payload content
	// (STATISTICS-DISPLAY-CLOSED-UNION: no URL/HTML/SVG/JS/ECharts/Vega).
	forbiddenPayloadRE = regexp.MustCompile(`(?is)<\s*(script|svg|iframe|object|embed|link)\b|javascript:|v-html|data:text/html|on(?:error|load|click)\s*=|"option"\s*:|echarts\.init|vega\.embed|eval\(|new\s+Function\(`)
)

type nonZeroDigestPattern struct{}

func (nonZeroDigestPattern) MatchString(value string) bool { return security.ValidDigest(value) }

// ValidateDefinition validates an immutable definition BEFORE it is frozen
// into a manifest revision. Unknown enum values, bound violations, forbidden
// field content, or an external-source reference on a non-read-only-tool kind
// are all rejected fail-closed.
func ValidateDefinition(d Definition) error {
	if !identityRE.MatchString(d.DefinitionID) {
		return fmt.Errorf("pluginstat: malformed definition_id %q", d.DefinitionID)
	}
	if !digestRE.MatchString(d.DefinitionDigest) {
		return fmt.Errorf("pluginstat: malformed definition_digest")
	}
	if !identityRE.MatchString(d.PluginID) || d.PluginRevision == "" {
		return fmt.Errorf("pluginstat: plugin_id/plugin_revision required")
	}
	switch d.PluginKind {
	case KindAnalysisAgent, KindReadOnlyTool, KindPureTransform:
	default:
		return fmt.Errorf("pluginstat: unknown plugin kind %q", d.PluginKind)
	}
	if d.BindingGeneration < 1 {
		return fmt.Errorf("pluginstat: binding_generation >= 1 required")
	}
	if len(d.HostProjectionRefs) < 1 {
		return fmt.Errorf("pluginstat: at least one host projection ref required")
	}
	if len(d.HostProjectionRefs) > MaxHostProjectionRefs {
		return fmt.Errorf("pluginstat: host projection refs > %d", MaxHostProjectionRefs)
	}
	for _, r := range d.HostProjectionRefs {
		if !identityRE.MatchString(r) {
			return fmt.Errorf("pluginstat: malformed host projection ref %q", r)
		}
		if forbiddenValueRE.MatchString(r) {
			return fmt.Errorf("pluginstat: host projection ref %q carries forbidden content (endpoint/credential/query/expression)", r)
		}
	}
	if hasDuplicates(d.HostProjectionRefs) {
		return fmt.Errorf("pluginstat: duplicate host projection ref")
	}
	if len(d.ExternalSourceCapabilityRefs) > MaxExternalCapabilityRefs {
		return fmt.Errorf("pluginstat: external source capability refs > %d", MaxExternalCapabilityRefs)
	}
	if len(d.ExternalSourceCapabilityRefs) > 0 && d.PluginKind != KindReadOnlyTool {
		return fmt.Errorf("pluginstat: external source capability only allowed for read-only-tool (got %s)", d.PluginKind)
	}
	for _, r := range d.ExternalSourceCapabilityRefs {
		if !identityRE.MatchString(r) {
			return fmt.Errorf("pluginstat: malformed external capability ref %q", r)
		}
	}
	switch d.MetricKind {
	case MetricGauge, MetricSum, MetricHistogram:
	default:
		return fmt.Errorf("pluginstat: unknown metric_kind %q", d.MetricKind)
	}
	switch d.Temporality {
	case TemporalCumulative, TemporalDelta:
	default:
		return fmt.Errorf("pluginstat: unknown temporality %q", d.Temporality)
	}
	if len(d.DimensionLabels) > MaxDimensionLabels {
		return fmt.Errorf("pluginstat: dimension labels > %d", MaxDimensionLabels)
	}
	for _, l := range d.DimensionLabels {
		if l == "" || len(l) > MaxDimensionLabelLen {
			return fmt.Errorf("pluginstat: dimension label malformed (%q)", l)
		}
		if strings.ContainsAny(l, "{}") {
			return fmt.Errorf("pluginstat: dimension label %q looks like a template/expression", l)
		}
	}
	if hasDuplicates(d.DimensionLabels) {
		return fmt.Errorf("pluginstat: duplicate dimension label")
	}
	if d.SeriesCardinalityLimit < MinSeriesCardinalityLimit || d.SeriesCardinalityLimit > MaxSeriesCardinalityLimit {
		return fmt.Errorf("pluginstat: series cardinality limit out of %d..%d", MinSeriesCardinalityLimit, MaxSeriesCardinalityLimit)
	}
	if d.DeadlineMS < MinDeadlineMS || d.DeadlineMS > MaxDeadlineMS {
		return fmt.Errorf("pluginstat: deadline_ms out of %d..%d", MinDeadlineMS, MaxDeadlineMS)
	}
	switch d.DisplayHint {
	case DisplayMetricCard, DisplayStatus, DisplayTimeseries, DisplayBar,
		DisplayHeatmap, DisplayTable, DisplayText, DisplayEvidenceList:
	default:
		return fmt.Errorf("pluginstat: unknown display_hint %q", d.DisplayHint)
	}
	if d.DefinitionDigest != ComputeDefinitionDigest(d) {
		return fmt.Errorf("pluginstat: definition digest does not bind exact immutable body")
	}
	return nil
}

// ComputeDefinitionDigest canonicalizes set-like reference/label vectors and
// binds every immutable definition field, including producer kind. The kind is
// an admission field even though it is not sent to the fixed renderer.
func ComputeDefinitionDigest(d Definition) string {
	d.DefinitionDigest = ""
	d = normalizeDefinitionSlices(d)
	sortStrings(d.HostProjectionRefs)
	sortStrings(d.ExternalSourceCapabilityRefs)
	sortStrings(d.DimensionLabels)
	raw, _ := json.Marshal(d)
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func normalizeDefinitionSlices(d Definition) Definition {
	d.HostProjectionRefs = append([]string{}, d.HostProjectionRefs...)
	d.ExternalSourceCapabilityRefs = append([]string{}, d.ExternalSourceCapabilityRefs...)
	d.DimensionLabels = append([]string{}, d.DimensionLabels...)
	return d
}

func hasDuplicates(values []string) bool {
	seen := make(map[string]struct{}, len(values))
	for _, v := range values {
		if _, ok := seen[v]; ok {
			return true
		}
		seen[v] = struct{}{}
	}
	return false
}

// Metric / Series / Table / Point / Truncation / Provenance are the artifact
// payload union (closed display renderer inputs only — no code/URL/HTML).
type Metric struct {
	MetricID    string      `json:"metric_id"`
	MetricKind  MetricKind  `json:"metric_kind"`
	Temporality Temporality `json:"temporality"`
	Value       float64     `json:"value"`
	Unit        string      `json:"unit"`
}

type Point struct {
	TimestampUnixMS int64   `json:"timestamp_unix_ms"`
	Value           float64 `json:"value"`
}

type Series struct {
	SeriesID   string            `json:"series_id"`
	Labels     map[string]string `json:"labels"`
	PointCount int               `json:"point_count"`
	Points     []Point           `json:"points"`
}

type Table struct {
	TableID  string   `json:"table_id"`
	Columns  []string `json:"columns"`
	RowCount int      `json:"row_count"`
	Rows     [][]any  `json:"rows"`
}

type Truncation struct {
	TruncatedRows   int    `json:"truncated_rows"`
	TruncatedSeries int    `json:"truncated_series"`
	ReasonCode      string `json:"reason_code"`
}

type Provenance struct {
	PluginID          string                    `json:"plugin_id"`
	PluginRevision    string                    `json:"plugin_revision"`
	ComputedAtUnixMS  int64                     `json:"computed_at_unix_ms"`
	DefinitionID      string                    `json:"definition_id"`
	DefinitionDigest  string                    `json:"definition_digest"`
	RunID             string                    `json:"run_id"`
	BindingGeneration int64                     `json:"binding_generation"`
	ExternalSource    *ExternalSourceProvenance `json:"external_source,omitempty"`
}

// ExternalSourceProvenance binds an approved read-only-tool capability to the
// exact request/response observed by the producer. It contains no endpoint or
// credential and is mandatory when the definition references an external
// source capability.
type ExternalSourceProvenance struct {
	CapabilityID     string `json:"capability_id"`
	RequestDigest    string `json:"request_digest"`
	ObservedAtUnixMS int64  `json:"observed_at_unix_ms"`
	ResponseDigest   string `json:"response_digest"`
	ETagOrVersion    string `json:"etag_or_version,omitempty"`
	Status           string `json:"status"`
}

func validateExternalProvenance(kind PluginKind, capabilities []string, p *ExternalSourceProvenance) error {
	if len(capabilities) == 0 {
		if p != nil {
			return errors.New("pluginstat: external provenance present without approved capability")
		}
		return nil
	}
	if kind != KindReadOnlyTool || len(capabilities) != 1 || p == nil ||
		p.CapabilityID != capabilities[0] || !identityRE.MatchString(p.CapabilityID) ||
		!digestRE.MatchString(p.RequestDigest) || !digestRE.MatchString(p.ResponseDigest) ||
		p.ObservedAtUnixMS < 1 || len(p.ETagOrVersion) > 256 {
		return errors.New("pluginstat: exact external-source provenance required")
	}
	switch p.Status {
	case "complete", "partial", "timeout":
	default:
		return errors.New("pluginstat: external-source status invalid")
	}
	return nil
}

// Artifact is PluginStatisticsArtifactV1.
type Artifact struct {
	SchemaVersion    string     `json:"schema_version"`
	RecordType       string     `json:"record_type"`
	ArtifactID       string     `json:"record_id"`
	ArtifactDigest   string     `json:"artifact_digest"`
	RunID            string     `json:"run_id"`
	DefinitionID     string     `json:"definition_id"`
	DefinitionDigest string     `json:"definition_digest"`
	Status           RunStatus  `json:"status"`
	Quality          Quality    `json:"quality"`
	Metrics          []Metric   `json:"metrics"`
	Series           []Series   `json:"series"`
	Tables           []Table    `json:"tables"`
	Truncation       Truncation `json:"truncation"`
	Provenance       Provenance `json:"provenance"`
	Bytes            int        `json:"bytes"`
	ActorRef         string     `json:"actor_ref"`
	ReasonCode       string     `json:"reason_code"`
	TraceID          string     `json:"trace_id"`
}

// ValidateArtifact enforces the numeric/payload safety bounds. NaN/Inf are
// rejected everywhere; text payloads are scanned for forbidden executable or
// render-dangerous content; cardinality/size bounds match the contract.
func ValidateArtifact(a Artifact) error {
	if a.SchemaVersion != "masi-plugin-statistics/v1" || a.RecordType != "artifact" {
		return fmt.Errorf("pluginstat: artifact schema/record type mismatch")
	}
	if !identityRE.MatchString(a.ArtifactID) || !digestRE.MatchString(a.ArtifactDigest) {
		return fmt.Errorf("pluginstat: malformed artifact identity/digest")
	}
	if a.RunID == "" || a.DefinitionID == "" || !digestRE.MatchString(a.DefinitionDigest) {
		return fmt.Errorf("pluginstat: artifact run/definition identity required")
	}
	if !identityRE.MatchString(a.ActorRef) || !identityRE.MatchString(a.TraceID) ||
		!regexp.MustCompile(`^[A-Z][A-Z0-9_]{0,63}$`).MatchString(a.ReasonCode) {
		return fmt.Errorf("pluginstat: artifact actor/reason/trace malformed")
	}
	if !identityRE.MatchString(a.Provenance.PluginID) || !identityRE.MatchString(a.Provenance.PluginRevision) ||
		a.Provenance.ComputedAtUnixMS < 1 || a.Provenance.DefinitionID != a.DefinitionID ||
		a.Provenance.DefinitionDigest != a.DefinitionDigest || a.Provenance.RunID != a.RunID ||
		a.Provenance.BindingGeneration < 1 {
		return fmt.Errorf("pluginstat: artifact provenance mismatch")
	}
	switch a.Status {
	case RunSucceeded, RunFailed, RunCancelled, RunExpired, RunFenced:
	default:
		return fmt.Errorf("pluginstat: artifact status must be terminal")
	}
	switch a.Quality {
	case QualityValid, QualityPartial, QualityGap, QualityStale, QualityNoData, QualityNotMeasurable, QualityInvalid:
	default:
		return fmt.Errorf("pluginstat: unknown artifact quality %q", a.Quality)
	}
	if a.Status != RunSucceeded && a.Quality == QualityValid {
		return fmt.Errorf("pluginstat: only a succeeded run may carry quality=valid")
	}
	if len(a.Metrics) > MaxMetrics {
		return fmt.Errorf("pluginstat: metrics > %d", MaxMetrics)
	}
	pointCount := 0
	for _, m := range a.Metrics {
		if !identityRE.MatchString(m.MetricID) {
			return fmt.Errorf("pluginstat: malformed metric_id %q", m.MetricID)
		}
		if math.IsNaN(m.Value) || math.IsInf(m.Value, 0) {
			return fmt.Errorf("pluginstat: metric %s value is NaN/Inf", m.MetricID)
		}
		if forbiddenPayloadRE.MatchString(m.Unit) {
			return fmt.Errorf("pluginstat: metric unit carries forbidden payload")
		}
		if len(m.Unit) > 64 {
			return fmt.Errorf("pluginstat: metric unit >64 bytes")
		}
	}
	if len(a.Series) > MaxSeries {
		return fmt.Errorf("pluginstat: series > %d", MaxSeries)
	}
	for _, s := range a.Series {
		if !identityRE.MatchString(s.SeriesID) {
			return fmt.Errorf("pluginstat: malformed series_id %q", s.SeriesID)
		}
		if len(s.Labels) > MaxDimensionLabels || s.PointCount != len(s.Points) {
			return fmt.Errorf("pluginstat: series labels/point_count mismatch")
		}
		for key, value := range s.Labels {
			if !identityRE.MatchString(key) || len(value) > 256 || forbiddenPayloadRE.MatchString(value) {
				return fmt.Errorf("pluginstat: series label malformed")
			}
		}
		pointCount += len(s.Points)
		for _, p := range s.Points {
			if p.TimestampUnixMS < 1 {
				return fmt.Errorf("pluginstat: point timestamp >= 1 required")
			}
			if math.IsNaN(p.Value) || math.IsInf(p.Value, 0) {
				return fmt.Errorf("pluginstat: series %s carries NaN/Inf point", s.SeriesID)
			}
		}
	}
	if pointCount > MaxPoints {
		return fmt.Errorf("pluginstat: total points > %d", MaxPoints)
	}
	if len(a.Tables) > MaxTables {
		return fmt.Errorf("pluginstat: tables > %d", MaxTables)
	}
	for _, table := range a.Tables {
		if !identityRE.MatchString(table.TableID) {
			return fmt.Errorf("pluginstat: malformed table_id %q", table.TableID)
		}
		if len(table.Columns) > 32 {
			return fmt.Errorf("pluginstat: table columns >32")
		}
		if len(table.Rows) > 2000 {
			return fmt.Errorf("pluginstat: table rows >2000")
		}
		if table.RowCount != len(table.Rows) {
			return fmt.Errorf("pluginstat: table row_count mismatch")
		}
		for _, row := range table.Rows {
			if len(row) != len(table.Columns) {
				return fmt.Errorf("pluginstat: table row/column width mismatch")
			}
		}
	}
	raw := canonicalJSONBytes(a)
	if len(raw) > MaxArtifactBytes {
		return fmt.Errorf("pluginstat: artifact bytes > %d", MaxArtifactBytes)
	}
	if a.Bytes != len(raw) || a.Bytes < 1 {
		return fmt.Errorf("pluginstat: artifact bytes mismatch")
	}
	if forbiddenPayloadRE.Match(raw) {
		return fmt.Errorf("pluginstat: artifact carries forbidden payload (URL/HTML/SVG/JS/ECharts/Vega)")
	}
	return nil
}

func ComputeArtifactDigest(a Artifact) string {
	a.ArtifactDigest = ""
	a.Bytes = 0
	return canonicalJSONDigest(a)
}

func canonicalJSONDigest(value any) string {
	canonical := canonicalJSONBytes(value)
	sum := sha256.Sum256(canonical)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func canonicalJSONBytes(value any) []byte {
	raw, _ := json.Marshal(value)
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var document any
	_ = decoder.Decode(&document)
	var canonical bytes.Buffer
	writeCanonicalJSON(&canonical, document)
	return canonical.Bytes()
}

func writeCanonicalJSON(buffer *bytes.Buffer, value any) {
	switch typed := value.(type) {
	case nil:
		buffer.WriteString("null")
	case bool:
		if typed {
			buffer.WriteString("true")
		} else {
			buffer.WriteString("false")
		}
	case string:
		var encoded bytes.Buffer
		encoder := json.NewEncoder(&encoded)
		encoder.SetEscapeHTML(false)
		_ = encoder.Encode(typed)
		buffer.Write(bytes.TrimSuffix(encoded.Bytes(), []byte{'\n'}))
	case json.Number:
		if integer, err := typed.Int64(); err == nil {
			buffer.WriteString(strconv.FormatInt(integer, 10))
		} else if number, err := typed.Float64(); err == nil && !math.IsNaN(number) && !math.IsInf(number, 0) {
			buffer.WriteString(strconv.FormatFloat(number, 'g', -1, 64))
		}
	case []any:
		buffer.WriteByte('[')
		for index, item := range typed {
			if index > 0 {
				buffer.WriteByte(',')
			}
			writeCanonicalJSON(buffer, item)
		}
		buffer.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		buffer.WriteByte('{')
		for index, key := range keys {
			if index > 0 {
				buffer.WriteByte(',')
			}
			writeCanonicalJSON(buffer, key)
			buffer.WriteByte(':')
			writeCanonicalJSON(buffer, typed[key])
		}
		buffer.WriteByte('}')
	}
}

// FinalizeArtifact fills the public wire envelope, semantic digest, derived
// point/row counts, and exact encoded byte count.
func FinalizeArtifact(a Artifact) Artifact {
	a.SchemaVersion = "masi-plugin-statistics/v1"
	a.RecordType = "artifact"
	for i := range a.Series {
		if a.Series[i].Labels == nil {
			a.Series[i].Labels = map[string]string{}
		}
		a.Series[i].PointCount = len(a.Series[i].Points)
	}
	for i := range a.Tables {
		a.Tables[i].RowCount = len(a.Tables[i].Rows)
	}
	a.ArtifactDigest = ComputeArtifactDigest(a)
	for i := 0; i < 4; i++ {
		raw := canonicalJSONBytes(a)
		if a.Bytes == len(raw) {
			break
		}
		a.Bytes = len(raw)
	}
	return a
}

// IsSameIdempotencyConflict reports whether (key,digest) collides with a
// stored run under the same key with a DIFFERENT digest (STATISTICS-
// IDEMPOTENCY-SAME-KEY).
func IsSameIdempotencyConflict(storedKey, storedDigest, key, digest string) bool {
	if storedKey != key {
		return false
	}
	return storedDigest != digest
}
