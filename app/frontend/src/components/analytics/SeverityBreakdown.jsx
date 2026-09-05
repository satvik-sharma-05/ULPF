import { CHROME, MARKS, severityMeta, GLASS, glassPanel } from './theme.js'
import { formatCompact, formatPct } from './format.js'
import SeverityTag from '../logs/SeverityTag.jsx'

// Replaces the donut. A donut needed hue to separate seven slices; with a
// monochrome palette adjacent grey arcs are genuinely indistinguishable, so
// this is a labelled bar list instead - ordered by severity rank, with the
// count and share printed on every row. Nothing here depends on telling two
// shades apart.
export default function SeverityBreakdown({ rows, loading }) {
  const sorted = [...(rows || [])].sort(
    (a, b) => severityMeta(a.severity).rank - severityMeta(b.severity).rank,
  )
  const total = sorted.reduce((s, r) => s + (r.count || 0), 0)

  return (
    <section className="overflow-hidden" style={glassPanel()}>
      <header className="px-4 py-2.5" style={{ borderBottom: `1px solid ${CHROME.border}` }}>
        <h3 className="text-xs font-semibold uppercase tracking-wide" style={{ color: CHROME.ink }}>
          Severity mix
        </h3>
        <p className="text-[10px]" style={{ color: CHROME.inkMuted }}>
          {formatCompact(total)} logs, most severe first
        </p>
      </header>

      {loading ? (
        <div className="px-4 py-10 text-center text-xs" style={{ color: CHROME.inkMuted }}>Loading…</div>
      ) : total === 0 ? (
        <div className="px-4 py-10 text-center text-xs" style={{ color: CHROME.inkMuted }}>No data</div>
      ) : (
        <div className="px-4 py-3 flex flex-col gap-2.5">
          {sorted.map((r) => {
            const share = total ? (r.count / total) * 100 : 0
            return (
              <div key={r.severity}>
                <div className="flex items-baseline justify-between mb-1 gap-2">
                  <SeverityTag severity={r.severity} />
                  <span className="flex items-baseline gap-2.5 text-[11px] shrink-0">
                    <span className="font-mono" style={{ color: CHROME.ink }}>{formatCompact(r.count)}</span>
                    <span className="font-mono w-12 text-right" style={{ color: CHROME.inkMuted }}>
                      {formatPct(share)}
                    </span>
                  </span>
                </div>
                <div style={{ height: 4, background: MARKS.track }}>
                  <div style={{ height: 4, width: `${Math.max(0.5, share)}%`, background: severityMeta(r.severity).color }} />
                </div>
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}
