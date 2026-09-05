import { useMemo, useRef, useState } from 'react'
import { CHROME, MARKS } from './theme.js'
import { formatDay, formatPct } from './format.js'
import Panel from './Panel.jsx'

const W = 720, H = 190
const M = { top: 14, right: 16, bottom: 24, left: 40 }
const IW = W - M.left - M.right, IH = H - M.top - M.bottom

/* Error RATE over time, as an area chart with a mean reference line.
 *
 * Deliberately separate from the volume chart rather than a second axis on it:
 * a dual-axis plot invents a correlation between two differently-scaled
 * measures. Rate answers "is it getting sicker", volume answers "is it getting
 * busier", and they are different questions.
 */
export default function ErrorRateChart({ data, loading }) {
  const [hover, setHover] = useState(null)
  const svgRef = useRef(null)
  const rows = data || []
  const n = rows.length

  const { max, mean, path, area } = useMemo(() => {
    if (!n) return { max: 1, mean: 0, path: '', area: '' }
    const vals = rows.map((r) => r.error_rate || 0)
    const mx = Math.max(1, Math.ceil(Math.max(...vals) * 1.25))
    const mn = vals.reduce((a, b) => a + b, 0) / vals.length
    const x = (i) => M.left + (n === 1 ? IW / 2 : (IW * i) / (n - 1))
    const y = (v) => M.top + IH - (v / mx) * IH
    const pts = rows.map((r, i) => `${x(i)},${y(r.error_rate || 0)}`)
    return {
      max: mx, mean: mn,
      path: `M${pts.join(' L')}`,
      area: `M${M.left},${M.top + IH} L${pts.join(' L')} L${x(n - 1)},${M.top + IH} Z`,
    }
  }, [rows, n])

  const x = (i) => M.left + (n === 1 ? IW / 2 : (IW * i) / (n - 1))
  const y = (v) => M.top + IH - (v / max) * IH

  return (
    <Panel title="Error rate" subtitle="Percentage of logs that are ERROR or worse, per day" bodyClass="px-2 pt-2 pb-1">
      {loading ? (
        <div className="h-[190px] flex items-center justify-center text-xs" style={{ color: CHROME.inkMuted }}>Loading…</div>
      ) : n === 0 ? (
        <div className="h-[190px] flex items-center justify-center text-xs" style={{ color: CHROME.inkMuted }}>No data</div>
      ) : (
        <div className="relative">
          <svg
            ref={svgRef} viewBox={`0 0 ${W} ${H}`} className="w-full h-auto"
            onPointerMove={(e) => {
              const r = svgRef.current.getBoundingClientRect()
              const lx = (e.clientX - r.left) * (W / r.width)
              const i = Math.round(((lx - M.left) / IW) * (n - 1))
              setHover(Math.max(0, Math.min(n - 1, i)))
            }}
            onPointerLeave={() => setHover(null)}
          >
            {[0, 0.5, 1].map((f) => (
              <g key={f}>
                <line x1={M.left} x2={W - M.right} y1={y(max * f)} y2={y(max * f)} stroke={CHROME.gridline} strokeWidth="1" />
                <text x={M.left - 6} y={y(max * f) + 3} textAnchor="end" fontSize="9" fill={CHROME.inkMuted}>
                  {(max * f).toFixed(0)}%
                </text>
              </g>
            ))}

            {/* area wash, then the 2px line on top */}
            <path d={area} fill={MARKS.primary} opacity="0.12" />
            <path
              d={path} fill="none" stroke={MARKS.primary} strokeWidth="2"
              strokeLinejoin="round" strokeLinecap="round"
              style={{ strokeDasharray: 2000, strokeDashoffset: 0, animation: 'er-draw 1100ms ease-out both' }}
            />

            {/* mean reference - the "is today unusual" baseline */}
            <line
              x1={M.left} x2={W - M.right} y1={y(mean)} y2={y(mean)}
              stroke={CHROME.inkDim} strokeWidth="1" strokeDasharray="4 4"
            />
            <text x={W - M.right} y={y(mean) - 4} textAnchor="end" fontSize="9" fill={CHROME.inkDim}>
              avg {mean.toFixed(1)}%
            </text>

            {hover != null && (
              <>
                <line x1={x(hover)} x2={x(hover)} y1={M.top} y2={M.top + IH} stroke={CHROME.borderStrong} strokeWidth="1" />
                <circle cx={x(hover)} cy={y(rows[hover].error_rate || 0)} r="4"
                        fill={MARKS.primary} stroke={CHROME.surface} strokeWidth="2" />
              </>
            )}

            {rows.map((r, i) =>
              i % Math.max(1, Math.ceil(n / 7)) === 0 ? (
                <text key={i} x={x(i)} y={H - 6} textAnchor="middle" fontSize="9" fill={CHROME.inkMuted}>
                  {formatDay(r.bucket)}
                </text>
              ) : null,
            )}
          </svg>

          {hover != null && (
            <div
              className="pointer-events-none absolute top-2 px-2.5 py-1.5 text-[11px]"
              style={{
                left: `${(x(hover) / W) * 100}%`,
                transform: hover > n / 2 ? 'translateX(-105%)' : 'translateX(8px)',
                background: CHROME.surfaceActive, border: `1px solid ${CHROME.borderStrong}`,
              }}
            >
              <div className="font-mono" style={{ color: CHROME.ink }}>{rows[hover].bucket}</div>
              <div style={{ color: CHROME.inkSecondary }}>
                {formatPct(rows[hover].error_rate || 0)} of {(rows[hover].total || 0).toLocaleString()}
              </div>
            </div>
          )}
          <style>{`@keyframes er-draw{from{stroke-dashoffset:2000}to{stroke-dashoffset:0}}`}</style>
        </div>
      )}
    </Panel>
  )
}
