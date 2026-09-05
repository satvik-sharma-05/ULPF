import { useMemo, useRef, useState } from 'react'
import { CHROME, MARKS, SERIES, GLASS, glassPanel } from './theme.js'
import { formatCompact, formatDay } from './format.js'

const W = 760
const H = 220
const MARGIN = { top: 14, right: 14, bottom: 26, left: 46 }
const INNER_W = W - MARGIN.left - MARGIN.right
const INNER_H = H - MARGIN.top - MARGIN.bottom

function niceMax(n) {
  if (n <= 0) return 10
  const magnitude = Math.pow(10, Math.floor(Math.log10(n)))
  const norm = n / magnitude
  const step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10
  return step * magnitude
}

/* Total volume as bars, errors as a filled area + line on the SAME axis -
 * both are log counts, so a second axis would invent a relationship that is
 * not in the data. The two series are separated by mark SHAPE (bar vs line)
 * as well as by hue, so the chart still reads for a colour-blind viewer and
 * in a black-and-white print of a slide.
 *
 * Motion: bars grow from the baseline on a short stagger and the error line
 * draws itself left to right. It is entrance-only - once settled nothing
 * moves, because a dashboard that keeps animating is a dashboard you cannot
 * read. The whole thing is skipped under prefers-reduced-motion.
 */
const REDUCED = typeof window !== 'undefined'
  && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
export default function TimeSeriesChart({ data, loading, refreshing }) {
  const [hoverIdx, setHoverIdx] = useState(null)
  const [showTable, setShowTable] = useState(false)
  const svgRef = useRef(null)

  const rows = data || []
  const n = rows.length
  const maxVal = useMemo(() => niceMax(Math.max(1, ...rows.map((r) => r.total || 0))), [rows])

  const bandW = n > 0 ? INNER_W / n : 0
  const barW = Math.max(2, Math.min(22, bandW - 6))
  const xFor = (i) => MARGIN.left + bandW * i + bandW / 2
  const yFor = (v) => MARGIN.top + INNER_H - (v / maxVal) * INNER_H

  const linePoints = rows.map((r, i) => `${xFor(i)},${yFor(r.errors || 0)}`).join(' ')

  // Closed path for the area under the error line: the line itself, then back
  // along the baseline. Built here rather than inline so the render stays
  // readable.
  const baseY = MARGIN.top + INNER_H
  const areaPath = n === 0
    ? ''
    : `M ${xFor(0)},${baseY} `
      + rows.map((r, i) => `L ${xFor(i)},${yFor(r.errors || 0)}`).join(' ')
      + ` L ${xFor(n - 1)},${baseY} Z`

  // Rough path length, used to seed the stroke-dash draw-in. It only has to be
  // an over-estimate: too long simply means the dash never visibly repeats.
  const lineLength = Math.max(INNER_W, n * bandW) * 2
  const yTicks = [0, 0.5, 1].map((f) => Math.round(maxVal * f))
  const labelEvery = Math.max(1, Math.ceil(n / 7))

  function handleMove(e) {
    if (!svgRef.current || n === 0) return
    const rect = svgRef.current.getBoundingClientRect()
    const localX = (e.clientX - rect.left) * (W / rect.width)
    const idx = Math.round((localX - MARGIN.left - bandW / 2) / bandW)
    setHoverIdx(Math.max(0, Math.min(n - 1, idx)))
  }

  const hovered = hoverIdx !== null ? rows[hoverIdx] : null

  return (
    <section className="overflow-hidden" style={glassPanel()}>
      <header
        className="px-4 py-2.5 flex items-center justify-between gap-3 flex-wrap"
        style={{ borderBottom: `1px solid ${CHROME.border}` }}
      >
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide" style={{ color: CHROME.ink }}>
            Volume per day
          </h3>
          <p className="text-[10px]" style={{ color: CHROME.inkMuted }}>Total logs and errors over time</p>
        </div>
        <div className="flex items-center gap-3">
          <span className="flex items-center gap-1.5 text-[10px]" style={{ color: CHROME.inkSecondary }}>
            <span className="inline-block" style={{ width: 9, height: 9, background: MARKS.primary }} />
            Total
          </span>
          <span className="flex items-center gap-1.5 text-[10px]" style={{ color: CHROME.inkSecondary }}>
            <span className="inline-block" style={{ width: 12, height: 3, background: SERIES[1] }} />
            Errors
          </span>
          <button
            onClick={() => setShowTable((s) => !s)}
            className="text-[10px] px-2 py-1"
            style={{ border: `1px solid ${CHROME.border}`, color: CHROME.inkSecondary }}
          >
            {showTable ? 'Chart' : 'Table'}
          </button>
        </div>
      </header>

      {loading ? (
        <div className="h-[220px] flex items-center justify-center text-xs" style={{ color: CHROME.inkMuted }}>
          Loading…
        </div>
      ) : n === 0 ? (
        <div className="h-[220px] flex items-center justify-center text-xs" style={{ color: CHROME.inkMuted }}>
          No data in range
        </div>
      ) : showTable ? (
        <div className="max-h-[240px] overflow-y-auto">
          <table className="w-full text-[11px]" style={{ fontVariantNumeric: 'tabular-nums' }}>
            <thead className="sticky top-0" style={{ background: CHROME.surfaceRaised }}>
              <tr>
                {['Day', 'Total', 'Errors', 'Alerts'].map((h) => (
                  <th key={h} className="text-left px-3 py-1.5 font-medium" style={{ color: CHROME.inkMuted }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.bucket} style={{ borderTop: `1px solid ${CHROME.gridline}` }}>
                  <td className="px-3 py-1 font-mono" style={{ color: CHROME.inkSecondary }}>{r.bucket}</td>
                  <td className="px-3 py-1 font-mono" style={{ color: CHROME.ink }}>{(r.total || 0).toLocaleString()}</td>
                  <td className="px-3 py-1 font-mono" style={{ color: CHROME.ink }}>{(r.errors || 0).toLocaleString()}</td>
                  <td className="px-3 py-1 font-mono" style={{ color: CHROME.ink }}>{(r.alerts || 0).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="relative px-2 pt-2" style={{ opacity: refreshing ? 0.55 : 1, transition: 'opacity 150ms' }}>
          <svg
            ref={svgRef}
            viewBox={`0 0 ${W} ${H}`}
            className="w-full h-auto select-none"
            onPointerMove={handleMove}
            onPointerLeave={() => setHoverIdx(null)}
          >
            {yTicks.map((t) => (
              <g key={t}>
                <line x1={MARGIN.left} x2={W - MARGIN.right} y1={yFor(t)} y2={yFor(t)}
                      stroke={CHROME.gridline} strokeWidth="1" />
                <text x={MARGIN.left - 7} y={yFor(t) + 3} textAnchor="end" fontSize="9" fill={CHROME.inkMuted}>
                  {formatCompact(t)}
                </text>
              </g>
            ))}
            <line x1={MARGIN.left} x2={W - MARGIN.right} y1={MARGIN.top + INNER_H} y2={MARGIN.top + INNER_H}
                  stroke={CHROME.baseline} strokeWidth="1" />

            {/* Error area under the line: a solid fill at low alpha, never a
                gradient. It gives the second series visual weight without
                competing with the bars for the same ink. */}
            <path
              d={areaPath}
              fill={SERIES[1]}
              opacity="0.14"
              style={REDUCED ? undefined : { animation: 'ts-fade 620ms ease-out both 260ms' }}
            />

            {rows.map((r, i) => {
              const y = yFor(r.total || 0)
              const h = Math.max(0, MARGIN.top + INNER_H - y)
              return (
                <rect
                  key={i}
                  x={xFor(i) - barW / 2}
                  y={y}
                  width={barW}
                  height={h}
                  rx={Math.min(3, barW / 2)}
                  fill={MARKS.primary}
                  opacity={hoverIdx === null || hoverIdx === i ? 1 : 0.45}
                  style={{
                    transition: 'opacity 140ms',
                    // fill-box + bottom origin makes scaleY grow the bar up
                    // out of the axis instead of out of the SVG's corner.
                    transformBox: 'fill-box',
                    transformOrigin: '50% 100%',
                    ...(REDUCED ? null : {
                      animation: `ts-grow 560ms cubic-bezier(.2,.75,.3,1) both ${Math.min(i * 45, 700)}ms`,
                    }),
                  }}
                />
              )
            })}

            <polyline
              points={linePoints}
              fill="none"
              stroke={SERIES[1]}
              strokeWidth="2.5"
              strokeLinejoin="round"
              strokeLinecap="round"
              style={REDUCED ? undefined : {
                strokeDasharray: lineLength,
                strokeDashoffset: lineLength,
                animation: 'ts-draw 900ms cubic-bezier(.4,.1,.2,1) both 320ms',
              }}
            />

            {/* A dot per point, so a single-day range still shows the error
                series - a polyline of one point draws nothing at all. */}
            {rows.map((r, i) => (
              <circle
                key={`d${i}`}
                cx={xFor(i)}
                cy={yFor(r.errors || 0)}
                r={hoverIdx === i ? 4.5 : 2.5}
                fill={SERIES[1]}
                stroke={CHROME.surface}
                strokeWidth="1.5"
                style={{
                  transition: 'r 140ms',
                  ...(REDUCED ? null : { animation: `ts-fade 400ms ease-out both ${900 + i * 25}ms` }),
                }}
              />
            ))}

            {hovered && (
              <line x1={xFor(hoverIdx)} x2={xFor(hoverIdx)} y1={MARGIN.top} y2={MARGIN.top + INNER_H}
                    stroke={CHROME.borderStrong} strokeWidth="1" />
            )}

            {rows.map((r, i) =>
              i % labelEvery === 0 ? (
                <text key={i} x={xFor(i)} y={H - 7} textAnchor="middle" fontSize="9" fill={CHROME.inkMuted}>
                  {formatDay(r.bucket)}
                </text>
              ) : null,
            )}
          </svg>

          {hovered && (
            <div
              className="pointer-events-none absolute top-2 px-2.5 py-1.5 text-[11px]"
              style={{
                left: `${(xFor(hoverIdx) / W) * 100}%`,
                transform: hoverIdx > n / 2 ? 'translateX(-105%)' : 'translateX(8px)',
                background: CHROME.surfaceActive,
                border: `1px solid ${CHROME.borderStrong}`,
                minWidth: 130,
              }}
            >
              <div className="font-mono mb-1" style={{ color: CHROME.ink }}>{hovered.bucket}</div>
              {[['Total', hovered.total], ['Errors', hovered.errors], ['Alerts', hovered.alerts]].map(([k, v]) => (
                <div key={k} className="flex justify-between gap-3">
                  <span style={{ color: CHROME.inkMuted }}>{k}</span>
                  <span className="font-mono" style={{ color: CHROME.ink }}>{formatCompact(v ?? 0)}</span>
                </div>
              ))}
            </div>
          )}
          <style>{`
            @keyframes ts-grow { from { transform: scaleY(0); } to { transform: scaleY(1); } }
            @keyframes ts-draw { to { stroke-dashoffset: 0; } }
            @keyframes ts-fade { from { opacity: 0; } }
          `}</style>
        </div>
      )}
    </section>
  )
}
