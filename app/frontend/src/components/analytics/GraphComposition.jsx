import { CHROME } from './theme.js'
import { formatCompact } from './format.js'
import Panel from './Panel.jsx'

/* Node counts per label - a treemap-ish proportional grid showing what the
 * knowledge graph is made of. Area encodes magnitude; :Log always dominates,
 * which is itself the honest picture. */
export default function GraphComposition({ rows, loading }) {
  const list = (rows || []).filter((r) => r.count > 0).slice(0, 18)
  const total = list.reduce((s, r) => s + r.count, 0) || 1
  const max = Math.max(...list.map((r) => r.count), 1)

  return (
    <Panel
      title="Graph composition"
      subtitle={`${list.length} node labels · ${formatCompact(total)} nodes`}
    >
      {loading ? (
        <div className="py-10 text-center text-xs" style={{ color: CHROME.inkMuted }}>Loading…</div>
      ) : list.length === 0 ? (
        <div className="py-10 text-center text-xs" style={{ color: CHROME.inkMuted }}>No nodes</div>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {list.map((r, i) => {
            // Area ~ magnitude, on a sqrt scale so :Log doesn't crush the rest
            // into invisibility while still reading as clearly biggest.
            const t = Math.sqrt(r.count) / Math.sqrt(max)
            const size = 44 + t * 74
            return (
              <div
                key={r.label}
                title={`${r.label}: ${r.count.toLocaleString()} nodes`}
                className="flex flex-col items-center justify-center px-2 overflow-hidden"
                style={{
                  width: size, height: size,
                  background: CHROME.surfaceRaised,
                  border: `1px solid ${CHROME.border}`,
                  animation: `gc-in 420ms cubic-bezier(.2,.7,.3,1) both ${i * 35}ms`,
                }}
              >
                <span className="text-[9px] font-mono truncate max-w-full" style={{ color: CHROME.inkMuted }}>
                  {r.label}
                </span>
                <span className="text-[11px] font-semibold" style={{ color: CHROME.ink }}>
                  {formatCompact(r.count)}
                </span>
              </div>
            )
          })}
          <style>{`@keyframes gc-in{from{opacity:0;transform:scale(.7)}to{opacity:1;transform:none}}`}</style>
        </div>
      )}
    </Panel>
  )
}
