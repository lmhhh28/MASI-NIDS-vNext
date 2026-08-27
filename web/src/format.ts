export function formatTime(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return 'Not observed'
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'medium',
    timeZone: 'UTC',
  }).format(new Date(value)) + ' UTC'
}

export function formatNumber(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'Not reported'
  return new Intl.NumberFormat().format(value)
}

export function formatIdentity(value: unknown): string {
  return typeof value === 'string' && value.length > 0 ? value : 'Not assigned'
}

export function abbreviateDigest(value: unknown): string {
  if (typeof value !== 'string' || !value.startsWith('sha256:')) return 'Not reported'
  return `${value.slice(0, 15)}…${value.slice(-8)}`
}

export function displayText(value: unknown): string {
  if (value === null || value === undefined || value === '') return 'Not reported'
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (typeof value === 'number') return formatNumber(value)
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return `${value.length} item${value.length === 1 ? '' : 's'}`
  if (typeof value === 'object') return `${Object.keys(value).length} fields`
  return 'Unsupported value'
}
