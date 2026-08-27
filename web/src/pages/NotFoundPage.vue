<script setup lang="ts">
import StatePanel from '@/components/StatePanel.vue'
</script>
<template>
  <StatePanel
    state="unsupported"
    title="Route not found"
    detail="This URL is not part of the qualified SOC information architecture."
  /><p class="return">
    <RouterLink to="/overview">
      Return to Overview
    </RouterLink>
  </p>
</template>
<style scoped>.return { text-align: center; }</style>
