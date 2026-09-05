import { useMemo, useState } from 'react'
import { CHROME, SERIES, GLASS } from './analytics/theme.js'

/* Charts the rows a Text-to-Cypher answer came back with.
 *
 * The data is already in the response - `rows` is the query result the answer
 * was written from - so this needs no new endpoint, no server-side plotting
 * library and nothing added to the deployment. That matters on an air-gapped
 * box: the obvious implementation (Matplotlib on the backend, PNG down the
 * wire) would add a dependency, a round trip and a non-interactive raster
 * image, and would still be drawing the same numbers already on the client.
 *
 * Chart TYPE is chosen from the shape of the data and can then be overridden.
 * The first version drew a bar chart for everything, which is the safe default
 * and the wrong one surprisingly often: asking for "severity breakdown" is
 * asking what share each level is, and a bar chart makes you compute the
 * shares yourself. The rule used here:
 *
 *   a time-like label column                           -> line
 *   one positive measure, <= 8 categories              -> pie
 *   labels longer than ~14 characters                  -> horizontal bars
 *   two numeric columns and no useful label            -> scatter
 *   anything else                                      -> grouped bars
 *
 * A pie is OFFERED for any single positive measure regardless of how many
 * categories there are - the tail beyond the eighth slice is pooled into a
 * labelled "Other". Refusing instead was the first behaviour and it was
 * wrong: "generate a pie chart of the hosts" over sixty hosts is exactly the
 * case a pie is for, and answering it with a bar chart is not answering it.
 * What is still refused is a pie of values that include negatives, which has
 * no meaningful geometry.
 *
 * Not implemented: violin and box plots. Both describe a DISTRIBUTION, and
 * these rows are almost always aggregates - `severity, count` carries one
 * observation per category, so a violin over it would be drawing a shape from
 * a single number. Adding one would mean inventing spread the data does not
 * contain.
 */

const MIN_ROWS = 2
const MAX_ROWS = 60
//  Slices drawn individually before the rest are pooled into "Other". Eight
//  is about where a reader stops being able to match a slice to its legend;
//  beyond that the extra slices are decoration, not information.
const MAX_PIE_SLICES = 8
const LONG_LABEL = 14

function isNumeric(v) {
  return typeof v === 'number' && Number.isFinite(v)
}

function analyse(rows) {
  if (!Array.isArray(rows) || rows.length < MIN_ROWS || rows.length > MAX_ROWS) return null
  const columns = Object.keys(rows[0] || {})
  if (columns.length < 2) return null

  const numeric = columns.filter((c) => rows.every((r) => isNumeric(r[c])))
  const labels = columns.filter((c) => {
    if (numeric.includes(c)) return false
    const distinct = new Set(rows.map((r) => String(r[c]))).size
    return distinct >= Math.min(rows.length, MIN_ROWS)
  })
  if (numeric.length === 0 || labels.length === 0) return null

  const timeish = labels.find((c) => /^(day|date|bucket|timestamp|hour|month|week)$/i.test(c))
  const labelKey = timeish || labels[0]
  const valueKeys = numeric.slice(0, 3)

  const values = rows.map((r) => r[valueKeys[0]])
  const allPositive = values.every((v) => v >= 0)
  const longest = Math.max(...rows.map((r) => String(r[labelKey] ?? '').length))

  // Which types this data can honestly support.
  const available = ['bar', 'hbar']
  // A pie needs one positive measure - NOT a small number of categories. A
  // long tail is pooled into "Other" rather than refused: 60 hosts is exactly
  // the case someone asks a pie chart for, and answering "too many
  // categories" with a bar chart is not answering the question.
  if (valueKeys.length === 1 && allPositive && values.some((v) => v > 0)) {
    available.push('pie', 'donut')
  }
  if (timeish || rows.length >= 4) available.push('line', 'area')
  if (numeric.length >= 2) available.push('scatter')

  let preferred = 'bar'
  if (timeish) preferred = 'line'
  else if (available.includes('pie') && rows.length <= MAX_PIE_SLICES) preferred = 'pie'
  else if (longest > LONG_LABEL) preferred = 'hbar'

  return { labelKey, valueKeys, isTime: Boolean(timeish), available, preferred, numeric }
}

function niceMax(n) {
  if (n <= 0) return 1
  const mag = Math.pow(10, Math.floor(Math.log10(n)))
  const norm = n / mag
  return (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag
}

function compact(n) {
  const a = Math.abs(n)
  if (a >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, '') + 'B'
  if (a >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M'
  if (a >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'K'
  return String(Math.round(n * 100) / 100)
}

function truncate(s, n) {
  const t = String(s ?? '')
  return t.length > n ? t.slice(0, n - 1) + '…' : t
}

/* Arc path for a pie/donut slice. Angles run clockwise from twelve o'clock,
 * which is where a reader expects a pie to start. */
function arcPath(cx, cy, rOuter, rInner, a0, a1) {
  const p = (r, a) => [cx + r * Math.cos(a - Math.PI / 2), cy + r * Math.sin(a - Math.PI / 2)]
  const big = a1 - a0 > Math.PI ? 1 : 0
  const [x0, y0] = p(rOuter, a0)
  const [x1, y1] = p(rOuter, a1)
  if (rInner <= 0) {
    return `M ${cx} ${cy} L ${x0} ${y0} A ${rOuter} ${rOuter} 0 ${big} 1 ${x1} ${y1} Z`
  }
  const [x2, y2] = p(rInner, a1)
  const [x3, y3] = p(rInner, a0)
  return `M ${x0} ${y0} A ${rOuter} ${rOuter} 0 ${big} 1 ${x1} ${y1} `
       + `L ${x2} ${y2} A ${rInner} ${rInner} 0 ${big} 0 ${x3} ${y3} Z`
}

const TYPE_LABEL = {
  bar: 'Bars', hbar: 'Horizontal', pie: 'Pie', donut: 'Donut',
  line: 'Line', area: 'Area', scatter: 'Scatter',
}

export default function RowsChart({ rows }) {
  const spec = useMemo(() => analyse(rows), [rows])
  // Collapsed by default. A chart on every answer that could carry one is
  // noise in a transcript people scroll; showing one should be a choice.
  // The choice is remembered, so someone who does want a chart on every
  // answer sets it once. Wrapped because localStorage throws outright in
  // some privacy modes.
  const [open, setOpen] = useState(() => {
    try { return localStorage.getItem('ulpf.chart') === 'always' } catch { return false }
  })
  const [type, setType] = useState(null)

  function toggleOpen() {
    setOpen((o) => {
      const next = !o
      try { localStorage.setItem('ulpf.chart', next ? 'always' : 'ask') } catch { /* unavailable */ }
      return next
    })
  }

  if (!spec) return null
  const kind = type && spec.available.includes(type) ? type : spec.preferred

  const W = 560
  const H = 240
  const M = { top: 14, right: 12, bottom: 42, left: 46 }
  const IW = W - M.left - M.right
  const IH = H - M.top - M.bottom

  const { labelKey, valueKeys } = spec
  const labelsOf = rows.map((r) => String(r[labelKey] ?? ''))
  const maxVal = niceMax(Math.max(...rows.flatMap((r) => valueKeys.map((k) => r[k] || 0))))
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * maxVal)

  const y = (v) => M.top + IH - (v / maxVal) * IH
  const band = IW / rows.length

  // The legend has to follow the same pooling the pie does, or it lists
  // categories that were never drawn.
  const pieLegend = (() => {
    const sorted = [...rows].sort((a, b) => (b[valueKeys[0]] || 0) - (a[valueKeys[0]] || 0))
      .map((r) => String(r[labelKey] ?? ''))
    if (sorted.length <= MAX_PIE_SLICES) return sorted
    return [...sorted.slice(0, MAX_PIE_SLICES - 1),
            `Other (${sorted.length - MAX_PIE_SLICES + 1})`]
  })()

  const legend = (
    <div className="flex flex-wrap gap-x-3 gap-y-1 mb-1">
      {(kind === 'pie' || kind === 'donut' ? pieLegend : valueKeys).map((k, i) => (
        <span key={k + i} className="text-[10px] flex items-center gap-1" style={{ color: CHROME.inkMuted }}>
          <span className="inline-block" style={{ width: 9, height: 9, background: SERIES[i % SERIES.length] }} />
          {truncate(k, 22)}
        </span>
      ))}
    </div>
  )

  function renderBars() {
    const barW = Math.max(2, Math.min(26, (band - 6) / valueKeys.length))
    return (
      <>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={M.left} x2={W - M.right} y1={y(t)} y2={y(t)} stroke={CHROME.gridline} strokeWidth="1" />
            <text x={M.left - 6} y={y(t) + 3} textAnchor="end" fontSize="9" fill={CHROME.inkDim}>{compact(t)}</text>
          </g>
        ))}
        <line x1={M.left} x2={W - M.right} y1={M.top + IH} y2={M.top + IH} stroke={CHROME.baseline} strokeWidth="1" />
        {rows.map((r, i) => valueKeys.map((k, s) => {
          const v = r[k] || 0
          const top = y(v)
          const x = M.left + band * i + (band - barW * valueKeys.length) / 2 + s * barW
          return (
            <rect key={`${i}-${k}`} x={x} y={top} width={barW} height={Math.max(0, M.top + IH - top)}
              fill={SERIES[s % SERIES.length]}>
              <title>{`${labelsOf[i]} · ${k}: ${r[k]}`}</title>
            </rect>
          )
        }))}
        {rows.map((r, i) => (
          <text key={`l${i}`} x={M.left + band * i + band / 2} y={M.top + IH + 14}
            textAnchor="end" fontSize="9" fill={CHROME.inkDim}
            transform={`rotate(-38 ${M.left + band * i + band / 2} ${M.top + IH + 14})`}>
            {truncate(labelsOf[i], 16)}
          </text>
        ))}
      </>
    )
  }

  function renderHBars() {
    const L = 128
    const iw = W - L - M.right
    const rowH = Math.max(10, Math.min(24, (H - M.top - 18) / rows.length))
    return (
      <>
        {rows.map((r, i) => {
          const v = r[valueKeys[0]] || 0
          const w = Math.max(1, (v / maxVal) * iw)
          const yy = M.top + rowH * i
          return (
            <g key={i}>
              <text x={L - 6} y={yy + rowH * 0.7} textAnchor="end" fontSize="9.5" fill={CHROME.inkDim}>
                {truncate(labelsOf[i], 20)}
              </text>
              <rect x={L} y={yy + 2} width={w} height={rowH - 5} fill={SERIES[i % SERIES.length]}>
                <title>{`${labelsOf[i]}: ${v}`}</title>
              </rect>
              <text x={L + w + 4} y={yy + rowH * 0.7} fontSize="9" fill={CHROME.inkMuted}>{compact(v)}</text>
            </g>
          )
        })}
      </>
    )
  }

  function renderPie(inner) {
    const cx = W / 2
    const cy = M.top + IH / 2
    const R = Math.min(IH, IW) / 2 - 4
    const total = rows.reduce((a, r) => a + (r[valueKeys[0]] || 0), 0)
    if (total <= 0) return null

    // Pool the tail. Drawn largest-first so the pooled slice is genuinely the
    // small remainder, and labelled with how many categories it stands for -
    // an "Other" slice that does not say what it contains is a slice that
    // hides something.
    const sorted = [...rows].sort((a, b) => (b[valueKeys[0]] || 0) - (a[valueKeys[0]] || 0))
    let slices = sorted.map((r) => ({
      label: String(r[labelKey] ?? ''), value: r[valueKeys[0]] || 0,
    }))
    if (slices.length > MAX_PIE_SLICES) {
      const head = slices.slice(0, MAX_PIE_SLICES - 1)
      const tail = slices.slice(MAX_PIE_SLICES - 1)
      head.push({
        label: `Other (${tail.length})`,
        value: tail.reduce((a, x) => a + x.value, 0),
        pooled: tail.length,
      })
      slices = head
    }

    let acc = 0
    return (
      <>
        {slices.map((r, i) => {
          const v = r.value
          const a0 = (acc / total) * Math.PI * 2
          acc += v
          const a1 = (acc / total) * Math.PI * 2
          const pct = ((v / total) * 100).toFixed(1)
          const mid = (a0 + a1) / 2
          const lr = inner ? R * 0.78 : R * 0.62
          const lx = cx + lr * Math.cos(mid - Math.PI / 2)
          const ly = cy + lr * Math.sin(mid - Math.PI / 2)
          // The pooled slice is greyed so it never reads as a category of its
          // own weight.
          const fill = r.pooled ? CHROME.inkDim : SERIES[i % SERIES.length]
          return (
            <g key={i}>
              <path d={arcPath(cx, cy, R, inner ? R * 0.55 : 0, a0, a1)}
                fill={fill} stroke={CHROME.surface} strokeWidth="1.5">
                <title>{`${r.label}: ${v} (${pct}%)`}</title>
              </path>
              {/* Only label a slice big enough to hold the text - a 1% slice
                  with a number over it is noise, and the tooltip has it. */}
              {(a1 - a0) > 0.32 && (
                <text x={lx} y={ly + 3} textAnchor="middle" fontSize="9.5" fontWeight="700" fill="#fff">
                  {pct}%
                </text>
              )}
            </g>
          )
        })}
        {inner && (
          <text x={cx} y={cy + 4} textAnchor="middle" fontSize="13" fontWeight="700" fill={CHROME.ink}>
            {compact(total)}
          </text>
        )}
      </>
    )
  }

  function renderLine(filled) {
    const px = (i) => M.left + (rows.length === 1 ? IW / 2 : (IW / (rows.length - 1)) * i)
    return (
      <>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={M.left} x2={W - M.right} y1={y(t)} y2={y(t)} stroke={CHROME.gridline} strokeWidth="1" />
            <text x={M.left - 6} y={y(t) + 3} textAnchor="end" fontSize="9" fill={CHROME.inkDim}>{compact(t)}</text>
          </g>
        ))}
        <line x1={M.left} x2={W - M.right} y1={M.top + IH} y2={M.top + IH} stroke={CHROME.baseline} strokeWidth="1" />
        {valueKeys.map((k, s) => {
          const pts = rows.map((r, i) => `${px(i)},${y(r[k] || 0)}`).join(' ')
          const colour = SERIES[s % SERIES.length]
          return (
            <g key={k}>
              {filled && (
                <polygon points={`${M.left},${M.top + IH} ${pts} ${px(rows.length - 1)},${M.top + IH}`}
                  fill={colour} opacity="0.16" />
              )}
              <polyline points={pts} fill="none" stroke={colour} strokeWidth="2"
                strokeLinejoin="round" strokeLinecap="round" />
              {rows.map((r, i) => (
                <circle key={i} cx={px(i)} cy={y(r[k] || 0)} r="2.6" fill={colour}>
                  <title>{`${labelsOf[i]} · ${k}: ${r[k]}`}</title>
                </circle>
              ))}
            </g>
          )
        })}
        {rows.map((r, i) => (
          (rows.length <= 12 || i % Math.ceil(rows.length / 12) === 0) && (
            <text key={`l${i}`} x={px(i)} y={M.top + IH + 14} textAnchor="end" fontSize="9"
              fill={CHROME.inkDim} transform={`rotate(-38 ${px(i)} ${M.top + IH + 14})`}>
              {truncate(labelsOf[i], 16)}
            </text>
          )
        ))}
      </>
    )
  }

  function renderScatter() {
    const [kx, ky] = spec.numeric
    const xs = rows.map((r) => r[kx])
    const ys = rows.map((r) => r[ky])
    const xMax = niceMax(Math.max(...xs))
    const yMax = niceMax(Math.max(...ys))
    const px = (v) => M.left + (v / xMax) * IW
    const py = (v) => M.top + IH - (v / yMax) * IH
    return (
      <>
        {[0, 0.5, 1].map((f) => (
          <line key={f} x1={M.left} x2={W - M.right} y1={py(f * yMax)} y2={py(f * yMax)}
            stroke={CHROME.gridline} strokeWidth="1" />
        ))}
        <line x1={M.left} x2={W - M.right} y1={M.top + IH} y2={M.top + IH} stroke={CHROME.baseline} strokeWidth="1" />
        <text x={M.left - 6} y={M.top + 8} textAnchor="end" fontSize="9" fill={CHROME.inkDim}>{compact(yMax)}</text>
        {rows.map((r, i) => (
          <circle key={i} cx={px(r[kx])} cy={py(r[ky])} r="4" fill={SERIES[1]} opacity="0.75">
            <title>{`${labelsOf[i]} · ${kx}: ${r[kx]}, ${ky}: ${r[ky]}`}</title>
          </circle>
        ))}
        <text x={W / 2} y={H - 6} textAnchor="middle" fontSize="9" fill={CHROME.inkDim}>{kx} →</text>
      </>
    )
  }

  const body = kind === 'bar' ? renderBars()
    : kind === 'hbar' ? renderHBars()
    : kind === 'pie' ? renderPie(false)
    : kind === 'donut' ? renderPie(true)
    : kind === 'line' ? renderLine(false)
    : kind === 'area' ? renderLine(true)
    : renderScatter()

  return (
    <div className="mt-2">
      <div className="flex items-center gap-2 flex-wrap mb-1">
        <button type="button" onClick={toggleOpen}
          className="text-[11px] px-2 py-0.5"
          style={{ background: open ? CHROME.surfaceActive : 'transparent',
                   color: CHROME.inkSecondary,
                   border: `1px solid ${CHROME.border}` }}
          title={open ? 'Hide charts on answers from now on'
                      : 'Show charts on answers from now on'}>
          {open ? 'Hide chart' : '▮ Chart'}
        </button>
        {open && spec.available.map((t) => (
          <button key={t} type="button" onClick={() => setType(t)}
            className="text-[10px] px-1.5 py-0.5 font-semibold"
            style={{
              background: kind === t ? CHROME.primary : 'transparent',
              color: kind === t ? CHROME.primaryInk : CHROME.inkMuted,
              border: `1px solid ${kind === t ? CHROME.primary : CHROME.border}`,
            }}>
            {TYPE_LABEL[t]}
          </button>
        ))}
      </div>

      {open && (
        <div className="p-2" style={{ background: CHROME.surface, border: `1px solid ${GLASS.borderOuter}` }}>
          {legend}
          <div className="overflow-x-auto">
            <svg width={W} height={H} role="img"
              aria-label={`${TYPE_LABEL[kind]} chart of ${valueKeys.join(', ')} by ${labelKey}`}>
              {body}
            </svg>
          </div>
          <div className="text-[10px] mt-0.5" style={{ color: CHROME.inkDim }}>
            by {labelKey}
          </div>
        </div>
      )}
    </div>
  )
}
