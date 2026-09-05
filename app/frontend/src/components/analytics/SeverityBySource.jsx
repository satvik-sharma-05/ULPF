import { CHROME, SERIES } from './theme.js'
import { formatCompact } from './format.js'
import Panel from './Panel.jsx'

/* Stacked severity split per source type.
 *
 * Part-to-whole per row, so a source that is merely chatty (mostly INFO) reads
 * differently from one that is actually failing. Segments carry a 2px surface
 * gap rather than a stroke - the gap is what separates touching fills without
 * adding ink that isn't data.
 */
const BANDS = [
  { key: 'critical', label: 'Critical', color: SERIES[4] },
  { key: 'errors', label: 'Error', color: SERIES[0] },
  { key: 'warnings', label: 'Warning', color: SERIES[2] },
  { key: '_rest', label: 'Info / other', color: SERIES[1] },
]

export default function SeverityBySource({ rows, loading }) {
  const list = (rows || []).map((r) => ({
    ...r,
    _rest: Math.max(0, (r.total || 0) - (r.critical || 0) - (r.errors || 0) - (r.warnings || 0)),
  }))

  return (
    <Panel
      title="Health by source"
      subtitle="Severity split per source type — chatty vs actually failing"
      action={
        <div className="flex items-center gap-2 flex-wrap justify-end">
          {BANDS.map((b) => (
            <span key={b.key} className="flex items-center gap-1 text-[9px]" style={{ color: CHROME.inkMuted }}>
              <span className="w-2 h-2" style={{ background: b.color }} />
              {b.label}
            </span>
          ))}
        </div>
      }
    >
      {loading ? (
        <div className="py-10 text-center text-xs" style={{ color: CHROME.inkMuted }}>Loading…</div>
      ) : list.length === 0 ? (
        <div className="py-10 text-center text-xs" style={{ color: CHROME.inkMuted }}>No data</div>
      ) : (
        <div className="flex flex-col gap-2.5">
          {list.map((r, idx) => {
            const total = r.total || 1
            const bad = (r.critical || 0) + (r.errors || 0)
            return (
              <div key={r.source_type}>
                <div className="flex items-baseline justify-between text-[11px] mb-1">
                  <span className="font-mono" style={{ color: CHROME.inkSecondary }}>{r.source_type}</span>
                  <span className="flex gap-2">
                    <span style={{ color: CHROME.inkMuted }}>
                      {((bad / total) * 100).toFixed(1)}% bad
                    </span>
                    <span className="font-mono" style={{ color: CHROME.ink }}>{formatCompact(r.total)}</span>
                  </span>
                </div>
                <div className="flex h-2.5 overflow-hidden" style={{ background: CHROME.gridline }}>
                  {BANDS.map((b) => {
                    const v = r[b.key] || 0
                    if (!v) return null
                    return (
                      <div
                        key={b.key}
                        title={`${b.label}: ${v.toLocaleString()}`}
                        style={{
                          width: `${(v / total) * 100}%`,
                          background: b.color,
                          marginRight: 2,   // surface gap between segments
                          animation: `sb-grow 620ms cubic-bezier(.2,.7,.3,1) both ${idx * 55}ms`,
                          transformOrigin: 'left',
                        }}
                      />
                    )
                  })}
                </div>
              </div>
            )
          })}
          <style>{`@keyframes sb-grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}`}</style>
        </div>
      )}
    </Panel>
  )
}
