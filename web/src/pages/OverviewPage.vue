<script setup lang="ts">
import { computed } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { ElButton } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { fetchDashboard } from '@/api/resources'
import { formatNumber, formatTime } from '@/format'
import PageHeading from '@/components/PageHeading.vue'
import StatePanel from '@/components/StatePanel.vue'
import StatusMark from '@/components/StatusMark.vue'

const query = useQuery({
  queryKey: ['dashboard'],
  queryFn: fetchDashboard,
  refetchInterval: 60_000,
})

const metrics = computed(() => {
  const counts = query.data.value?.counts
  if (!counts) return []
  return [
    { label: 'Alerts · 24h', value: counts.alerts_24h, detail: `${counts.events_24h} committed events`, to: '/detection/events', tone: counts.degraded_events_24h > 0 ? 'partial' : 'current' },
    { label: 'Open incidents', value: counts.open_incidents, detail: 'Awaiting triage or closure', to: '/detection/incidents', tone: counts.open_incidents > 0 ? 'warning' : 'current' },
    { label: 'Managed targets', value: counts.targets_active, detail: `${counts.targets_total} registered · ${counts.targets_attention} attention`, to: '/operations/targets', tone: counts.targets_attention > 0 ? 'partial' : 'current' },
    { label: 'Pending approvals', value: counts.pending_approvals, detail: 'Unexpired exact proposals', to: '/governance/approvals', tone: counts.pending_approvals > 0 ? 'warning' : 'current' },
    { label: 'Model shards', value: counts.model_shards_ready, detail: `${counts.model_shards_unavailable} unavailable`, to: '/operations/models', tone: counts.model_shards_unavailable > 0 ? 'hold' : 'current' },
    { label: 'Active plugins', value: counts.plugins_active, detail: `${counts.analysis_attention} analysis attention`, to: '/plugins/catalog', tone: counts.analysis_attention > 0 ? 'partial' : 'current' },
  ]
})
</script>

<template>
  <div class="overview">
    <PageHeading
      eyebrow="Overview"
      title="Operational evidence at a glance"
      description="One bounded PostgreSQL statement snapshot—no browser-side joins, hidden averages, or inferred success."
    >
      <ElButton
        :icon="Refresh"
        :loading="query.isFetching.value"
        @click="query.refetch()"
      >
        Refresh snapshot
      </ElButton>
    </PageHeading>
    <StatePanel
      v-if="query.isPending.value"
      state="loading"
      detail="Loading one authorized dashboard snapshot."
    />
    <StatePanel
      v-else-if="query.isError.value"
      state="error"
      title="Dashboard held"
      :detail="query.error.value instanceof Error ? query.error.value.message : 'Snapshot rejected.'"
      retryable
      @retry="query.refetch()"
    />
    <template v-else-if="query.data.value">
      <section
        class="snapshot-banner"
        aria-label="Snapshot status"
      >
        <StatusMark :value="query.data.value.state" />
        <span>Snapshot <b class="mono">{{ query.data.value.snapshot_id }}</b></span>
        <time>{{ formatTime(query.data.value.snapshot_unix_ms) }}</time>
        <span class="snapshot-banner__reason mono">{{ query.data.value.reason_code }}</span>
      </section>

      <section
        class="metric-grid"
        aria-label="Key operational counts"
      >
        <RouterLink
          v-for="metric in metrics"
          :key="metric.label"
          :to="metric.to"
          class="metric"
        >
          <span class="metric__label">{{ metric.label }}</span>
          <strong>{{ formatNumber(metric.value) }}</strong>
          <span class="metric__detail">{{ metric.detail }}</span>
          <StatusMark
            :value="metric.tone"
            compact
          />
        </RouterLink>
      </section>

      <section
        class="evidence-rail"
        aria-labelledby="evidence-rail-title"
      >
        <header>
          <p>Current chain</p><h2 id="evidence-rail-title">
            Signal → governance → device truth
          </h2>
        </header>
        <ol>
          <li>
            <span class="evidence-rail__index">01</span><div><b>Detection facts</b><small>{{ query.data.value.counts.events_24h }} events · {{ query.data.value.counts.degraded_events_24h }} degraded quality</small></div><RouterLink to="/detection/events">
              Inspect
            </RouterLink>
          </li>
          <li>
            <span class="evidence-rail__index">02</span><div><b>Authorization facts</b><small>{{ query.data.value.counts.pending_approvals }} pending · {{ query.data.value.counts.active_effects }} active intents</small></div><RouterLink to="/governance/operations">
              Inspect
            </RouterLink>
          </li>
          <li>
            <span class="evidence-rail__index">03</span><div><b>Target & model facts</b><small>{{ query.data.value.counts.targets_active }} active targets · {{ query.data.value.counts.model_shards_ready }} ready shards</small></div><RouterLink to="/operations/targets">
              Inspect
            </RouterLink>
          </li>
        </ol>
      </section>

      <div class="overview-columns">
        <section class="fact-panel">
          <header>
            <div><p>Detection</p><h2>Recent alerts & abstentions</h2></div><RouterLink to="/detection/events">
              All events
            </RouterLink>
          </header>
          <div
            v-if="query.data.value.recent_alerts.length"
            class="table-scroll"
            role="region"
            aria-label="Recent alerts table"
            tabindex="0"
          >
            <table>
              <thead><tr><th>Event</th><th>Decision</th><th>Quality</th><th>Observed</th></tr></thead><tbody>
                <tr
                  v-for="alert in query.data.value.recent_alerts"
                  :key="alert.event_id"
                >
                  <td class="mono">
                    {{ alert.event_id }}
                  </td><td>
                    <StatusMark
                      :value="alert.decision"
                      compact
                    />
                  </td><td>
                    <StatusMark
                      :value="alert.quality"
                      compact
                    />
                  </td><td>{{ formatTime(alert.event_time_unix_ms) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p
            v-else
            class="empty-note"
          >
            No recent alert or abstention facts. This is not treated as zero risk.
          </p>
        </section>

        <section class="fact-panel">
          <header>
            <div><p>Effects</p><h2>Active operation intents</h2></div><RouterLink to="/governance/operations">
              All operations
            </RouterLink>
          </header>
          <ul
            v-if="query.data.value.active_operations.length"
            class="operation-list"
          >
            <li
              v-for="operation in query.data.value.active_operations"
              :key="operation.effect_intent_id"
            >
              <div><b class="mono">{{ operation.operation_id }}</b><small>{{ operation.effect_kind }} · {{ operation.target_id }}</small></div><StatusMark
                :value="operation.claim_state"
                compact
              />
            </li>
          </ul>
          <p
            v-else
            class="empty-note"
          >
            No active effect intents in the authorized scope.
          </p>
        </section>
      </div>

      <section class="target-strip">
        <header>
          <div><p>Target registry</p><h2>Lifecycle and lease readback</h2></div><RouterLink to="/operations/targets">
            Managed targets
          </RouterLink>
        </header>
        <div class="target-strip__items">
          <article
            v-for="target in query.data.value.target_health"
            :key="target.target_id"
          >
            <div><b>{{ target.display_name }}</b><small class="mono">{{ target.target_id }}</small></div><StatusMark
              :value="target.lifecycle"
              compact
            /><span>assignment {{ target.assignment_generation ?? '—' }}</span><time>{{ target.lease_expires_at_unix_ms ? formatTime(target.lease_expires_at_unix_ms) : 'No active lease' }}</time>
          </article>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.overview { display: grid; gap: var(--space-5); }.snapshot-banner { min-height: 2.75rem; display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-4); padding: var(--space-2) var(--space-3); border-left: 3px solid var(--state-hold); background: var(--surface-panel); color: var(--text-secondary); font-size: var(--text-sm); }.snapshot-banner__reason { margin-left: auto; color: var(--text-muted); }
.metric-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); border: 1px solid var(--border-subtle); background: var(--surface-panel); }.metric { min-width: 0; display: grid; align-content: start; gap: var(--space-1); padding: var(--space-4); border-right: 1px solid var(--border-subtle); color: var(--text-primary); text-decoration: none; }.metric:last-child { border-right: 0; }.metric:hover { background: var(--surface-muted); }.metric__label { color: var(--text-secondary); font-size: var(--text-xs); font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; }.metric strong { font-size: var(--text-xl); font-variant-numeric: tabular-nums; line-height: 1.1; }.metric__detail { min-height: 2.4em; color: var(--text-muted); font-size: var(--text-xs); }
.evidence-rail { display: grid; grid-template-columns: 16rem 1fr; border: 1px solid var(--border-subtle); background: var(--surface-sidebar); color: var(--text-inverse); }.evidence-rail > header { padding: var(--space-5); border-right: 1px solid rgb(255 255 255 / 12%); }.evidence-rail p, .fact-panel header p, .target-strip header p { margin: 0; color: var(--action-primary); font-size: var(--text-xs); font-weight: 750; text-transform: uppercase; letter-spacing: 0.08em; }.evidence-rail h2, .fact-panel h2, .target-strip h2 { margin: var(--space-1) 0 0; font-size: var(--text-lg); }.evidence-rail ol { display: grid; grid-template-columns: repeat(3, 1fr); margin: 0; padding: 0; list-style: none; }.evidence-rail li { display: grid; grid-template-columns: auto 1fr; gap: var(--space-3); padding: var(--space-4); border-right: 1px solid rgb(255 255 255 / 12%); }.evidence-rail li:last-child { border: 0; }.evidence-rail__index { color: #66cae0; font-family: var(--font-mono); }.evidence-rail li div { display: grid; }.evidence-rail small { color: #aebdc7; }.evidence-rail a { grid-column: 2; color: #76d1e5; font-size: var(--text-sm); }
.overview-columns { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(20rem, 0.65fr); gap: var(--space-5); }.fact-panel, .target-strip { min-width: 0; border: 1px solid var(--border-subtle); background: var(--surface-panel); }.fact-panel > header, .target-strip > header { min-height: 4.5rem; display: flex; align-items: center; justify-content: space-between; gap: var(--space-4); padding: var(--space-3) var(--space-4); border-bottom: 1px solid var(--border-subtle); }.fact-panel header a, .target-strip header a { font-size: var(--text-sm); }
.table-scroll { max-width: 100%; overflow-x: auto; }.table-scroll:focus-visible { outline: 2px solid var(--action-focus); outline-offset: -2px; } table { width: 100%; min-width: 32rem; border-collapse: collapse; font-size: var(--text-sm); } th, td { padding: var(--space-2) var(--space-3); border-bottom: 1px solid var(--border-subtle); text-align: left; white-space: nowrap; } th { color: var(--text-muted); font-size: var(--text-xs); }.operation-list { margin: 0; padding: 0; list-style: none; }.operation-list li { min-height: var(--row-height); display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); padding: var(--space-2) var(--space-3); border-bottom: 1px solid var(--border-subtle); }.operation-list li div { min-width: 0; display: grid; }.operation-list small { overflow: hidden; color: var(--text-muted); text-overflow: ellipsis; white-space: nowrap; }.empty-note { padding: var(--space-6); color: var(--text-muted); }
.target-strip__items { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); }.target-strip article { min-width: 0; display: grid; gap: var(--space-2); padding: var(--space-3); border-right: 1px solid var(--border-subtle); }.target-strip article:last-child { border: 0; }.target-strip article div { display: grid; }.target-strip article small, .target-strip article span, .target-strip article time { overflow: hidden; color: var(--text-muted); font-size: var(--text-xs); text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 80rem) { .metric-grid { grid-template-columns: repeat(3, 1fr); }.metric:nth-child(3) { border-right: 0; }.metric:nth-child(-n+3) { border-bottom: 1px solid var(--border-subtle); }.evidence-rail { grid-template-columns: 1fr; }.evidence-rail > header { border-right: 0; border-bottom: 1px solid rgb(255 255 255 / 12%); }.overview-columns { grid-template-columns: 1fr; }.target-strip__items { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 44rem) { .metric-grid { grid-template-columns: repeat(2, 1fr); }.metric:nth-child(3) { border-right: 1px solid var(--border-subtle); }.metric:nth-child(even) { border-right: 0; }.metric:nth-child(-n+4) { border-bottom: 1px solid var(--border-subtle); }.evidence-rail ol { grid-template-columns: 1fr; }.evidence-rail li { border-right: 0; border-bottom: 1px solid rgb(255 255 255 / 12%); }.target-strip__items { grid-template-columns: 1fr; }.snapshot-banner__reason { width: 100%; margin-left: 0; } }
</style>
