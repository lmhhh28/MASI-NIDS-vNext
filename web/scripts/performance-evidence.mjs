export function finiteValues(values) {
  return values.filter((value) => typeof value === 'number' && Number.isFinite(value))
}

export function percentile(values, ratio) {
  if (!(ratio > 0 && ratio <= 1)) throw new RangeError('percentile ratio must be in (0, 1]')
  const sorted = finiteValues(values).sort((left, right) => left - right)
  if (sorted.length === 0) return null
  return sorted[Math.max(0, Math.ceil(sorted.length * ratio) - 1)]
}

export function maximum(values) {
  const measured = finiteValues(values)
  return measured.length === 0 ? null : Math.max(...measured)
}

export function summarizeMeasurement(values, expectedSamples) {
  const sampleCount = finiteValues(values).length
  return {
    status: sampleCount === expectedSamples
      ? 'MEASURED'
      : (sampleCount === 0 ? 'NOT_MEASURED' : 'PARTIAL'),
    sample_count: sampleCount,
    expected_sample_count: expectedSamples,
  }
}

export function thresholdExceeded(value, maximumValue) {
  return typeof value === 'number' && Number.isFinite(value) && value > maximumValue
}

export function classifyPerformance(failures, thresholdFailures, measurementHolds) {
  if (failures.length > 0 || thresholdFailures.length > 0) return 'FAIL'
  if (measurementHolds.length > 0) return 'HOLD'
  return 'PASS'
}
