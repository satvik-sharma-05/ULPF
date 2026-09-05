import { useState } from 'react'
import { CHROME } from './analytics/theme.js'

// Shows what the LLM planner actually decided to do - the single most useful
// thing to surface for agentic retrieval. A wrong answer is far easier to
// diagnose from "it called graph_neighbors on the wrong entity" than from the
// final prose alone.
function paramsToText(params) {
  if (!params || Object.keys(params).length === 0) return ''
  return Object.entries(params).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(', ')
}

export default function PlanTrace({ plan }) {
  const [open, setOpen] = useState(true)
  if (!plan || plan.length === 0) return null

  return (
    <div className="mt-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-[10px] px-2 py-1"
        style={{ border: `1px solid ${CHROME.border}`, color: CHROME.inkSecondary }}
      >
        {open ? 'Hide' : 'Show'} planner trace ({plan.length} step{plan.length === 1 ? '' : 's'})
      </button>
      {open && (
        <ol className="mt-1.5 flex flex-col gap-1">
          {plan.map((step, i) => (
            <li key={i} className="text-[11px] px-2.5 py-1.5"
                style={{ background: CHROME.surfaceRaised, border: `1px solid ${CHROME.border}` }}>
              <div className="flex items-center gap-2">
                {/* ok/failed as a glyph, not a colored dot */}
                <span className="font-mono text-[10px] w-3 shrink-0" style={{ color: CHROME.ink }} aria-hidden="true">
                  {step.ok ? '✓' : '×'}
                </span>
                <span className="font-mono font-semibold" style={{ color: CHROME.ink }}>{step.tool}</span>
                <span className="font-mono truncate" style={{ color: CHROME.inkMuted }}>
                  {paramsToText(step.params)}
                </span>
              </div>
              {step.reason && (
                <div className="italic mt-0.5 pl-5" style={{ color: CHROME.inkMuted }}>{step.reason}</div>
              )}
              <div className="mt-0.5 pl-5" style={{ color: CHROME.inkSecondary }}>{step.note}</div>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}
