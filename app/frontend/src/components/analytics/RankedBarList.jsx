import { CHROME, MARKS, SERIES, GLASS, glassPanel } from './theme.js'
import { formatCompact } from './format.js'

// Ranked bars, each row taking the next step of the palette. The colour is
// decorative - rank is already carried by the ordering and by the printed
// value - so cycling hues costs nothing and stops a long list reading as a
// grey block. A caller that needs one fixed colour passes `color`.
//
// Every value is direct-labelled, so the bar is a visual aid rather than the
// only way to read the number.
export default function RankedBarList({ title, subtitle, rows, loading, color, emptyLabel = 'No data' }) {
  const list = rows || []
  const max = Math.max(1, ...list.map((r) => r.value || 0))

  return (
    <section className="overflow-hidden" style={glassPanel()}>
      <header className="px-4 py-2.5" style={{ borderBottom: `1px solid ${CHROME.border}` }}>
        <h3 className="text-xs font-semibold uppercase tracking-wide" style={{ color: CHROME.ink }}>{title}</h3>
        {subtitle && <p className="text-[10px]" style={{ color: CHROME.inkMuted }}>{subtitle}</p>}
      </header>

      {loading ? (
        <div className="px-4 py-10 text-center text-xs" style={{ color: CHROME.inkMuted }}>Loading…</div>
      ) : list.length === 0 ? (
        <div className="px-4 py-10 text-center text-xs" style={{ color: CHROME.inkMuted }}>{emptyLabel}</div>
      ) : (
        <div className="px-4 py-3 flex flex-col gap-2">
          {list.map((r, i) => (
            <div key={r.label}>
              <div className="flex items-baseline justify-between text-[11px] mb-1 gap-2">
                <span className="truncate font-mono" style={{ color: CHROME.inkSecondary }} title={r.label}>
                  {r.label}
                </span>
                <span className="flex items-baseline gap-2 shrink-0">
                  {r.secondary !== undefined && (
                    <span className="text-[10px]" style={{ color: CHROME.inkMuted }}>{r.secondary}</span>
                  )}
                  <span className="font-mono" style={{ color: CHROME.ink }}>{formatCompact(r.value)}</span>
                </span>
              </div>
              <div style={{ height: 4, background: MARKS.track }}>
                <div
                  style={{
                    height: 4,
                    width: `${Math.max(1, ((r.value || 0) / max) * 100)}%`,
                    background: color || SERIES[i % SERIES.length],
                  }}
                />
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
