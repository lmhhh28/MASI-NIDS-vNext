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
import StatusMark from '@/components/StatusMark.vue'
import FactDrawer from '@/components/FactDrawer.vue'
import { sessionBoundKey } from '@/api/context'

const route = useRoute()
const router = useRouter()
const initialPageSize = Number(route.query.page_size ?? 50)
const pageSize = ref([50, 100, 200].includes(initialPageSize) ? initialPageSize : 50)
const cursor = computed(() => (typeof route.query.cursor === 'string' ? route.query.cursor : ''))
const resourceKey = computed(() => route.meta.resource as ResourceKey)
const definition = computed(() => resourceDefinitions[resourceKey.value])
const selectedID = computed(() => (typeof route.query.fact === 'string' ? route.query.fact : ''))

const query = useQuery({
  queryKey: sessionBoundKey('resource', resourceKey, cursor, pageSize),
  queryFn: () => fetchResource(resourceKey.value, cursor.value, pageSize.value),
})

const selectedFromPage = computed(() => query.data.value?.items.find((item) => {
  const identity = item[definition.value.identityField]
  return typeof identity === 'string' && identity === selectedID.value
}) ?? null)
const detail = useQuery({
  queryKey: sessionBoundKey('resource-detail', resourceKey, selectedID),
  queryFn: () => fetchResourceDetail(resourceKey.value, selectedID.value),
  enabled: computed(() =>
    selectedID.value !== ''
    && query.isSuccess.value
    && selectedFromPage.value === null
    && supportsResourceDetail(resourceKey.value),
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
const detailError = computed(() => {
  if (!selectedID.value || selected.value) return ''
  if (!supportsResourceDetail(resourceKey.value)) {
    return 'This list has no exact detail endpoint; open a fact from the currently loaded bounded page.'
  }
  return detail.isError.value
    ? (detail.error.value instanceof Error ? detail.error.value.message : 'The exact detail request failed closed.')
    : ''
})
const detailLoading = computed(() =>
  selectedID.value !== ''
  && selectedFromPage.value === null
  && supportsResourceDetail(resourceKey.value)
  && detail.isFetching.value,
)

watch(pageSize, (value) => {
  const next: LocationQueryRaw = { ...route.query, page_size: String(value) }
  delete next.cursor
  void router.replace({ query: next })
})

function inspect(item: Record<string, unknown>): void {
  const identity = item[definition.value.identityField]
  if (typeof identity !== 'string' || identity === '') return
  void router.push({ query: { ...route.query, fact: identity } })
}

function nextPage(): void {
  const next = query.data.value?.cursor
  if (next) void router.push({ query: { page_size: String(pageSize.value), cursor: next } })
}

function firstPage(): void {
  void router.push({ query: { page_size: String(pageSize.value) } })
}
</script>

<template>
  <div class="resource-page">
    <PageHeading
      :eyebrow="definition.eyebrow"
      :title="definition.title"
      :description="definition.description"
    >
      <slot name="actions" />
      <label class="page-size"><span class="sr-only">Rows per page</span><select v-model.number="pageSize"><option :value="50">50 rows</option><option :value="100">100 rows</option><option :value="200">200 rows</option></select></label>
      <ElButton
        :icon="Refresh"
        :loading="query.isFetching.value"
        @click="query.refetch()"
      >
        Refresh
      </ElButton>
    </PageHeading>

    <div
      v-if="query.data.value"
      class="projection-strip"
    >
      <StatusMark :value="query.data.value.projection_type" />
      <span><strong>{{ query.data.value.total_count.toLocaleString() }}</strong> authorized facts</span>
      <span>Generation <b class="mono">{{ query.data.value.generation }}</b></span>
      <span class="projection-strip__scope">Scope <b>{{ query.data.value.authorized_scope }}</b></span>
    </div>

    <StatePanel
      v-if="query.isPending.value"
      state="loading"
      detail="Loading the bounded server projection."
    />
    <StatePanel
      v-else-if="query.isError.value"
      state="error"
      title="Projection unavailable"
      :detail="query.error.value instanceof Error ? query.error.value.message : 'The request failed closed.'"
      retryable
      @retry="query.refetch()"
    />
    <StatePanel
      v-else-if="query.data.value?.items.length === 0"
      state="empty"
      detail="The server returned an empty, authorized projection. No zero value has been inferred."
    />
    <template v-else-if="query.data.value">
      <ResourceTable
        :columns="definition.columns"
        :items="query.data.value.items"
        :identity-field="definition.identityField"
        @select="inspect"
      >
        <template #row-actions="{ item }">
          <slot
            name="row-actions"
            :item="item"
          />
        </template>
      </ResourceTable>
      <nav
        class="pagination"
        aria-label="Projection pages"
      >
        <ElButton
          :disabled="cursor === ''"
          @click="firstPage"
        >
          First page
        </ElButton>
        <span>Showing up to {{ pageSize }} rows</span>
        <ElButton
          :disabled="query.data.value.cursor === ''"
          @click="nextPage"
        >
          Next page
        </ElButton>
      </nav>
    </template>

    <FactDrawer
      v-model:open="drawerOpen"
      :title="selected ? String(selected[definition.identityField] ?? selectedID) : (selectedID || 'Fact details')"
      :item="selected"
      :loading="detailLoading"
      :error="detailError"
    />
  </div>
</template>

<style scoped>
.resource-page { display: grid; gap: var(--space-5); }.page-size select { min-height: 2.5rem; padding: 0 var(--space-3); border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: var(--surface-panel); color: var(--text-primary); }.projection-strip { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-4); min-height: 2.5rem; color: var(--text-secondary); font-size: var(--text-sm); }.projection-strip__scope { margin-left: auto; max-width: 30rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.pagination { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); color: var(--text-secondary); font-size: var(--text-sm); }
@media (max-width: 40rem) { .projection-strip__scope { width: 100%; margin-left: 0; }.pagination span { display: none; } }
</style>
