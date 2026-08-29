import { describe, expect, it } from 'vitest'
import { assertStatisticsArtifact } from '@/api/statistics'

const digest = `sha256:${'a'.repeat(64)}`
function artifact() {
  return {
    schema_version: 'masi-plugin-statistics/v1', record_type: 'artifact', record_id: 'artifact-1',
    artifact_digest: digest, run_id: 'run-1', definition_id: 'definition-1', definition_digest: digest,
    status: 'succeeded', quality: 'valid', metrics: [{ metric_id: 'count', metric_kind: 'sum', temporality: 'cumulative', value: 1, unit: 'events' }],
    series: [], tables: [], truncation: { truncated_rows: 0, truncated_series: 0, reason_code: 'NONE' },
    provenance: { plugin_id: 'plugin-1', plugin_revision: 'manifest-1:1', computed_at_unix_ms: 1, definition_id: 'definition-1', definition_digest: digest, run_id: 'run-1', binding_generation: 1 },
    bytes: 512, actor_ref: 'plugin-1', reason_code: 'SUCCEEDED', trace_id: 'trace-1',
  }
}

describe('statistics artifact guard', () => {
  it('accepts finite bounded artifacts', () => expect(assertStatisticsArtifact(artifact()).record_id).toBe('artifact-1'))
  it('keeps the host actor distinct from producer provenance', () => {
    expect(assertStatisticsArtifact({ ...artifact(), actor_ref: 'plugin-host' }).provenance.plugin_id).toBe('plugin-1')
  })
  it('rejects NaN and unknown major', () => {
    expect(() => assertStatisticsArtifact({ ...artifact(), metrics: [{ ...artifact().metrics[0], value: Number.NaN }] })).toThrow('NaN')
    expect(() => assertStatisticsArtifact({ ...artifact(), schema_version: 'masi-plugin-statistics/v2' })).toThrow('unsupported')
  })
  it('rejects count, provenance, and closed-shape confusion', () => {
    expect(() => assertStatisticsArtifact({
      ...artifact(),
      series: [{ series_id: 'series-1', labels: {}, point_count: 2, points: [{ timestamp_unix_ms: 1, value: 1 }] }],
    })).toThrow('point_count')
    expect(() => assertStatisticsArtifact({
      ...artifact(),
      provenance: { ...artifact().provenance, definition_digest: `sha256:${'b'.repeat(64)}` },
    })).toThrow('do not match')
    expect(() => assertStatisticsArtifact({ ...artifact(), executable_payload: 'forbidden' })).toThrow('closed contract')
  })
})
