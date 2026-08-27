import { readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { gzipSync } from 'node:zlib'

const dist = resolve('dist')
const manifest = JSON.parse(readFileSync(join(dist, '.vite', 'manifest.json'), 'utf8'))
const gzipBytes = (relative) => gzipSync(readFileSync(join(dist, relative)), { level: 9 }).byteLength

function closure(key, visited = new Set()) {
  if (visited.has(key)) return visited
  visited.add(key)
  const entry = manifest[key]
  if (!entry) throw new Error(`manifest entry missing: ${key}`)
  for (const dependency of entry.imports ?? []) closure(dependency, visited)
  return visited
}

const entryKey = Object.keys(manifest).find((key) => manifest[key].isEntry)
if (!entryKey) throw new Error('production manifest has no entry')
const entryClosure = closure(entryKey)
const initialJS = [...entryClosure].reduce((total, key) => total + gzipBytes(manifest[key].file), 0)
const initialCSSFiles = new Set([...entryClosure].flatMap((key) => manifest[key].css ?? []))
const initialCSS = [...initialCSSFiles].reduce((total, file) => total + gzipBytes(file), 0)
const routeBudgets = []
for (const [key, entry] of Object.entries(manifest)) {
  if (!entry.isDynamicEntry) continue
  const files = closure(key)
  const javascript = [...files].reduce((total, item) => total + gzipBytes(manifest[item].file), 0)
  const cssFiles = new Set([...files].flatMap((item) => manifest[item].css ?? []))
  const css = [...cssFiles].reduce((total, file) => total + gzipBytes(file), 0)
  routeBudgets.push({ route_entry: key, gzip_bytes: javascript + css })
}
const maximumRoute = Math.max(0, ...routeBudgets.map((route) => route.gzip_bytes))
const result = {
  schema_version: 'web-bundle-budget/v1',
  initial_javascript_gzip_bytes: initialJS,
  initial_css_gzip_bytes: initialCSS,
  maximum_route_gzip_bytes: maximumRoute,
  thresholds: { initial_javascript: 256000, initial_css: 81920, route: 614400 },
  routes: routeBudgets,
  result: initialJS <= 256000 && initialCSS <= 81920 && maximumRoute <= 614400 ? 'PASS' : 'FAIL',
}
process.stdout.write(`${JSON.stringify(result)}\n`)
if (result.result !== 'PASS') process.exitCode = 1
