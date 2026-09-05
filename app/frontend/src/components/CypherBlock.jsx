import { useState } from 'react'
import { CHROME } from './analytics/theme.js'

export default function CypherBlock({ cypher }) {
  const [open, setOpen] = useState(false)
  if (!cypher) return null
  return (
    <div className="mt-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-[10px] px-2 py-1"
        style={{ border: `1px solid ${CHROME.border}`, color: CHROME.inkSecondary }}
      >
        {open ? 'Hide' : 'Show'} generated Cypher
      </button>
      {open && (
        <pre
          className="mt-1.5 p-3 text-[11px] font-mono overflow-x-auto whitespace-pre-wrap"
          style={{ background: CHROME.surfaceRaised, color: CHROME.ink, border: `1px solid ${CHROME.border}` }}
        >
          {cypher}
        </pre>
      )}
    </div>
  )
}
