<script setup lang="ts">
import { computed } from 'vue'
import { ElDrawer } from 'element-plus'
import { displayText, formatTime } from '@/format'
import StatusMark from './StatusMark.vue'
import StatePanel from './StatePanel.vue'

const props = defineProps<{
  open: boolean
  title: string
  item: Record<string, unknown> | null
  loading?: boolean
  error?: string
}>()

defineEmits<{ 'update:open': [value: boolean] }>()

const entries = computed(() => Object.entries(props.item ?? {}).slice(0, 64))

function isStatusField(field: string): boolean {
  return /(status|state|quality|decision|risk|lifecycle|gap|revoked|disabled|ready|loaded)$/.test(field)
}

function isTimeField(field: string): boolean {
  return /(_at_unix_ms|_time_unix_ms|deadline_unix_ms|expires_at_unix_ms)$/.test(field)
}

function label(field: string): string {
  return field.replaceAll('_', ' ').replace(/^./, (character) => character.toUpperCase())
}
</script>

<template>
  <ElDrawer
    :model-value="open"
    :title="title"
    size="min(34rem, 92vw)"
    destroy-on-close
    @update:model-value="$emit('update:open', $event)"
  >
    <StatePanel
      v-if="loading"
      state="loading"
      detail="Loading the exact authorized detail boundary."
    />
    <StatePanel
      v-else-if="error"
      state="unsupported"
      title="Detail unavailable"
      :detail="error"
    />
    <dl
      v-else-if="item"
      class="facts"
    >
      <div
        v-for="[field, value] in entries"
        :key="field"
        class="fact"
      >
        <dt>{{ label(field) }}</dt>
        <dd>
          <StatusMark
            v-if="isStatusField(field)"
            :value="value"
          />
          <time v-else-if="isTimeField(field)">{{ formatTime(value) }}</time>
          <span
            v-else-if="typeof value !== 'object' || value === null"
            :class="{ mono: /(id|digest|cursor|generation)$/.test(field) }"
          >{{ displayText(value) }}</span>
          <ul
            v-else-if="Array.isArray(value)"
            class="bounded-list"
          >
            <li
              v-for="(entry, index) in value.slice(0, 64)"
              :key="index"
            >
              <span v-if="typeof entry !== 'object' || entry === null">{{ displayText(entry) }}</span>
              <dl
                v-else
                class="nested-facts"
              >
                <div
                  v-for="[nestedField, nestedValue] in Object.entries(entry).slice(0, 16)"
                  :key="nestedField"
                >
                  <dt>{{ label(nestedField) }}</dt><dd>{{ displayText(nestedValue) }}</dd>
                </div>
              </dl>
            </li>
          </ul>
          <dl
            v-else
            class="nested-facts"
          >
            <div
              v-for="[nestedField, nestedValue] in Object.entries(value).slice(0, 32)"
              :key="nestedField"
            >
              <dt>{{ label(nestedField) }}</dt><dd>{{ displayText(nestedValue) }}</dd>
            </div>
          </dl>
        </dd>
      </div>
    </dl>
  </ElDrawer>
</template>

<style scoped>
.facts { margin: 0; display: grid; }.fact { display: grid; grid-template-columns: minmax(9rem, 0.38fr) 1fr; gap: var(--space-4); padding: var(--space-3) 0; border-bottom: 1px solid var(--border-subtle); }.fact > dt { color: var(--text-muted); font-size: var(--text-sm); }.fact > dd { min-width: 0; margin: 0; overflow-wrap: anywhere; }
.bounded-list { margin: 0; padding-left: var(--space-5); display: grid; gap: var(--space-3); }.nested-facts { margin: 0; display: grid; gap: var(--space-2); }.nested-facts > div { display: grid; grid-template-columns: minmax(7rem, 0.4fr) 1fr; gap: var(--space-2); }.nested-facts dt { color: var(--text-muted); font-size: var(--text-xs); }.nested-facts dd { margin: 0; overflow-wrap: anywhere; }
@media (max-width: 32rem) { .fact { grid-template-columns: 1fr; gap: var(--space-1); } }
</style>
