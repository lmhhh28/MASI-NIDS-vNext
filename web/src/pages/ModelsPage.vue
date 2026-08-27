<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { ElAlert, ElButton, ElDialog } from 'element-plus'
import { Plus, Promotion, Refresh } from '@element-plus/icons-vue'
import { fetchResource, resourceDefinitions, type ResourceColumn, type ResourceKey } from '@/api/resources'
import { submitRollout, submitRolloutAdvance } from '@/api/models'
import { useSessionQuery } from '@/api/session'
import { useUiStore } from '@/stores/ui'
import PageHeading from '@/components/PageHeading.vue'
import ResourceTable from '@/components/ResourceTable.vue'
import StatePanel from '@/components/StatePanel.vue'
import StatusMark from '@/components/StatusMark.vue'
import FactDrawer from '@/components/FactDrawer.vue'

type ModelTab = 'model-bindings' | 'model-rollouts' | 'model-pools' | 'model-revisions'
const tabs: Array<{ key: ModelTab; label: string }> = [
  { key: 'model-bindings', label: 'Shard bindings' },
  { key: 'model-rollouts', label: 'Rollout groups' },
  { key: 'model-pools', label: 'Inference pools' },
  { key: 'model-revisions', label: 'Revisions' },
]
const tab = ref<ModelTab>('model-bindings')
const queries = {
  'model-bindings': useQuery({ queryKey: ['resource', 'model-bindings', '', 50], queryFn: () => fetchResource('model-bindings') }),
  'model-rollouts': useQuery({ queryKey: ['resource', 'model-rollouts', '', 50], queryFn: () => fetchResource('model-rollouts') }),
  'model-pools': useQuery({ queryKey: ['resource', 'model-pools', '', 50], queryFn: () => fetchResource('model-pools') }),
  'model-revisions': useQuery({ queryKey: ['resource', 'model-revisions', '', 50], queryFn: () => fetchResource('model-revisions') }),
}
const activeQuery = computed(() => queries[tab.value])
const definition = computed(() => resourceDefinitions[tab.value as ResourceKey])
const bindingExtraColumns: ResourceColumn[] = [
  { field: 'previous_revision_id', label: 'Previous', kind: 'identity' },
  { field: 'loaded', label: 'Loaded', kind: 'status' },
  { field: 'ready', label: 'Ready', kind: 'status' },
]
const columns = computed(() => tab.value === 'model-bindings' ? [...definition.value.columns, ...bindingExtraColumns] : definition.value.columns)
const selected = ref<Record<string, unknown> | null>(null)
const drawerOpen = ref(false)
const dialogOpen = ref(false)
const dialogKind = ref<'create' | 'advance'>('create')
const errorMessage = ref('')
const successMessage = ref('')
const session = useSessionQuery()
const queryClient = useQueryClient()
const ui = useUiStore()

const form = reactive({
  groupID: `rollout-${crypto.randomUUID()}`, orderedShards: '', logicalPoolID: '',
  targetGeneration: 1, targetRevisionID: '', scope: '', wireProfileDigest: '',
  runtimeProfileDigest: '', optimizationProfileDigest: '',
  availabilityProfile: 'availability-single/v1',
  minReadyReplicas: 1, incarnationID: '',
})
const digestPattern = /^sha256:[0-9a-f]{64}$/
const shards = computed(() => [...new Set(form.orderedShards.split(',').map((value) => value.trim()).filter(Boolean))])

function validationError(): string {
  if (!form.scope || shards.value.length < 1 || shards.value.length > 256) return 'Scope and between 1 and 256 ordered shard identities are required.'
  if (dialogKind.value === 'advance') return ''
  if (!form.groupID || !form.logicalPoolID || !form.targetRevisionID || !form.incarnationID) return 'Group, pool, revision, and model-control incarnation identities are required.'
  if (![form.wireProfileDigest, form.runtimeProfileDigest, form.optimizationProfileDigest].every((value) => digestPattern.test(value))) return 'Wire, selected runtime, and optimization profiles require exact sha256 digests.'
  if (!Number.isSafeInteger(form.targetGeneration) || form.targetGeneration < 1 || !Number.isSafeInteger(form.minReadyReplicas) || form.minReadyReplicas < 1) return 'Generation and minimum ready replicas must be positive integers.'
  return ''
}

const mutation = useMutation({
  mutationFn: async () => {
    const activeSession = session.data.value
    if (!activeSession) throw new Error('SESSION_UNAVAILABLE')
    const validation = validationError()
    if (validation) throw new Error(validation)
    if (dialogKind.value === 'advance') return submitRolloutAdvance(form.groupID, form.scope, shards.value, activeSession)
    return submitRollout({
      groupID: form.groupID, orderedShards: shards.value, logicalPoolID: form.logicalPoolID,
      targetGeneration: form.targetGeneration, targetRevisionID: form.targetRevisionID,
      scope: form.scope, wireProfileDigest: form.wireProfileDigest,
      runtimeProfileDigest: form.runtimeProfileDigest,
      optimizationProfileDigest: form.optimizationProfileDigest,
      availabilityProfile: form.availabilityProfile === 'availability-ha/v1' ? 'availability-ha/v1' : 'availability-single/v1',
      minReadyReplicas: form.minReadyReplicas, modelControlIncarnationID: form.incarnationID,
    }, activeSession)
  },
  onSuccess: () => {
    successMessage.value = dialogKind.value === 'advance'
      ? 'One shard step was advanced. Mixed/current state remains per-shard and requires readback.'
      : 'The ordered rollout group was frozen. No replica or route is current until staged readback and commit complete.'
    errorMessage.value = ''; dialogOpen.value = false
    void queryClient.invalidateQueries({ queryKey: ['resource'] }); void queryClient.invalidateQueries({ queryKey: ['dashboard'] })
  },
  onError: (error) => { errorMessage.value = error instanceof Error ? error.message : 'Model operation failed closed.' },
})

function inspect(item: Record<string, unknown>): void { selected.value = item; drawerOpen.value = true }
function createRollout(): void { dialogKind.value = 'create'; form.groupID = `rollout-${crypto.randomUUID()}`; errorMessage.value = ''; dialogOpen.value = true }
function advanceRollout(item: Record<string, unknown>): void {
  dialogKind.value = 'advance'
  form.groupID = typeof item.group_id === 'string' ? item.group_id : ''
  form.orderedShards = Array.isArray(item.ordered_shards) ? item.ordered_shards.filter((value): value is string => typeof value === 'string').join(', ') : ''
  errorMessage.value = ''; dialogOpen.value = true
}
function refreshAll(): void { for (const query of Object.values(queries)) void query.refetch() }
</script>

<template>
  <div class="models-page">
    <ElAlert
      v-if="successMessage"
      type="success"
      show-icon
      :title="successMessage"
      @close="successMessage = ''"
    />
    <ElAlert
      v-if="errorMessage && !dialogOpen"
      type="error"
      show-icon
      :title="errorMessage"
      @close="errorMessage = ''"
    />
    <PageHeading
      eyebrow="Operations & audit"
      title="Model operations"
      description="Desired, selected, observed, current, and previous identities remain distinct across every shard."
    >
      <ElButton
        type="primary"
        :icon="Plus"
        :disabled="!ui.dangerousViewportSupported"
        @click="createRollout"
      >
        New rollout
      </ElButton>
      <ElButton
        :icon="Refresh"
        @click="refreshAll"
      >
        Refresh all
      </ElButton>
    </PageHeading>
    <aside
      class="model-rule"
      role="note"
    >
      <strong>No automatic fallback</strong> CPU/CUDA runtime profile and availability profile are explicit, digest-bound choices. Loaded or Ready never becomes current without route withdrawal, PostgreSQL CAS, and exact commit handshake.
    </aside>
    <nav
      class="tabs"
      aria-label="Model operation views"
    >
      <button
        v-for="entry in tabs"
        :key="entry.key"
        type="button"
        :aria-current="tab === entry.key ? 'page' : undefined"
        @click="tab = entry.key"
      >
        {{ entry.label }}
      </button>
    </nav>
    <div
      v-if="activeQuery.data.value"
      class="projection-strip"
    >
      <StatusMark :value="activeQuery.data.value.projection_type" /><span>{{ activeQuery.data.value.total_count }} facts</span><span>generation {{ activeQuery.data.value.generation }}</span>
    </div>
    <StatePanel
      v-if="activeQuery.isPending.value"
      state="loading"
      detail="Loading bounded model-control facts."
    />
    <StatePanel
      v-else-if="activeQuery.isError.value"
      state="error"
      title="Model projection unavailable"
      :detail="activeQuery.error.value instanceof Error ? activeQuery.error.value.message : 'Projection failed closed.'"
      retryable
      @retry="activeQuery.refetch()"
    />
    <StatePanel
      v-else-if="activeQuery.data.value?.items.length === 0"
      state="empty"
      detail="No facts are present for this model-control view."
    />
    <ResourceTable
      v-else-if="activeQuery.data.value"
      :columns="columns"
      :items="activeQuery.data.value.items"
      :identity-field="definition.identityField"
      @select="inspect"
    >
      <template #row-actions="{ item }">
        <button
          v-if="tab === 'model-rollouts'"
          type="button"
          :disabled="!ui.dangerousViewportSupported"
          @click="advanceRollout(item)"
        >
          Advance one shard
        </button>
      </template>
    </ResourceTable>
    <aside
      v-if="!ui.dangerousViewportSupported"
      class="viewport-hold"
    >
      <StatusMark value="hold" /> Model mutations require a viewport at least 1024 CSS pixels wide.
    </aside>
    <FactDrawer
      v-model:open="drawerOpen"
      :title="selected ? String(selected[definition.identityField] ?? 'Model fact') : 'Model fact'"
      :item="selected"
    />

    <ElDialog
      v-model="dialogOpen"
      :title="dialogKind === 'create' ? 'Freeze ordered rollout group' : 'Advance one rollout shard'"
      width="min(48rem, 94vw)"
      destroy-on-close
      align-center
    >
      <ElAlert
        v-if="errorMessage"
        class="dialog-alert"
        type="error"
        :closable="false"
        show-icon
        :title="errorMessage"
      />
      <form
        class="model-form"
        @submit.prevent="mutation.mutate()"
      >
        <label><span>Rollout group</span><input
          v-model="form.groupID"
          class="mono"
          :readonly="dialogKind === 'advance'"
          required
        ></label>
        <label><span>Authorization scope</span><input
          v-model="form.scope"
          required
          autocomplete="off"
        ></label>
        <label class="wide"><span>Ordered shard IDs <small>comma-separated; order is frozen</small></span><input
          v-model="form.orderedShards"
          class="mono"
          required
          autocomplete="off"
        ></label>
        <template v-if="dialogKind === 'create'">
          <label><span>Logical pool</span><input
            v-model="form.logicalPoolID"
            required
            autocomplete="off"
          ></label>
          <label><span>Target generation</span><input
            v-model.number="form.targetGeneration"
            type="number"
            min="1"
            required
          ></label>
          <label><span>Target model revision</span><input
            v-model="form.targetRevisionID"
            required
            autocomplete="off"
          ></label>
          <label><span>Model-control incarnation</span><input
            v-model="form.incarnationID"
            required
            autocomplete="off"
          ></label>
          <label class="wide"><span>Central wire profile digest · inference-central-grpc-batch/v1</span><input
            v-model="form.wireProfileDigest"
            class="mono"
            pattern="sha256:[0-9a-f]{64}"
            required
            autocomplete="off"
          ></label>
          <label class="wide"><span>Selected CPU/CUDA runtime profile digest</span><input
            v-model="form.runtimeProfileDigest"
            class="mono"
            pattern="sha256:[0-9a-f]{64}"
            required
            autocomplete="off"
          ></label>
          <label class="wide"><span>Optimization profile digest</span><input
            v-model="form.optimizationProfileDigest"
            class="mono"
            pattern="sha256:[0-9a-f]{64}"
            required
            autocomplete="off"
          ></label>
          <label><span>Availability profile</span><select v-model="form.availabilityProfile"><option value="availability-single/v1">Single failure domain</option><option value="availability-ha/v1">HA · separately qualified</option></select></label>
          <label><span>Minimum ready replicas</span><input
            v-model.number="form.minReadyReplicas"
            type="number"
            min="1"
            max="1024"
            required
          ></label>
        </template>
        <p class="wide caution">
          {{ dialogKind === 'advance' ? 'Only the next ordered shard is eligible. Observe the per-shard stage and mixed state after this request.' : 'Production forbids Edge-local inference, old-model fallback, and automatic CPU/CUDA switching.' }}
        </p>
      </form>
      <template #footer>
        <ElButton @click="dialogOpen = false">
          Cancel
        </ElButton><ElButton
          type="primary"
          :icon="Promotion"
          :loading="mutation.isPending.value"
          @click="mutation.mutate()"
        >
          {{ dialogKind === 'create' ? 'Freeze rollout group' : 'Advance one shard' }}
        </ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.models-page { display: grid; gap: var(--space-5); }.model-rule { padding: var(--space-3) var(--space-4); border-left: 3px solid var(--state-hold); background: var(--state-hold-surface); color: var(--text-secondary); }.model-rule strong { display: block; color: var(--text-primary); }.tabs { display: flex; overflow-x: auto; border-bottom: 1px solid var(--border-strong); }.tabs button { min-height: 2.75rem; padding: 0 var(--space-4); border: 0; border-bottom: 3px solid transparent; background: transparent; color: var(--text-secondary); font-weight: 700; cursor: pointer; }.tabs button[aria-current='page'] { border-bottom-color: var(--action-primary); color: var(--text-primary); }.projection-strip { display: flex; align-items: center; gap: var(--space-4); color: var(--text-muted); font-size: var(--text-sm); }.viewport-hold { display: flex; align-items: center; gap: var(--space-3); padding: var(--space-3); border: 1px solid var(--state-hold); background: var(--state-hold-surface); }.dialog-alert { margin-bottom: var(--space-4); }.model-form { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-4); }.model-form label { display: grid; gap: var(--space-1); color: var(--text-secondary); font-size: var(--text-sm); }.model-form label > span { font-weight: 700; }.model-form small { color: var(--text-muted); font-weight: 400; }.model-form input, .model-form select { min-height: 2.6rem; padding: var(--space-2) var(--space-3); border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: var(--surface-panel); color: var(--text-primary); }.wide { grid-column: 1 / -1; }.caution { margin: 0; padding: var(--space-3); border-left: 3px solid var(--state-warning); background: var(--state-warning-surface); }
@media (max-width: 44rem) { .model-form { grid-template-columns: 1fr; }.wide { grid-column: 1; } }
</style>
