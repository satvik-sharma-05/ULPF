/* Standalone SVG for the printable PDF.
 *
 * The PDF export prints an HTML document, so the chart has to be markup rather
 * than a React component - and inline SVG rather than a raster, so it stays
 * sharp at print resolution instead of looking like a screenshot pasted into a
 * report.
 *
 * The inference below deliberately mirrors RowsChart on screen and
 * charts.from_rows() on the server. Three copies of one rule is a real cost,
 * but the alternative is worse: an exported PDF showing a different chart from
 * the one the user clicked export on.
 */

const MIN_ROWS = 2
const MAX_ROWS = 40

// Slot-assigned, never cycled - the same series is the same colour on screen,
// in the deck and here.
const SERIES = ['#2A78D6', '#EB6834', '#1BAF7A', '#EDA100', '#E87BA4', '#4A3AA7']

const num = (v) => {
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

export function inferSpec(rows) {
  if (!Array.isArray(rows) || rows.length < MIN_ROWS || rows.length > MAX_ROWS) return null
  const columns = Object.keys(rows[0] || {})
  if (columns.length < 2) return null

  const numeric = columns.filter((c) => rows.every((r) => num(r[c]) !== null))
  const labels = columns.filter(
    (c) => !numeric.includes(c)
      && new Set(rows.map((r) => String(r[c]))).size >= Math.min(rows.length, MIN_ROWS),
  )
  if (!numeric.length || !labels.length) return null

  const timeish = labels.find((c) => /^(day|date|bucket|timestamp|hour|month)$/i.test(c))
  const xKey = timeish || labels[0]
  const keys = numeric.slice(0, 3)
  const data = timeish
    ? [...rows].sort((a, b) => String(a[xKey]).localeCompare(String(b[xKey])))
    : [...rows].sort((a, b) => (num(b[keys[0]]) || 0) - (num(a[keys[0]]) || 0))

  return { xKey, keys, data, title: `${keys[0].replace(/_/g, ' ')} by ${xKey.replace(/_/g, ' ')}` }
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ))
}

function niceMax(n) {
  if (n <= 0) return 1
  const mag = 10 ** Math.floor(Math.log10(n))
  const norm = n / mag
  return (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag
}

const compact = (n) => (
  Math.abs(n) >= 1e6 ? `${(n / 1e6).toFixed(1)}M`
    : Math.abs(n) >= 1e3 ? `${(n / 1e3).toFixed(1)}K`
      : String(Math.round(n * 100) / 100)
)

/** Grouped bar chart as an SVG string, or '' when the rows are not chartable. */
export function chartSvg(rows, width = 660, height = 260) {
  const spec = inferSpec(rows)
  if (!spec) return ''

  const { xKey, keys, data, title } = spec
  const M = { top: 26, right: 12, bottom: 54, left: 52 }
  const iw = width - M.left - M.right
  const ih = height - M.top - M.bottom
  const max = niceMax(Math.max(1, ...data.flatMap((r) => keys.map((k) => num(r[k]) || 0))))
  const band = iw / data.length
  const barW = Math.max(2, Math.min(30, (band - 8) / keys.length))
  const y = (v) => M.top + ih - (v / max) * ih
  const every = Math.max(1, Math.ceil(data.length / 10))

  const grid = [0, 0.5, 1].map((f) => {
    const v = Math.round(max * f)
    return `<line x1="${M.left}" x2="${width - M.right}" y1="${y(v)}" y2="${y(v)}" stroke="#E4D5C2"/>`
      + `<text x="${M.left - 6}" y="${y(v) + 3}" text-anchor="end" font-size="9" fill="#6B5B4D">${compact(v)}</text>`
  }).join('')

  const bars = data.flatMap((r, i) => keys.map((k, s) => {
    const v = num(r[k]) || 0
    const x = M.left + band * i + (band - barW * keys.length) / 2 + s * barW
    const top = y(v)
    return `<rect x="${x.toFixed(1)}" y="${top.toFixed(1)}" width="${barW.toFixed(1)}" `
      + `height="${Math.max(0, M.top + ih - top).toFixed(1)}" fill="${SERIES[s % SERIES.length]}"/>`
  })).join('')

  const ticks = data.map((r, i) => (i % every === 0
    ? `<text x="${(M.left + band * i + band / 2).toFixed(1)}" y="${height - 34}" `
      + `text-anchor="end" font-size="8" fill="#6B5B4D" `
      + `transform="rotate(-35 ${(M.left + band * i + band / 2).toFixed(1)} ${height - 34})">`
      + `${esc(String(r[xKey]).slice(0, 16))}</text>`
    : '')).join('')

  // Legend only for two or more series; with one the title already names it.
  const legend = keys.length > 1
    ? keys.map((k, s) => `<rect x="${M.left + s * 110}" y="${height - 14}" width="9" height="9" `
        + `fill="${SERIES[s % SERIES.length]}"/>`
        + `<text x="${M.left + s * 110 + 14}" y="${height - 6}" font-size="9" fill="#4F4034">${esc(k)}</text>`).join('')
    : ''

  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" `
    + `width="100%" style="max-width:${width}px">`
    + `<text x="0" y="12" font-size="11" font-weight="bold" fill="#2B2118">${esc(title)}</text>`
    + `<line x1="${M.left}" x2="${width - M.right}" y1="${M.top + ih}" y2="${M.top + ih}" stroke="#94826F"/>`
    + grid + bars + ticks + legend
    + '</svg>'
}
