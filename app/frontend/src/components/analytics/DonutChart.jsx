import { useMemo, useState } from 'react'
import { CHROME, SERIES } from './theme.js'
import { formatCompact, formatPct } from './format.js'
import Panel from './Panel.jsx'

/* A donut for part-to-whole.
 *
 * Two rules from the dataviz guidance are enforced here rather than left to
 * the caller, because they are what make a pie readable at all:
 *  - at most 6 slices; everything past that folds into "Other". Past ~6 the
 *    adjacent arcs blur and the chart stops answering anything.
 *  - a 2px surface gap between segments, so touching arcs separate by white
 *    space rather than by a stroke that would add non-data ink.
 *
 * The legend carries the value AND the share for every slice, so nothing is
 * gated behind hovering - the chart is the summary, the legend is the table.
 *
 * A row may carry its own `color`. That matters wherever the categories
 * already have a meaning attached to a colour elsewhere in the app - alert
 * priority, severity - because taking the next free palette step there would
 * paint P3 Warning orange and P2 Error blue, contradicting every other panel
 * on the page. Rows without a colour fall back to the palette cycle.
 */
const SIZE = 190
const STROKE = 30
const R = (SIZE - STROKE) / 2
const CIRC = 2 * Math.PI * R
const MAX_SLICES = 6

export default function DonutChart({
  title, subtitle, rows, loading,
  labelKey = 'label', valueKey = 'value',
  emptyLabel = 'No data', centerLabel = 'Total',
}) {
  const [hover, setHover] = useState(null)

  const { slices, total } = useMemo(() => {
    const list = (rows || [])
      .map((r) => ({ label: String(r[labelKey] ?? '—'), value: Number(r[valueKey]) || 0, color: r.color }))
      .filter((r) => r.value > 0)
      .sort((a, b) => b.value - a.value)

    let shaped = list
    if (list.length > MAX_SLICES) {
      const head = list.slice(0, MAX_SLICES - 1)
      const rest = list.slice(MAX_SLICES - 1).reduce((s, r) => s + r.value, 0)
      shaped = [...head, { label: `Other (${list.length - MAX_SLICES + 1})`, value: rest, _other: true }]
    }
    const sum = shaped.reduce((s, r) => s + r.value, 0)

    let acc = 0
    const out = shaped.map((r, i) => {
      const frac = sum ? r.value / sum : 0
      const dash = Math.max(0, frac * CIRC - 2)   // 2px surface gap
      const fill = r._other ? CHROME.borderStrong : (r.color || SERIES[i % SERIES.length])
      const seg = { ...r, frac, dash, offset: acc, color: fill }
      acc += frac * CIRC
      return seg
    })
    return { slices: out, total: sum }
  }, [rows, labelKey, valueKey])

  return (
    <Panel title={title} subtitle={subtitle}>
      {loading ? (
        <div className="py-14 text-center text-xs" style={{ color: CHROME.inkMuted }}>Loading…</div>
      ) : total === 0 ? (
        <div className="py-14 text-center text-xs" style={{ color: CHROME.inkMuted }}>{emptyLabel}</div>
      ) : (
        <div className="flex flex-col sm:flex-row items-center gap-5">
          <div className="relative shrink-0">
            <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`}>
              <g transform={`translate(${SIZE / 2}, ${SIZE / 2}) rotate(-90)`}>
                <circle r={R} fill="none" stroke={CHROME.gridline} strokeWidth={STROKE} />
                {slices.map((sl, i) => (
                  <circle
                    key={sl.label}
                    r={R}
                    fill="none"
                    stroke={sl.color}
                    strokeWidth={hover === i ? STROKE + 5 : STROKE}
                    strokeDasharray={`${sl.dash} ${CIRC - sl.dash}`}
                    strokeDashoffset={-sl.offset}
                    style={{
                      transition: 'stroke-width 140ms',
                      cursor: 'pointer',
                      animation: `dn-in 700ms cubic-bezier(.2,.7,.3,1) both ${i * 90}ms`,
                    }}
                    onPointerEnter={() => setHover(i)}
                    onPointerLeave={() => setHover(null)}
                  />
                ))}
              </g>
            </svg>
            <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
              {hover !== null ? (
                <>
                  <div className="text-[11px] max-w-[110px] truncate" style={{ color: CHROME.inkMuted }}>
                    {slices[hover].label}
                  </div>
                  <div className="text-xl font-bold" style={{ color: CHROME.ink }}>
                    {formatPct(slices[hover].frac * 100)}
                  </div>
                </>
              ) : (
                <>
                  <div className="text-[11px]" style={{ color: CHROME.inkMuted }}>{centerLabel}</div>
                  <div className="text-xl font-bold" style={{ color: CHROME.ink }}>{formatCompact(total)}</div>
                </>
              )}
            </div>
          </div>

          <ul className="flex-1 w-full flex flex-col gap-1.5 min-w-0">
            {slices.map((sl, i) => (
              <li
                key={sl.label}
                className="flex items-center justify-between gap-2 text-xs px-2 py-1 cursor-default"
                style={{ background: hover === i ? CHROME.surfaceRaised : 'transparent' }}
                onPointerEnter={() => setHover(i)}
                onPointerLeave={() => setHover(null)}
              >
                <span className="flex items-center gap-2 min-w-0">
                  <span className="w-2.5 h-2.5 shrink-0" style={{ background: sl.color }} />
                  <span className="truncate" style={{ color: CHROME.inkSecondary }} title={sl.label}>
                    {sl.label}
                  </span>
                </span>
                <span className="flex items-baseline gap-2.5 shrink-0">
                  <span className="font-mono font-semibold" style={{ color: CHROME.ink }}>
                    {formatCompact(sl.value)}
                  </span>
                  <span className="font-mono w-12 text-right" style={{ color: CHROME.inkMuted }}>
                    {formatPct(sl.frac * 100)}
                  </span>
                </span>
              </li>
            ))}
          </ul>
          <style>{`@keyframes dn-in{from{opacity:0;stroke-dashoffset:0}to{opacity:1}}`}</style>
        </div>
      )}
    </Panel>
  )
}
