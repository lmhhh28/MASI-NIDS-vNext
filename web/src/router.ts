import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import type { ResourceKey } from '@/api/resources'

declare module 'vue-router' {
  interface RouteMeta {
    title: string
    description: string
    navigationGroup?: string
    resource?: ResourceKey
    dangerous?: boolean
  }
}

const ResourcePage = () => import('@/pages/ResourcePage.vue')

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/overview', meta: { title: 'Overview', description: 'System overview' } },
  { path: '/overview', component: () => import('@/pages/OverviewPage.vue'), meta: { title: 'Overview', description: 'A bounded, single-snapshot view of detection and operations.', navigationGroup: 'Overview' } },
  { path: '/detection/events', component: ResourcePage, meta: { title: 'Events', description: 'Canonical detection outcomes.', navigationGroup: 'Detection', resource: 'events' } },
  { path: '/detection/incidents', component: ResourcePage, meta: { title: 'Incidents', description: 'Cases awaiting triage.', navigationGroup: 'Detection', resource: 'incidents' } },
  { path: '/evidence', component: ResourcePage, meta: { title: 'Evidence register', description: 'Immutable evidence references.', navigationGroup: 'Evidence', resource: 'evidence' } },
  { path: '/evidence/captures', component: ResourcePage, meta: { title: 'Bounded captures', description: 'Governed, bounded packet evidence.', navigationGroup: 'Evidence', resource: 'captures' } },
  { path: '/governance/response-rules', component: () => import('@/pages/GovernancePage.vue'), props: { mode: 'proposals' }, meta: { title: 'Response rules', description: 'Propose time-bounded response overlays.', navigationGroup: 'Effects & governance', resource: 'proposals', dangerous: true } },
  { path: '/governance/firewall-policies', component: () => import('@/pages/GovernancePage.vue'), props: { mode: 'firewall' }, meta: { title: 'Firewall policies', description: 'Inspect and activate immutable baseline revisions.', navigationGroup: 'Effects & governance', resource: 'firewall-revisions', dangerous: true } },
  { path: '/governance/approvals', component: () => import('@/pages/GovernancePage.vue'), props: { mode: 'approvals' }, meta: { title: 'Approvals', description: 'Exact-context maker-checker authorization.', navigationGroup: 'Effects & governance', resource: 'proposals', dangerous: true } },
  { path: '/governance/operations', component: ResourcePage, meta: { title: 'Effect operations', description: 'Durable intent state and reconciliation.', navigationGroup: 'Effects & governance', resource: 'intents' } },
  { path: '/governance/rule-effectiveness', component: () => import('@/pages/RuleEffectivenessPage.vue'), meta: { title: 'Rule effectiveness', description: 'Installation, match, and outcome kept separate.', navigationGroup: 'Effects & governance', resource: 'rule-effectiveness' } },
  { path: '/analysis', component: () => import('@/pages/AnalysisPage.vue'), meta: { title: 'Analysis', description: 'Bounded non-executable agent tasks.', navigationGroup: 'Analysis', resource: 'analysis-tasks' } },
  { path: '/plugins/catalog', component: () => import('@/pages/PluginCatalogPage.vue'), meta: { title: 'Plugin catalog', description: 'Qualified exact plugin revisions.', navigationGroup: 'Plugins', resource: 'plugins', dangerous: true } },
  { path: '/plugins/statistics', component: () => import('@/pages/PluginStatisticsPage.vue'), meta: { title: 'Plugin statistics', description: 'Validated current and historical statistics.', navigationGroup: 'Plugins', resource: 'statistics-current' } },
  { path: '/plugins/:pluginId/statistics', component: () => import('@/pages/PluginStatisticsPage.vue'), meta: { title: 'Plugin statistics', description: 'Statistics for one exact plugin.', navigationGroup: 'Plugins', resource: 'statistics-current' } },
  { path: '/operations/targets', component: () => import('@/pages/OperationsPage.vue'), props: { mode: 'targets' }, meta: { title: 'Managed targets', description: 'Target lifecycle, assignment, and observation.', navigationGroup: 'Operations & audit', resource: 'targets', dangerous: true } },
  { path: '/operations/fleet', component: () => import('@/pages/OperationsPage.vue'), props: { mode: 'fleet' }, meta: { title: 'Fleet operations', description: 'Static waves and per-target child state.', navigationGroup: 'Operations & audit', resource: 'fleet', dangerous: true } },
  { path: '/operations/models', component: () => import('@/pages/ModelsPage.vue'), meta: { title: 'Model operations', description: 'Exact revisions, pools, bindings, and rollout groups.', navigationGroup: 'Operations & audit', resource: 'model-bindings', dangerous: true } },
  { path: '/operations/audit', component: ResourcePage, meta: { title: 'Audit trail', description: 'Evidence-backed governance facts.', navigationGroup: 'Operations & audit', resource: 'audit' } },
  { path: '/:pathMatch(.*)*', component: () => import('@/pages/NotFoundPage.vue'), meta: { title: 'Not found', description: 'The requested route is unavailable.' } },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.afterEach((route) => {
  document.title = `${route.meta.title} · MASI NIDS`
})
