import { CHROME } from './analytics/theme.js'

// ModeSelector - the manual strategy control. Defaults to a manual choice
// (text2cypher), not "auto": the point of this control for a demo is to let
// the presenter show each retrieval strategy on demand, rather than leaving
// it to an agent's judgment. "Auto (planner)" is included too, so the LLM
// planner - which can call multiple tools in sequence for one question - can
// still be demonstrated. Just not as the default.
const MODES = [
  { value: 'text2cypher', label: 'Text → Cypher', hint: 'Counts, filters, rankings, time ranges' },
  { value: 'graphrag', label: 'GraphRAG', hint: 'Explanations, root cause, "why" questions' },
  { value: 'auto', label: 'Auto (planner)', hint: 'LLM planner picks and sequences tools itself' },
]

export default function ModeSelector({ mode, onChange }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {MODES.map((m) => {
        const active = mode === m.value
        return (
          <button
            key={m.value}
            type="button"
            onClick={() => onChange(m.value)}
            title={m.hint}
            className="px-2.5 py-1 text-[11px] font-medium transition-colors"
            style={{
              background: active ? CHROME.primary : 'transparent',
              color: active ? CHROME.primaryInk : CHROME.inkSecondary,
              border: `1px solid ${active ? CHROME.primary : CHROME.border}`,
            }}
          >
            {m.label}
          </button>
        )
      })}
    </div>
  )
}
