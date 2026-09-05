import { useEffect, useMemo, useState } from 'react'
import IntegrityPanel from '../components/analytics/IntegrityPanel.jsx'
import Panel from '../components/analytics/Panel.jsx'
import StatCard from '../components/analytics/StatCard.jsx'
import PageHeader from '../components/PageHeader.jsx'
import AnalyticsFilterBar from '../components/analytics/AnalyticsFilterBar.jsx'
import { Reveal } from '../components/ui/motion.jsx'
import { CHROME, SERIES, STATUS, severityMeta } from '../components/analytics/theme.js'
import { formatCompact } from '../components/analytics/format.js'
import {
  exportUrl, fetchExportFormats, fetchFilters, fetchSchema, fetchSchemaCoverage,
} from '../analyticsApi.js'

/* The Universal Event Schema, and the way out of the system.
 *
 * This page is the answer to two ULPF requirements that were previously true
 * of the code but invisible to anyone using it:
 *
 *   (c) normalize fields into a common event taxonomy - the taxonomy is
 *       declared, served from the backend, and rendered here with its ECS and
 *       OCSF equivalents, so it is a contract an integrator can build against
 *       rather than an internal convention.
 *   (g) efficient SIEM and Data Lake integration - the export below streams
 *       the same events out in four wire formats, honouring whatever filter
 *       is set, with the lineage fields intact.
 *
 * The coverage column is the part worth defending: it measures the live graph
 * against the schema's own claims rather than restating them. A required
 * field below 100% is called out as a contract violation, because a schema
 * document that cannot be wrong is not telling you anything.
 */

const EMPTY_FILTER = { start: '', end: '', severity: '', source_type: '', hostname: '', search: '' }

const GROUP_COLOR = {
  identity: SERIES[1],
  source: SERIES[3],
  severity: SERIES[0],
  content: SERIES[4],
  lineage: SERIES[5],
  parsing: SERIES[2],
  enrichment: SERIES[3],
}

export default function SchemaPage() {
  const [schema, setSchema] = useState(null)
  const [coverage, setCoverage] = useState(null)
  const [formats, setFormats] = useState([])
  const [filterOptions, setFilterOptions] = useState(null)
  const [filter, setFilter] = useState(EMPTY_FILTER)
  const [includeRaw, setIncludeRaw] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    Promise.all([fetchSchema(), fetchSchemaCoverage(), fetchExportFormats()])
      .then(([s, c, f]) => { setSchema(s); setCoverage(c); setFormats(f) })
      .catch((e) => setError(e.message))
    fetchFilters().then(setFilterOptions).catch(() => setFilterOptions(null))
  }, [])

  const coverageByField = useMemo(() => {
    const m = {}
    for (const r of coverage?.fields || []) m[r.field] = r
    return m
  }, [coverage])

  const totalEvents = coverage?.fields?.[0]?.total ?? null
  const violations = coverage?.violations || []
  const filterActive = Object.values(filter).some((v) => String(v || '') !== '')

  return (
    <div className="h-full overflow-y-auto" style={{ background: CHROME.pagePlane }}>
      <PageHeader
        title="Universal Event Schema"
        subtitle="The common taxonomy every source is normalized into — and the way out to a SIEM or Data Lake"
      />

      <div className="px-6 py-5 flex flex-col gap-5">
        {error && (
          <div className="text-xs px-3 py-2"
               style={{ border: `1px solid ${CHROME.borderStrong}`, color: CHROME.ink }}>
            {error}
          </div>
        )}

        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <StatCard label="Schema fields" value={schema?.fields?.length} />
          <StatCard label="Required fields" value={schema?.required_fields?.length}
                    sublabel="guaranteed on every event" />
          <StatCard label="Events normalized" value={totalEvents} emphasis />
          <StatCard
            label="Contract violations"
            value={coverage ? violations.length : undefined}
            sublabel={coverage
              ? (violations.length === 0
                  ? 'every required field on every event'
                  : 'required fields with gaps')
              : undefined}
            accent={violations.length === 0 ? STATUS.good : STATUS.danger}
          />
        </div>

        {/* ---- Export ---- */}
        <Reveal>
          <Panel
            title="SIEM & Data Lake export"
            subtitle="Streams the normalized events out. Lineage fields travel with them, so an exported event is still traceable to its original line."
            accent={SERIES[5]}
          >
            <div className="flex flex-col gap-4">
              <AnalyticsFilterBar
                value={filter}
                onChange={setFilter}
                onClear={() => setFilter(EMPTY_FILTER)}
                filters={filterOptions}
                bounds={null}
              />

              <label className="flex items-center gap-2 text-xs" style={{ color: CHROME.inkSecondary }}>
                <input
                  type="checkbox"
                  checked={includeRaw}
                  onChange={(e) => setIncludeRaw(e.target.checked)}
                />
                Include the verbatim original event (<span className="font-mono">raw_message</span>)
                <span className="text-[11px]" style={{ color: CHROME.inkMuted }}>
                  — on by default; turning it off makes the export smaller but no longer lossless
                </span>
              </label>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {formats.map((f, i) => (
                  <a
                    key={f.id}
                    href={exportUrl(filter, { format: f.id, includeRaw })}
                    className="flex flex-col gap-1.5 px-4 py-3 transition-transform hover:-translate-y-0.5"
                    style={{
                      background: CHROME.surface,
                      border: `1px solid ${CHROME.border}`,
                      borderLeft: `4px solid ${SERIES[i % SERIES.length]}`,
                      textDecoration: 'none',
                    }}
                  >
                    <span className="flex items-baseline justify-between gap-2">
                      <span className="text-sm font-bold" style={{ color: CHROME.ink }}>{f.label}</span>
                      <span className="text-[11px] font-semibold" style={{ color: CHROME.accentText }}>
                        Download ↓
                      </span>
                    </span>
                    <span className="text-[11px] leading-snug" style={{ color: CHROME.inkMuted }}>
                      {f.blurb}
                    </span>
                  </a>
                ))}
              </div>

              <p className="text-[11px]" style={{ color: CHROME.inkMuted }}>
                {filterActive
                  ? 'The filter above applies to the export — you get exactly the slice you selected.'
                  : `Exports all ${totalEvents ? formatCompact(totalEvents) : ''} events. Set a filter above to narrow it.`}
                {' '}The response streams, so the file is written as it is produced rather than
                assembled in memory first.
              </p>
            </div>
          </Panel>
        </Reveal>

        {/* ---- The taxonomy ---- */}
        {(schema?.groups || []).map((g, gi) => {
          const fields = (schema.fields || []).filter((f) => f.group === g.id)
          if (fields.length === 0) return null
          return (
            <Reveal key={g.id} delay={gi * 30}>
              <Panel title={g.label} subtitle={g.blurb} accent={GROUP_COLOR[g.id] || SERIES[gi % SERIES.length]} bodyClass="">
                <div className="overflow-x-auto">
                  <table className="w-full text-[11px]" style={{ minWidth: 860 }}>
                    <thead style={{ background: CHROME.surfaceRaised }}>
                      <tr>
                        {['Field', 'Type', 'ECS', 'OCSF', 'Coverage', 'Description'].map((h) => (
                          <th key={h} className="text-left px-3 py-2 font-semibold whitespace-nowrap"
                              style={{ color: CHROME.inkMuted }}>
                            {h}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {fields.map((f) => {
                        const cov = coverageByField[f.field]
                        const pct = cov?.coverage ?? null
                        const bad = cov?.violates_contract
                        return (
                          <tr key={f.field} style={{ borderTop: `1px solid ${CHROME.gridline}` }}>
                            <td className="px-3 py-2 font-mono whitespace-nowrap" style={{ color: CHROME.ink }}>
                              {f.field}
                              {f.required && (
                                <span className="ml-1.5 text-[9px] px-1 py-0.5"
                                      style={{ background: CHROME.surfaceActive, color: CHROME.inkSecondary }}>
                                  req
                                </span>
                              )}
                            </td>
                            <td className="px-3 py-2 font-mono whitespace-nowrap" style={{ color: CHROME.inkMuted }}>
                              {f.type}
                            </td>
                            <td className="px-3 py-2 font-mono whitespace-nowrap" style={{ color: CHROME.accentText }}>
                              {f.ecs}
                            </td>
                            <td className="px-3 py-2 font-mono whitespace-nowrap" style={{ color: CHROME.inkMuted }}>
                              {f.ocsf}
                            </td>
                            <td className="px-3 py-2 whitespace-nowrap" style={{ minWidth: 110 }}>
                              {pct === null ? (
                                <span style={{ color: CHROME.inkDim }}>—</span>
                              ) : (
                                <span className="flex items-center gap-1.5">
                                  <span style={{ width: 42, height: 5, background: CHROME.gridline,}}>
                                    <span style={{
                                      display: 'block', height: 5,
                                      width: `${Math.max(2, pct)}%`,
                                      background: bad ? STATUS.danger : (pct >= 99.5 ? STATUS.good : SERIES[2]),
                                    }} />
                                  </span>
                                  <span className="font-mono" style={{ color: bad ? STATUS.danger : CHROME.inkSecondary }}>
                                    {pct.toFixed(0)}%
                                  </span>
                                </span>
                              )}
                            </td>
                            <td className="px-3 py-2 leading-snug" style={{ color: CHROME.inkSecondary }}>
                              {f.description}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              </Panel>
            </Reveal>
          )
        })}

        {/* ---- Severity ladder ---- */}
        <Reveal>
          <Panel
            title="Severity ladder"
            subtitle="Every vendor scale — numeric syslog priorities, CEF/LEEF severities, bare words — collapses onto this one ordering"
            accent={SERIES[0]}
          >
            <div className="flex flex-wrap gap-2">
              {(schema?.severity_ladder || []).map((s) => {
                const meta = severityMeta(s.severity)
                return (
                  <div key={`${s.severity}-${s.score}`}
                       className="flex items-center gap-2 px-3 py-2"
                       style={{ background: CHROME.surface, border: `1px solid ${CHROME.border}`,
                                borderLeft: `4px solid ${meta.color}` }}>
                    <span className="text-xs font-bold" style={{ color: meta.textColor }}>{s.severity}</span>
                    <span className="text-[10px] font-mono" style={{ color: CHROME.inkMuted }}>
                      score {s.score} · syslog {s.syslog}
                    </span>
                  </div>
                )
              })}
            </div>
          </Panel>
        </Reveal>

        {/* The integrity ledger belongs on this page: everything else here
            documents the schema an event is normalized into, and this is what
            says the stored event has not been altered since. The schema is the
            contract; the ledger is the evidence the contract was kept. */}
        <Reveal>
          <IntegrityPanel />
        </Reveal>
      </div>
    </div>
  )
}
