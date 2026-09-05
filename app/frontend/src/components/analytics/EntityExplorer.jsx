import { useState } from 'react'
import { CHROME, SERIES } from './theme.js'
import { formatCompact } from './format.js'
import Panel from './Panel.jsx'

/* The entity layer, which the dashboard otherwise never shows.
 *
 * This is the part that makes the store a knowledge graph rather than a log
 * table: users, IPs, devices, VMs and operations extracted out of free text
 * and ranked by how many logs reference them.
 */
const ORDER = ['User', 'IPAddress', 'Device', 'VM', 'Datastore', 'Operation',
               'Pod', 'ErrorCode', 'File', 'SystemdUnit', 'Executable', 'Task']

export default function EntityExplorer({ entities, loading }) {
  const available = ORDER.filter((k) => (entities || {})[k]?.length)
  const [active, setActive] = useState(null)
  const current = active && available.includes(active) ? active : available[0]
  const rows = (entities || {})[current] || []
  const max = Math.max(1, ...rows.map((r) => r.mentions || 0))

  return (
    <Panel
      title="Entity explorer"
      subtitle="Extracted entities ranked by how many logs reference them"
      action={
        <span className="text-[10px] font-mono" style={{ color: CHROME.inkMuted }}>
          {available.length} types
        </span>
      }
    >
      {loading ? (
        <div className="py-10 text-center text-xs" style={{ color: CHROME.inkMuted }}>Loading…</div>
      ) : available.length === 0 ? (
        <div className="py-10 text-center text-xs" style={{ color: CHROME.inkMuted }}>No entities extracted</div>
      ) : (
        <>
          <div className="flex flex-wrap gap-1 mb-3">
            {available.map((k) => (
              <button
                key={k}
                onClick={() => setActive(k)}
                className="px-2 py-1 text-[10px] transition-colors"
                style={{
                  background: k === current ? CHROME.accent : 'transparent',
                  color: k === current ? CHROME.accentInk : CHROME.inkMuted,
                  border: `1px solid ${k === current ? CHROME.accent : CHROME.border}`,
                }}
              >
                {k}
              </button>
            ))}
          </div>
          <div className="flex flex-col gap-2">
            {rows.map((r, i) => (
              <div key={`${current}-${r.value}`} style={{ animation: `ee-in 360ms ease both ${i * 50}ms` }}>
                <div className="flex items-baseline justify-between text-[11px] mb-1 gap-2">
                  <span className="font-mono truncate" style={{ color: CHROME.inkSecondary }} title={r.value}>
                    {r.value}
                  </span>
                  <span className="font-mono shrink-0" style={{ color: CHROME.ink }}>
                    {formatCompact(r.mentions)}
                  </span>
                </div>
                <div className="h-1.5" style={{ background: CHROME.gridline }}>
                  <div
                    className="h-1.5"
                    style={{
                      width: `${((r.mentions || 0) / max) * 100}%`,
                      background: SERIES[i % SERIES.length],
                      animation: 'ee-grow 600ms cubic-bezier(.2,.7,.3,1) both',
                      transformOrigin: 'left',
                    }}
                  />
                </div>
              </div>
            ))}
          </div>
          <style>{`@keyframes ee-in{from{opacity:0;transform:translateX(-6px)}to{opacity:1;transform:none}}
@keyframes ee-grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}`}</style>
        </>
      )}
    </Panel>
  )
}
