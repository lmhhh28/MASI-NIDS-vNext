<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { ElButton } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { fetchResource, fetchResourceDetail } from '@/api/resources'
import { fetchStatisticsArtifact } from '@/api/statistics'
import { formatTime } from '@/format'
import PageHeading from '@/components/PageHeading.vue'
import PluginArtifactRenderer from '@/components/PluginArtifactRenderer.vue'
import StatePanel from '@/components/StatePanel.vue'
import StatusMark from '@/components/StatusMark.vue'
import { sessionBoundKey } from '@/api/context'

const route = useRoute()
const router = useRouter()
const selectedArtifactID = computed(() => typeof route.query.artifact === 'string' ? route.query.artifact : '')

const definitions = useQuery({ queryKey: sessionBoundKey('resource', 'statistics-definitions', '', 50), queryFn: () => fetchResource('statistics-definitions') })
const current = useQuery({ queryKey: sessionBoundKey('resource', 'statistics-current', '', 50), queryFn: () => fetchResource('statistics-current') })
const runs = useQuery({ queryKey: sessionBoundKey('resource', 'statistics-runs', '', 50), queryFn: () => fetchResource('statistics-runs') })
const schedules = useQuery({ queryKey: sessionBoundKey('resource', 'statistics-schedules', '', 50), queryFn: () => fetchResource('statistics-schedules') })
const artifact = useQuery({
  queryKey: sessionBoundKey('statistics-artifact', selectedArtifactID),
  queryFn: () => fetchStatisticsArtifact(selectedArtifactID.value),
  enabled: computed(() => selectedArtifactID.value !== ''),
})

const pluginID = computed(() => typeof route.params.pluginId === 'string' ? route.params.pluginId : '')
const visibleDefinitions = computed(() => (definitions.data.value?.items ?? []).filter((definition) => pluginID.value === '' || definition.plugin_id === pluginID.value))
const visibleDefinitionIDs = computed(() => new Set(visibleDefinitions.value.map((definition) => String(definition.definition_id))))
const visibleCurrent = computed(() => (current.data.value?.items ?? []).filter((entry) => pluginID.value === '' || visibleDefinitionIDs.value.has(String(entry.definition_id))))
const selectedCurrent = computed(() => visibleCurrent.value.find(
  (entry) => entry.artifact_id === selectedArtifactID.value,
) ?? null)
const selectedDefinitionID = computed(() => {
  if (typeof route.query.definition === 'string') return route.query.definition
  if (typeof selectedCurrent.value?.definition_id === 'string') return selectedCurrent.value.definition_id
  return artifact.data.value?.definition_id ?? ''
})

const selectedDefinitionFromPage = computed(() => visibleDefinitions.value.find(
  (item) => item.definition_id === selectedDefinitionID.value,
) ?? null)
const definitionDetail = useQuery({
  queryKey: sessionBoundKey('resource-detail', 'statistics-definitions', selectedDefinitionID),
  queryFn: () => fetchResourceDetail('statistics-definitions', selectedDefinitionID.value),
  enabled: computed(() =>
    selectedDefinitionID.value !== ''
    && definitions.isSuccess.value
    && selectedDefinitionFromPage.value === null,
  ),
})
const selectedDefinition = computed(() => selectedDefinitionFromPage.value ?? definitionDetail.data.value ?? null)

function displayHint(definitionID: string): string {
  const definition = visibleDefinitions.value.find((item) => item.definition_id === definitionID)
  return typeof definition?.display_hint === 'string' ? definition.display_hint : ''
}
const selectedDisplayHint = computed(() => typeof selectedDefinition.value?.display_hint === 'string' ? selectedDefinition.value.display_hint : '')
const artifactContractError = computed(() => {
  const value = artifact.data.value
  const definition = selectedDefinition.value
  if (!value || !definition) return ''
  if (value.definition_id !== selectedDefinitionID.value || definition.definition_id !== selectedDefinitionID.value) {
    return 'The artifact and exact definition identities do not match.'
  }
  if (value.definition_digest !== definition.definition_digest) {
    return 'The artifact definition digest does not match the qualified definition.'
  }
  if (value.provenance.plugin_id !== definition.plugin_id
      || value.provenance.binding_generation !== definition.binding_generation) {
    return 'The artifact provenance does not match the qualified plugin binding.'
  }
  if (pluginID.value !== '' && definition.plugin_id !== pluginID.value) {
    return 'The artifact definition is outside this plugin route.'
  }
  return ''
})

function openArtifact(item: Record<string, unknown>): void {
  const artifactID = typeof item.artifact_id === 'string' ? item.artifact_id : ''
  const definitionID = typeof item.definition_id === 'string' ? item.definition_id : ''
  if (!artifactID || !definitionID) return
  void router.push({ query: { ...route.query, artifact: artifactID, definition: definitionID } })
}

function refreshAll(): void {
  void definitions.refetch(); void current.refetch(); void runs.refetch(); void schedules.refetch()
  if (selectedArtifactID.value) void artifact.refetch()
  if (selectedDefinitionID.value && selectedDefinitionFromPage.value === null) void definitionDetail.refetch()
}
</script>

<template>
  <div class="statistics-page">
    <PageHeading
      eyebrow="Plugins"
      :title="pluginID ? `Statistics · ${pluginID}` : 'Plugin statistics'"
      description="Go-validated current/history projections rendered only by the platform's eight built-in display kinds."
    >
      <ElButton
        :icon="Refresh"
        :loading="definitions.isFetching.value || current.isFetching.value"
        @click="refreshAll"
      >
        Refresh
      </ElButton>
    </PageHeading>

    <aside
      class="renderer-policy"
      role="note"
    >
      <strong>Host-owned display boundary</strong>
      No plugin route, component, HTML, SVG, CSS, URL, JavaScript, ECharts option, Vega expression, or raw JSON fallback is executed here.
    </aside>

    <StatePanel
      v-if="definitions.isPending.value || current.isPending.value"
      state="loading"
      detail="Loading bounded definitions and current artifact identities."
    />
    <StatePanel
      v-else-if="definitions.isError.value || current.isError.value"
      state="error"
      title="Statistics projection unavailable"
      detail="The core application remains available; plugin statistics failed closed."
      retryable
      @retry="refreshAll"
    />
    <template v-else>
      <div class="statistics-layout">
        <section class="definition-panel">
          <header><div><p>Current</p><h2>Validated artifacts</h2></div><span>{{ visibleCurrent.length }} shown</span></header>
          <ul v-if="visibleCurrent.length">
            <li
              v-for="item in visibleCurrent"
              :key="String(item.definition_id)"
            >
              <button
                type="button"
                :aria-current="selectedArtifactID === item.artifact_id ? 'true' : undefined"
                @click="openArtifact(item)"
              >
                <span><b class="mono">{{ item.definition_id }}</b><small>{{ displayHint(String(item.definition_id)) || 'unsupported display' }}</small></span>
                <StatusMark
                  :value="item.quality"
                  compact
                />
                <small>{{ formatTime(item.updated_at_unix_ms) }}</small>
              </button>
            </li>
          </ul>
          <p
            v-else
            class="empty"
          >
            No current artifact identity in this scope. No value has been inferred.
          </p>
        </section>

        <section
          class="renderer-panel"
          aria-live="polite"
        >
          <StatePanel
            v-if="selectedArtifactID === ''"
            state="empty"
            title="Select a validated artifact"
            detail="The renderer loads one exact artifact on demand, keeping requests and memory bounded."
          />
          <StatePanel
            v-else-if="artifact.isPending.value"
            state="loading"
            detail="Validating the exact artifact envelope."
          />
          <StatePanel
            v-else-if="artifact.isError.value"
            state="unsupported"
            title="Artifact rejected"
            :detail="artifact.error.value instanceof Error ? artifact.error.value.message : 'Artifact validation failed.'"
            retryable
            @retry="artifact.refetch()"
          />
          <StatePanel
            v-else-if="definitionDetail.isFetching.value"
            state="loading"
            detail="Loading the exact immutable statistics definition."
          />
          <StatePanel
            v-else-if="definitionDetail.isError.value || !selectedDefinition"
            state="unsupported"
            title="Definition unavailable"
            detail="The exact qualified display definition is required; the artifact is not rendered without it."
          />
          <StatePanel
            v-else-if="artifactContractError"
            state="unsupported"
            title="Artifact binding rejected"
            :detail="artifactContractError"
          />
          <PluginArtifactRenderer
            v-else-if="artifact.data.value"
            :artifact="artifact.data.value"
            :display-hint="selectedDisplayHint"
          />
        </section>
      </div>

      <section class="ledger">
        <header><div><p>Durable ledger</p><h2>Recent runs and immutable schedules</h2></div><span>Definition {{ selectedDefinitionID || 'not selected' }}</span></header>
        <div class="ledger__columns">
          <div>
            <h3>Runs</h3><ul>
              <li
                v-for="run in (runs.data.value?.items ?? []).slice(0, 8)"
                :key="String(run.run_id)"
              >
                <span class="mono">{{ run.run_id }}</span><StatusMark
                  :value="run.status"
                  compact
                /><small>{{ run.definition_id }}</small>
              </li>
            </ul><p v-if="!runs.data.value?.items.length">
              No durable runs.
            </p>
          </div>
          <div>
            <h3>Schedules</h3><ul>
              <li
                v-for="schedule in (schedules.data.value?.items ?? []).slice(0, 8)"
                :key="String(schedule.schedule_id)"
              >
                <span class="mono">{{ schedule.schedule_id }}</span><StatusMark
                  :value="schedule.disabled ? 'disabled' : 'active'"
                  compact
                /><small>rev {{ schedule.schedule_revision }} · {{ schedule.interval_seconds }}s</small>
              </li>
            </ul><p v-if="!schedules.data.value?.items.length">
              No immutable schedule revisions.
            </p>
          </div>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.statistics-page { display: grid; gap: var(--space-5); }.renderer-policy { padding: var(--space-3) var(--space-4); border-left: 3px solid var(--state-hold); background: var(--state-hold-surface); color: var(--text-secondary); }.renderer-policy strong { display: block; color: var(--text-primary); }.statistics-layout { min-width: 0; display: grid; grid-template-columns: 20rem minmax(0, 1fr); gap: var(--space-5); }.definition-panel, .ledger { border: 1px solid var(--border-subtle); background: var(--surface-panel); }.definition-panel > header, .ledger > header { min-height: 4.25rem; display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); padding: var(--space-3) var(--space-4); border-bottom: 1px solid var(--border-subtle); }.definition-panel header p, .ledger header p { margin: 0; color: var(--action-primary); font-size: var(--text-xs); font-weight: 750; text-transform: uppercase; letter-spacing: .07em; }.definition-panel h2, .ledger h2 { margin: 0; font-size: var(--text-lg); }.definition-panel header span, .ledger header span { color: var(--text-muted); font-size: var(--text-xs); }.definition-panel ul { margin: 0; padding: 0; list-style: none; }.definition-panel li { border-bottom: 1px solid var(--border-subtle); }.definition-panel button { width: 100%; display: grid; grid-template-columns: 1fr auto; gap: var(--space-2); padding: var(--space-3); border: 0; border-left: 3px solid transparent; background: transparent; color: var(--text-primary); text-align: left; cursor: pointer; }.definition-panel button:hover { background: var(--surface-muted); }.definition-panel button[aria-current='true'] { border-left-color: var(--action-primary); background: var(--surface-selected); }.definition-panel button > span { min-width: 0; display: grid; }.definition-panel button small { color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.definition-panel button > small { grid-column: 1 / -1; }.empty { padding: var(--space-5); color: var(--text-muted); }.renderer-panel { min-width: 0; }.ledger__columns { display: grid; grid-template-columns: 1fr 1fr; }.ledger__columns > div { padding: var(--space-4); border-right: 1px solid var(--border-subtle); }.ledger__columns > div:last-child { border: 0; }.ledger h3 { margin: 0 0 var(--space-3); }.ledger ul { margin: 0; padding: 0; list-style: none; }.ledger li { display: grid; grid-template-columns: 1fr auto; gap: var(--space-2); padding: var(--space-2) 0; border-bottom: 1px solid var(--border-subtle); }.ledger li small { grid-column: 1 / -1; color: var(--text-muted); }
@media (max-width: 70rem) { .statistics-layout { grid-template-columns: 1fr; }.definition-panel ul { display: grid; grid-template-columns: repeat(2, 1fr); }.definition-panel li:nth-child(odd) { border-right: 1px solid var(--border-subtle); } }
@media (max-width: 42rem) { .definition-panel ul, .ledger__columns { grid-template-columns: 1fr; }.definition-panel li:nth-child(odd), .ledger__columns > div { border-right: 0; }.ledger__columns > div { border-bottom: 1px solid var(--border-subtle); } }
</style>
