import { getStatisticsArtifact, type ArtifactRecord } from '@masi/control-api'
import { controlClient, withRequestSlot } from './client'
import { asRecord, ContractError, responseData } from './guards'

const qualities = ['valid', 'partial', 'gap', 'stale', 'no_data', 'not_measurable', 'invalid']
const statuses = ['pending', 'running', 'succeeded', 'failed', 'cancelled', 'expired', 'fenced']
const metricKinds = ['gauge', 'sum', 'histogram']
const temporalities = ['delta', 'cumulative']
const identityPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/
const digestPattern = /^sha256:(?!0{64}$)[0-9a-f]{64}$/
const reasonPattern = /^[A-Z][A-Z0-9_]{0,63}$/

function finite(value: unknown): boolean {
  return typeof value === 'number' && Number.isFinite(value)
}

function exactKeys(record: Record<string, unknown>, required: string[], optional: string[] = []): void {
  const allowed = new Set([...required, ...optional])
  if (required.some((key) => !(key in record)) || Object.keys(record).some((key) => !allowed.has(key))) {
    throw new ContractError('STATISTICS_ARTIFACT_SHAPE_INVALID', 'Artifact fields do not match the closed contract.')
  }
}

function identity(value: unknown, field: string): string {
  if (typeof value !== 'string' || !identityPattern.test(value)) {
    throw new ContractError('STATISTICS_IDENTITY_INVALID', `${field} is not a bounded identity.`)
  }
  return value
}

function digest(value: unknown, field: string): string {
  if (typeof value !== 'string' || !digestPattern.test(value)) {
    throw new ContractError('STATISTICS_DIGEST_INVALID', `${field} is not an exact sha256 digest.`)
  }
  return value
}

function boundedInteger(value: unknown, field: string, minimum: number, maximum: number): number {
  if (!Number.isSafeInteger(value) || (value as number) < minimum || (value as number) > maximum) {
    throw new ContractError('STATISTICS_INTEGER_INVALID', `${field} is outside its integer bound.`)
  }
  return value as number
}

export function assertStatisticsArtifact(value: unknown): ArtifactRecord {
  const record = asRecord(value, 'STATISTICS_ARTIFACT_INVALID')
  const rootKeys = [
    'schema_version', 'record_type', 'record_id', 'artifact_digest', 'run_id', 'definition_id',
    'definition_digest', 'status', 'quality', 'metrics', 'series', 'tables', 'truncation',
    'provenance', 'bytes', 'actor_ref', 'reason_code', 'trace_id',
  ]
  exactKeys(record, rootKeys)
  if (record.schema_version !== 'masi-plugin-statistics/v1' || record.record_type !== 'artifact') {
    throw new ContractError('STATISTICS_ARTIFACT_MAJOR_UNSUPPORTED', 'The plugin statistics artifact major is unsupported.')
  }
  if (!qualities.includes(String(record.quality)) || !statuses.includes(String(record.status))) {
    throw new ContractError('STATISTICS_ARTIFACT_STATE_INVALID', 'Artifact status or quality is unsupported.')
  }
  identity(record.record_id, 'record_id')
  digest(record.artifact_digest, 'artifact_digest')
  const runID = identity(record.run_id, 'run_id')
  const definitionID = identity(record.definition_id, 'definition_id')
  const definitionDigest = digest(record.definition_digest, 'definition_digest')
  identity(record.actor_ref, 'actor_ref')
  identity(record.trace_id, 'trace_id')
  if (typeof record.reason_code !== 'string' || !reasonPattern.test(record.reason_code)) {
    throw new ContractError('STATISTICS_REASON_INVALID', 'Artifact reason_code is invalid.')
  }
  if (!Number.isSafeInteger(record.bytes) || (record.bytes as number) < 1 || (record.bytes as number) > 1_048_576) {
    throw new ContractError('STATISTICS_ARTIFACT_OVERSIZE', 'Artifact bytes are outside the 1 MiB bound.')
  }
  if (!Array.isArray(record.metrics) || record.metrics.length > 32 ||
      !Array.isArray(record.series) || record.series.length > 64 ||
      !Array.isArray(record.tables) || record.tables.length > 8) {
    throw new ContractError('STATISTICS_ARTIFACT_CARDINALITY', 'Artifact collection cardinality exceeds its contract.')
  }
  for (const metricValue of record.metrics) {
    const metric = asRecord(metricValue)
    exactKeys(metric, ['metric_id', 'metric_kind', 'temporality', 'value', 'unit'])
    identity(metric.metric_id, 'metric_id')
    if (!metricKinds.includes(String(metric.metric_kind)) || !temporalities.includes(String(metric.temporality))) {
      throw new ContractError('STATISTICS_METRIC_KIND_INVALID', 'Metric kind or temporality is unsupported.')
    }
    if (!finite(metric.value)) throw new ContractError('STATISTICS_NON_FINITE', 'Metric contains NaN or Infinity.')
    if (typeof metric.unit !== 'string' || metric.unit.length > 64) throw new ContractError('STATISTICS_UNIT_INVALID', 'Metric unit exceeds its bound.')
  }
  if (new Set(record.metrics.map((item) => (item as Record<string, unknown>).metric_id)).size !== record.metrics.length) {
    throw new ContractError('STATISTICS_DUPLICATE_IDENTITY', 'Metric identities must be unique.')
  }
  let totalPoints = 0
  const seriesIDs = new Set<string>()
  for (const seriesValue of record.series) {
    const series = asRecord(seriesValue)
    exactKeys(series, ['series_id', 'labels', 'point_count', 'points'])
    const seriesID = identity(series.series_id, 'series_id')
    if (seriesIDs.has(seriesID)) throw new ContractError('STATISTICS_DUPLICATE_IDENTITY', 'Series identities must be unique.')
    seriesIDs.add(seriesID)
    const labels = asRecord(series.labels, 'STATISTICS_LABELS_INVALID')
    if (Object.keys(labels).length > 32 || Object.values(labels).some((label) => typeof label !== 'string')) {
      throw new ContractError('STATISTICS_LABELS_INVALID', 'Series labels exceed the closed string map bound.')
    }
    if (!Array.isArray(series.points) || series.points.length > 10_000) {
      throw new ContractError('STATISTICS_POINTS_OVERSIZE', 'Series exceeds the point bound.')
    }
    if (boundedInteger(series.point_count, 'point_count', 0, 10_000) !== series.points.length) {
      throw new ContractError('STATISTICS_POINT_COUNT_MISMATCH', 'Series point_count does not match points.')
    }
    totalPoints += series.points.length
    let previousTimestamp = 0
    for (const pointValue of series.points) {
      const point = asRecord(pointValue)
      exactKeys(point, ['timestamp_unix_ms', 'value'])
      const timestamp = boundedInteger(point.timestamp_unix_ms, 'timestamp_unix_ms', 1, Number.MAX_SAFE_INTEGER)
      if (!finite(point.value) || timestamp <= previousTimestamp) {
        throw new ContractError('STATISTICS_POINT_INVALID', 'Series point is non-finite or has an invalid timestamp.')
      }
      previousTimestamp = timestamp
    }
  }
  if (totalPoints > 10_000) throw new ContractError('STATISTICS_PAGE_POINTS_OVERSIZE', 'Artifact exceeds the per-page point bound.')
  const tableIDs = new Set<string>()
  for (const tableValue of record.tables) {
    const table = asRecord(tableValue)
    exactKeys(table, ['table_id', 'columns', 'row_count', 'rows'])
    const tableID = identity(table.table_id, 'table_id')
    if (tableIDs.has(tableID)) throw new ContractError('STATISTICS_DUPLICATE_IDENTITY', 'Table identities must be unique.')
    tableIDs.add(tableID)
    if (!Array.isArray(table.columns) || table.columns.length > 32 || !Array.isArray(table.rows) || table.rows.length > 2_000) {
      throw new ContractError('STATISTICS_TABLE_OVERSIZE', 'Artifact table exceeds its row or column bound.')
    }
    if (table.columns.some((column) => typeof column !== 'string') || new Set(table.columns).size !== table.columns.length) {
      throw new ContractError('STATISTICS_COLUMNS_INVALID', 'Artifact table columns must be unique strings.')
    }
    if (boundedInteger(table.row_count, 'row_count', 0, 2_000) !== table.rows.length) {
      throw new ContractError('STATISTICS_ROW_COUNT_MISMATCH', 'Table row_count does not match rows.')
    }
    for (const row of table.rows) {
      if (!Array.isArray(row) || row.length !== table.columns.length || row.some((cell) =>
        cell !== null && (!['string', 'number', 'boolean'].includes(typeof cell) || (typeof cell === 'number' && !Number.isFinite(cell))),
      )) {
        throw new ContractError('STATISTICS_TABLE_CELL_INVALID', 'Artifact table contains an unsupported cell.')
      }
    }
  }
  const truncation = asRecord(record.truncation, 'STATISTICS_TRUNCATION_INVALID')
  exactKeys(truncation, ['truncated_rows', 'truncated_series', 'reason_code'])
  boundedInteger(truncation.truncated_rows, 'truncated_rows', 0, 10_000)
  boundedInteger(truncation.truncated_series, 'truncated_series', 0, 64)
  if (typeof truncation.reason_code !== 'string' || !reasonPattern.test(truncation.reason_code)) {
    throw new ContractError('STATISTICS_TRUNCATION_INVALID', 'Truncation reason is invalid.')
  }
  const provenance = asRecord(record.provenance, 'STATISTICS_PROVENANCE_INVALID')
  exactKeys(provenance, ['plugin_id', 'plugin_revision', 'computed_at_unix_ms', 'definition_id', 'definition_digest', 'run_id', 'binding_generation'], ['external_source'])
  identity(provenance.plugin_id, 'provenance.plugin_id')
  identity(provenance.plugin_revision, 'provenance.plugin_revision')
  boundedInteger(provenance.computed_at_unix_ms, 'computed_at_unix_ms', 1, Number.MAX_SAFE_INTEGER)
  boundedInteger(provenance.binding_generation, 'binding_generation', 1, Number.MAX_SAFE_INTEGER)
  if (identity(provenance.definition_id, 'provenance.definition_id') !== definitionID
      || digest(provenance.definition_digest, 'provenance.definition_digest') !== definitionDigest
      || identity(provenance.run_id, 'provenance.run_id') !== runID) {
    throw new ContractError('STATISTICS_PROVENANCE_MISMATCH', 'Artifact and provenance identities do not match.')
  }
  if (provenance.external_source !== undefined) {
    const external = asRecord(provenance.external_source, 'STATISTICS_EXTERNAL_PROVENANCE_INVALID')
    exactKeys(external, ['capability_id', 'request_digest', 'observed_at_unix_ms', 'response_digest', 'status'], ['etag_or_version'])
    identity(external.capability_id, 'external_source.capability_id')
    digest(external.request_digest, 'external_source.request_digest')
    digest(external.response_digest, 'external_source.response_digest')
    boundedInteger(external.observed_at_unix_ms, 'external_source.observed_at_unix_ms', 1, Number.MAX_SAFE_INTEGER)
    if (!['complete', 'partial', 'timeout'].includes(String(external.status))
        || (external.etag_or_version !== undefined && (typeof external.etag_or_version !== 'string' || external.etag_or_version.length > 256))) {
      throw new ContractError('STATISTICS_EXTERNAL_PROVENANCE_INVALID', 'External source provenance is invalid.')
    }
  }
  return record as unknown as ArtifactRecord
}

export async function fetchStatisticsArtifact(artifactID: string): Promise<ArtifactRecord> {
  const result: unknown = await withRequestSlot((signal) => getStatisticsArtifact({
    client: controlClient,
    path: { artifactID },
    signal,
  }))
  const artifact = assertStatisticsArtifact(responseData(result))
  if (artifact.record_id !== artifactID) {
    throw new ContractError('STATISTICS_ARTIFACT_IDENTITY_MISMATCH', 'The exact artifact response identity changed.')
  }
  return artifact
}
