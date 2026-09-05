import { useMemo, useState } from 'react'
import { CHROME, HEAT_STEPS } from './theme.js'
import { formatCompact, formatDay } from './format.js'
import Panel from './Panel.jsx'

/* Day × hour-of-day heatmap.
 *
 * Sequential encoding, one hue light→dark, per the dataviz rule for magnitude:
 * a rainbow here would imply categories where there is only "more" and "less".
 * The scale is applied to sqrt(value) rather than the raw count because log
 * volume is heavily skewed - one batch-window cell would otherwise saturate
 * and flatten every other cell to the same near-empty shade.
 */
const STEPS = HEAT_STEPS

export default function ActivityHeatmap({ data, loading }) {
  const [hover, setHover] = useState(null)
  const [metric, setMetric] = useState('total')

  const { days, grid, max, dayTotals } = useMemo(() => {
    const rows = data || []
    const dayset = [...new Set(rows.map((r) => r.day))].sort()
    const g = {}
    let m = 0
    for (const r of rows) {
      const v = metric === 'errors' ? (r.errors || 0) : (r.total || 0)
      g[`${r.day}|${r.hour}`] = v
      if (v > m) m = v
    }
    // Row totals so a day's overall size is readable without hovering 24
    // cells - the grid shows the SHAPE of a day, this shows its size.
    const totals = {}
    for (const r of rows) {
      const v = metric === 'errors' ? (r.errors || 0) : (r.total || 0)
      totals[r.day] = (totals[r.day] || 0) + v
    }
    return { days: dayset, grid: g, max: m, dayTotals: totals }
  }, [data, metric])

  const shade = (v) => {
    if (!v) return CHROME.gridline
    const t = Math.sqrt(v) / Math.sqrt(max || 1)
    return STEPS[Math.min(STEPS.length - 1, Math.max(0, Math.round(t * (STEPS.length - 1))))]
  }

  return (
    <Panel
      title="Activity by hour"
      subtitle={`When logs land, UTC · ${metric === 'errors' ? 'errors only' : 'all logs'}`}
      action={
        <div className="flex">
          {['total', 'errors'].map((m) => (
            <button
              key={m}
              onClick={() => setMetric(m)}
              className="px-2 py-1 text-[10px] transition-colors"
              style={{
                background: metric === m ? CHROME.accent : 'transparent',
                color: metric === m ? CHROME.accentInk : CHROME.inkMuted,
                border: `1px solid ${metric === m ? CHROME.accent : CHROME.border}`,
                marginLeft: -1,
              }}
            >
              {m === 'total' ? 'All' : 'Errors'}
            </button>
          ))}
        </div>
      }
    >
      {loading ? (
        <div className="py-12 text-center text-xs" style={{ color: CHROME.inkMuted }}>Loading…</div>
      ) : days.length === 0 ? (
        <div className="py-12 text-center text-xs" style={{ color: CHROME.inkMuted }}>No dated logs</div>
      ) : (
        <div className="relative overflow-x-auto">
          <div className="inline-block min-w-full">
            {/* hour ruler */}
            <div className="flex gap-[2px] mb-1 ml-[74px]">
              {Array.from({ length: 24 }, (_, h) => (
                <div key={h} className="w-[22px] text-center text-[9px]" style={{ color: CHROME.inkDim }}>
                  {h % 3 === 0 ? String(h).padStart(2, '0') : ''}
                </div>
              ))}
            </div>
            {days.map((day, di) => (
              <div key={day} className="flex items-center gap-[2px] mb-[2px]">
                <div className="w-[70px] text-[9px] font-mono shrink-0 text-right pr-1" style={{ color: CHROME.inkMuted }}>
                  {formatDay(day)}
                </div>
                {Array.from({ length: 24 }, (_, h) => {
                  const v = grid[`${day}|${h}`] || 0
                  const key = `${day}|${h}`
                  return (
                    <div
                      key={h}
                      onMouseEnter={() => setHover({ day, hour: h, value: v })}
                      onMouseLeave={() => setHover(null)}
                      className="w-[22px] h-[22px] cursor-default"
                      style={{
                        background: shade(v),
                        border: `1px solid ${CHROME.gridline}`,
                        outline: hover && hover.day === day && hover.hour === h
                          ? `2px solid ${CHROME.ink}` : 'none',
                        // A hovered cell lifts rather than only outlining, so
                        // the pointer's target is unmistakable on a dense grid.
                        transform: hover && hover.day === day && hover.hour === h ? 'scale(1.25)' : 'none',
                        transition: 'transform 120ms ease, outline-color 120ms',
                        zIndex: hover && hover.day === day && hover.hour === h ? 2 : 'auto',
                        position: 'relative',
                        // Staggered fade-in across the grid: reads as the data
                        // arriving rather than a block appearing.
                        animation: `hm-in 320ms ease both ${Math.min(di * 22 + h * 6, 700)}ms`,
                      }}
                      title={`${day} ${String(h).padStart(2, '0')}:00 — ${v.toLocaleString()}`}
                    />
                  )
                })}
                <span className="ml-2 text-[10px] font-mono shrink-0" style={{ color: CHROME.inkMuted }}>
                  {formatCompact(dayTotals[day] || 0)}
                </span>
              </div>
            ))}
          </div>

          <div className="flex items-center gap-2 mt-3 ml-[74px]">
            <span className="text-[9px]" style={{ color: CHROME.inkDim }}>less</span>
            {STEPS.map((c) => (
              <span key={c} className="w-[18px] h-[12px]" style={{ background: c }} />
            ))}
            <span className="text-[9px]" style={{ color: CHROME.inkDim }}>more</span>
            {hover && (
              <span className="ml-3 text-[10px] font-mono" style={{ color: CHROME.ink }}>
                {formatDay(hover.day)} {String(hover.hour).padStart(2, '0')}:00 ·{' '}
                {formatCompact(hover.value)}
              </span>
            )}
          </div>
          <style>{`@keyframes hm-in{from{opacity:0;transform:scale(.6)}to{opacity:1;transform:none}}`}</style>
        </div>
      )}
    </Panel>
  )
}
