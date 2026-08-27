import { readFileSync, readdirSync, statSync } from 'node:fs'
import { extname, join, relative, resolve } from 'node:path'

function files(root) {
  const output = []
  for (const name of readdirSync(root)) {
    const path = join(root, name)
    if (statSync(path).isDirectory()) output.push(...files(path))
    else output.push(path)
  }
  return output
}

const sourceRoot = resolve('src')
const distRoot = resolve('dist')
const forbidden = [
  ['v-html', /\bv-html\b/],
  ['innerHTML', /\.innerHTML\s*=/],
  ['eval', /\beval\s*\(/],
  ['Function constructor', /new\s+Function\s*\(/],
  ['Service Worker', /serviceWorker\.register|navigator\.serviceWorker/],
  ['iframe', /<iframe\b/i],
  ['remote module', /import\s*\(\s*['"]https?:\/\//],
]
const failures = []
for (const path of files(sourceRoot).filter((path) => ['.ts', '.vue', '.css'].includes(extname(path)))) {
  const body = readFileSync(path, 'utf8')
  for (const [label, pattern] of forbidden) if (pattern.test(body)) failures.push(`${relative(sourceRoot, path)}: ${label}`)
}
const distFiles = files(distRoot)
for (const path of distFiles) {
  if (path.endsWith('.map')) failures.push(`${relative(distRoot, path)}: production source map`)
  if (/\.(woff2?|ttf|otf)$/i.test(path)) failures.push(`${relative(distRoot, path)}: bundled non-profile font`)
}
const html = readFileSync(join(distRoot, 'index.html'), 'utf8')
if (/\b(?:src|href)=["']https?:\/\//i.test(html)) failures.push('index.html: remote runtime asset')
if (!distFiles.some((path) => /assets[/\\].+-[A-Za-z0-9_-]{8,}\.(?:js|css)$/.test(path))) failures.push('content-hashed assets missing')
const lock = JSON.parse(readFileSync(resolve('package-lock.json'), 'utf8'))
if (lock.lockfileVersion !== 3 || Object.values(lock.packages ?? {}).some((entry) => entry && entry.resolved && !entry.integrity)) failures.push('lockfile integrity closure invalid')
process.stdout.write(`${JSON.stringify({ schema_version: 'web-security-static/v1', checked_source_files: files(sourceRoot).length, checked_dist_files: distFiles.length, failures, result: failures.length ? 'FAIL' : 'PASS' })}\n`)
if (failures.length) process.exitCode = 1
