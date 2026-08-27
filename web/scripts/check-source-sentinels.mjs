import { readFileSync, readdirSync, statSync } from 'node:fs'
import { extname, join, relative, resolve } from 'node:path'

function files(root) {
  return readdirSync(root).flatMap((name) => {
    const path = join(root, name)
    return statSync(path).isDirectory() ? files(path) : [path]
  })
}

const root = resolve('src')
const failures = []
const rules = [
  ['unfinished implementation', /\b(?:TODO|FIXME|HACK|XXX)\b/],
  ['hand-written fetch client', /\bfetch\s*\(/],
  ['XHR client', /\bXMLHttpRequest\b/],
  ['axios client', /\baxios\b/],
  ['raw HTML sink', /\bv-html\b|\.innerHTML\s*=/],
  ['dynamic code', /\beval\s*\(|new\s+Function\s*\(/],
  ['offline service worker', /serviceWorker\.register|navigator\.serviceWorker/],
  ['remote runtime import', /import\s*\(\s*['"]https?:\/\//],
  ['iframe boundary', /<iframe\b/i],
]
for (const path of files(root).filter((path) => ['.ts', '.vue', '.css'].includes(extname(path)))) {
  const body = readFileSync(path, 'utf8')
  for (const [label, pattern] of rules) if (pattern.test(body)) failures.push(`${relative(root, path)}: ${label}`)
}
const pinia = readFileSync(resolve(root, 'stores/ui.ts'), 'utf8')
for (const forbidden of ['csrf', 'token', 'event_id', 'incident_id', 'binding_generation']) {
  if (pinia.toLowerCase().includes(forbidden)) failures.push(`stores/ui.ts: server fact ${forbidden}`)
}
process.stdout.write(`${JSON.stringify({ schema_version: 'web-source-sentinels/v1', checked_files: files(root).length, failures, result: failures.length ? 'FAIL' : 'PASS' })}\n`)
if (failures.length) process.exitCode = 1
