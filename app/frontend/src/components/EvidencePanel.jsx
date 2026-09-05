import { useState } from 'react'
import { CHROME } from './analytics/theme.js'
import SeverityTag from './logs/SeverityTag.jsx'

// GraphRAG evidence: collapsed by default so the answer reads first, but
// present for every graphrag/hybrid response - the retrieval is the part
// worth being transparent about, since a wrong answer originates there
// rather than in the final prose.
function LogRow({ log, tag }) {
  return (
    <div className="py-1.5" style={{ borderBottom: `1px solid ${CHROME.gridline}` }}>
      <div className="flex items-center gap-2 text-[10px] font-mono" style={{ color: CHROME.inkMuted }}>
        <span>{log.timestamp}</span>
        <SeverityTag severity={log.severity} compact />
        <span>{log.hostname}/{log.process}</span>
        {tag && <span className="ml-auto italic">{tag}</span>}
      </div>
      <div className="text-[11px] mt-0.5" style={{ color: CHROME.inkSecondary }}>{log.message}</div>
    </div>
  )
}

export default function EvidencePanel({ context, retrievalMethod }) {
  const [open, setOpen] = useState(false)
  if (!context || (!context.logs?.length && !context.neighbours?.length)) return null
  const total = (context.logs?.length || 0) + (context.neighbours?.length || 0)

  return (
    <div className="mt-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-[10px] px-2 py-1"
        style={{ border: `1px solid ${CHROME.border}`, color: CHROME.inkSecondary }}
      >
        {open ? 'Hide' : 'Show'} evidence ({total} log{total === 1 ? '' : 's'}
        {retrievalMethod ? `, via ${retrievalMethod}` : ''})
      </button>
      {open && (
        <div className="mt-1.5 p-3 max-h-64 overflow-y-auto"
             style={{ background: CHROME.surfaceRaised, border: `1px solid ${CHROME.border}` }}>
          {context.logs?.map((log) => <LogRow key={`seed-${log.id}`} log={log} tag="seed" />)}
          {context.neighbours?.map((log) => (
            <LogRow key={`nbr-${log.id}`} log={log} tag={`via ${log.via_label}`} />
          ))}
        </div>
      )}
    </div>
  )
}
