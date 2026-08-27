<script setup lang="ts">
defineProps<{ eyebrow: string; title: string; description: string }>()
</script>

<template>
  <header class="page-heading">
    <div>
      <p class="page-heading__eyebrow">
        {{ eyebrow }}
      </p>
      <h1>{{ title }}</h1>
      <p class="page-heading__description">
        {{ description }}
      </p>
    </div>
    <div class="page-heading__actions">
      <slot />
    </div>
  </header>
</template>

<style scoped>
.page-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-6); padding-bottom: var(--space-5); border-bottom: 1px solid var(--border-subtle); }
.page-heading__eyebrow { margin: 0 0 var(--space-1); color: var(--action-primary); font-size: var(--text-xs); font-weight: 750; letter-spacing: 0.08em; text-transform: uppercase; }
h1 { margin: 0; font-size: clamp(1.5rem, 3vw, var(--text-2xl)); line-height: var(--line-tight); letter-spacing: -0.025em; }
.page-heading__description { max-width: 52rem; margin: var(--space-2) 0 0; color: var(--text-secondary); }
.page-heading__actions { display: flex; gap: var(--space-2); flex: 0 0 auto; }
@media (max-width: 48rem) { .page-heading { flex-direction: column; } .page-heading__actions { width: 100%; } }
</style>
