import { CHROME, severityMeta } from './theme.js'
import Panel from './Panel.jsx'

/* The most-repeated error messages - noise ranking.
 *
 * The single most actionable panel here: one pattern usually accounts for a
 * large share of all errors, and fixing it clears the board.
 */
export default function TopMessages({ rows, loading }) {
  const list = rows || []
  const max = Math.max(1, ...list.map((r) => r.occurrences || 0))

  return (
    <Panel title="Most repeated errors" subtitle="Grouped by message prefix — the noise worth silencing">
      {loading ? (
        <div className="py-10 text-center text-xs" style={{ color: CHROME.inkMuted }}>Loading…</div>
      ) : list.length === 0 ? (
        <div className="py-10 text-center text-xs" style={{ color: CHROME.inkMuted }}>No repeated errors</div>
      ) : (
        <ol className="flex flex-col gap-2.5">
          {list.map((r, i) => {
            const meta = severityMeta(r.severity)
            return (
              <li key={i} style={{ animation: `tm-in 420ms ease both ${i * 45}ms` }}>
                <div className="flex items-baseline gap-2 mb-1">
                  <span className="text-[10px] font-mono w-4 shrink-0" style={{ color: CHROME.inkDim }}>
                    {String(i + 1).padStart(2, '0')}
                  </span>
                  <span
                    className="text-[11px] font-mono flex-1 truncate"
                    style={{ color: CHROME.inkSecondary }}
                    title={r.pattern}
                  >
                    {r.pattern}
                  </span>
                  <span className="text-[11px] font-mono shrink-0" style={{ color: CHROME.ink }}>
                    {(r.occurrences || 0).toLocaleString()}×
                  </span>
                </div>
                <div className="ml-6 flex items-center gap-2">
                  <div className="flex-1 h-1" style={{ background: CHROME.gridline }}>
                    <div
                      className="h-1"
                      style={{
                        width: `${((r.occurrences || 0) / max) * 100}%`,
                        background: meta.color,
                        animation: 'tm-grow 700ms cubic-bezier(.2,.7,.3,1) both',
                        transformOrigin: 'left',
                      }}
                    />
                  </div>
                  <span className="text-[9px] shrink-0" style={{ color: CHROME.inkDim }}>
                    {(r.hosts || []).length > 1 ? `${(r.hosts || []).length}+ hosts` : (r.hosts || [])[0] || ''}
                  </span>
                </div>
              </li>
            )
          })}
          <style>{`@keyframes tm-in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@keyframes tm-grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}`}</style>
        </ol>
      )}
    </Panel>
  )
}
