<script setup lang="ts">
import type { ResourceColumn } from '@/api/resources'
import { abbreviateDigest, displayText, formatNumber, formatTime } from '@/format'
import StatusMark from './StatusMark.vue'

defineProps<{
  columns: ResourceColumn[]
  items: Array<Record<string, unknown>>
  identityField: string
}>()

defineEmits<{ select: [item: Record<string, unknown>] }>()

function formatted(value: unknown, kind: ResourceColumn['kind']): string {
  if (kind === 'time') return formatTime(value)
  if (kind === 'number') return formatNumber(value)
  if (kind === 'digest') return abbreviateDigest(value)
  return displayText(value)
}
</script>

<template>
  <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th
            v-for="column in columns"
            :key="column.field"
            scope="col"
          >
            {{ column.label }}
          </th><th scope="col">
            <span class="sr-only">Row action</span>
          </th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in items"
          :key="String(item[identityField])"
        >
          <td
            v-for="column in columns"
            :key="column.field"
            :class="{ mono: column.kind === 'identity' || column.kind === 'digest', numeric: column.kind === 'number' }"
          >
            <StatusMark
              v-if="column.kind === 'status'"
              :value="item[column.field]"
              compact
            />
            <span
              v-else
              :title="column.kind === 'digest' ? String(item[column.field] ?? '') : undefined"
            >{{ formatted(item[column.field], column.kind) }}</span>
          </td>
          <td class="row-action">
            <button
              type="button"
              @click="$emit('select', item)"
            >
              Inspect<span class="sr-only"> {{ item[identityField] }}</span>
            </button>
            <slot
              name="row-actions"
              :item="item"
            />
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.table-scroll { max-width: 100%; overflow: auto; border: 1px solid var(--border-subtle); background: var(--surface-panel); box-shadow: var(--shadow-panel); }
table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); } th, td { height: var(--row-height); padding: var(--space-2) var(--space-3); border-bottom: 1px solid var(--border-subtle); text-align: left; white-space: nowrap; } th { position: sticky; top: 0; z-index: 1; background: var(--surface-muted); color: var(--text-secondary); font-size: var(--text-xs); letter-spacing: 0.02em; } tbody tr:hover { background: color-mix(in srgb, var(--surface-selected) 45%, transparent); } tbody tr:last-child td { border-bottom: 0; }.numeric { text-align: right; font-variant-numeric: tabular-nums; }.row-action { display: flex; justify-content: flex-end; gap: var(--space-2); text-align: right; }.row-action button, .row-action :deep(button) { min-height: 2rem; border: 0; background: transparent; color: var(--action-primary); font-weight: 700; cursor: pointer; }.row-action button:hover, .row-action :deep(button:hover) { text-decoration: underline; }
</style>
