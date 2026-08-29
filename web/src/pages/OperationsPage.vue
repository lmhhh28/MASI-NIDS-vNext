<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useMutation, useQueryClient } from '@tanstack/vue-query'
import { ElAlert, ElButton, ElDialog } from 'element-plus'
import { Plus, Promotion } from '@element-plus/icons-vue'
import ResourcePage from './ResourcePage.vue'
import StatusMark from '@/components/StatusMark.vue'
import { useSessionQuery } from '@/api/session'
import { submitFleetAdvance, submitTargetLifecycle, submitTargetRegistration, type TargetLifecycleAction } from '@/api/operations'
import { useUiStore } from '@/stores/ui'

defineProps<{ mode: 'targets' | 'fleet' }>()
const session = useSessionQuery()
const queryClient = useQueryClient()
const ui = useUiStore()
const dialogOpen = ref(false)
const dialogKind = ref<'register' | 'lifecycle' | 'fleet-advance'>('register')
const errorMessage = ref('')
const successMessage = ref('')

const target = reactive<{
  targetID: string
  displayName: string
  endpoint: string
  deviceID: number
  role: string
  desiredProfileDigest: string
  credentialRef: string
  scope: string
  tlsServerName: string
  tlsIdentityRef: string
  targetSetDigest: string
  action: TargetLifecycleAction
  reasonCode: string
}>({
  targetID: '', displayName: '', endpoint: '', deviceID: 1, role: 'p4runtime-primary',
  desiredProfileDigest: '', credentialRef: '', scope: '', tlsServerName: '',
  tlsIdentityRef: '', targetSetDigest: '', action: 'verify',
  reasonCode: 'TARGET_LIFECYCLE_APPROVED',
})
const fleet = reactive({
  fleetID: '', currentWave: 0, nextWave: 1, scope: '', targetSetDigest: '',
  completedVectorDigest: '', gateProposalID: '', decisionReason: '',
})

const title = computed(() => ({ register: 'Register stable target', lifecycle: 'Change target lifecycle', 'fleet-advance': 'Advance fleet wave gate' })[dialogKind.value])
const digestPattern = /^sha256:(?!0{64}$)[0-9a-f]{64}$/

function validationError(): string {
  if (dialogKind.value === 'register') {
    if (!target.displayName || !/^https:\/\/[^/?#]+:[0-9]{1,5}$/.test(target.endpoint)) return 'Display name and exact TLS P4Runtime endpoint are required.'
    if (!Number.isSafeInteger(target.deviceID) || target.deviceID < 1) return 'Device ID must be a positive integer.'
    if (![target.desiredProfileDigest, target.targetSetDigest].every((value) => digestPattern.test(value))) return 'Profile and target-set digests must be exact sha256 values.'
    if (!target.scope || !target.tlsServerName || !target.tlsIdentityRef || !target.credentialRef) return 'Scope, TLS identity, and credential reference are required.'
    if (target.tlsIdentityRef !== target.credentialRef) return 'TLS identity reference must exactly match the credential reference.'
  } else if (dialogKind.value === 'lifecycle') {
    if (!target.targetID || !target.scope || !digestPattern.test(target.targetSetDigest)) return 'Target, scope, and exact target-set digest are required.'
    if (!/^[A-Z][A-Z0-9_]{0,63}$/.test(target.reasonCode)) return 'Reason code must use the closed uppercase code format.'
  } else {
    if (!fleet.fleetID || !fleet.scope || !digestPattern.test(fleet.targetSetDigest)) return 'Fleet, scope, and exact target-set digest are required.'
    if (fleet.nextWave !== fleet.currentWave + 1) return 'The next wave must immediately follow the current wave.'
    if (fleet.gateProposalID && (!digestPattern.test(fleet.completedVectorDigest) || !/^[A-Z][A-Z0-9_]{0,63}$/.test(fleet.decisionReason))) return 'Manual gates require exact completed-vector digest and decision reason.'
  }
  return ''
}

const mutation = useMutation({
  mutationFn: async () => {
    const activeSession = session.data.value
    if (!activeSession) throw new Error('SESSION_UNAVAILABLE')
    const validation = validationError()
    if (validation) throw new Error(validation)
    if (dialogKind.value === 'register') {
      return submitTargetRegistration({
        displayName: target.displayName, p4runtimeEndpoint: target.endpoint, deviceID: target.deviceID,
        role: target.role, desiredProfileDigest: target.desiredProfileDigest,
        credentialRef: target.credentialRef, scope: target.scope,
        tlsServerName: target.tlsServerName, tlsIdentityRef: target.tlsIdentityRef,
        targetSetDigest: target.targetSetDigest,
      }, activeSession)
    }
    if (dialogKind.value === 'lifecycle') {
      return submitTargetLifecycle(target.action, target.targetID, target.scope, target.targetSetDigest, target.reasonCode, activeSession)
    }
    return submitFleetAdvance({
      fleetID: fleet.fleetID, currentWave: fleet.currentWave, nextWave: fleet.nextWave,
      scope: fleet.scope, targetSetDigest: fleet.targetSetDigest,
      ...(fleet.completedVectorDigest ? { completedVectorDigest: fleet.completedVectorDigest } : {}),
      ...(fleet.gateProposalID ? { gateProposalID: fleet.gateProposalID } : {}),
      ...(fleet.decisionReason ? { decisionReason: fleet.decisionReason } : {}),
    }, activeSession)
  },
  onSuccess: () => {
    successMessage.value = dialogKind.value === 'fleet-advance'
      ? 'The next static wave gate was opened; per-target child outcomes remain authoritative.'
      : 'The target-control fact was accepted. Connection or primary state is not inferred.'
    errorMessage.value = ''
    dialogOpen.value = false
    void queryClient.invalidateQueries({ queryKey: ['session-bound'] })
  },
  onError: (error) => { errorMessage.value = error instanceof Error ? error.message : 'Operation failed closed.' },
})

function openRegistration(): void {
  dialogKind.value = 'register'; errorMessage.value = ''; dialogOpen.value = true
}

function openLifecycle(item: Record<string, unknown>): void {
  target.targetID = typeof item.target_id === 'string' ? item.target_id : ''
  const lifecycle = typeof item.lifecycle === 'string' ? item.lifecycle : ''
  target.action = lifecycle === 'candidate' ? 'verify' : lifecycle === 'verified' ? 'activate' : 'drain'
  dialogKind.value = 'lifecycle'; errorMessage.value = ''; dialogOpen.value = true
}

function openFleetAdvance(item: Record<string, unknown>): void {
  fleet.fleetID = typeof item.fleet_operation_id === 'string' ? item.fleet_operation_id : ''
  fleet.targetSetDigest = typeof item.target_set_digest === 'string' ? item.target_set_digest : ''
  dialogKind.value = 'fleet-advance'; errorMessage.value = ''; dialogOpen.value = true
}
</script>

<template>
  <div class="operations-page">
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
      <template #actions>
        <ElButton
          v-if="mode === 'targets'"
          type="primary"
          :icon="Plus"
          :disabled="!ui.dangerousViewportSupported"
          @click="openRegistration"
        >
          Register target
        </ElButton>
      </template>
      <template #row-actions="{ item }">
        <button
          v-if="mode === 'targets'"
          type="button"
          :disabled="!ui.dangerousViewportSupported"
          @click="openLifecycle(item)"
        >
          Manage lifecycle
        </button>
        <button
          v-else
          type="button"
          :disabled="!ui.dangerousViewportSupported"
          @click="openFleetAdvance(item)"
        >
          Advance wave
        </button>
      </template>
    </ResourcePage>
    <aside
      v-if="!ui.dangerousViewportSupported"
      class="viewport-hold"
    >
      <StatusMark value="hold" /> Target and fleet mutations require a viewport at least 1024 CSS pixels wide.
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
        v-if="dialogKind === 'register'"
        class="operation-form"
        @submit.prevent="mutation.mutate()"
      >
        <label><span>Display name</span><input
          v-model="target.displayName"
          required
          autocomplete="off"
        ></label>
        <label><span>P4Runtime TLS endpoint</span><input
          v-model="target.endpoint"
          required
          placeholder="https://switch.example:9559"
          autocomplete="off"
        ></label>
        <label><span>Device ID</span><input
          v-model.number="target.deviceID"
          type="number"
          min="1"
          required
        ></label>
        <label><span>Role</span><input
          v-model="target.role"
          required
          autocomplete="off"
        ></label>
        <label class="wide"><span>Desired profile digest</span><input
          v-model="target.desiredProfileDigest"
          class="mono"
          pattern="sha256:(?!0{64}$)[0-9a-f]{64}"
          required
          autocomplete="off"
        ></label>
        <label><span>Credential reference</span><input
          v-model="target.credentialRef"
          required
          autocomplete="off"
        ></label>
        <label><span>Authorization scope</span><input
          v-model="target.scope"
          required
          autocomplete="off"
        ></label>
        <label><span>TLS server name</span><input
          v-model="target.tlsServerName"
          required
          autocomplete="off"
        ></label>
        <label><span>TLS identity reference</span><input
          v-model="target.tlsIdentityRef"
          required
          autocomplete="off"
        ></label>
        <label class="wide"><span>Frozen target-set digest</span><input
          v-model="target.targetSetDigest"
          class="mono"
          pattern="sha256:(?!0{64}$)[0-9a-f]{64}"
          required
          autocomplete="off"
        ></label>
      </form>
      <form
        v-else-if="dialogKind === 'lifecycle'"
        class="operation-form"
        @submit.prevent="mutation.mutate()"
      >
        <label class="wide"><span>Stable target</span><input
          v-model="target.targetID"
          class="mono"
          readonly
        ></label>
        <label><span>Lifecycle action</span><select v-model="target.action"><option value="verify">Verify candidate</option><option value="activate">Activate verified target</option><option value="drain">Drain</option><option value="disable">Disable</option><option value="quarantine">Quarantine</option><option value="retire">Retire permanently</option></select></label>
        <label><span>Reason code</span><input
          v-model="target.reasonCode"
          pattern="[A-Z][A-Z0-9_]{0,63}"
          required
          autocomplete="off"
        ></label>
        <label><span>Authorization scope</span><input
          v-model="target.scope"
          required
          autocomplete="off"
        ></label>
        <label><span>Frozen target-set digest</span><input
          v-model="target.targetSetDigest"
          class="mono"
          pattern="sha256:(?!0{64}$)[0-9a-f]{64}"
          required
          autocomplete="off"
        ></label>
        <p class="wide caution">
          Lifecycle facts do not prove Edge connectivity, P4Runtime primary, or dataplane readiness. Retire is terminal and revokes the current assignment first.
        </p>
      </form>
      <form
        v-else
        class="operation-form"
        @submit.prevent="mutation.mutate()"
      >
        <label class="wide"><span>Fleet operation</span><input
          v-model="fleet.fleetID"
          class="mono"
          readonly
        ></label>
        <label><span>Current wave</span><input
          v-model.number="fleet.currentWave"
          type="number"
          min="0"
          max="63"
          required
        ></label>
        <label><span>Next wave</span><input
          v-model.number="fleet.nextWave"
          type="number"
          min="0"
          max="63"
          required
        ></label>
        <label><span>Authorization scope</span><input
          v-model="fleet.scope"
          required
          autocomplete="off"
        ></label>
        <label><span>Frozen target-set digest</span><input
          v-model="fleet.targetSetDigest"
          class="mono"
          pattern="sha256:(?!0{64}$)[0-9a-f]{64}"
          required
          autocomplete="off"
        ></label>
        <label><span>Manual gate proposal <small>optional</small></span><input
          v-model="fleet.gateProposalID"
          autocomplete="off"
        ></label>
        <label><span>Completed-vector digest <small>required for manual gate</small></span><input
          v-model="fleet.completedVectorDigest"
          class="mono"
          autocomplete="off"
        ></label>
        <label class="wide"><span>Decision reason <small>required for manual gate</small></span><input
          v-model="fleet.decisionReason"
          autocomplete="off"
        ></label>
        <p class="wide caution">
          Only the immediately following frozen wave can open. Mixed child state remains visible and is never collapsed to a successful parent.
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
          Submit controlled operation
        </ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.operations-page { display: grid; gap: var(--space-4); }.viewport-hold { display: flex; gap: var(--space-3); align-items: center; padding: var(--space-3); border: 1px solid var(--state-hold); background: var(--state-hold-surface); }.dialog-alert { margin-bottom: var(--space-4); }.operation-form { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-4); }.operation-form label { display: grid; gap: var(--space-1); color: var(--text-secondary); font-size: var(--text-sm); }.operation-form label > span { font-weight: 700; }.operation-form small { color: var(--text-muted); font-weight: 400; }.operation-form input, .operation-form select { min-height: 2.6rem; padding: var(--space-2) var(--space-3); border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: var(--surface-panel); color: var(--text-primary); }.wide { grid-column: 1 / -1; }.caution { margin: 0; padding: var(--space-3); border-left: 3px solid var(--state-warning); background: var(--state-warning-surface); color: var(--text-secondary); }
@media (max-width: 44rem) { .operation-form { grid-template-columns: 1fr; }.wide { grid-column: 1; } }
</style>
