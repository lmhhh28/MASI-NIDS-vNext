<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { ElButton } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { fetchResource } from '@/api/resources'
import { asRecord } from '@/api/guards'
import { formatNumber, formatTime } from '@/format'
import PageHeading from '@/components/PageHeading.vue'
import StatePanel from '@/components/StatePanel.vue'
import StatusMark from '@/components/StatusMark.vue'

const route = useRoute()
const router = useRouter()
const cursor = computed(() => typeof route.query.cursor === 'string' ? route.query.cursor : '')
const query = useQuery({
  queryKey: computed(() => ['resource', 'rule-effectiveness', cursor.value, 50]),
  queryFn: () => fetchResource('rule-effectiveness', cursor.value, 50),
})

function nested(item: Record<string, unknown>, field: string): Record<string, unknown> {
  try { return asRecord(item[field]) } catch { return {} }
}

function readback(item: Record<string, unknown>): Record<string, unknown> {
  return nested(nested(item, 'installation'), 'readback')
}

function ratio(item: Record<string, unknown>): string {
  const dataplane = nested(item, 'dataplane')
  const quality = typeof dataplane.quality === 'string' ? dataplane.quality : ''
  const eligible = dataplane.eligible_packets
  const value = dataplane.packet_match_ratio
  if (quality === 'no-eligible-traffic') return 'No eligible traffic; no ratio computed'
  if (['not-covered', 'not-measurable', 'invalid', 'reset', 'gap'].includes(quality)) return 'Ratio held by quality state'
  if (typeof eligible !== 'number' || eligible <= 0 || typeof value !== 'number') return 'Not measurable'
  return `${(value * 100).toFixed(2)}% of eligible packets matched`
}

function qualityReasons(item: Record<string, unknown>): string {
  const value = nested(item, 'dataplane').quality_reasons
  if (!Array.isArray(value)) return 'Not reported'
  const reasons = value.filter((reason): reason is string => typeof reason === 'string')
  return reasons.join(', ') || 'NONE'
}

function firstPage(): void { void router.push({ query: {} }) }
function nextPage(): void {
  const next = query.data.value?.cursor
  if (next) void router.push({ query: { cursor: next } })
}
</script>

<template>
  <div class="rules">
    <PageHeading
      eyebrow="Effects & governance"
      title="Rule effectiveness"
      description="Three independent evidence layers. Installation is not a match; a match is not an outcome."
    >
      <ElButton
        :icon="Refresh"
        :loading="query.isFetching.value"
        @click="query.refetch()"
      >
        Refresh
      </ElButton>
    </PageHeading>

    <aside
      class="interpretation"
      role="note"
    >
      <strong>Interpretation rule</strong>
      Counter growth only proves dataplane match at the qualified observation point. It never proves the configured action succeeded or the attack stopped.
    </aside>

    <StatePanel
      v-if="query.isPending.value"
      state="loading"
      detail="Loading bounded rule evidence."
    />
    <StatePanel
      v-else-if="query.isError.value"
      state="error"
      title="Rule evidence unavailable"
      :detail="query.error.value instanceof Error ? query.error.value.message : 'The projection failed closed.'"
      retryable
      @retry="query.refetch()"
    />
    <StatePanel
      v-else-if="query.data.value?.items.length === 0"
      state="empty"
      detail="No qualified rule observation epoch is present. This is not a 0% effectiveness result."
    />
    <template v-else-if="query.data.value">
      <article
        v-for="item in query.data.value.items"
        :key="String(item.epoch_id)"
        class="rule"
      >
        <header>
          <div>
            <p class="rule__eyebrow">
              Rule observation epoch
            </p>
            <h2 class="mono">
              {{ item.rule_id }}
            </h2>
          </div>
          <div class="rule__identity">
            <StatusMark :value="item.quality_status" />
            <span class="mono">{{ item.target_id }}</span>
            <span class="mono">{{ item.epoch_id }}</span>
          </div>
        </header>
        <ol class="stages">
          <li>
            <span class="stage__number">1</span>
            <div class="stage__body">
              <p>Exact installation readback</p>
              <StatusMark :value="nested(item, 'installation').status" />
              <dl>
                <div><dt>Observation epoch</dt><dd>{{ formatNumber(nested(item, 'installation').observation_epoch) }}</dd></div>
                <div><dt>Reset epoch</dt><dd>{{ formatNumber(nested(item, 'installation').reset_epoch) }}</dd></div>
                <div>
                  <dt>Operation result</dt><dd>
                    <StatusMark
                      :value="readback(item).status"
                      compact
                    />
                  </dd>
                </div>
                <div><dt>Entries</dt><dd>{{ formatNumber(readback(item).observed_entries) }} / {{ formatNumber(readback(item).expected_entries) }}</dd></div>
                <div><dt>Mismatches</dt><dd>{{ formatNumber(readback(item).mismatched_entries) }}</dd></div>
                <div><dt>Active bank</dt><dd>{{ formatNumber(readback(item).active_bank) }}</dd></div>
              </dl>
            </div>
          </li>
          <li>
            <span class="stage__number">2</span>
            <div class="stage__body">
              <p>Dataplane match</p>
              <StatusMark :value="nested(item, 'dataplane').quality" />
              <strong class="stage__result">{{ ratio(item) }}</strong>
              <dl>
                <div><dt>Direct packets</dt><dd>{{ formatNumber(nested(item, 'dataplane').direct_packets) }}</dd></div>
                <div><dt>Direct bytes</dt><dd>{{ formatNumber(nested(item, 'dataplane').direct_bytes) }}</dd></div>
                <div><dt>Eligible packets</dt><dd>{{ formatNumber(nested(item, 'dataplane').eligible_packets) }}</dd></div>
                <div><dt>Coverage</dt><dd>{{ typeof nested(item, 'dataplane').coverage === 'number' ? `${(Number(nested(item, 'dataplane').coverage) * 100).toFixed(1)}%` : 'Not reported' }}</dd></div>
                <div>
                  <dt>Formula</dt><dd class="mono">
                    direct_delta / eligible_delta
                  </dd>
                </div>
                <div><dt>Read completed</dt><dd>{{ formatTime(nested(item, 'dataplane').read_completed_at_unix_ms) }}</dd></div>
              </dl>
              <p class="stage__reasons">
                Quality reasons: {{ qualityReasons(item) }}
              </p>
            </div>
          </li>
          <li>
            <span class="stage__number">3</span>
            <div class="stage__body">
              <p>Independent packet/action outcome</p>
              <StatusMark :value="nested(item, 'outcome').status" />
              <dl>
                <div>
                  <dt>Expected</dt><dd>
                    <StatusMark
                      :value="nested(item, 'outcome').expected"
                      compact
                    />
                  </dd>
                </div>
                <div>
                  <dt>Observed</dt><dd>
                    <StatusMark
                      :value="nested(item, 'outcome').actual"
                      compact
                    />
                  </dd>
                </div>
              </dl>
              <p class="stage__reasons">
                This result is written by the independent packet oracle, never inferred from the direct counter.
              </p>
            </div>
          </li>
        </ol>
      </article>
      <nav
        class="pagination"
        aria-label="Rule evidence pages"
      >
        <ElButton
          :disabled="cursor === ''"
          @click="firstPage"
        >
          First page
        </ElButton>
        <span>{{ query.data.value.total_count }} observation epochs in scope</span>
        <ElButton
          :disabled="query.data.value.cursor === ''"
          @click="nextPage"
        >
          Next page
        </ElButton>
      </nav>
    </template>
  </div>
</template>

<style scoped>
.rules { display: grid; gap: var(--space-5); }.interpretation { padding: var(--space-3) var(--space-4); border-left: 3px solid var(--action-primary); background: var(--surface-panel); color: var(--text-secondary); }.interpretation strong { display: block; color: var(--text-primary); }
.rule { border: 1px solid var(--border-subtle); background: var(--surface-panel); box-shadow: var(--shadow-panel); }.rule > header { display: flex; justify-content: space-between; gap: var(--space-4); padding: var(--space-3) var(--space-4); border-bottom: 1px solid var(--border-subtle); }.rule__eyebrow { margin: 0; color: var(--text-muted); font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .07em; }.rule h2 { margin: var(--space-1) 0 0; font-size: var(--text-md); }.rule__identity { display: flex; align-items: center; flex-wrap: wrap; justify-content: flex-end; gap: var(--space-3); color: var(--text-muted); font-size: var(--text-xs); }
.stages { display: grid; grid-template-columns: repeat(3, 1fr); margin: 0; padding: 0; list-style: none; }.stages > li { position: relative; display: grid; grid-template-columns: 2rem 1fr; gap: var(--space-3); padding: var(--space-4); border-right: 1px solid var(--border-subtle); }.stages > li:last-child { border: 0; }.stage__number { width: 1.75rem; height: 1.75rem; display: grid; place-items: center; border: 1px solid var(--action-primary); border-radius: 50%; color: var(--action-primary); font-family: var(--font-mono); font-weight: 700; }.stage__body { min-width: 0; display: grid; align-content: start; gap: var(--space-3); }.stage__body > p:first-child { margin: 0; font-size: var(--text-xs); font-weight: 750; text-transform: uppercase; letter-spacing: .06em; }.stage__result { color: var(--text-primary); }.stage__body dl { margin: 0; display: grid; }.stage__body dl div { display: flex; justify-content: space-between; gap: var(--space-3); padding: var(--space-1) 0; border-bottom: 1px solid var(--border-subtle); }.stage__body dt { color: var(--text-muted); }.stage__body dd { margin: 0; text-align: right; }.stage__reasons { margin: 0; color: var(--text-muted); font-size: var(--text-xs); }.pagination { display: flex; justify-content: space-between; align-items: center; gap: var(--space-3); color: var(--text-muted); font-size: var(--text-sm); }
@media (max-width: 70rem) { .stages { grid-template-columns: 1fr; }.stages > li { border-right: 0; border-bottom: 1px solid var(--border-subtle); } }
@media (max-width: 40rem) { .rule > header { flex-direction: column; }.rule__identity { justify-content: flex-start; }.pagination span { display: none; } }
</style>
