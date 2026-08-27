<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useMutation, useQueryClient } from '@tanstack/vue-query'
import { ElAlert, ElButton, ElDialog } from 'element-plus'
import { Plus, Stamp } from '@element-plus/icons-vue'
import type { EffectProposalDetail } from '@masi/control-api'
import ResourcePage from './ResourcePage.vue'
import StatusMark from '@/components/StatusMark.vue'
import { formatTime } from '@/format'
import { useSessionQuery } from '@/api/session'
import { useUiStore } from '@/stores/ui'
import {
  decideProposal,
  loadProposal,
  submitFirewallActivation,
  submitFirewallActivationProposal,
  submitProposal,
} from '@/api/mutations'

defineProps<{ mode: 'proposals' | 'firewall' | 'approvals' }>()
const session = useSessionQuery()
const queryClient = useQueryClient()
const ui = useUiStore()

type DialogKind = 'proposal' | 'review' | 'firewall-proposal' | 'firewall-activate'
const dialogOpen = ref(false)
const dialogKind = ref<DialogKind>('proposal')
const proposalDetail = ref<EffectProposalDetail | null>(null)
const detailLoading = ref(false)
const pendingDecision = ref<'approve' | 'reject'>('approve')
const operationError = ref('')
const operationResult = ref('')

const proposalForm = reactive({
  targetIDs: '', scope: '', policyDigest: '', riskLevel: 'R1',
  evidenceRefs: '', note: '', expiryMinutes: 30,
})
const firewallForm = reactive({
  revisionID: '', targetID: '', targetSetDigest: '', evidenceRefs: '', note: '',
  expiryMinutes: 30, operationID: '', scope: '',
})

const digestPattern = /^sha256:[0-9a-f]{64}$/
const phishingResistant = computed(() => ['webauthn-fido2', 'passkey', 'hardware-key'].includes(session.data.value?.step_up ?? 'none'))
const sameMaker = computed(() => proposalDetail.value?.actor_ref === session.data.value?.actor_ref)
const dialogTitle = computed(() => ({
  proposal: 'Propose a bounded response rule',
  review: 'Review exact proposal context',
  'firewall-proposal': 'Propose baseline activation',
  'firewall-activate': 'Prepare authorized activation',
})[dialogKind.value])

function splitValues(value: string): string[] {
  return [...new Set(value.split(',').map((item) => item.trim()).filter(Boolean))]
}

function validate(): string {
  if (dialogKind.value === 'proposal') {
    const targets = splitValues(proposalForm.targetIDs)
    if (targets.length < 1 || targets.length > 128) return 'Provide between 1 and 128 stable target IDs.'
    if (!proposalForm.scope || proposalForm.scope.length > 256) return 'Scope is required and must be at most 256 characters.'
    if (!digestPattern.test(proposalForm.policyDigest)) return 'Policy digest must be sha256 plus 64 lowercase hexadecimal characters.'
    if (proposalForm.note.length > 2048) return 'Note exceeds the 2 KiB contract bound.'
  }
  if (dialogKind.value === 'firewall-proposal' || dialogKind.value === 'firewall-activate') {
    if (!digestPattern.test(firewallForm.targetSetDigest)) return 'Target-set digest must be an exact sha256 digest.'
  }
  if (dialogKind.value === 'firewall-activate' && (!firewallForm.operationID || !firewallForm.scope)) {
    return 'Operation identity and exact authorization scope are required.'
  }
  if (dialogKind.value === 'review') {
    if (!proposalDetail.value) return 'The exact proposal context has not loaded.'
    if (sameMaker.value) return 'Maker-checker policy forbids the proposing identity from authorizing this proposal.'
    if (pendingDecision.value === 'approve' && proposalDetail.value.risk_level === 'R3' && !phishingResistant.value) {
      return 'R3 approval requires a phishing-resistant step-up session.'
    }
  }
  return ''
}

const mutation = useMutation({
  mutationFn: async () => {
    const activeSession = session.data.value
    if (!activeSession) throw new Error('SESSION_UNAVAILABLE')
    const validation = validate()
    if (validation) throw new Error(validation)
    if (dialogKind.value === 'proposal') {
      return submitProposal({
        effectKind: 'firewall-overlay', targetIDs: splitValues(proposalForm.targetIDs),
        scope: proposalForm.scope, policyDigest: proposalForm.policyDigest,
        riskLevel: proposalForm.riskLevel === 'R2' ? 'R2' : 'R1', evidenceRefs: splitValues(proposalForm.evidenceRefs),
        note: proposalForm.note, expiresAtUnixMS: Date.now() + proposalForm.expiryMinutes * 60_000,
      }, activeSession)
    }
    if (dialogKind.value === 'review' && proposalDetail.value) {
      const firewall = ['firewall-baseline-activate', 'firewall-rollback'].includes(proposalDetail.value.effect_kind)
      return decideProposal(
        proposalDetail.value.proposal_id,
        pendingDecision.value,
        pendingDecision.value === 'approve' ? 'APPROVED_EXACT_CONTEXT' : 'REJECTED_EXACT_CONTEXT',
        activeSession,
        firewall,
      )
    }
    if (dialogKind.value === 'firewall-proposal') {
      return submitFirewallActivationProposal({
        revisionID: firewallForm.revisionID, targetSetDigest: firewallForm.targetSetDigest,
        evidenceRefs: splitValues(firewallForm.evidenceRefs),
        expiresAtUnixMS: Date.now() + firewallForm.expiryMinutes * 60_000,
        note: firewallForm.note,
      }, activeSession)
    }
    return submitFirewallActivation({
      targetID: firewallForm.targetID, revisionID: firewallForm.revisionID,
      operationID: firewallForm.operationID, scope: firewallForm.scope,
      targetSetDigest: firewallForm.targetSetDigest,
    }, activeSession)
  },
  onSuccess: () => {
    operationResult.value = dialogKind.value === 'firewall-activate'
      ? 'Activation was prepared. This is not P4 success; follow the original operation through readback.'
      : 'The immutable governance fact was accepted. No external effect is implied.'
    operationError.value = ''
    dialogOpen.value = false
    void queryClient.invalidateQueries({ queryKey: ['resource'] })
    void queryClient.invalidateQueries({ queryKey: ['dashboard'] })
  },
  onError: (error) => {
    operationError.value = error instanceof Error ? error.message : 'The mutation failed closed.'
  },
})

function openProposal(): void {
  dialogKind.value = 'proposal'
  operationError.value = ''
  dialogOpen.value = true
}

async function openReview(item: Record<string, unknown>): Promise<void> {
  const proposalID = typeof item.proposal_id === 'string' ? item.proposal_id : ''
  if (!proposalID) return
  proposalDetail.value = null
  operationError.value = ''
  dialogKind.value = 'review'
  dialogOpen.value = true
  detailLoading.value = true
  try {
    proposalDetail.value = await loadProposal(proposalID)
  } catch (error) {
    operationError.value = error instanceof Error ? error.message : 'Exact proposal context could not be loaded.'
  } finally {
    detailLoading.value = false
  }
}

function openFirewall(item: Record<string, unknown>, kind: 'firewall-proposal' | 'firewall-activate'): void {
  dialogKind.value = kind
  operationError.value = ''
  firewallForm.revisionID = typeof item.revision_id === 'string' ? item.revision_id : ''
  firewallForm.targetID = typeof item.target_id === 'string' ? item.target_id : ''
  if (kind === 'firewall-activate') firewallForm.operationID = `fw-activation-${crypto.randomUUID()}`
  dialogOpen.value = true
}
</script>

<template>
  <div class="governance">
    <ElAlert
      v-if="operationResult"
      type="success"
      show-icon
      :title="operationResult"
      @close="operationResult = ''"
    />
    <ElAlert
      v-if="operationError && !dialogOpen"
      type="error"
      show-icon
      :title="operationError"
      @close="operationError = ''"
    />
    <ResourcePage>
      <template #actions>
        <ElButton
          v-if="mode === 'proposals'"
          type="primary"
          :icon="Plus"
          :disabled="!ui.dangerousViewportSupported"
          @click="openProposal"
        >
          New proposal
        </ElButton>
      </template>
      <template #row-actions="{ item }">
        <button
          v-if="mode === 'approvals'"
          type="button"
          :disabled="!ui.dangerousViewportSupported"
          @click="openReview(item)"
        >
          Review
        </button>
        <template v-if="mode === 'firewall'">
          <button
            type="button"
            :disabled="!ui.dangerousViewportSupported"
            @click="openFirewall(item, 'firewall-proposal')"
          >
            Propose activation
          </button>
          <button
            type="button"
            :disabled="!ui.dangerousViewportSupported"
            @click="openFirewall(item, 'firewall-activate')"
          >
            Prepare authorized
          </button>
        </template>
      </template>
    </ResourcePage>

    <aside
      v-if="!ui.dangerousViewportSupported"
      class="viewport-hold"
      role="status"
    >
      <StatusMark value="hold" />
      High-risk governance actions require a viewport at least 1024 CSS pixels wide so exact context remains visible.
    </aside>

    <ElDialog
      v-model="dialogOpen"
      :title="dialogTitle"
      width="min(42rem, 94vw)"
      destroy-on-close
      align-center
    >
      <ElAlert
        v-if="operationError"
        class="dialog-alert"
        type="error"
        :closable="false"
        show-icon
        :title="operationError"
      />

      <form
        v-if="dialogKind === 'proposal'"
        class="governance-form"
        @submit.prevent="mutation.mutate()"
      >
        <label><span>Target IDs <small>comma-separated, max 128</small></span><input
          v-model="proposalForm.targetIDs"
          required
          autocomplete="off"
        ></label>
        <label><span>Authorization scope</span><input
          v-model="proposalForm.scope"
          required
          autocomplete="off"
        ></label>
        <label><span>Exact policy digest</span><input
          v-model="proposalForm.policyDigest"
          class="mono"
          required
          pattern="sha256:[0-9a-f]{64}"
          autocomplete="off"
        ></label>
        <label><span>Risk level</span><select v-model="proposalForm.riskLevel"><option value="R1">R1 · scoped operator</option><option value="R2">R2 · maker-checker</option></select></label>
        <label><span>Evidence references <small>comma-separated</small></span><input
          v-model="proposalForm.evidenceRefs"
          autocomplete="off"
        ></label>
        <label><span>Expiry in minutes</span><input
          v-model.number="proposalForm.expiryMinutes"
          type="number"
          min="1"
          max="1440"
          required
        ></label>
        <label class="governance-form__wide"><span>Operator note <small>plain text, max 2048</small></span><textarea
          v-model="proposalForm.note"
          maxlength="2048"
          rows="4"
        /></label>
      </form>

      <section
        v-else-if="dialogKind === 'review'"
        class="review"
        aria-live="polite"
      >
        <p v-if="detailLoading">
          Loading the exact immutable proposal…
        </p>
        <template v-else-if="proposalDetail">
          <ElAlert
            v-if="sameMaker"
            type="warning"
            :closable="false"
            show-icon
            title="This session is the maker. Authorization is blocked by maker-checker policy."
          />
          <dl>
            <div>
              <dt>Proposal</dt><dd class="mono">
                {{ proposalDetail.proposal_id }}
              </dd>
            </div>
            <div>
              <dt>Maker</dt><dd class="mono">
                {{ proposalDetail.actor_ref }}
              </dd>
            </div>
            <div><dt>Governance</dt><dd><StatusMark :value="proposalDetail.governance_status" /></dd></div>
            <div><dt>Risk / effect</dt><dd><StatusMark :value="proposalDetail.risk_level" /> {{ proposalDetail.effect_kind }}</dd></div>
            <div>
              <dt>Targets</dt><dd class="mono">
                {{ proposalDetail.target_ids.join(', ') }}
              </dd>
            </div>
            <div>
              <dt>Target set</dt><dd class="mono">
                {{ proposalDetail.target_set_digest }}
              </dd>
            </div>
            <div>
              <dt>Policy</dt><dd class="mono">
                {{ proposalDetail.policy_digest }}
              </dd>
            </div>
            <div><dt>Evidence</dt><dd>{{ proposalDetail.evidence_refs.length ? proposalDetail.evidence_refs.join(', ') : 'No evidence references' }}</dd></div>
            <div><dt>Expires</dt><dd>{{ formatTime(proposalDetail.expires_at_unix_ms) }}</dd></div>
            <div><dt>Note</dt><dd>{{ proposalDetail.note || 'No note' }}</dd></div>
          </dl>
          <fieldset>
            <legend>Terminal decision</legend>
            <label><input
              v-model="pendingDecision"
              type="radio"
              value="approve"
            > Approve exact context</label>
            <label><input
              v-model="pendingDecision"
              type="radio"
              value="reject"
            > Reject exact context</label>
          </fieldset>
          <p class="review__step-up">
            Session step-up: <b>{{ session.data.value?.step_up }}</b>. Approval never implies device success.
          </p>
        </template>
      </section>

      <form
        v-else
        class="governance-form"
        @submit.prevent="mutation.mutate()"
      >
        <label><span>Revision</span><input
          v-model="firewallForm.revisionID"
          class="mono"
          readonly
        ></label>
        <label v-if="dialogKind === 'firewall-activate'"><span>Target</span><input
          v-model="firewallForm.targetID"
          class="mono"
          readonly
        ></label>
        <label><span>Exact target-set digest</span><input
          v-model="firewallForm.targetSetDigest"
          class="mono"
          required
          pattern="sha256:[0-9a-f]{64}"
          autocomplete="off"
        ></label>
        <template v-if="dialogKind === 'firewall-proposal'">
          <label><span>Evidence references <small>comma-separated</small></span><input
            v-model="firewallForm.evidenceRefs"
            autocomplete="off"
          ></label>
          <label><span>Expiry in minutes</span><input
            v-model.number="firewallForm.expiryMinutes"
            type="number"
            min="1"
            max="1440"
            required
          ></label>
          <label class="governance-form__wide"><span>Activation note</span><textarea
            v-model="firewallForm.note"
            maxlength="2048"
            rows="4"
          /></label>
        </template>
        <template v-else>
          <label><span>Original operation identity</span><input
            v-model="firewallForm.operationID"
            class="mono"
            required
            autocomplete="off"
          ></label>
          <label><span>Authorization scope</span><input
            v-model="firewallForm.scope"
            required
            autocomplete="off"
          ></label>
          <p class="governance-form__wide caution">
            Prepare only after a different Operator has authorized the exact R3 proposal. A 202 response means durable preparation, not P4 success.
          </p>
        </template>
      </form>

      <template #footer>
        <ElButton @click="dialogOpen = false">
          Cancel
        </ElButton>
        <ElButton
          type="primary"
          :icon="Stamp"
          :loading="mutation.isPending.value"
          :disabled="detailLoading || (dialogKind === 'review' && sameMaker)"
          @click="mutation.mutate()"
        >
          {{ dialogKind === 'review' ? (pendingDecision === 'approve' ? 'Authorize exact proposal' : 'Record rejection') : 'Submit immutable request' }}
        </ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.governance { display: grid; gap: var(--space-4); }.viewport-hold { display: flex; align-items: center; gap: var(--space-3); padding: var(--space-3); border: 1px solid var(--state-hold); background: var(--state-hold-surface); color: var(--text-secondary); }.dialog-alert { margin-bottom: var(--space-4); }
.governance-form { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-4); }.governance-form label { display: grid; gap: var(--space-1); color: var(--text-secondary); font-size: var(--text-sm); }.governance-form label > span { font-weight: 700; }.governance-form small { color: var(--text-muted); font-weight: 400; }.governance-form input, .governance-form select, .governance-form textarea { width: 100%; min-height: 2.6rem; padding: var(--space-2) var(--space-3); border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: var(--surface-panel); color: var(--text-primary); }.governance-form textarea { resize: vertical; }.governance-form__wide { grid-column: 1 / -1; }.caution { margin: 0; padding: var(--space-3); border-left: 3px solid var(--state-warning); background: var(--state-warning-surface); color: var(--text-secondary); }
.review { display: grid; gap: var(--space-4); }.review dl { margin: 0; display: grid; }.review dl div { display: grid; grid-template-columns: 9rem 1fr; gap: var(--space-3); padding: var(--space-2) 0; border-bottom: 1px solid var(--border-subtle); }.review dt { color: var(--text-muted); }.review dd { min-width: 0; margin: 0; overflow-wrap: anywhere; }.review fieldset { display: flex; gap: var(--space-5); padding: var(--space-3); border: 1px solid var(--border-strong); }.review__step-up { margin: 0; color: var(--text-secondary); }
button:disabled { color: var(--state-disabled); cursor: not-allowed; }
@media (max-width: 44rem) { .governance-form { grid-template-columns: 1fr; }.governance-form__wide { grid-column: 1; }.review dl div { grid-template-columns: 1fr; gap: 0; } }
</style>
