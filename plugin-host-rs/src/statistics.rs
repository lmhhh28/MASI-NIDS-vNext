//! Host-side validation and exact envelope finalization for plugin statistics.

use std::collections::{BTreeMap, BTreeSet};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use crate::admission::{ValidatedBinding, sha256_bytes};
use crate::error::{HostError, HostResult, ReasonCode};

/// Go-frozen input bundle on the existing public execution adapter.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FrozenInputBundle {
    /// Public statistics schema.
    pub schema_version: String,
    /// Closed record discriminator.
    pub record_type: String,
    /// Stable bundle identity.
    pub record_id: String,
    /// Canonical full-bundle digest excluding this field.
    pub bundle_digest: String,
    /// Durable Go run identity.
    pub run_id: String,
    /// Exact Manager request digest.
    pub request_digest: String,
    /// Exact producer plugin.
    pub plugin_id: String,
    /// Exact immutable producer revision.
    pub plugin_revision: String,
    /// Exact producer configuration digest.
    pub config_digest: String,
    /// Exact active producer binding generation.
    pub binding_generation: u64,
    /// Exact definition identity.
    pub definition_id: String,
    /// Immutable definition revision.
    pub definition_revision: String,
    /// Exact immutable definition digest.
    pub definition_digest: String,
    /// Canonical source revision.
    pub source_revision: String,
    /// Exact source projection/profile digest.
    pub source_profile_digest: String,
    /// Source fact generation.
    pub source_generation: u64,
    /// Source reset/observation epoch.
    pub source_epoch: String,
    /// Inclusive first source sequence.
    pub source_sequence_start: u64,
    /// Inclusive last source sequence.
    pub source_sequence_end: u64,
    /// Input coverage in `0.0..=1.0`.
    pub coverage: f64,
    /// Closed input quality.
    pub quality: String,
    /// Go-computed semantic frozen-input digest.
    pub frozen_input_digest: String,
    /// Exact authorized scope.
    pub scope: String,
    /// Authorized data-class registry reference.
    pub data_class_ref: String,
    /// Half-open window start.
    pub window_start_unix_ms: i64,
    /// Half-open window end.
    pub window_end_unix_ms: i64,
    /// Canonical as-of time for the frozen projection.
    pub as_of_unix_ms: i64,
    /// Scalar-only rows.
    pub rows: Vec<BTreeMap<String, serde_json::Value>>,
    /// At most one approved external capability.
    pub external_source_capability_refs: Vec<String>,
    /// Exact serialized-input ceiling applied by Go and Host.
    pub bytes_limit: usize,
    /// Exact row/cardinality ceiling applied by Go and Host.
    pub cardinality_limit: usize,
    /// Exact run total deadline budget.
    pub deadline_ms: u32,
    /// Go actor that froze the bundle.
    pub actor_ref: String,
    /// Stable freeze reason.
    pub reason_code: String,
    /// End-to-end trace identity.
    pub trace_id: String,
}

/// Exact request and binding facts against which a frozen input bundle is fenced.
pub struct InputBundleValidationContext<'a> {
    /// Expected immutable definition identity.
    pub definition_id: &'a str,
    /// Expected immutable definition digest.
    pub definition_digest: &'a str,
    /// Expected semantic frozen-input digest.
    pub frozen_digest: &'a str,
    /// Expected durable run identity.
    pub run_id: &'a str,
    /// Expected authorized scope.
    pub scope: &'a str,
    /// Exact active binding.
    pub binding: &'a ValidatedBinding,
    /// Expected total execution deadline.
    pub deadline_ms: u32,
}

/// Plugin-produced data-only candidate. The Host supplies every authoritative envelope identity.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct StatisticsCandidate {
    /// Artifact quality namespace, independent of run status.
    pub quality: String,
    /// Closed metric values.
    pub metrics: Vec<Metric>,
    /// Bounded time series.
    pub series: Vec<Series>,
    /// Bounded tables.
    pub tables: Vec<Table>,
    /// Honest truncation report.
    pub truncation: Truncation,
    /// Stable producer reason.
    pub reason_code: String,
}

/// Metric data.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Metric {
    /// Stable metric ID.
    pub metric_id: String,
    /// `gauge|sum|histogram`.
    pub metric_kind: String,
    /// `delta|cumulative`.
    pub temporality: String,
    /// Finite numeric value.
    pub value: f64,
    /// Bounded plain-text unit.
    pub unit: String,
}

/// One time-series point.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Point {
    /// Point timestamp.
    pub timestamp_unix_ms: i64,
    /// Finite value.
    pub value: f64,
}

/// One bounded series.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Series {
    /// Stable series identity.
    pub series_id: String,
    /// Canonically sorted bounded dimensions.
    pub labels: BTreeMap<String, String>,
    /// Derived point count.
    pub point_count: usize,
    /// Ordered points.
    pub points: Vec<Point>,
}

/// One bounded table.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Table {
    /// Stable table identity.
    pub table_id: String,
    /// Bounded typed-field identities.
    pub columns: Vec<String>,
    /// Derived row count.
    pub row_count: usize,
    /// Scalar cells only.
    pub rows: Vec<Vec<serde_json::Value>>,
}

/// Truncation summary.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Truncation {
    /// Rows omitted at the bound.
    pub truncated_rows: usize,
    /// Series omitted at the bound.
    pub truncated_series: usize,
    /// Stable reason.
    pub reason_code: String,
}

/// Exact artifact provenance.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Provenance {
    /// Exact producer plugin.
    pub plugin_id: String,
    /// Exact producer revision.
    pub plugin_revision: String,
    /// Host observation time.
    pub computed_at_unix_ms: i64,
    /// Definition identity.
    pub definition_id: String,
    /// Definition digest.
    pub definition_digest: String,
    /// Durable Go run identity.
    pub run_id: String,
    /// Exact active binding generation.
    pub binding_generation: u64,
}

/// Full `PluginStatisticsArtifactV1`, field order intentionally matches Go's canonical struct.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct StatisticsArtifact {
    /// Contract schema.
    pub schema_version: String,
    /// Record union discriminator.
    pub record_type: String,
    /// Artifact identity.
    pub record_id: String,
    /// Canonical digest excluding this field and `bytes`.
    pub artifact_digest: String,
    /// Durable Go run identity.
    pub run_id: String,
    /// Definition identity.
    pub definition_id: String,
    /// Definition digest.
    pub definition_digest: String,
    /// Terminal run status.
    pub status: String,
    /// Independent artifact quality.
    pub quality: String,
    /// Metrics.
    pub metrics: Vec<Metric>,
    /// Series.
    pub series: Vec<Series>,
    /// Tables.
    pub tables: Vec<Table>,
    /// Truncation.
    pub truncation: Truncation,
    /// Exact provenance.
    pub provenance: Provenance,
    /// Encoded byte size including this field.
    pub bytes: usize,
    /// Host actor reference.
    pub actor_ref: String,
    /// Stable reason.
    pub reason_code: String,
    /// Trace reference.
    pub trace_id: String,
}

/// Decode and validate a frozen bundle before allocating a plugin runtime call.
pub fn validate_input_bundle(
    raw: &[u8],
    context: &InputBundleValidationContext<'_>,
) -> HostResult<FrozenInputBundle> {
    if raw.is_empty() || raw.len() > 2 * 1024 * 1024 {
        return Err(HostError::new(
            ReasonCode::ResourceExhausted,
            "statistics input exceeds 2 MiB",
        ));
    }
    let bundle: FrozenInputBundle = serde_json::from_slice(raw).map_err(|error| {
        HostError::new(
            ReasonCode::InvalidArgument,
            format!("statistics input JSON: {error}"),
        )
    })?;
    if bundle.schema_version != "masi-plugin-statistics/v1"
        || bundle.record_type != "input-bundle"
        || bundle.definition_id != context.definition_id
        || bundle.definition_digest != context.definition_digest
        || bundle.frozen_input_digest != context.frozen_digest
        || bundle.run_id != context.run_id
        || bundle.plugin_id != context.binding.envelope.plugin_id
        || bundle.plugin_revision != context.binding.envelope.plugin_revision
        || bundle.config_digest != context.binding.envelope.config_digest
        || bundle.binding_generation != context.binding.envelope.binding_generation
        || bundle.scope != context.scope
        || bundle.window_start_unix_ms < 1
        || bundle.window_end_unix_ms <= bundle.window_start_unix_ms
        || bundle.as_of_unix_ms < bundle.window_end_unix_ms
        || bundle.source_generation < 1
        || bundle.source_sequence_end < bundle.source_sequence_start
        || !bundle.coverage.is_finite()
        || !(0.0..=1.0).contains(&bundle.coverage)
        || !matches!(
            bundle.quality.as_str(),
            "valid" | "partial" | "gap" | "stale" | "no_data" | "not_measurable" | "invalid"
        )
        || bundle.rows.len() > 10_000
        || bundle.bytes_limit < 1
        || bundle.bytes_limit > 2 * 1024 * 1024
        || raw.len() > bundle.bytes_limit
        || bundle.cardinality_limit < 1
        || bundle.cardinality_limit > 10_000
        || bundle.rows.len() > bundle.cardinality_limit
        || bundle.deadline_ms == 0
        || bundle.deadline_ms != context.deadline_ms
        || bundle.external_source_capability_refs.len() > 1
    {
        return Err(HostError::new(
            ReasonCode::Fenced,
            "statistics input identity/window/bounds mismatch",
        ));
    }
    for (value, field) in [
        (&bundle.record_id, "input bundle record_id"),
        (&bundle.run_id, "run_id"),
        (&bundle.plugin_id, "plugin_id"),
        (&bundle.plugin_revision, "plugin_revision"),
        (&bundle.definition_id, "definition_id"),
        (&bundle.definition_revision, "definition_revision"),
        (&bundle.scope, "scope"),
        (&bundle.data_class_ref, "data_class_ref"),
        (&bundle.source_epoch, "source_epoch"),
        (&bundle.actor_ref, "actor_ref"),
        (&bundle.trace_id, "trace_id"),
    ] {
        validate_identity(value, field)?;
    }
    if bundle.source_revision.is_empty() || bundle.source_revision.len() > 256 {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            "statistics source_revision is empty or oversized",
        ));
    }
    validate_reason(&bundle.reason_code)?;
    validate_digest_text(&bundle.bundle_digest)?;
    validate_digest_text(&bundle.request_digest)?;
    validate_digest_text(&bundle.config_digest)?;
    validate_digest_text(&bundle.definition_digest)?;
    validate_digest_text(&bundle.source_profile_digest)?;
    validate_digest_text(&bundle.frozen_input_digest)?;
    let mut external = BTreeSet::new();
    for capability in &bundle.external_source_capability_refs {
        validate_identity(capability, "external capability")?;
        if !external.insert(capability) {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "duplicate external capability",
            ));
        }
    }
    for row in &bundle.rows {
        if row.len() > 64 {
            return Err(HostError::new(
                ReasonCode::ResourceExhausted,
                "statistics row has more than 64 fields",
            ));
        }
        for (key, value) in row {
            validate_identity(key, "input field")?;
            validate_scalar(value)?;
        }
    }
    if compute_frozen_input_digest(&bundle)? != bundle.frozen_input_digest {
        return Err(HostError::new(
            ReasonCode::DigestMismatch,
            "statistics frozen-input digest mismatch",
        ));
    }
    if compute_input_bundle_digest(&bundle)? != bundle.bundle_digest {
        return Err(HostError::new(
            ReasonCode::DigestMismatch,
            "statistics full-bundle digest mismatch",
        ));
    }
    Ok(bundle)
}

/// Compute the semantic frozen-input digest used for run idempotency.
pub fn compute_frozen_input_digest(bundle: &FrozenInputBundle) -> HostResult<String> {
    let mut external = bundle.external_source_capability_refs.clone();
    external.sort();
    let mut canonical = format!(
        "{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{:016x}|{}|{}|{}|{}|{}|{}|{}|{}",
        bundle.run_id,
        bundle.request_digest,
        bundle.plugin_id,
        bundle.plugin_revision,
        bundle.config_digest,
        bundle.binding_generation,
        bundle.definition_id,
        bundle.definition_revision,
        bundle.definition_digest,
        bundle.scope,
        bundle.data_class_ref,
        bundle.source_revision,
        bundle.source_profile_digest,
        bundle.source_generation,
        bundle.source_epoch,
        bundle.source_sequence_start,
        bundle.source_sequence_end,
        bundle.coverage.to_bits(),
        bundle.quality,
        bundle.window_start_unix_ms,
        bundle.window_end_unix_ms,
        bundle.as_of_unix_ms,
        bundle.bytes_limit,
        bundle.cardinality_limit,
        bundle.deadline_ms,
        bundle.rows.len()
    );
    if !external.is_empty() {
        canonical.push_str("|external:[");
        canonical.push_str(&external.join(" "));
        canonical.push(']');
    }
    for row in &bundle.rows {
        for (key, value) in row {
            canonical.push('|');
            canonical.push_str(key);
            canonical.push('=');
            canonical.push_str(&canonical_scalar(value)?);
        }
    }
    Ok(sha256_bytes(canonical.as_bytes()))
}

/// Compute the full strict input-bundle digest excluding its self-reference.
pub fn compute_input_bundle_digest(bundle: &FrozenInputBundle) -> HostResult<String> {
    let mut canonical = bundle.clone();
    canonical.bundle_digest.clear();
    let value = serde_json::to_value(&canonical).map_err(|error| {
        HostError::new(
            ReasonCode::InvalidArgument,
            format!("statistics input bundle canonicalization: {error}"),
        )
    })?;
    let encoded = canonical_json_bytes(&value)?;
    Ok(sha256_bytes(&encoded))
}

fn canonical_scalar(value: &serde_json::Value) -> HostResult<String> {
    match value {
        serde_json::Value::String(value) => Ok(format!("s:{value}")),
        serde_json::Value::Bool(value) => Ok(if *value { "b:1" } else { "b:0" }.to_owned()),
        serde_json::Value::Number(number) => {
            if let Some(value) = number.as_i64() {
                Ok(format!("i:{value}"))
            } else if let Some(value) = number.as_u64() {
                Ok(format!("u:{value}"))
            } else if let Some(value) = number.as_f64().filter(|value| value.is_finite()) {
                Ok(format!("f:{:016x}", value.to_bits()))
            } else {
                Err(HostError::new(
                    ReasonCode::InvalidArgument,
                    "statistics numeric scalar is non-finite/out of range",
                ))
            }
        }
        _ => Err(HostError::new(
            ReasonCode::InvalidArgument,
            "statistics input scalar is nested or null",
        )),
    }
}

/// Validate a plugin candidate, supply exact identities and return canonical JSON bytes.
pub fn finalize_artifact(
    raw_candidate: &[u8],
    binding: &ValidatedBinding,
    run_id: &str,
    definition_id: &str,
    definition_digest: &str,
    trace_id: &str,
    host_id: &str,
) -> HostResult<Vec<u8>> {
    if raw_candidate.is_empty() || raw_candidate.len() > 1024 * 1024 {
        return Err(HostError::new(
            ReasonCode::ResourceExhausted,
            "statistics candidate exceeds 1 MiB",
        ));
    }
    let mut candidate: StatisticsCandidate =
        serde_json::from_slice(raw_candidate).map_err(|error| {
            HostError::new(
                ReasonCode::InvalidArgument,
                format!("statistics candidate JSON: {error}"),
            )
        })?;
    validate_candidate(&mut candidate)?;
    let now = unix_ms()?;
    let mut artifact = StatisticsArtifact {
        schema_version: "masi-plugin-statistics/v1".to_owned(),
        record_type: "artifact".to_owned(),
        record_id: format!("artifact-{run_id}"),
        artifact_digest: String::new(),
        run_id: run_id.to_owned(),
        definition_id: definition_id.to_owned(),
        definition_digest: definition_digest.to_owned(),
        status: "succeeded".to_owned(),
        quality: candidate.quality,
        metrics: candidate.metrics,
        series: candidate.series,
        tables: candidate.tables,
        truncation: candidate.truncation,
        provenance: Provenance {
            plugin_id: binding.envelope.plugin_id.clone(),
            plugin_revision: binding.envelope.plugin_revision.clone(),
            computed_at_unix_ms: now,
            definition_id: definition_id.to_owned(),
            definition_digest: definition_digest.to_owned(),
            run_id: run_id.to_owned(),
            binding_generation: binding.envelope.binding_generation,
        },
        bytes: 0,
        actor_ref: host_id.to_owned(),
        reason_code: candidate.reason_code,
        trace_id: trace_id.to_owned(),
    };
    validate_identity(&artifact.record_id, "artifact id")?;
    validate_identity(run_id, "run id")?;
    validate_identity(definition_id, "definition id")?;
    validate_digest_text(definition_digest)?;
    artifact.artifact_digest = compute_artifact_digest(&artifact)?;
    for _ in 0..4 {
        let value = serde_json::to_value(&artifact).map_err(|error| {
            HostError::new(ReasonCode::Internal, format!("artifact encode: {error}"))
        })?;
        let raw = canonical_json_bytes(&value)?;
        if artifact.bytes == raw.len() {
            if raw.len() > 1024 * 1024 {
                return Err(HostError::new(
                    ReasonCode::ResourceExhausted,
                    "final artifact exceeds 1 MiB",
                ));
            }
            return Ok(raw);
        }
        artifact.bytes = raw.len();
    }
    Err(HostError::new(
        ReasonCode::Internal,
        "artifact encoded byte count did not converge",
    ))
}

/// Compute the Go-compatible artifact digest excluding self-reference and encoded size.
pub fn compute_artifact_digest(artifact: &StatisticsArtifact) -> HostResult<String> {
    let mut canonical = artifact.clone();
    canonical.artifact_digest.clear();
    canonical.bytes = 0;
    let value = serde_json::to_value(&canonical).map_err(|error| {
        HostError::new(
            ReasonCode::Internal,
            format!("artifact digest encode: {error}"),
        )
    })?;
    let raw = canonical_json_bytes(&value)?;
    Ok(sha256_bytes(&raw))
}

fn canonical_json_bytes(value: &serde_json::Value) -> HostResult<Vec<u8>> {
    fn append(value: &serde_json::Value, output: &mut String) -> Result<(), serde_json::Error> {
        match value {
            serde_json::Value::Null => output.push_str("null"),
            serde_json::Value::Bool(value) => {
                output.push_str(if *value { "true" } else { "false" })
            }
            serde_json::Value::Number(number) => {
                if let Some(value) = number.as_i64() {
                    output.push_str(&value.to_string());
                } else if let Some(value) = number.as_u64() {
                    output.push_str(&value.to_string());
                } else if let Some(value) = number.as_f64() {
                    output.push_str(&value.to_string());
                }
            }
            serde_json::Value::String(value) => output.push_str(&serde_json::to_string(value)?),
            serde_json::Value::Array(values) => {
                output.push('[');
                for (index, item) in values.iter().enumerate() {
                    if index > 0 {
                        output.push(',');
                    }
                    append(item, output)?;
                }
                output.push(']');
            }
            serde_json::Value::Object(values) => {
                let mut keys: Vec<_> = values.keys().collect();
                keys.sort();
                output.push('{');
                for (index, key) in keys.into_iter().enumerate() {
                    if index > 0 {
                        output.push(',');
                    }
                    output.push_str(&serde_json::to_string(key)?);
                    output.push(':');
                    append(&values[key], output)?;
                }
                output.push('}');
            }
        }
        Ok(())
    }

    let mut output = String::new();
    append(value, &mut output).map_err(|error| {
        HostError::new(
            ReasonCode::Internal,
            format!("canonical JSON encode: {error}"),
        )
    })?;
    Ok(output.into_bytes())
}

fn validate_candidate(candidate: &mut StatisticsCandidate) -> HostResult<()> {
    match candidate.quality.as_str() {
        "valid" | "partial" | "gap" | "stale" | "no_data" | "not_measurable" | "invalid" => {}
        _ => {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "unknown artifact quality",
            ));
        }
    }
    validate_reason(&candidate.reason_code)?;
    if candidate.metrics.len() > 32 || candidate.series.len() > 64 || candidate.tables.len() > 8 {
        return Err(HostError::new(
            ReasonCode::ResourceExhausted,
            "statistics metric/series/table count exceeds profile",
        ));
    }
    let mut metric_ids = BTreeSet::new();
    for metric in &candidate.metrics {
        validate_identity(&metric.metric_id, "metric id")?;
        if !metric_ids.insert(metric.metric_id.clone())
            || !matches!(metric.metric_kind.as_str(), "gauge" | "sum" | "histogram")
            || !matches!(metric.temporality.as_str(), "delta" | "cumulative")
            || !metric.value.is_finite()
            || metric.unit.len() > 64
            || forbidden_text(&metric.unit)
        {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "metric is duplicated, unknown, non-finite or unsafe",
            ));
        }
    }
    let mut total_points = 0usize;
    let mut series_ids = BTreeSet::new();
    for series in &mut candidate.series {
        validate_identity(&series.series_id, "series id")?;
        if !series_ids.insert(series.series_id.clone()) || series.labels.len() > 8 {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "series identity/dimensions invalid",
            ));
        }
        for (key, value) in &series.labels {
            if key.len() > 64 || value.len() > 128 || forbidden_text(value) {
                return Err(HostError::new(
                    ReasonCode::InvalidArgument,
                    "series dimension exceeds profile or is unsafe",
                ));
            }
            validate_identity(key, "dimension key")?;
        }
        let mut previous = None;
        for point in &series.points {
            if point.timestamp_unix_ms < 1 || !point.value.is_finite() {
                return Err(HostError::new(
                    ReasonCode::InvalidArgument,
                    "series point is invalid/non-finite",
                ));
            }
            if previous.is_some_and(|value| point.timestamp_unix_ms <= value) {
                return Err(HostError::new(
                    ReasonCode::InvalidArgument,
                    "series points are duplicate or out of order",
                ));
            }
            previous = Some(point.timestamp_unix_ms);
        }
        series.point_count = series.points.len();
        total_points = total_points.saturating_add(series.points.len());
    }
    if total_points > 10_000 {
        return Err(HostError::new(
            ReasonCode::ResourceExhausted,
            "statistics numeric points exceed 10000",
        ));
    }
    let mut total_rows = 0usize;
    let mut table_ids = BTreeSet::new();
    for table in &mut candidate.tables {
        validate_identity(&table.table_id, "table id")?;
        if !table_ids.insert(table.table_id.clone()) || table.columns.len() > 32 {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "table identity/columns invalid",
            ));
        }
        let mut columns = BTreeSet::new();
        for column in &table.columns {
            validate_identity(column, "column id")?;
            if !columns.insert(column) {
                return Err(HostError::new(
                    ReasonCode::InvalidArgument,
                    "duplicate table column",
                ));
            }
        }
        for row in &table.rows {
            if row.len() != table.columns.len() {
                return Err(HostError::new(
                    ReasonCode::InvalidArgument,
                    "table row/column width mismatch",
                ));
            }
            for value in row {
                validate_scalar(value)?;
            }
        }
        table.row_count = table.rows.len();
        total_rows = total_rows.saturating_add(table.rows.len());
    }
    if total_rows > 2000
        || candidate.truncation.truncated_rows > 10_000
        || candidate.truncation.truncated_series > 64
    {
        return Err(HostError::new(
            ReasonCode::ResourceExhausted,
            "statistics table/truncation count exceeds profile",
        ));
    }
    validate_reason(&candidate.truncation.reason_code)?;
    let encoded = serde_json::to_vec(candidate).map_err(|error| {
        HostError::new(
            ReasonCode::InvalidArgument,
            format!("candidate encode: {error}"),
        )
    })?;
    if encoded.len() > 1024 * 1024
        || forbidden_text(std::str::from_utf8(&encoded).unwrap_or_default())
    {
        return Err(HostError::new(
            ReasonCode::CapabilityDenied,
            "statistics candidate contains executable/display payload",
        ));
    }
    Ok(())
}

fn validate_scalar(value: &serde_json::Value) -> HostResult<()> {
    match value {
        serde_json::Value::Bool(_) => Ok(()),
        serde_json::Value::Number(number) => number
            .as_f64()
            .filter(|value| value.is_finite())
            .map(|_| ())
            .ok_or_else(|| {
                HostError::new(
                    ReasonCode::InvalidArgument,
                    "numeric scalar is non-finite/out of range",
                )
            }),
        serde_json::Value::String(value) if value.len() <= 4096 && !forbidden_text(value) => Ok(()),
        _ => Err(HostError::new(
            ReasonCode::InvalidArgument,
            "nested, unsafe or oversized scalar rejected",
        )),
    }
}

fn forbidden_text(value: &str) -> bool {
    let lower = value.to_ascii_lowercase();
    [
        "<script",
        "<svg",
        "<iframe",
        "javascript:",
        "data:text/html",
        "v-html",
        "echarts",
        "renderitem",
        "formatter",
        "vega",
        "http://",
        "https://",
        "eval(",
        "new function",
        "onclick=",
        "onerror=",
    ]
    .iter()
    .any(|needle| lower.contains(needle))
}

fn validate_identity(value: &str, field: &str) -> HostResult<()> {
    if value.is_empty()
        || value.len() > 128
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._:-".contains(&byte))
    {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            format!("{field} malformed"),
        ));
    }
    Ok(())
}

fn validate_digest_text(value: &str) -> HostResult<()> {
    if value.len() != 71
        || !value.starts_with("sha256:")
        || !value[7..]
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            "digest malformed",
        ));
    }
    Ok(())
}

fn validate_reason(value: &str) -> HostResult<()> {
    if value.is_empty()
        || value.len() > 64
        || !value.bytes().enumerate().all(|(index, byte)| {
            if index == 0 {
                byte.is_ascii_uppercase()
            } else {
                byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_'
            }
        })
    {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            "reason code malformed",
        ));
    }
    Ok(())
}

fn unix_ms() -> HostResult<i64> {
    let duration = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| HostError::new(ReasonCode::Internal, "clock precedes epoch"))?;
    i64::try_from(duration.as_millis())
        .map_err(|_| HostError::new(ReasonCode::Internal, "clock overflow"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_nan_and_browser_code() {
        let mut candidate = StatisticsCandidate {
            quality: "valid".to_owned(),
            metrics: vec![Metric {
                metric_id: "m1".to_owned(),
                metric_kind: "gauge".to_owned(),
                temporality: "delta".to_owned(),
                value: f64::NAN,
                unit: "count".to_owned(),
            }],
            series: Vec::new(),
            tables: Vec::new(),
            truncation: Truncation {
                truncated_rows: 0,
                truncated_series: 0,
                reason_code: "NONE".to_owned(),
            },
            reason_code: "STATISTICS_COMPUTED".to_owned(),
        };
        assert!(validate_candidate(&mut candidate).is_err());
        candidate.metrics[0].value = 1.0;
        candidate.metrics[0].unit = "<svg onload=x>".to_owned();
        assert!(validate_candidate(&mut candidate).is_err());
    }

    #[test]
    fn rejects_duplicate_or_out_of_order_points() {
        let mut candidate = StatisticsCandidate {
            quality: "valid".to_owned(),
            metrics: Vec::new(),
            series: vec![Series {
                series_id: "s1".to_owned(),
                labels: BTreeMap::new(),
                point_count: 2,
                points: vec![
                    Point {
                        timestamp_unix_ms: 2,
                        value: 1.0,
                    },
                    Point {
                        timestamp_unix_ms: 2,
                        value: 2.0,
                    },
                ],
            }],
            tables: Vec::new(),
            truncation: Truncation {
                truncated_rows: 0,
                truncated_series: 0,
                reason_code: "NONE".to_owned(),
            },
            reason_code: "STATISTICS_COMPUTED".to_owned(),
        };
        assert!(validate_candidate(&mut candidate).is_err());
    }
}
