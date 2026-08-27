import { readFileSync, writeFileSync } from 'node:fs'

function argument(name) {
  const index = process.argv.indexOf(name)
  if (index < 0 || !process.argv[index + 1]) throw new Error(`missing ${name}`)
  return process.argv[index + 1]
}

const inputs = argument('--inputs').split(',')
const evidencePath = argument('--evidence')
if (inputs.length !== 3) throw new Error('exactly three browser evidence files are required')
const documents = inputs.map((path) => JSON.parse(readFileSync(path, 'utf8')))
const first = documents[0]
const runs = documents.flatMap((document) => document.runs)
const failures = documents.flatMap((document) => document.failures)
const browsers = new Set(runs.map((run) => run.browser))
for (const document of documents) {
  if (document.schema_version !== 'web-control-pairwise-rehearsal/v1' || document.boundary !== first.boundary) throw new Error('browser evidence identity drift')
  if (JSON.stringify(document.participants) !== JSON.stringify(first.participants)) throw new Error('participant digest drift across browsers')
}
const result = {
  schema_version: 'web-control-pairwise-rehearsal/v1',
  boundary: first.boundary,
  started_at: documents.map((document) => document.started_at).sort()[0],
  finished_at: documents.map((document) => document.finished_at).sort().at(-1),
  participants: first.participants,
  real_boundaries: first.real_boundaries,
  runs,
  failures,
  result: failures.length === 0 && runs.length === 3 && browsers.size === 3 ? 'PASS' : 'FAIL',
  qualification: 'NOT_QUALIFIED',
  qualification_scope: first.qualification_scope,
}
writeFileSync(evidencePath, `${JSON.stringify(result, null, 2)}\n`, { encoding: 'utf8', flag: 'wx' })
process.stdout.write(`${JSON.stringify(result, null, 2)}\n`)
if (result.result !== 'PASS') process.exitCode = 1
