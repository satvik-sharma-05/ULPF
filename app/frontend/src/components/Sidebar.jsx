import { CHROME, GLASS, SERIES, STATUS } from './analytics/theme.js'

// Each destination carries one palette step. The dot is always shown and the
// active item repeats the same colour as its left rail, so the nav gives every
// page a small visual identity you start to navigate by - while the label
// still says what it is, since colour alone is never the wayfinding.
/* `short` is the two-letter mark shown on the collapsed rail. It exists
 * because the first version of the rail showed only each item's colour dot,
 * and six coloured dots are not navigation - you cannot tell Home from
 * Analytics without hovering every one of them. Two letters are unambiguous
 * ("Al" vs "An") where single initials would not be, and the full label stays
 * in the tooltip. */
const NAV = [
  { id: 'home', label: 'Home', short: 'Ho', hint: 'Overview and quick ask', color: SERIES[0] },
  { id: 'analytics', label: 'Analytics', short: 'An', hint: 'Volume, trends and insights', color: SERIES[1] },
  { id: 'alerts', label: 'Alerts', short: 'Al', hint: 'Triage queue and alert analytics', color: SERIES[5] },
  { id: 'live', label: 'Live logs', short: 'Lv', hint: 'Newest logs as they land', color: SERIES[2] },
  { id: 'lab', label: 'Parser Lab', short: 'Pl', hint: 'Paste or drop a log, see it parsed live - nothing stored', color: SERIES[2] },
  { id: 'schema', label: 'Schema & export', short: 'Sc', hint: 'Universal Event Schema, SIEM / Data Lake export', color: SERIES[4] },
  { id: 'chat', label: 'Chat', short: 'Ch', hint: 'Ask questions about the graph', color: SERIES[3] },
  { id: 'modes', label: 'Modes', short: 'Mo', hint: 'Sample / production / custom', color: SERIES[4] },
]

const MODE_LABEL = { sample: 'Sample (online)', production: 'Production (airgapped)', custom: 'Custom (online)' }

/* Collapsed, the sidebar keeps every destination reachable rather than
 * hiding them behind a menu: it narrows to a rail of the same nav items,
 * each still a real button with its colour dot and its label as a tooltip.
 * A collapse that removes navigation is a worse trade than the space it
 * frees, particularly on the narrow viewports that motivate it.
 */
const WIDE = 'w-56'
const RAIL = 'w-14'

export default function Sidebar({
  active, onNavigate, health, mode, modes, onModeChange, modeBusy,
  collapsed = false, onToggle,
}) {
  return (
    <nav
      className={`${collapsed ? RAIL : WIDE} shrink-0 flex flex-col relative z-10 transition-[width] duration-200`}
      style={{
        background: GLASS.chrome,
        backdropFilter: GLASS.blurStrong,
        WebkitBackdropFilter: GLASS.blurStrong,
        borderRight: `1px solid ${GLASS.borderOuter}`,
      }}
    >
      <div
        className={`py-4 flex items-center gap-2.5 ${collapsed ? 'px-3 justify-center' : 'px-4'}`}
        style={{ borderBottom: `1px solid ${GLASS.borderOuter}` }}
      >
        <button
          onClick={onToggle}
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          aria-expanded={!collapsed}
          className="w-8 h-8 flex items-center justify-center text-[11px] font-extrabold shrink-0"
          style={{ background: CHROME.primary, color: CHROME.primaryInk }}
        >
          UL
        </button>
        {!collapsed && (
          <div className="min-w-0">
            <div className="text-xs font-bold leading-tight tracking-tight" style={{ color: CHROME.ink }}>
              Universal Log
            </div>
            <div className="text-xs font-bold leading-tight tracking-tight" style={{ color: CHROME.ink }}>
              Pre-processing Framework
            </div>
          </div>
        )}
      </div>

      {/* Mode selector, in the sidebar so the active ingestion mode is visible
          from every page rather than only on the Modes tab. On the rail there
          is no room for a select, so it collapses to the mode's initial with
          the full label as a tooltip - the mode never becomes invisible,
          because "which mode am I in" is exactly the question this answers. */}
      {collapsed ? (
        <div
          className="px-3 py-3 flex justify-center"
          style={{ borderBottom: `1px solid ${GLASS.borderOuter}` }}
          title={`Mode: ${MODE_LABEL[mode] || mode || 'unknown'}`}
        >
          <span
            className="w-8 h-8 flex items-center justify-center text-[11px] font-bold"
            style={{ background: CHROME.surfaceActive, color: CHROME.ink }}
          >
            {(MODE_LABEL[mode] || mode || '?').charAt(0).toUpperCase()}
          </span>
        </div>
      ) : (
      <div className="px-4 py-3" style={{ borderBottom: `1px solid ${GLASS.borderOuter}` }}>
        <div className="text-[10px] uppercase tracking-wide mb-1.5" style={{ color: CHROME.inkMuted }}>
          Mode
        </div>
        <select
          value={mode || ''}
          onChange={(e) => onModeChange?.(e.target.value)}
          disabled={modeBusy || !modes?.length}
          className="w-full text-xs px-2 py-1.5 disabled:opacity-50"
          style={{ background: CHROME.surfaceRaised, border: `1px solid ${CHROME.border}`, color: CHROME.ink }}
        >
          {(modes || []).map((m) => (
            <option key={m.mode} value={m.mode}>
              {MODE_LABEL[m.mode] || m.mode}
              {m.problems?.length ? ' — needs setup' : ''}
            </option>
          ))}
        </select>
      </div>
      )}

      <div className="flex-1 py-2">
        {NAV.map((item) => {
          const isActive = active === item.id
          return (
            <button
              key={item.id}
              onClick={() => onNavigate(item.id)}
              title={collapsed ? `${item.label} - ${item.hint}` : item.hint}
              className={`w-full text-left py-2 text-xs font-semibold transition-colors flex items-center gap-2.5 ${collapsed ? 'px-0 justify-center' : 'px-4'}`}
              style={{
                background: isActive ? CHROME.surfaceActive : 'transparent',
                color: isActive ? CHROME.ink : CHROME.inkSecondary,
                boxShadow: isActive ? `inset 3px 0 0 ${item.color}` : 'none',
              }}
              onMouseEnter={(e) => { if (!isActive) e.currentTarget.style.background = CHROME.surfaceRaised }}
              onMouseLeave={(e) => { if (!isActive) e.currentTarget.style.background = 'transparent' }}
            >
              {collapsed ? (
                <span
                  className="w-8 h-7 flex items-center justify-center text-[10px] font-mono font-bold"
                  style={{
                    background: isActive ? item.color : 'transparent',
                    color: isActive ? '#FFFFFF' : CHROME.inkSecondary,
                    boxShadow: isActive ? 'none' : `inset 0 -2px 0 ${item.color}`,
                  }}
                >
                  {item.short}
                </span>
              ) : (
                <>
                  <span
                    className="w-1.5 h-1.5 shrink-0 transition-opacity"
                    style={{ background: item.color, opacity: isActive ? 1 : 0.45 }}
                    aria-hidden="true"
                  />
                  {item.label}
                </>
              )}
            </button>
          )
        })}
      </div>

      <div
        className={`py-3 flex flex-col gap-1.5 ${collapsed ? 'px-0 items-center' : 'px-4'}`}
        style={{ borderTop: `1px solid ${GLASS.borderOuter}` }}
      >
        <StatusLine label="Neo4j" ok={health?.neo4j} collapsed={collapsed} />
        <StatusLine label="LLM" ok={health?.llm} detail={health?.model} collapsed={collapsed} />
      </div>
    </nav>
  )
}

function StatusLine({ label, ok, detail, collapsed }) {
  if (collapsed) {
    // On the rail the status is just the light. The label and detail move into
    // the tooltip rather than disappearing.
    return (
      <span
        className="inline-block w-2 h-2"
        style={{ background: ok ? STATUS.good : STATUS.danger }}
        title={`${label}: ${ok ? (detail || 'up') : 'down'}`}
      />
    )
  }
  return (
    <div className="flex items-center gap-2 text-[10px]">
      <span
        className="inline-block w-1.5 h-1.5 shrink-0"
        style={{ background: ok ? STATUS.good : STATUS.danger }}
        aria-hidden="true"
      />
      <span style={{ color: CHROME.inkSecondary }}>{label}</span>
      <span className="ml-auto truncate font-mono" style={{ color: CHROME.inkMuted }} title={detail || ''}>
        {ok ? (detail || 'up') : 'down'}
      </span>
    </div>
  )
}
