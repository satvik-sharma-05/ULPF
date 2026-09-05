import { CHROME, STATUS } from '../analytics/theme.js'

// A mode card does two distinct things, so it exposes two controls rather
// than overloading one click: the body selects the card for INSPECTION (show
// me this mode's setup) while the button ACTIVATES it. Conflating them meant
// you couldn't read about production without switching to it.
export default function ModeCard({ spec, active, selected, busy, onInspect, onActivate }) {
  const ready = !spec.problems || spec.problems.length === 0

  return (
    <div
      onClick={() => onInspect(spec.mode)}
      className="flex flex-col gap-3 p-4 cursor-pointer transition-colors"
      style={{
        background: selected ? CHROME.surfaceRaised : CHROME.surface,
        border: `1px solid ${active ? CHROME.primary : selected ? CHROME.borderStrong : CHROME.border}`,
      }}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-xs font-semibold" style={{ color: CHROME.ink }}>{spec.label}</div>
          <div className="text-[10px] truncate" style={{ color: CHROME.inkMuted }} title={spec.source}>
            {spec.source}
          </div>
        </div>
        {active && (
          <span className="text-[9px] uppercase tracking-wide px-1.5 py-0.5 shrink-0 font-medium"
                style={{ background: CHROME.primary, color: CHROME.primaryInk }}>
            Active
          </span>
        )}
      </div>

      <p className="text-[11px] leading-relaxed" style={{ color: CHROME.inkSecondary }}>
        {spec.description}
      </p>

      <div className="flex items-center gap-3 text-[10px] flex-wrap" style={{ color: CHROME.inkMuted }}>
        <span className="flex items-center gap-1" style={{ color: ready ? STATUS.good : STATUS.warning }}>
          <span className="w-1.5 h-1.5 inline-block"
                style={{ background: ready ? STATUS.good : STATUS.warning }} aria-hidden="true" />
          {ready ? 'Ready' : `${spec.problems.length} to configure`}
        </span>
        <span>Embeddings {spec.embeddings_enabled ? 'on' : 'off'}</span>
        {spec.requires_kafka && <span>Needs Kafka</span>}
      </div>

      {!ready && (
        <ul className="flex flex-col gap-1">
          {spec.problems.map((p) => (
            <li key={p} className="text-[10px] px-2 py-1.5 leading-relaxed"
                style={{ background: CHROME.surfaceActive, color: CHROME.inkSecondary }}>
              {p}
            </li>
          ))}
        </ul>
      )}

      {spec.neo4j_uri && (
        <div className="text-[10px] font-mono truncate" style={{ color: CHROME.inkMuted }} title={spec.neo4j_uri}>
          {spec.neo4j_uri}
        </div>
      )}

      <button
        onClick={(e) => { e.stopPropagation(); onActivate?.(spec.mode) }}
        disabled={active || busy}
        className="mt-auto px-3 py-1.5 text-[11px] font-medium disabled:opacity-40"
        style={
          active
            ? { border: `1px solid ${CHROME.border}`, color: CHROME.inkMuted }
            : { background: CHROME.primary, color: CHROME.primaryInk }
        }
      >
        {active ? 'Currently active' : busy ? 'Switching…' : 'Activate this mode'}
      </button>
    </div>
  )
}
