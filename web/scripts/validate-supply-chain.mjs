import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import Ajv2020 from 'ajv/dist/2020.js'
import addFormats from 'ajv-formats'

const repo = resolve('..')
const load = (path) => JSON.parse(readFileSync(resolve(repo, path), 'utf8'))
const schema = load('contracts/supply-chain/v1/schema.json')
const registry = load('contracts/supply-chain/v1/web-components.json')
const ajv = new Ajv2020({ allErrors: true, strict: false })
addFormats(ajv)
if (!ajv.validate(schema, registry)) throw new Error(`component registry: ${ajv.errorsText()}`)
const lock = load('web/package-lock.json')
if (lock.lockfileVersion !== 3) throw new Error('package-lock version must be 3')
for (const [path, entry] of Object.entries(lock.packages ?? {})) {
  if (!path || !entry.resolved) continue
  if (!entry.integrity || !entry.resolved.startsWith('https://registry.npmjs.org/')) throw new Error(`lock integrity/registry drift: ${path}`)
}
const sbom = load('web/sbom.cdx.json')
if (sbom.bomFormat !== 'CycloneDX' || sbom.specVersion !== '1.6' || !Array.isArray(sbom.components)) throw new Error('CycloneDX SBOM identity invalid')
const names = new Set(sbom.components.map((component) => `${component.group ? `${component.group}/` : ''}${component.name}`))
for (const name of ['vue', 'vue-router', 'pinia', 'element-plus', 'echarts', '@tanstack/vue-query']) {
  if (!names.has(name)) throw new Error(`runtime SBOM missing ${name}`)
}
const disallowed = []
for (const component of sbom.components) {
  const text = JSON.stringify(component.licenses ?? []).toUpperCase()
  if (/AGPL|LGPL|GPL-[123]/.test(text)) disallowed.push(`${component.name}@${component.version}`)
}
if (disallowed.length) throw new Error(`disallowed runtime license: ${disallowed.join(',')}`)
const notices = readFileSync(resolve(repo, 'web/THIRD_PARTY_NOTICES'), 'utf8')
for (const fragment of ['Vue 3.5.41', 'Apache ECharts 6.1.0', 'Playwright Test 1.62.1', 'axe-core 4.13.0', 'no Owner-approved project-wide']) {
  if (!notices.includes(fragment)) throw new Error(`third-party notices missing ${fragment}`)
}
process.stdout.write(`${JSON.stringify({ schema_version: 'web-supply-chain-validation/v1', registry_components: registry.components.length, lock_packages: Object.keys(lock.packages ?? {}).length, runtime_sbom_components: sbom.components.length, disallowed_runtime_licenses: disallowed, result: 'PASS', qualification: 'NOT_QUALIFIED', qualification_scope: 'LOCAL_LOCK_SBOM_NOTICE_GATE' })}\n`)
