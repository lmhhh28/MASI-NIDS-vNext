import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import PluginArtifactRenderer from '@/components/PluginArtifactRenderer.vue'
import type { ArtifactRecord } from '@masi/control-api'

const digest = `sha256:${'a'.repeat(64)}`
const artifact: ArtifactRecord = {
  schema_version: 'masi-plugin-statistics/v1', record_type: 'artifact', record_id: 'artifact-1',
  artifact_digest: digest, run_id: 'run-1', definition_id: 'definition-1', definition_digest: digest,
  status: 'succeeded', quality: 'valid', metrics: [], series: [],
  tables: [{ table_id: 'host-table', columns: ['untrusted'], row_count: 1, rows: [['<img src=x onerror=alert(1)>']] }],
  truncation: { truncated_rows: 0, truncated_series: 0, reason_code: 'NONE' },
  provenance: { plugin_id: 'plugin-1', plugin_revision: 'manifest-1:1', computed_at_unix_ms: 1, definition_id: 'definition-1', definition_digest: digest, run_id: 'run-1', binding_generation: 1 },
  bytes: 512, actor_ref: 'plugin-1', reason_code: 'SUCCEEDED', trace_id: 'trace-1',
}

describe('fixed plugin renderer', () => {
  it('renders untrusted cells as text and never creates plugin markup', () => {
    const wrapper = mount(PluginArtifactRenderer, { props: { artifact, displayHint: 'table' } })
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.text()).toContain('<img src=x onerror=alert(1)>')
  })

  it('rejects an unknown display kind without raw JSON fallback', () => {
    const wrapper = mount(PluginArtifactRenderer, { props: { artifact, displayHint: 'vega' } })
    expect(wrapper.text()).toContain('Display kind rejected')
    expect(wrapper.text()).toContain('No raw JSON fallback')
  })
})
