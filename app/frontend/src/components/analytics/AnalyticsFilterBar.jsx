import { CHROME, GLASS, SERIES, glassPanel, severityMeta } from './theme.js'

/* The filter bar for the whole analytics dashboard.
 *
 * Everything here maps onto one backend LogFilter, which is threaded through
 * every aggregation - so changing a date here changes the tiles, the charts,
 * the rankings and the written insights together. There is deliberately no
 * per-panel filter: two panels disagreeing about which logs they describe is
 * the failure mode this design exists to prevent.
 *
 * The presets are not decoration. The corpus is historical and batch-ingested,
 * so "last 7 days" of wall-clock time is usually empty; the presets are
 * computed from the data's OWN range (earliest/latest in the graph), which is
 * what a person actually means by "the recent part of this data".
 */

function shiftDays(day, delta) {
  const d = new Date(`${day}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + delta)
  return d.toISOString().slice(0, 10)
}

export default function AnalyticsFilterBar({
  value, onChange, onClear, filters, bounds, resultCount, loading,
}) {
  const set = (patch) => onChange({ ...value, ...patch })

  const active = Object.values(value || {}).filter((v) => v !== undefined && v !== null && String(v) !== '').length
  const earliest = bounds?.earliest ? String(bounds.earliest).slice(0, 10) : null
  const latest = bounds?.latest ? String(bounds.latest).slice(0, 10) : null

  // Windows measured back from the LAST day present in the graph, not from
  // today - see the note above.
  const presets = latest
    ? [
        { label: 'Last day', start: latest, end: latest },
        { label: 'Last 7 days', start: shiftDays(latest, -6), end: latest },
        { label: 'Last 30 days', start: shiftDays(latest, -29), end: latest },
        { label: 'All time', start: '', end: '' },
      ]
    : []

  // Inputs are the one place the glass stops: a translucent field over a
  // drifting backdrop makes typed text hard to read, and a form control that
  // is hard to read is not a style choice.
  const field = {
    background: CHROME.surface,
    border: `1px solid ${GLASS.borderOuter}`,
    color: CHROME.ink,
  }

  return (
    <section className="overflow-hidden" style={glassPanel()}>

      <div className="px-4 py-3 flex flex-col gap-3">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-baseline gap-2">
            <h3 className="text-xs font-bold uppercase tracking-wide" style={{ color: CHROME.ink }}>
              Filter
            </h3>
            <span className="text-[10px]" style={{ color: CHROME.inkMuted }}>
              {active === 0
                ? 'Showing the whole graph'
                : `${active} filter${active === 1 ? '' : 's'} applied to every panel below`}
            </span>
          </div>

          <div className="flex items-center gap-2">
            {resultCount !== undefined && resultCount !== null && (
              <span className="text-[11px] font-mono" style={{ color: CHROME.inkSecondary }}>
                {loading ? 'loading…' : `${resultCount.toLocaleString()} logs`}
              </span>
            )}
            {active > 0 && (
              <button
                onClick={onClear}
                className="text-[11px] px-2.5 py-1"
                style={{ border: `1px solid ${CHROME.borderStrong}`, color: CHROME.inkSecondary }}
              >
                Clear all
              </button>
            )}
          </div>
        </div>

        {/* Search sits first and wide: it is the control people reach for. */}
        <input
          type="search"
          value={value.search || ''}
          onChange={(e) => set({ search: e.target.value })}
          placeholder="Search log messages…"
          className="w-full text-sm px-3 py-2"
          style={field}
        />

        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-wide font-semibold" style={{ color: CHROME.inkMuted }}>
              From
            </span>
            <input
              type="date"
              value={value.start || ''}
              min={earliest || undefined}
              max={latest || undefined}
              onChange={(e) => set({ start: e.target.value })}
              className="text-xs px-2 py-1.5"
              style={field}
            />
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-wide font-semibold" style={{ color: CHROME.inkMuted }}>
              To
            </span>
            <input
              type="date"
              value={value.end || ''}
              min={value.start || earliest || undefined}
              max={latest || undefined}
              onChange={(e) => set({ end: e.target.value })}
              className="text-xs px-2 py-1.5"
              style={field}
            />
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-wide font-semibold" style={{ color: CHROME.inkMuted }}>
              Severity
            </span>
            <select
              value={value.severity || ''}
              onChange={(e) => set({ severity: e.target.value })}
              className="text-xs px-2 py-1.5"
              style={{ ...field, color: value.severity ? severityMeta(value.severity).textColor : CHROME.ink }}
            >
              <option value="">Any</option>
              {(filters?.severities || []).map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>

          <label className="flex flex-col gap-1 min-w-0">
            <span className="text-[10px] uppercase tracking-wide font-semibold" style={{ color: CHROME.inkMuted }}>
              Source type
            </span>
            <select
              value={value.source_type || ''}
              onChange={(e) => set({ source_type: e.target.value })}
              className="text-xs px-2 py-1.5 max-w-[190px]"
              style={field}
            >
              <option value="">Any</option>
              {(filters?.source_types || []).map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>

          <label className="flex flex-col gap-1 min-w-0">
            <span className="text-[10px] uppercase tracking-wide font-semibold" style={{ color: CHROME.inkMuted }}>
              Host
            </span>
            <select
              value={value.hostname || ''}
              onChange={(e) => set({ hostname: e.target.value })}
              className="text-xs px-2 py-1.5 max-w-[220px]"
              style={field}
            >
              <option value="">Any</option>
              {(filters?.hostnames || []).map((h) => <option key={h} value={h}>{h}</option>)}
            </select>
          </label>
        </div>

        {presets.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] uppercase tracking-wide font-semibold mr-1" style={{ color: CHROME.inkMuted }}>
              Range
            </span>
            {presets.map((p) => {
              const on = (value.start || '') === p.start && (value.end || '') === p.end
              return (
                <button
                  key={p.label}
                  onClick={() => set({ start: p.start, end: p.end })}
                  className="text-[11px] px-2.5 py-1 transition-colors"
                  style={{
                    background: on ? SERIES[3] : CHROME.surfaceRaised,
                    color: on ? '#FFFFFF' : CHROME.inkSecondary,
                    border: `1px solid ${on ? SERIES[3] : CHROME.border}`,
                  }}
                >
                  {p.label}
                </button>
              )
            })}
            {earliest && latest && (
              <span className="text-[10px] ml-1 font-mono" style={{ color: CHROME.inkDim }}>
                graph spans {earliest} → {latest}
              </span>
            )}
          </div>
        )}
      </div>
    </section>
  )
}
