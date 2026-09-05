// format.js - small formatting helpers shared by the analytics dashboard.

export function formatCompact(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '-'
  const abs = Math.abs(n)
  if (abs >= 1_000_000_000) return (n / 1_000_000_000).toFixed(1).replace(/\.0$/, '') + 'B'
  if (abs >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M'
  if (abs >= 1_000) return (n / 1_000).toFixed(1).replace(/\.0$/, '') + 'K'
  return n.toLocaleString()
}

export function formatPct(n, digits = 1) {
  if (n === null || n === undefined || Number.isNaN(n)) return '-'
  return `${n.toFixed(digits)}%`
}

export function formatDay(iso) {
  if (!iso) return ''
  const d = new Date(`${iso}T00:00:00Z`)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })
}
