<script setup lang="ts">
import { computed } from 'vue'
import type { ArtifactRecord } from '@masi/control-api'
import { abbreviateDigest, displayText, formatNumber, formatTime } from '@/format'
import StatusMark from './StatusMark.vue'
import StatePanel from './StatePanel.vue'
import StatisticsChart from './StatisticsChart.vue'

const props = defineProps<{ artifact: ArtifactRecord; displayHint: string }>()
const allowed = ['metric-card', 'status', 'timeseries', 'bar', 'heatmap', 'table', 'text', 'evidence-list'] as const
type DisplayHint = typeof allowed[number]
const kind = computed<DisplayHint | null>(() => allowed.includes(props.displayHint as DisplayHint) ? props.displayHint as DisplayHint : null)
const chartKind = computed(() => kind.value === 'timeseries' || kind.value === 'bar' || kind.value === 'heatmap' ? kind.value : null)

function firstPoint(seriesIndex: number): string {
  const point = props.artifact.series[seriesIndex]?.points[0]
  return point ? formatTime(point.timestamp_unix_ms) : 'No points'
}

function lastPoint(seriesIndex: number): string {
  const points = props.artifact.series[seriesIndex]?.points
  const point = points?.[points.length - 1]
  return point ? `${formatTime(point.timestamp_unix_ms)} · ${formatNumber(point.value)}` : 'No points'
}
</script>

<template>
  <article class="artifact">
    <header class="artifact__header">
      <div>
        <p>Built-in {{ kind ?? 'unsupported' }} renderer</p><h2 class="mono">
          {{ artifact.definition_id }}
        </h2>
      </div>
      <div class="artifact__states">
        <StatusMark :value="artifact.status" /><StatusMark :value="artifact.quality" />
      </div>
    </header>
    <section
      class="artifact__provenance"
      aria-label="Artifact provenance"
    >
      <span>Plugin <b class="mono">{{ artifact.provenance.plugin_id }}</b></span>
      <span>Revision <b class="mono">{{ artifact.provenance.plugin_revision }}</b></span>
      <span>Binding <b>{{ artifact.provenance.binding_generation }}</b></span>
      <time>Computed {{ formatTime(artifact.provenance.computed_at_unix_ms) }}</time>
      <span>Quality <b>{{ artifact.quality }}</b></span>
      <span>Truncation <b>{{ artifact.truncation.truncated_rows }} rows / {{ artifact.truncation.truncated_series }} series</b></span>
    </section>

    <StatePanel
      v-if="kind === null"
      state="unsupported"
      title="Display kind rejected"
      detail="The artifact requested a display kind outside the eight-kind host registry. No raw JSON fallback is available."
    />

    <section
      v-else-if="kind === 'metric-card'"
      class="metric-cards"
      aria-label="Artifact metrics"
    >
      <article
        v-for="metric in artifact.metrics"
        :key="metric.metric_id"
      >
        <span>{{ metric.metric_id }}</span><strong>{{ formatNumber(metric.value) }}</strong><small>{{ metric.unit }} · {{ metric.metric_kind }} / {{ metric.temporality }}</small>
      </article>
      <p
        v-if="artifact.metrics.length === 0"
        class="artifact__empty"
      >
        No metric facts. No value was replaced with zero.
      </p>
    </section>

    <section
      v-else-if="kind === 'status'"
      class="status-renderer"
    >
      <StatusMark :value="artifact.status" /><StatusMark :value="artifact.quality" /><strong>{{ artifact.reason_code }}</strong>
      <p>Status is derived from the validated artifact envelope; it does not imply a core service or device state.</p>
    </section>

    <section
      v-else-if="chartKind"
      class="chart-renderer"
    >
      <StatisticsChart
        :artifact="artifact"
        :kind="chartKind"
      />
      <details>
        <summary>Equivalent data summary</summary>
        <table v-if="chartKind === 'bar'">
          <caption>Metric values and units</caption><thead><tr><th>Metric</th><th>Value</th><th>Unit</th><th>Kind</th></tr></thead><tbody>
            <tr
              v-for="metric in artifact.metrics"
              :key="metric.metric_id"
            >
              <th scope="row">
                {{ metric.metric_id }}
              </th><td>{{ formatNumber(metric.value) }}</td><td>{{ metric.unit }}</td><td>{{ metric.metric_kind }} / {{ metric.temporality }}</td>
            </tr>
          </tbody>
        </table>
        <table v-else>
          <caption>Series coverage summary</caption><thead><tr><th>Series</th><th>Labels</th><th>Points</th><th>First point</th><th>Last point / value</th></tr></thead><tbody>
            <tr
              v-for="(series, index) in artifact.series"
              :key="series.series_id"
            >
              <th scope="row">
                {{ series.series_id }}
              </th><td>{{ Object.entries(series.labels).map(([key, value]) => `${key}=${value}`).join(', ') || 'none' }}</td><td>{{ series.point_count }}</td><td>{{ firstPoint(index) }}</td><td>{{ lastPoint(index) }}</td>
            </tr>
          </tbody>
        </table>
      </details>
    </section>

    <section
      v-else-if="kind === 'table'"
      class="table-renderer"
    >
      <div
        v-for="table in artifact.tables"
        :key="table.table_id"
        class="table-scroll"
      >
        <table>
          <caption>{{ table.table_id }} · {{ table.row_count }} rows</caption><thead>
            <tr>
              <th
                v-for="column in table.columns"
                :key="column"
              >
                {{ column }}
              </th>
            </tr>
          </thead><tbody>
            <tr
              v-for="(row, rowIndex) in table.rows"
              :key="rowIndex"
            >
              <td
                v-for="(cell, cellIndex) in row"
                :key="cellIndex"
              >
                {{ displayText(cell) }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p
        v-if="artifact.tables.length === 0"
        class="artifact__empty"
      >
        No table rows. Null and no-data remain explicit.
      </p>
    </section>

    <section
      v-else-if="kind === 'text'"
      class="text-renderer"
    >
      <h3>{{ artifact.reason_code }}</h3><p>Artifact {{ artifact.record_id }} is {{ artifact.status }} with {{ artifact.quality }} quality.</p><p>This host-owned text view does not interpret plugin HTML, Markdown, URL, SVG, CSS, or script content.</p>
    </section>

    <section
      v-else-if="kind === 'evidence-list'"
      class="evidence-renderer"
    >
      <dl>
        <div>
          <dt>Artifact</dt><dd class="mono">
            {{ artifact.record_id }}
          </dd>
        </div><div>
          <dt>Digest</dt><dd
            class="mono"
            :title="artifact.artifact_digest"
          >
            {{ abbreviateDigest(artifact.artifact_digest) }}
          </dd>
        </div><div>
          <dt>Run</dt><dd class="mono">
            {{ artifact.run_id }}
          </dd>
        </div><div>
          <dt>Definition digest</dt><dd
            class="mono"
            :title="artifact.definition_digest"
          >
            {{ abbreviateDigest(artifact.definition_digest) }}
          </dd>
        </div><div><dt>Reason</dt><dd>{{ artifact.reason_code }}</dd></div><div><dt>Bytes</dt><dd>{{ formatNumber(artifact.bytes) }}</dd></div>
      </dl>
    </section>
  </article>
</template>

<style scoped>
.artifact { border: 1px solid var(--border-subtle); background: var(--surface-panel); }.artifact__header { display: flex; justify-content: space-between; gap: var(--space-4); padding: var(--space-4); border-bottom: 1px solid var(--border-subtle); }.artifact__header p { margin: 0; color: var(--action-primary); font-size: var(--text-xs); font-weight: 750; text-transform: uppercase; letter-spacing: .07em; }.artifact__header h2 { margin: var(--space-1) 0 0; font-size: var(--text-lg); }.artifact__states { display: flex; align-items: center; gap: var(--space-2); }.artifact__provenance { display: flex; flex-wrap: wrap; gap: var(--space-3) var(--space-5); padding: var(--space-3) var(--space-4); border-bottom: 1px solid var(--border-subtle); background: var(--surface-muted); color: var(--text-secondary); font-size: var(--text-xs); }
.metric-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr)); }.metric-cards article { display: grid; gap: var(--space-1); padding: var(--space-4); border-right: 1px solid var(--border-subtle); }.metric-cards span { color: var(--text-secondary); }.metric-cards strong { font-size: var(--text-xl); font-variant-numeric: tabular-nums; }.metric-cards small { color: var(--text-muted); }.status-renderer, .text-renderer { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-4); padding: var(--space-6); }.status-renderer p { width: 100%; margin: 0; color: var(--text-muted); }.chart-renderer { padding: var(--space-4); }.chart-renderer details { margin-top: var(--space-3); }.chart-renderer summary { cursor: pointer; color: var(--action-primary); font-weight: 700; }.table-scroll { max-width: 100%; overflow: auto; padding: var(--space-4); }table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }caption { padding: var(--space-2); text-align: left; color: var(--text-secondary); }th,td { padding: var(--space-2); border: 1px solid var(--border-subtle); text-align: left; }.evidence-renderer dl { margin: 0; padding: var(--space-4); display: grid; }.evidence-renderer dl div { display: grid; grid-template-columns: 10rem 1fr; gap: var(--space-3); padding: var(--space-2); border-bottom: 1px solid var(--border-subtle); }.evidence-renderer dt { color: var(--text-muted); }.evidence-renderer dd { margin: 0; overflow-wrap: anywhere; }.artifact__empty { padding: var(--space-6); color: var(--text-muted); }
@media (max-width: 40rem) { .artifact__header { flex-direction: column; }.evidence-renderer dl div { grid-template-columns: 1fr; gap: 0; } }
</style>
