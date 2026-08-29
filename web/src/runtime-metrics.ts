let chartInstances = 0
let applicationTimers = 0

export function adjustChartInstances(delta: 1 | -1): void {
  chartInstances = Math.max(0, chartInstances + delta)
}

export function adjustApplicationTimers(delta: 1 | -1): void {
  applicationTimers = Math.max(0, applicationTimers + delta)
}

export function runtimeResourceMetrics(): { chart_instances: number; application_timers: number } {
  return { chart_instances: chartInstances, application_timers: applicationTimers }
}
