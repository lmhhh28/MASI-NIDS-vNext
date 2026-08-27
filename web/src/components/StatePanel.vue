<script setup lang="ts">
import { WarningFilled, Refresh } from '@element-plus/icons-vue'
import { ElButton } from 'element-plus'

withDefaults(
  defineProps<{
    state: 'loading' | 'empty' | 'error' | 'unauthorized' | 'unavailable' | 'unsupported'
    title?: string
    detail?: string
    retryable?: boolean
  }>(),
  { title: '', detail: '', retryable: false },
)

defineEmits<{ retry: [] }>()
</script>

<template>
  <section
    class="state-panel"
    :aria-busy="state === 'loading'"
    :aria-live="state === 'error' ? 'assertive' : 'polite'"
  >
    <div
      v-if="state === 'loading'"
      class="state-panel__loading"
      aria-label="Loading"
    >
      <span
        v-for="index in 3"
        :key="index"
        class="state-panel__skeleton"
      />
    </div>
    <template v-else>
      <WarningFilled
        class="state-panel__icon"
        aria-hidden="true"
      />
      <h2>{{ title || (state === 'empty' ? 'No facts in this scope' : 'This view is not available') }}</h2>
      <p>{{ detail }}</p>
      <ElButton
        v-if="retryable"
        :icon="Refresh"
        @click="$emit('retry')"
      >
        Retry request
      </ElButton>
    </template>
  </section>
</template>

<style scoped>
.state-panel { min-height: 15rem; display: grid; place-content: center; justify-items: center; gap: var(--space-3); padding: var(--space-8); text-align: center; color: var(--text-secondary); }
.state-panel h2 { margin: 0; font-size: var(--text-lg); color: var(--text-primary); }
.state-panel p { max-width: 34rem; margin: 0; }
.state-panel__icon { width: 2rem; color: var(--state-hold); }
.state-panel__loading { width: min(48rem, 80vw); display: grid; gap: var(--space-3); }
.state-panel__skeleton { height: 3rem; border: 1px solid var(--border-subtle); background: linear-gradient(90deg, var(--surface-muted), var(--surface-panel), var(--surface-muted)); background-size: 200% 100%; animation: pulse 1.4s ease-in-out infinite; }
@keyframes pulse { from { background-position: 100% 0; } to { background-position: -100% 0; } }
</style>
