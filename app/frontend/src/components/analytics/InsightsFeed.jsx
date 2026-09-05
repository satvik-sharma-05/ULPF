import { CHROME, GLASS, glassPanel } from './theme.js'

// Insight severity as a bracketed WORD, not a colored chip - in a monochrome
// palette the level has to be readable as text or it isn't encoded at all.
const LEVELS = {
  critical: { tag: 'CRITICAL', glyph: '!!' },
  warning: { tag: 'WARNING', glyph: '!' },
  good: { tag: 'IMPROVED', glyph: '+' },
  info: { tag: 'INFO', glyph: '·' },
}

export default function InsightsFeed({ insights, loading }) {
  return (
    <section className="overflow-hidden" style={glassPanel()}>
      <header className="px-4 py-2.5" style={{ borderBottom: `1px solid ${CHROME.border}` }}>
        <h2 className="text-xs font-semibold uppercase tracking-wide" style={{ color: CHROME.ink }}>
          Insights
        </h2>
        <p className="text-[10px]" style={{ color: CHROME.inkMuted }}>
          Computed from the same aggregates as the numbers above
        </p>
      </header>

      {loading ? (
        <div className="px-4 py-8 text-center text-xs" style={{ color: CHROME.inkMuted }}>Loading…</div>
      ) : !insights || insights.length === 0 ? (
        <div className="px-4 py-8 text-center text-xs" style={{ color: CHROME.inkMuted }}>No insights yet</div>
      ) : (
        <ul>
          {insights.map((ins, i) => {
            const meta = LEVELS[ins.level] || LEVELS.info
            return (
              <li
                key={ins.id}
                className="px-4 py-2.5 flex gap-3"
                style={{ borderTop: i === 0 ? 'none' : `1px solid ${CHROME.gridline}` }}
              >
                <span
                  className="shrink-0 w-5 h-5 flex items-center justify-center text-[9px] font-mono mt-0.5"
                  style={{ border: `1px solid ${CHROME.borderStrong}`, color: CHROME.ink }}
                  aria-hidden="true"
                >
                  {meta.glyph}
                </span>
                <div className="min-w-0">
                  <div className="flex items-baseline gap-2 flex-wrap">
                    <span className="text-xs font-medium" style={{ color: CHROME.ink }}>{ins.title}</span>
                    <span className="text-[9px] font-mono uppercase tracking-wide" style={{ color: CHROME.inkMuted }}>
                      [{meta.tag}]
                    </span>
                  </div>
                  <p className="text-[11px] mt-0.5 leading-relaxed" style={{ color: CHROME.inkSecondary }}>
                    {ins.detail}
                  </p>
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
