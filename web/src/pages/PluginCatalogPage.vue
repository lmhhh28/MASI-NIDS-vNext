<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useMutation, useQueryClient } from '@tanstack/vue-query'
import { ElAlert, ElButton, ElDialog } from 'element-plus'
import { SetUp } from '@element-plus/icons-vue'
import ResourcePage from './ResourcePage.vue'
import StatusMark from '@/components/StatusMark.vue'
import { useSessionQuery } from '@/api/session'
import { submitPluginActivation, submitPluginLifecycle, submitPluginQualification, type PluginLifecycleAction } from '@/api/plugins'
import { useUiStore } from '@/stores/ui'

type Action = 'qualify' | 'activate' | PluginLifecycleAction
const action = ref<Action>('qualify')
const dialogOpen = ref(false)
const errorMessage = ref('')
const successMessage = ref('')
const session = useSessionQuery()
const queryClient = useQueryClient()
const ui = useUiStore()
const form = reactive({
  pluginID: '', manifestID: '', manifestRevision: 1, manifestDigest: '',
  qualificationStatus: 'qualified',
  bindingGeneration: 1, configDigest: '', capabilityDigest: '', resourceProfileDigest: '',
  scope: '', targetSetDigest: '', previousBindingGeneration: 1,
})
const digestPattern = /^sha256:(?!0{64}$)[0-9a-f]{64}$/
const title = computed(() => ({ qualify: 'Record qualification', activate: 'Activate exact binding', drain: 'Drain active binding', revoke: 'Revoke active binding', rollback: 'Create rollback binding' })[action.value])

function validationError(): string {
  if (!form.pluginID || !form.scope || !digestPattern.test(form.targetSetDigest)) return 'Plugin, scope, and exact target-set digest are required.'
  if (action.value === 'qualify' && (!form.manifestID || form.manifestRevision < 1)) return 'Exact manifest identity and revision are required.'
  if (action.value === 'activate') {
    if (!form.manifestID || form.manifestRevision < 1 || form.bindingGeneration < 1) return 'Manifest and new binding generation are required.'
    if (![form.manifestDigest, form.configDigest, form.capabilityDigest, form.resourceProfileDigest].every((value) => digestPattern.test(value))) return 'Manifest, config, capability, and resource profile require exact sha256 digests.'
  }
  if (action.value === 'rollback' && form.previousBindingGeneration < 1) return 'Rollback requires the exact previous qualified binding generation.'
  return ''
}

const mutation = useMutation({
  mutationFn: async () => {
    const activeSession = session.data.value
    if (!activeSession) throw new Error('SESSION_UNAVAILABLE')
    const validation = validationError()
    if (validation) throw new Error(validation)
    if (action.value === 'qualify') return submitPluginQualification({
      pluginID: form.pluginID, manifestID: form.manifestID, manifestRevision: form.manifestRevision,
      status: form.qualificationStatus === 'qualified' ? 'qualified' : form.qualificationStatus === 'hold' ? 'hold' : 'unqualified',
      scope: form.scope, targetSetDigest: form.targetSetDigest,
    }, activeSession)
    if (action.value === 'activate') return submitPluginActivation({
      pluginID: form.pluginID, bindingGeneration: form.bindingGeneration,
      manifestID: form.manifestID, manifestRevision: form.manifestRevision,
      manifestDigest: form.manifestDigest, configDigest: form.configDigest,
      capabilityDigest: form.capabilityDigest, resourceProfileDigest: form.resourceProfileDigest,
      scope: form.scope, targetSetDigest: form.targetSetDigest,
    }, activeSession)
    return submitPluginLifecycle(
      action.value,
      form.pluginID,
      form.scope,
      form.targetSetDigest,
      action.value === 'rollback' ? form.previousBindingGeneration : undefined,
      activeSession,
    )
  },
  onSuccess: () => {
    successMessage.value = `${title.value} was accepted as a control fact. Runtime readiness remains separately observed.`
    errorMessage.value = ''; dialogOpen.value = false
    void queryClient.invalidateQueries({ queryKey: ['session-bound'] })
  },
  onError: (error) => { errorMessage.value = error instanceof Error ? error.message : 'Plugin operation failed closed.' },
})

function open(item: Record<string, unknown>): void {
  form.pluginID = typeof item.plugin_id === 'string' ? item.plugin_id : ''
  form.manifestID = typeof item.manifest_id === 'string' ? item.manifest_id : ''
  form.manifestRevision = typeof item.manifest_revision === 'number' ? item.manifest_revision : 1
  form.manifestDigest = typeof item.manifest_digest === 'string' ? item.manifest_digest : ''
  action.value = 'qualify'; errorMessage.value = ''; dialogOpen.value = true
}
</script>

<template>
  <div class="catalog-page">
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
    <ResourcePage>
      <template #row-actions="{ item }">
        <button
          type="button"
          :disabled="!ui.dangerousViewportSupported"
          @click="open(item)"
        >
          Manage
        </button>
      </template>
    </ResourcePage>
    <aside
      class="plugin-boundary"
      role="note"
    >
      <strong>Lifecycle boundary</strong> Catalog, qualification, binding generation, drain, revoke, and rollback are Go-owned facts. Runtime Host and plugins never receive PostgreSQL credentials or effect ownership.
    </aside>
    <aside
      v-if="!ui.dangerousViewportSupported"
      class="viewport-hold"
    >
      <StatusMark value="hold" /> Plugin lifecycle actions require a viewport at least 1024 CSS pixels wide.
    </aside>

    <ElDialog
      v-model="dialogOpen"
      :title="title"
      width="min(44rem, 94vw)"
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
        class="plugin-form"
        @submit.prevent="mutation.mutate()"
      >
        <label class="wide"><span>Plugin</span><input
          v-model="form.pluginID"
          class="mono"
          readonly
        ></label>
        <label><span>Action</span><select v-model="action"><option value="qualify">Record qualification</option><option value="activate">Activate exact binding</option><option value="drain">Drain</option><option value="revoke">Revoke</option><option value="rollback">Rollback to exact previous</option></select></label>
        <label><span>Authorization scope</span><input
          v-model="form.scope"
          required
          autocomplete="off"
        ></label>
        <label class="wide"><span>Frozen target-set digest</span><input
          v-model="form.targetSetDigest"
          class="mono"
          pattern="sha256:(?!0{64}$)[0-9a-f]{64}"
          required
          autocomplete="off"
        ></label>
        <template v-if="action === 'qualify' || action === 'activate'">
          <label><span>Manifest ID</span><input
            v-model="form.manifestID"
            class="mono"
            required
          ></label>
          <label><span>Manifest revision</span><input
            v-model.number="form.manifestRevision"
            type="number"
            min="1"
            required
          ></label>
        </template>
        <label v-if="action === 'qualify'"><span>Qualification</span><select v-model="form.qualificationStatus"><option value="qualified">Qualified</option><option value="hold">Hold</option><option value="unqualified">Unqualified</option></select></label>
        <template v-if="action === 'activate'">
          <label><span>New binding generation</span><input
            v-model.number="form.bindingGeneration"
            type="number"
            min="1"
            required
          ></label>
          <label class="wide"><span>Manifest digest</span><input
            v-model="form.manifestDigest"
            class="mono"
            pattern="sha256:(?!0{64}$)[0-9a-f]{64}"
            required
          ></label>
          <label class="wide"><span>Config digest</span><input
            v-model="form.configDigest"
            class="mono"
            pattern="sha256:(?!0{64}$)[0-9a-f]{64}"
            required
          ></label>
          <label class="wide"><span>Capability digest</span><input
            v-model="form.capabilityDigest"
            class="mono"
            pattern="sha256:(?!0{64}$)[0-9a-f]{64}"
            required
          ></label>
          <label class="wide"><span>Resource profile digest</span><input
            v-model="form.resourceProfileDigest"
            class="mono"
            pattern="sha256:(?!0{64}$)[0-9a-f]{64}"
            required
          ></label>
        </template>
        <label v-if="action === 'rollback'"><span>Previous binding generation</span><input
          v-model.number="form.previousBindingGeneration"
          type="number"
          min="1"
          required
        ></label>
        <p class="wide caution">
          No action is optimistic. Activation succeeds only for an exact qualified revision and generation; revoke fences late work and does not create an effect intent.
        </p>
      </form>
      <template #footer>
        <ElButton @click="dialogOpen = false">
          Cancel
        </ElButton><ElButton
          type="primary"
          :icon="SetUp"
          :loading="mutation.isPending.value"
          @click="mutation.mutate()"
        >
          Submit exact lifecycle fact
        </ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.catalog-page { display: grid; gap: var(--space-4); }.plugin-boundary { padding: var(--space-3) var(--space-4); border-left: 3px solid var(--action-primary); background: var(--surface-panel); color: var(--text-secondary); }.plugin-boundary strong { display: block; color: var(--text-primary); }.viewport-hold { display: flex; align-items: center; gap: var(--space-3); padding: var(--space-3); border: 1px solid var(--state-hold); background: var(--state-hold-surface); }.dialog-alert { margin-bottom: var(--space-4); }.plugin-form { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-4); }.plugin-form label { display: grid; gap: var(--space-1); color: var(--text-secondary); font-size: var(--text-sm); }.plugin-form label > span { font-weight: 700; }.plugin-form input, .plugin-form select { min-height: 2.6rem; padding: var(--space-2) var(--space-3); border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: var(--surface-panel); color: var(--text-primary); }.wide { grid-column: 1 / -1; }.caution { margin: 0; padding: var(--space-3); border-left: 3px solid var(--state-warning); background: var(--state-warning-surface); }
@media (max-width: 44rem) { .plugin-form { grid-template-columns: 1fr; }.wide { grid-column: 1; } }
</style>
