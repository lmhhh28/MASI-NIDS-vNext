import { getStatisticsArtifact, type ArtifactRecord } from '@masi/control-api'
import { controlClient, withRequestSlot } from './client'
import { asRecord, ContractError, responseData } from './guards'

const qualities = ['valid', 'partial', 'gap', 'stale', 'no_data', 'not_measurable', 'invalid']
const statuses = ['pending', 'running', 'succeeded', 'failed', 'cancelled', 'expired', 'fenced']

function finite(value: unknown): boolean {
  return typeof value === 'number' && Number.isFinite(value)
}

export function assertStatisticsArtifact(value: unknown): ArtifactRecord {
  const record = asRecord(value, 'STATISTICS_ARTIFACT_INVALID')
  if (record.schema_version !== 'masi-plugin-statistics/v1' || record.record_type !== 'artifact') {
    throw new ContractError('STATISTICS_ARTIFACT_MAJOR_UNSUPPORTED', 'The plugin statistics artifact major is unsupported.')
  }
  if (!qualities.includes(String(record.quality)) || !statuses.includes(String(record.status))) {
    throw new ContractError('STATISTICS_ARTIFACT_STATE_INVALID', 'Artifact status or quality is unsupported.')
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
    if (!finite(metric.value)) throw new ContractError('STATISTICS_NON_FINITE', 'Metric contains NaN or Infinity.')
  }
  let totalPoints = 0
  for (const seriesValue of record.series) {
    const series = asRecord(seriesValue)
    if (!Array.isArray(series.points) || series.points.length > 10_000) {
      throw new ContractError('STATISTICS_POINTS_OVERSIZE', 'Series exceeds the point bound.')
    }
    totalPoints += series.points.length
    for (const pointValue of series.points) {
      const point = asRecord(pointValue)
      if (!finite(point.value) || !Number.isSafeInteger(point.timestamp_unix_ms)) {
        throw new ContractError('STATISTICS_POINT_INVALID', 'Series point is non-finite or has an invalid timestamp.')
      }
    }
  }
  if (totalPoints > 10_000) throw new ContractError('STATISTICS_PAGE_POINTS_OVERSIZE', 'Artifact exceeds the per-page point bound.')
  for (const tableValue of record.tables) {
    const table = asRecord(tableValue)
    if (!Array.isArray(table.columns) || table.columns.length > 32 || !Array.isArray(table.rows) || table.rows.length > 2_000) {
      throw new ContractError('STATISTICS_TABLE_OVERSIZE', 'Artifact table exceeds its row or column bound.')
    }
    for (const row of table.rows) {
      if (!Array.isArray(row) || row.length !== table.columns.length || row.some((cell) => cell !== null && !['string', 'number', 'boolean'].includes(typeof cell))) {
        throw new ContractError('STATISTICS_TABLE_CELL_INVALID', 'Artifact table contains an unsupported cell.')
      }
    }
  }
  asRecord(record.truncation, 'STATISTICS_TRUNCATION_INVALID')
  asRecord(record.provenance, 'STATISTICS_PROVENANCE_INVALID')
  return record as unknown as ArtifactRecord
}

export async function fetchStatisticsArtifact(artifactID: string): Promise<ArtifactRecord> {
  const result: unknown = await withRequestSlot(() => getStatisticsArtifact({
    client: controlClient,
    path: { artifactID },
  }))
  return assertStatisticsArtifact(responseData(result))
}
