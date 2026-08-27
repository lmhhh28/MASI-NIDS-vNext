<script setup lang="ts">
import {
  Aim,
  Bell,
  Box,
  Camera,
  Connection,
  Cpu,
  DataAnalysis,
  Document,
  Histogram,
  Lock,
  MagicStick,
  Memo,
  Monitor,
  Operation,
  SetUp,
  Stamp,
  Tickets,
} from '@element-plus/icons-vue'

const props = defineProps<{ name: string }>()

const icons = {
  overview: DataAnalysis,
  events: Bell,
  incidents: Tickets,
  evidence: Document,
  captures: Camera,
  response: SetUp,
  firewall: Lock,
  approvals: Stamp,
  operations: Operation,
  rules: Aim,
  analysis: MagicStick,
  plugins: Box,
  statistics: Histogram,
  targets: Monitor,
  fleet: Connection,
  models: Cpu,
  audit: Memo,
} as const

const icon = icons[props.name as keyof typeof icons] ?? Document
</script>

<template>
  <component
    :is="icon"
    aria-hidden="true"
  />
</template>
