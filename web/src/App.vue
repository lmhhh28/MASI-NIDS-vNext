<script setup lang="ts">
import { watch } from 'vue'
import { useQueryClient } from '@tanstack/vue-query'
import AppShell from '@/components/AppShell.vue'
import StatePanel from '@/components/StatePanel.vue'
import { useSessionQuery } from '@/api/session'
import { invalidationStream } from '@/api/sse'
import { bindSessionContext, clearSessionContext } from '@/api/context'

const queryClient = useQueryClient()
const session = useSessionQuery()

watch(
  () => session.data.value,
  (value) => {
    if (value !== undefined) {
      if (bindSessionContext(value, queryClient)) invalidationStream.stop()
      invalidationStream.start(queryClient)
    } else {
      invalidationStream.stop()
      clearSessionContext()
    }
  },
  { immediate: true },
)
</script>

<template>
  <StatePanel
    v-if="session.isPending.value"
    state="loading"
    detail="Establishing the bounded same-origin session."
  />
  <section
    v-else-if="session.isError.value"
    class="login-boundary"
  >
    <div
      class="login-boundary__mark"
      aria-hidden="true"
    >
      <i /><i /><i />
    </div>
    <p class="login-boundary__eyebrow">
      MASI NIDS · SOC operations
    </p>
    <h1>Session required</h1>
    <p>The browser holds no token. Continue through the trusted same-origin identity flow to open your authorized scopes.</p>
    <a
      class="login-boundary__action"
      href="/oidc/login"
    >Continue with identity provider</a>
  </section>
  <AppShell
    v-else-if="session.data.value"
    :session="session.data.value"
  />
</template>

<style scoped>
.login-boundary { min-height: 100vh; display: grid; place-content: center; justify-items: start; gap: var(--space-3); max-width: 38rem; margin: auto; padding: var(--space-8); }
.login-boundary__mark { width: 3rem; height: 3rem; display: flex; align-items: flex-end; gap: 0.25rem; padding: 0.55rem; border: 1px solid var(--border-strong); background: var(--surface-sidebar); }
.login-boundary__mark i { width: 0.4rem; background: #65cee5; }.login-boundary__mark i:nth-child(1) { height: 42%; }.login-boundary__mark i:nth-child(2) { height: 90%; }.login-boundary__mark i:nth-child(3) { height: 65%; }
.login-boundary__eyebrow { margin: var(--space-3) 0 0; color: var(--action-primary); font-size: var(--text-xs); font-weight: 750; letter-spacing: 0.1em; text-transform: uppercase; }
h1 { margin: 0; font-size: clamp(2rem, 7vw, 4rem); line-height: 1; letter-spacing: -0.04em; }.login-boundary p:not(.login-boundary__eyebrow) { margin: 0; color: var(--text-secondary); font-size: var(--text-lg); }
.login-boundary__action { margin-top: var(--space-3); display: inline-flex; min-height: 2.75rem; align-items: center; padding: 0 var(--space-4); border-radius: var(--radius-sm); background: var(--action-primary); color: var(--action-on-primary); font-weight: 700; text-decoration: none; }
</style>
