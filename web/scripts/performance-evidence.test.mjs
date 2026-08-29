import assert from 'node:assert/strict'
import test from 'node:test'
import {
  classifyPerformance,
  maximum,
  percentile,
  summarizeMeasurement,
  thresholdExceeded,
} from './performance-evidence.mjs'

test('missing measurements remain null and cannot become PASS', () => {
  assert.equal(percentile([], 0.75), null)
  assert.equal(maximum([null, undefined, Number.NaN]), null)
  const summary = summarizeMeasurement([null, undefined], 2)
  assert.deepEqual(summary, { status: 'NOT_MEASURED', sample_count: 0, expected_sample_count: 2 })
  assert.equal(classifyPerformance([], [], ['LCP_NOT_MEASURED']), 'HOLD')
})

test('a measured zero is preserved as valid evidence', () => {
  assert.equal(percentile([0, 0, 0], 0.75), 0)
  assert.equal(maximum([0, 0]), 0)
  assert.deepEqual(summarizeMeasurement([0, 0], 2), {
    status: 'MEASURED', sample_count: 2, expected_sample_count: 2,
  })
  assert.equal(classifyPerformance([], [], []), 'PASS')
})

test('threshold violations outrank environmental holds', () => {
  assert.equal(thresholdExceeded(null, 1), false)
  assert.equal(thresholdExceeded(2, 1), true)
  assert.equal(classifyPerformance([], ['INP'], ['BROWSER_MISMATCH']), 'FAIL')
})
