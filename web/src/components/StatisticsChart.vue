<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useResizeObserver } from '@vueuse/core'
import { init, use, type ECharts, type EChartsCoreOption } from 'echarts/core'
import { BarChart, HeatmapChart, LineChart } from 'echarts/charts'
import { AriaComponent, DatasetComponent, GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { ArtifactRecord } from '@masi/control-api'
import { adjustChartInstances } from '@/runtime-metrics'

use([LineChart, BarChart, HeatmapChart, AriaComponent, DatasetComponent, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

const props = defineProps<{
  artifact: ArtifactRecord
  kind: 'timeseries' | 'bar' | 'heatmap'
}>()

const container = ref<HTMLElement | null>(null)
let chart: ECharts | undefined

const option = computed<EChartsCoreOption>(() => {
  const base: Record<string, unknown> = {
    animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    aria: { enabled: true, decal: { show: true }, description: `Plugin statistics ${props.kind}; quality ${props.artifact.quality}.` },
    textStyle: { fontFamily: 'ui-sans-serif, system-ui, sans-serif' },
    grid: { left: 56, right: 24, top: 44, bottom: 48, containLabel: true },
    tooltip: { trigger: props.kind === 'heatmap' ? 'item' : 'axis' },
  }
  if (props.kind === 'bar') {
    return {
      ...base,
      dataset: { source: [['metric', 'value'], ...props.artifact.metrics.map((metric) => [metric.metric_id, metric.value])] },
      xAxis: { type: 'category', axisLabel: { interval: 0, rotate: props.artifact.metrics.length > 8 ? 30 : 0 } },
      yAxis: { type: 'value' },
      series: [{ type: 'bar', encode: { x: 'metric', y: 'value' }, itemStyle: { color: '#08789c' } }],
    }
  }
  if (props.kind === 'heatmap') {
    const source: Array<Array<string | number>> = [['point', 'series', 'value']]
    props.artifact.series.forEach((series, seriesIndex) => {
      series.points.slice(0, 2_000).forEach((point, pointIndex) => source.push([pointIndex, seriesIndex, point.value]))
    })
    return {
      ...base,
      dataset: { source },
      xAxis: { type: 'category', name: 'Point index' },
      yAxis: { type: 'category', data: props.artifact.series.map((series) => series.series_id), name: 'Series' },
      visualMap: { min: 0, max: Math.max(1, ...props.artifact.series.flatMap((series) => series.points.map((point) => point.value))), calculable: false, orient: 'horizontal', left: 'center', bottom: 0 },
      series: [{ type: 'heatmap', encode: { x: 'point', y: 'series', value: 'value' } }],
    }
  }
  const datasets = props.artifact.series.map((series) => ({
    id: series.series_id,
    source: [['time', 'value'], ...series.points.slice(0, 2_000).map((point) => [point.timestamp_unix_ms, point.value])],
  }))
  return {
    ...base,
    dataset: datasets,
    legend: { type: 'scroll', data: props.artifact.series.map((series) => series.series_id) },
    xAxis: { type: 'time', name: 'UTC' },
    yAxis: { type: 'value' },
    series: props.artifact.series.map((series) => ({
      name: series.series_id,
      type: 'line',
      datasetId: series.series_id,
      encode: { x: 'time', y: 'value' },
      showSymbol: false,
      sampling: 'lttb',
    })),
  }
})

function render(): void {
  if (!container.value) return
  if (!chart) {
    chart = init(container.value, undefined, { renderer: 'canvas' })
    adjustChartInstances(1)
  }
  chart.setOption(option.value, { notMerge: true })
}

onMounted(render)
watch(option, render)
useResizeObserver(container, () => chart?.resize())
onBeforeUnmount(() => {
  if (chart) {
    chart.dispose()
    chart = undefined
    adjustChartInstances(-1)
  }
})
</script>

<template>
  <div
    ref="container"
    class="statistics-chart"
    role="img"
    :aria-label="`${kind} chart for ${artifact.definition_id}; quality ${artifact.quality}`"
  />
</template>

<style scoped>.statistics-chart { width: 100%; min-height: 22rem; }</style>
