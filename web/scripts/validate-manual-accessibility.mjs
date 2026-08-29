import { createHash } from 'node:crypto'
import { lstatSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import Ajv2020 from 'ajv/dist/2020.js'
import addFormats from 'ajv-formats'

function argument(name, fallback = '') {
  const index = process.argv.indexOf(name)
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback
}

const repo = resolve(import.meta.dirname, '../..')
const input = argument('--input')
const outputArgument = argument('--output')
if (!outputArgument) throw new Error('missing --output')
const output = resolve(outputArgument)
const sourceTreeDigest = argument('--source-tree-digest')
const imageDigest = argument('--image-digest')
const digestPattern = /^sha256:(?!0{64}$)[0-9a-f]{64}$/
if (!digestPattern.test(sourceTreeDigest) || !digestPattern.test(imageDigest)) {
  throw new Error('manual accessibility gate requires exact source and image digests')
}

const load = (path) => JSON.parse(readFileSync(path, 'utf8'))
const schema = load(resolve(repo, 'contracts/evidence/web-manual-accessibility/v1/schema.json'))
const ajv = new Ajv2020({ allErrors: true, strict: false })
addFormats(ajv)
const validate = ajv.compile(schema)
let document
if (input === '') {
  const note = 'A human screen-reader, full-keyboard, 200% zoom/reflow, and high-risk focus review has not been supplied for this exact image.'
  document = {
    schema_version: 'web-manual-accessibility-evidence/v1',
    module_id: 'MOD-WEB-001',
    profile: 'web-browser/v1',
    source_tree_digest: sourceTreeDigest,
    image_digest: imageDigest,
    reviewed_at: null,
    reviewer_id: null,
    assistive_technology: null,
    platform: null,
    flows: [
      { flow_id: 'screen-reader-navigation', result: 'NOT_RUN', notes: note },
      { flow_id: 'full-keyboard', result: 'NOT_RUN', notes: note },
      { flow_id: 'zoom-200-reflow', result: 'NOT_RUN', notes: note },
      { flow_id: 'high-risk-dialog-focus', result: 'NOT_RUN', notes: note },
    ],
    evidence_artifacts: [],
    level: 'MODULE',
    applicability: 'APPLICABLE',
    result: 'NOT_RUN',
    qualification: 'NOT_QUALIFIED',
    reason_code: 'MANUAL_ACCESSIBILITY_REVIEW_NOT_SUPPLIED',
  }
} else {
  const resolvedInput = resolve(input)
  const stats = lstatSync(resolvedInput)
  if (!stats.isFile() || stats.isSymbolicLink() || stats.size > 1_048_576) throw new Error('manual accessibility input is absent, unsafe, or oversized')
  document = load(resolvedInput)
  if (document.source_tree_digest !== sourceTreeDigest || document.image_digest !== imageDigest) {
    throw new Error('manual accessibility evidence does not bind the exact candidate')
  }
}
if (!validate(document)) throw new Error(ajv.errorsText(validate.errors))
const encoded = `${JSON.stringify(document, null, 2)}\n`
writeFileSync(output, encoded, { encoding: 'utf8', flag: 'wx' })
process.stdout.write(`${JSON.stringify({ result: document.result, qualification: document.qualification, evidence_digest: `sha256:${createHash('sha256').update(encoded).digest('hex')}` })}\n`)
if (document.result === 'FAIL') process.exitCode = 1
if (document.result !== 'PASS' && document.result !== 'FAIL') process.exitCode = 2
