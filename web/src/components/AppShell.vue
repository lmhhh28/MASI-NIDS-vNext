<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useEventListener, useMediaQuery } from '@vueuse/core'
import { ElButton, ElTooltip } from 'element-plus'
import { Fold, Expand, Moon, Sunny, Operation } from '@element-plus/icons-vue'
import type { Session } from '@masi/control-api'
import NavIcon from './NavIcon.vue'
import StatusMark from './StatusMark.vue'
import { useUiStore } from '@/stores/ui'
import { streamState } from '@/api/sse'

defineProps<{ session: Session }>()

const route = useRoute()
const ui = useUiStore()
const isNarrow = useMediaQuery('(max-width: 48rem)')
const navigationHidden = computed(() => isNarrow.value && !ui.mobileNavigationOpen)
useEventListener(document, 'keydown', (event) => {
  if (event.key === 'Escape' && ui.mobileNavigationOpen) ui.mobileNavigationOpen = false
})

const groups = [
  { label: 'Overview', items: [{ to: '/overview', label: 'Overview', icon: 'overview' }] },
  { label: 'Detection', items: [{ to: '/detection/events', label: 'Events', icon: 'events' }, { to: '/detection/incidents', label: 'Incidents', icon: 'incidents' }] },
  { label: 'Evidence', items: [{ to: '/evidence', label: 'Evidence register', icon: 'evidence' }, { to: '/evidence/captures', label: 'Bounded captures', icon: 'captures' }] },
  { label: 'Effects & governance', items: [{ to: '/governance/response-rules', label: 'Response rules', icon: 'response' }, { to: '/governance/firewall-policies', label: 'Firewall policies', icon: 'firewall' }, { to: '/governance/approvals', label: 'Approvals', icon: 'approvals' }, { to: '/governance/operations', label: 'Operations', icon: 'operations' }, { to: '/governance/rule-effectiveness', label: 'Rule effectiveness', icon: 'rules' }] },
  { label: 'Analysis', items: [{ to: '/analysis', label: 'Agent tasks', icon: 'analysis' }] },
  { label: 'Plugins', items: [{ to: '/plugins/catalog', label: 'Catalog', icon: 'plugins' }, { to: '/plugins/statistics', label: 'Statistics', icon: 'statistics' }] },
  { label: 'Operations & audit', items: [{ to: '/operations/targets', label: 'Managed targets', icon: 'targets' }, { to: '/operations/fleet', label: 'Fleet operations', icon: 'fleet' }, { to: '/operations/models', label: 'Models', icon: 'models' }, { to: '/operations/audit', label: 'Audit trail', icon: 'audit' }] },
] as const

const streamLabel = computed(() => ({ live: 'live', connecting: 'connecting', retrying: 'retrying', polling: 'polling', stopped: 'stopped' })[streamState.mode])
</script>

<template>
  <div
    class="shell"
    :class="{ 'shell--collapsed': ui.sidebarCollapsed }"
  >
    <aside
      id="primary-navigation"
      class="sidebar"
      :class="{ 'sidebar--mobile-open': ui.mobileNavigationOpen }"
      aria-label="Primary"
      :aria-hidden="navigationHidden ? 'true' : undefined"
      :inert="navigationHidden"
    >
      <div class="brand">
        <span
          class="brand__mark"
          aria-hidden="true"
        ><i /><i /><i /></span>
        <span
          v-if="!ui.sidebarCollapsed"
          class="brand__word"
        ><strong>MASI</strong><small>Network defense</small></span>
      </div>
      <nav class="navigation">
        <section
          v-for="group in groups"
          :key="group.label"
          class="navigation__group"
        >
          <h2 v-if="!ui.sidebarCollapsed">
            {{ group.label }}
          </h2>
          <RouterLink
            v-for="item in group.items"
            :key="item.to"
            :to="item.to"
            :aria-label="ui.sidebarCollapsed ? item.label : undefined"
            @click="ui.mobileNavigationOpen = false"
          >
            <NavIcon :name="item.icon" />
            <span v-if="!ui.sidebarCollapsed">{{ item.label }}</span>
          </RouterLink>
        </section>
      </nav>
      <ElTooltip
        :content="ui.sidebarCollapsed ? 'Expand navigation' : 'Collapse navigation'"
        placement="right"
      >
        <ElButton
          class="sidebar__toggle"
          text
          :icon="ui.sidebarCollapsed ? Expand : Fold"
          :aria-label="ui.sidebarCollapsed ? 'Expand navigation' : 'Collapse navigation'"
          @click="ui.sidebarCollapsed = !ui.sidebarCollapsed"
        />
      </ElTooltip>
    </aside>

    <header class="topbar">
      <ElButton
        class="mobile-menu"
        text
        :icon="Operation"
        :aria-label="ui.mobileNavigationOpen ? 'Close navigation' : 'Open navigation'"
        aria-controls="primary-navigation"
        :aria-expanded="ui.mobileNavigationOpen"
        @click="ui.mobileNavigationOpen = !ui.mobileNavigationOpen"
      />
      <div class="topbar__route">
        <span>{{ route.meta.navigationGroup ?? 'MASI NIDS' }}</span><strong>{{ route.meta.title }}</strong>
      </div>
      <div class="topbar__context">
        <StatusMark
          :value="streamLabel"
          compact
        />
        <span class="topbar__actor mono">{{ session.actor_ref }}</span>
        <ElTooltip :content="ui.theme === 'dark' ? 'Use light theme' : 'Use dark theme'">
          <ElButton
            text
            :icon="ui.theme === 'dark' ? Sunny : Moon"
            :aria-label="ui.theme === 'dark' ? 'Use light theme' : 'Use dark theme'"
            @click="ui.toggleTheme"
          />
        </ElTooltip>
      </div>
    </header>

    <main
      id="main-content"
      class="content"
      tabindex="-1"
    >
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
.shell { min-height: 100vh; display: grid; grid-template: var(--topbar-height) 1fr / var(--sidebar-width) 1fr; }
.shell--collapsed { grid-template-columns: 4.25rem 1fr; }
.sidebar { position: fixed; inset: 0 auto 0 0; z-index: 50; width: var(--sidebar-width); display: flex; flex-direction: column; overflow: hidden; background: var(--surface-sidebar); color: var(--text-inverse); transition: width var(--motion-standard) ease; }
.shell--collapsed .sidebar { width: 4.25rem; }
.brand { height: var(--topbar-height); display: flex; align-items: center; gap: var(--space-3); padding: 0 var(--space-4); border-bottom: 1px solid rgb(255 255 255 / 10%); }
.brand__mark { width: 2rem; height: 2rem; display: flex; align-items: flex-end; gap: 0.18rem; padding: 0.35rem; border: 1px solid rgb(255 255 255 / 25%); }
.brand__mark i { width: 0.28rem; background: #65cee5; }
.brand__mark i:nth-child(1) { height: 42%; }.brand__mark i:nth-child(2) { height: 90%; }.brand__mark i:nth-child(3) { height: 65%; }
.brand__word { display: grid; line-height: 1.1; }.brand__word strong { letter-spacing: 0.14em; }.brand__word small { color: #9eb0be; font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.08em; }
.navigation { flex: 1; overflow-y: auto; padding: var(--space-3) var(--space-2); scrollbar-width: thin; }
.navigation__group { margin-bottom: var(--space-3); }.navigation__group h2 { margin: 0 var(--space-2) var(--space-1); color: #748997; font-size: 0.625rem; text-transform: uppercase; letter-spacing: 0.1em; }
.navigation a { min-height: 2.35rem; display: flex; align-items: center; gap: var(--space-3); padding: var(--space-2) var(--space-3); border-left: 2px solid transparent; color: #becbd4; text-decoration: none; font-size: var(--text-sm); }
.navigation a :deep(svg) { width: 1.05rem; flex: 0 0 auto; }.navigation a:hover { background: rgb(255 255 255 / 6%); color: #fff; }.navigation a.router-link-active { border-left-color: #5dc9e0; background: rgb(55 174 202 / 13%); color: #fff; }
.sidebar__toggle { margin: var(--space-2); color: #b9c7d0; }
.topbar { grid-column: 2; position: sticky; top: 0; z-index: 40; min-width: 0; display: flex; align-items: center; justify-content: space-between; gap: var(--space-4); padding: 0 var(--space-5); border-bottom: 1px solid var(--border-subtle); background: color-mix(in srgb, var(--surface-panel) 94%, transparent); backdrop-filter: blur(10px); }
.topbar__route { display: flex; align-items: baseline; gap: var(--space-2); min-width: 0; }.topbar__route span { color: var(--text-muted); font-size: var(--text-xs); }.topbar__route strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.topbar__context { display: flex; align-items: center; gap: var(--space-3); }.topbar__actor { max-width: 15rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-secondary); }
.content { grid-area: 2 / 2; min-width: 0; padding: var(--space-6); }
.mobile-menu { display: none; }
@media (max-width: 64rem) { .topbar__actor { display: none; } }
@media (max-width: 48rem) {
  .shell, .shell--collapsed { grid-template: var(--topbar-height) 1fr / 1fr; }
  .sidebar, .shell--collapsed .sidebar { width: min(18rem, 86vw); transform: translateX(-105%); box-shadow: 0 0 2rem rgb(0 0 0 / 35%); }
  .sidebar--mobile-open { transform: translateX(0); }
  .topbar { grid-column: 1; padding: 0 var(--space-3); }.mobile-menu { display: inline-flex; min-width: 44px; min-height: 44px; }.topbar__route span { display: none; }
  .content { grid-area: 2 / 1; padding: var(--space-4); }
}
</style>
