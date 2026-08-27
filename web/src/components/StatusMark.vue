<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{ value: unknown; compact?: boolean }>(), { compact: false })

const text = computed(() => {
  if (props.value === null || props.value === undefined || props.value === '') return 'unknown'
  if (typeof props.value === 'boolean') return props.value ? 'yes' : 'no'
  if (typeof props.value === 'string') return props.value.replaceAll('_', ' ')
  if (typeof props.value === 'number' && Number.isFinite(props.value)) return String(props.value)
  return 'unsupported'
})

const tone = computed(() => {
  const value = text.value.toLowerCase()
  if (/ready|healthy|active|applied|approved|approve|valid|current|succeeded|benign|yes/.test(value)) return 'healthy'
  if (/failed|danger|critical|rejected|reject|invalid|quarantined|error/.test(value)) return 'danger'
  if (/hold|unknown|gap|reset|not.covered|not.measurable|abstain|unavailable/.test(value)) return 'hold'
  if (/partial|warning|stale|pending|planned|running|claimed|draining|limited|attention/.test(value)) return 'warning'
  if (/disabled|retired|revoked|no/.test(value)) return 'disabled'
  return 'neutral'
})
</script>

<template>
  <span
    class="status-mark"
    :class="[`status-mark--${tone}`, { 'status-mark--compact': compact }]"
  >
    <span
      class="status-mark__shape"
      aria-hidden="true"
    />
    <span>{{ text }}</span>
  </span>
</template>

<style scoped>
.status-mark {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  min-height: 1.75rem;
  padding: 0.15rem 0.55rem;
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  background: var(--surface-muted);
  color: var(--text-secondary);
  font-size: var(--text-xs);
  font-weight: 650;
  line-height: 1.2;
  text-transform: capitalize;
  white-space: nowrap;
}
.status-mark__shape { width: 0.48rem; height: 0.48rem; border-radius: 50%; background: currentColor; }
.status-mark--healthy { color: var(--state-healthy); background: var(--state-healthy-surface); }
.status-mark--warning { color: var(--state-warning); background: var(--state-warning-surface); }
.status-mark--danger { color: var(--state-danger); background: var(--state-danger-surface); }
.status-mark--hold { color: var(--state-hold); background: var(--state-hold-surface); }
.status-mark--hold .status-mark__shape { border-radius: 1px; transform: rotate(45deg); }
.status-mark--disabled { color: var(--state-disabled); background: var(--state-disabled-surface); }
.status-mark--compact { min-height: 1.45rem; padding: 0.1rem 0.4rem; }
</style>
