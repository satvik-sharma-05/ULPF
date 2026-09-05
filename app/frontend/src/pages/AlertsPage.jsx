import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import StatCard from '../components/analytics/StatCard.jsx'
import Panel from '../components/analytics/Panel.jsx'
import ErrorRateChart from '../components/analytics/ErrorRateChart.jsx'
import ActivityHeatmap from '../components/analytics/ActivityHeatmap.jsx'
import SeverityBySource from '../components/analytics/SeverityBySource.jsx'
import TopMessages from '../components/analytics/TopMessages.jsx'
import RankedBarList from '../components/analytics/RankedBarList.jsx'
import DonutChart from '../components/analytics/DonutChart.jsx'
import LogTable from '../components/logs/LogTable.jsx'
import LogDetailPanel from '../components/logs/LogDetailPanel.jsx'
import FilterBar from '../components/logs/FilterBar.jsx'
import PageHeader from '../components/PageHeader.jsx'
import useDebounced from '../hooks/useDebounced.js'
import { Reveal } from '../components/ui/motion.jsx'
import { ALERT_LEVEL_META, CHROME, alertLevelMeta, severityMeta } from '../components/analytics/theme.js'
import { fetchAlerts, fetchDashboard, fetchFilters } from '../analyticsApi.js'

/* Alerts gets its own page rather than a panel on Analytics: triage is a
 * different job from exploration. Everything here is scoped to the actionable
 * band (P1-P3) - the charts answer "how bad, where, and is it getting worse",
 * and the table is what you actually work through.
 */

const REFRESH_MS = 30000
const ALERT_LIMIT = 200

export default function AlertsPage({ onAskAI }) {
  const [dashboard, setDashboard] = useState(null)
  const [alerts, setAlerts] = useState([])
  const [filters, setFilters] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState(null)

  const [level, setLevel] = useState('')
  const [host, setHost] = useState('')
  const [source, setSource] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const search = useDebounced(searchInput, 350)
  const reqIdRef = useRef(0)

  useEffect(() => {
    fetchFilters().then(setFilters).catch(() => setFilters(null))
  }, [])

  const loadAlerts = useCallback(async () => {
    const myReq = ++reqIdRef.current
    const data = await fetchAlerts({
      limit: ALERT_LIMIT, level: level || undefined,
      hostname: host, source_type: source, search,
    })
    if (myReq !== reqIdRef.current) return   // superseded - drop this response
    setAlerts(data)
  }, [level, host, source, search])

  const load = useCallback(async (background) => {
    if (background) setRefreshing(true)
    try {
      const [d] = await Promise.all([fetchDashboard(), loadAlerts()])
      setDashboard(d)
      setError(null)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [loadAlerts])

  useEffect(() => {
    load(false)
    const id = setInterval(() => load(true), REFRESH_MS)
    return () => clearInterval(id)
  }, [load])

  const ov = dashboard?.overview
  const byLevel = ov?.alerts_by_level || {}

  // Which hosts are actually alerting, from the rows on screen - so the panel
  // always agrees with the table beside it, including under a filter.
  const hostRanking = useMemo(() => {
    const counts = {}
    for (const a of alerts) counts[a.hostname || 'unknown'] = (counts[a.hostname || 'unknown'] || 0) + 1
    return Object.entries(counts)
      .map(([label, value]) => ({ label, value }))
      .sort((a, b) => b.value - a.value)
      .slice(0, 8)
  }, [alerts])

  const shareOf = (n) => (ov?.total_logs ? ((n / ov.total_logs) * 100).toFixed(2) + '% of all logs' : undefined)

  return (
    <div className="h-full overflow-y-auto" style={{ background: CHROME.pagePlane }}>
      <PageHeader
        title="Alerts"
        subtitle="Everything actionable — P1 Critical, P2 Error, P3 Warning"
        actions={
          <button
            onClick={() => load(true)}
            disabled={refreshing}
            className="px-3 py-1.5 text-xs disabled:opacity-50"
            style={{ border: `1px solid ${CHROME.border}`, color: CHROME.inkSecondary }}
          >
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
        }
      />

      <div className="px-6 py-5 flex flex-col gap-5">
        {error && (
          <div className="text-xs px-3 py-2"
               style={{ border: `1px solid ${CHROME.borderStrong}`, color: CHROME.ink }}>
            {error}
          </div>
        )}

        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <StatCard label="Total alerts" value={ov?.total_alerts} sublabel="P1–P3 actionable" emphasis />
          <StatCard label="Critical (P1)" value={byLevel.critical} sublabel={shareOf(byLevel.critical || 0)}
                    emphasis accent={severityMeta('CRITICAL').color} />
          <StatCard label="Errors (P2)" value={byLevel.error} sublabel={shareOf(byLevel.error || 0)}
                    emphasis accent={severityMeta('ERROR').color} />
          <StatCard label="Warnings (P3)" value={byLevel.warning} sublabel={shareOf(byLevel.warning || 0)}
                    accent={severityMeta('WARNING').color} />
        </div>

        {/* The queue comes first: this page is for triage, and the work
            is the table. The charts below it answer "how bad, where, and
            is it getting worse" once you have looked at the queue. */}
        <Panel
          title="Alert queue"
          subtitle="P1 Critical · P2 Error · P3 Warning — newest first, click a row for the full record"
          bodyClass=""
          action={
            <span className="text-[11px] font-mono" style={{ color: CHROME.inkMuted }}>
              {alerts.length}{alerts.length >= ALERT_LIMIT ? '+' : ''} shown
            </span>
          }
        >
          <FilterBar
            search={searchInput} onSearch={setSearchInput}
            source={source} onSource={setSource}
            host={host} onHost={setHost}
            filters={filters}
            onClear={() => { setHost(''); setSource(''); setSearchInput(''); setLevel('') }}
          >
            <select
              value={level}
              onChange={(e) => setLevel(e.target.value)}
              className="text-xs px-2 py-1.5"
              style={{ background: CHROME.surfaceRaised, border: `1px solid ${CHROME.border}`, color: CHROME.ink }}
            >
              <option value="">All levels (P1–P3)</option>
              <option value="critical">P1 · Critical</option>
              <option value="error">P2 · Error</option>
              <option value="warning">P3 · Warning</option>
            </select>
          </FilterBar>

          <div className="max-h-[560px] overflow-y-auto">
            <LogTable
              rows={alerts}
              loading={loading}
              onSelect={setSelectedId}
              selectedId={selectedId}
              showLevel
              emptyLabel={
                level
                  ? `No ${alertLevelMeta(level)?.label || level} alerts match these filters`
                  : 'No alerts match these filters'
              }
            />
          </div>
        </Panel>

        {/* Context for the queue above. */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Reveal>
            <DonutChart
              title="Alert mix" subtitle="Share of the actionable band by priority"
              rows={[
                { label: 'P1 Critical', value: byLevel.critical || 0, color: ALERT_LEVEL_META.critical.color },
                { label: 'P2 Error', value: byLevel.error || 0, color: ALERT_LEVEL_META.error.color },
                { label: 'P3 Warning', value: byLevel.warning || 0, color: ALERT_LEVEL_META.warning.color },
              ]}
              loading={loading} centerLabel="Alerts" emptyLabel="No alerts"
            />
          </Reveal>
          <Reveal delay={60}><ErrorRateChart data={dashboard?.error_rate} loading={loading} /></Reveal>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Reveal><SeverityBySource rows={dashboard?.severity_by_source} loading={loading} /></Reveal>
          <Reveal delay={60}>
            <RankedBarList
              title="Alerting hosts" subtitle="Hosts in the current alert view"
              rows={hostRanking} loading={loading}
              emptyLabel="No alerts match these filters"
            />
          </Reveal>
        </div>

        <Reveal><ActivityHeatmap data={dashboard?.heatmap} loading={loading} /></Reveal>

        <Reveal><TopMessages rows={dashboard?.top_messages} loading={loading} /></Reveal>

      </div>

      <LogDetailPanel logId={selectedId} onClose={() => setSelectedId(null)} onAskAI={onAskAI} />
    </div>
  )
}
