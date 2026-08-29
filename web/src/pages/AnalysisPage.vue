<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { LocationQueryRaw } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { ElButton } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import {
  fetchResource,
  fetchResourceDetail,
  resourceDefinitions,
  supportsResourceDetail,
  type ResourceKey,
} from '@/api/resources'
import PageHeading from '@/components/PageHeading.vue'
import ResourceTable from '@/components/ResourceTable.vue'
import StatePanel from '@/components/StatePanel.vue'
import FactDrawer from '@/components/FactDrawer.vue'
import StatusMark from '@/components/StatusMark.vue'
import { sessionBoundKey } from '@/api/context'

type AnalysisTab = 'analysis-tasks' | 'analysis-artifacts'
const route = useRoute()
const router = useRouter()
function routeTab(value: unknown): AnalysisTab {
  return value === 'analysis-artifacts' ? 'analysis-artifacts' : 'analysis-tasks'
}
const tab = ref<AnalysisTab>(routeTab(route.query.tab))
const queries = {
  'analysis-tasks': useQuery({ queryKey: sessionBoundKey('resource', 'analysis-tasks', '', 50), queryFn: () => fetchResource('analysis-tasks') }),
  'analysis-artifacts': useQuery({ queryKey: sessionBoundKey('resource', 'analysis-artifacts', '', 50), queryFn: () => fetchResource('analysis-artifacts') }),
}
const query = computed(() => queries[tab.value])
const definition = computed(() => resourceDefinitions[tab.value as ResourceKey])
const selectedID = computed(() => typeof route.query.fact === 'string' ? route.query.fact : '')
const selectedFromPage = computed(() => query.value.data.value?.items.find((item) => {
  const identity = item[definition.value.identityField]
  return typeof identity === 'string' && identity === selectedID.value
}) ?? null)
const detail = useQuery({
  queryKey: sessionBoundKey('resource-detail', tab, selectedID),
  queryFn: () => fetchResourceDetail(tab.value, selectedID.value),
  enabled: computed(() =>
    selectedID.value !== ''
    && query.value.isSuccess.value
    && selectedFromPage.value === null
    && supportsResourceDetail(tab.value),
  ),
})
const selected = computed(() => selectedFromPage.value ?? detail.data.value ?? null)
const drawerOpen = computed({
  get: () => selectedID.value !== '',
  set: (open: boolean) => {
    if (!open) {
      const next = { ...route.query }
      delete next.fact
      void router.replace({ query: next })
    }
  },
})
const detailLoading = computed(() =>
  selectedID.value !== ''
  && selectedFromPage.value === null
  && supportsResourceDetail(tab.value)
  && detail.isFetching.value,
)
const detailError = computed(() => {
  if (!selectedID.value || selected.value) return ''
  if (!supportsResourceDetail(tab.value)) {
    return 'Analysis tasks have no read-only detail endpoint; select a task from the loaded bounded page.'
  }
  return detail.isError.value
    ? (detail.error.value instanceof Error ? detail.error.value.message : 'The exact Analysis artifact failed closed.')
    : ''
})
watch(() => route.query.tab, (value) => { tab.value = routeTab(value) })

function inspect(item: Record<string, unknown>): void {
  const identity = item[definition.value.identityField]
  if (typeof identity === 'string' && identity !== '') {
    void router.push({ query: { ...route.query, tab: tab.value, fact: identity } })
  }
}
function selectTab(value: AnalysisTab): void {
  tab.value = value
  const next: LocationQueryRaw = { ...route.query, tab: value }
  delete next.fact
  void router.push({ query: next })
}
function refresh(): void { void queries['analysis-tasks'].refetch(); void queries['analysis-artifacts'].refetch() }
</script>

<template>
  <div class="analysis-page">
    <PageHeading
      eyebrow="Analysis"
      title="Evidence analysis"
      description="The official Analysis Agent can explain and recommend; every output remains non-executable and outside the realtime/effect loop."
    >
      <ElButton
        :icon="Refresh"
        @click="refresh"
      >
        Refresh
      </ElButton>
    </PageHeading>
    <aside
      class="analysis-boundary"
      role="note"
    >
      <StatusMark value="non-executable" /><div><strong>Agent boundary</strong> LangGraph, LLM, MCP, and A2A exist only here. An artifact can never become an effect intent, deployment, model binding, or device mutation.</div>
    </aside>
    <nav
      class="tabs"
      aria-label="Analysis views"
    >
      <button
        type="button"
        :aria-current="tab === 'analysis-tasks' ? 'page' : undefined"
        @click="selectTab('analysis-tasks')"
      >
        Tasks
      </button><button
        type="button"
        :aria-current="tab === 'analysis-artifacts' ? 'page' : undefined"
        @click="selectTab('analysis-artifacts')"
      >
        Artifacts
      </button>
    </nav>
    <StatePanel
      v-if="query.isPending.value"
      state="loading"
      detail="Loading bounded Analysis facts."
    />
    <StatePanel
      v-else-if="query.isError.value"
      state="error"
      title="Analysis unavailable"
      detail="Core detection, effects, targets, and native statistics remain available."
      retryable
      @retry="query.refetch()"
    />
    <StatePanel
      v-else-if="query.data.value?.items.length === 0"
      state="empty"
      detail="No Analysis facts in this authorized scope."
    />
    <ResourceTable
      v-else-if="query.data.value"
      :columns="definition.columns"
      :items="query.data.value.items"
      :identity-field="definition.identityField"
      @select="inspect"
    />
    <FactDrawer
      v-model:open="drawerOpen"
      :title="selected ? String(selected[definition.identityField] ?? selectedID) : (selectedID || 'Analysis fact')"
      :item="selected"
      :loading="detailLoading"
      :error="detailError"
    />
  </div>
</template>

<style scoped>
.analysis-page { display: grid; gap: var(--space-5); }.analysis-boundary { display: flex; align-items: center; gap: var(--space-3); padding: var(--space-3) var(--space-4); border-left: 3px solid var(--state-hold); background: var(--state-hold-surface); color: var(--text-secondary); }.analysis-boundary strong { display: block; color: var(--text-primary); }.tabs { display: flex; border-bottom: 1px solid var(--border-strong); }.tabs button { min-height: 2.75rem; padding: 0 var(--space-4); border: 0; border-bottom: 3px solid transparent; background: transparent; color: var(--text-secondary); font-weight: 700; cursor: pointer; }.tabs button[aria-current='page'] { border-bottom-color: var(--action-primary); color: var(--text-primary); }
</style>
