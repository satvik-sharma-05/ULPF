import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import StatCard from '../components/analytics/StatCard.jsx'
import TimeSeriesChart from '../components/analytics/TimeSeriesChart.jsx'
import SeverityBreakdown from '../components/analytics/SeverityBreakdown.jsx'
import RankedBarList from '../components/analytics/RankedBarList.jsx'
import InsightsFeed from '../components/analytics/InsightsFeed.jsx'
import ActivityHeatmap from '../components/analytics/ActivityHeatmap.jsx'
import ErrorRateChart from '../components/analytics/ErrorRateChart.jsx'
import SeverityBySource from '../components/analytics/SeverityBySource.jsx'
import TopMessages from '../components/analytics/TopMessages.jsx'
import EntityExplorer from '../components/analytics/EntityExplorer.jsx'
import GraphComposition from '../components/analytics/GraphComposition.jsx'
import DonutChart from '../components/analytics/DonutChart.jsx'
import AnalyticsFilterBar from '../components/analytics/AnalyticsFilterBar.jsx'
import { Reveal } from '../components/ui/motion.jsx'
import PageHeader from '../components/PageHeader.jsx'
import { CHROME, severityMeta } from '../components/analytics/theme.js'
import useDebounced from '../hooks/useDebounced.js'
import { fetchDashboard, fetchFilters } from '../analyticsApi.js'

const REFRESH_MS = 30000
const ALERT_LIMIT = 200

function logsPerSecond(overview) {
  if (!overview?.earliest_timestamp || !overview?.latest_timestamp || !overview?.total_logs) return null
  const seconds = (new Date(overview.latest_timestamp) - new Date(overview.earliest_timestamp)) / 1000
  if (!Number.isFinite(seconds) || seconds <= 0) return null
  return overview.total_logs / seconds
}

const EMPTY_FILTER = { start: '', end: '', severity: '', source_type: '', hostname: '', search: '' }

export default function AnalyticsPage() {
  const [dashboard, setDashboard] = useState(null)
  const [filterOptions, setFilterOptions] = useState(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState(null)

  // The typed search is debounced; the dropdowns and dates are not, because
  // those change once per interaction rather than once per keystroke.
  const [filter, setFilter] = useState(EMPTY_FILTER)
  const debouncedSearch = useDebounced(filter.search, 350)
  const effectiveFilter = useMemo(
    () => ({ ...filter, search: debouncedSearch }),
    [filter.start, filter.end, filter.severity, filter.source_type, filter.hostname, debouncedSearch],
  )

  // Monotonic guard: a slow response for an old filter must never overwrite a
  // newer one. Without this, typing quickly can leave the page showing results
  // for a query the user has already moved on from.
  const reqIdRef = useRef(0)

  // The unfiltered bounds, captured from the first load. The date pickers and
  // the range presets are anchored to the data's own span, not to wall-clock
  // time - this corpus is historical, so "last 7 days" of today is empty.
  const [bounds, setBounds] = useState(null)

  useEffect(() => {
    fetchFilters().then(setFilterOptions).catch(() => setFilterOptions(null))
  }, [])

  const load = useCallback(async (background) => {
    if (background) setRefreshing(true)
    const myReq = ++reqIdRef.current
    try {
      const d = await fetchDashboard(effectiveFilter)
      if (myReq !== reqIdRef.current) return   // superseded - drop this response
      setDashboard(d)
      setBounds((prev) => prev || (d.filtered ? null : {
        earliest: d.overview?.earliest_timestamp,
        latest: d.overview?.latest_timestamp,
      }))
      setError(null)
    } catch (e) {
      if (myReq !== reqIdRef.current) return
      setError(e.message)
    } finally {
      if (myReq === reqIdRef.current) {
        setLoading(false)
        setRefreshing(false)
      }
    }
  }, [effectiveFilter])

  useEffect(() => {
    load(false)
    const id = setInterval(() => load(true), REFRESH_MS)
    return () => clearInterval(id)
  }, [load])

  const overview = dashboard?.overview
  const lps = logsPerSecond(overview)
  // Days that actually contain logs, not the calendar span between the
  // first and last timestamp - the two differ by years here.
  const dayCount = dashboard?.timeseries?.length || 0

  return (
    <div className="h-full overflow-y-auto" style={{ background: CHROME.pagePlane }}>
      <PageHeader
        title="Analytics"
        subtitle={dashboard?.filtered
          ? "Filtered view — every panel below describes the same slice"
          : "Alerts, volume and derived insights across the log graph"}
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
          <div className="text-xs px-3 py-2" style={{ border: `1px solid ${CHROME.borderStrong}`, color: CHROME.ink }}>
            {error}
          </div>
        )}

        <AnalyticsFilterBar
          value={filter}
          onChange={setFilter}
          onClear={() => setFilter(EMPTY_FILTER)}
          filters={filterOptions}
          bounds={bounds}
          resultCount={overview?.total_logs}
          loading={loading || refreshing}
        />

        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
          <StatCard label="Total logs" value={overview?.total_logs} />
          <StatCard label="Alerts" value={overview?.total_alerts} sublabel="see the Alerts page" emphasis
                    accent={severityMeta('ERROR').color} />
          <StatCard label="Source types" value={overview?.total_sources} />
          <StatCard label="Users" value={overview?.total_users} />
          <StatCard
            label="Date range"
            // Each endpoint is its own non-breaking line rather than one long
            // string: as a single value it wrapped wherever it happened to run
            // out of room, splitting a date across two lines. The headline
            // number is the count of days that actually have logs, which is
            // the useful figure - the span is stretched by a handful of
            // records carrying bogus 2012 timestamps, so "14 years" would say
            // more about the data quality than about the corpus.
            value={
              overview?.earliest_timestamp ? (
                <span className="flex flex-col leading-tight">
                  <span className="whitespace-nowrap">
                    {String(overview.earliest_timestamp).slice(0, 10)}
                  </span>
                  <span className="whitespace-nowrap" style={{ color: CHROME.inkMuted }}>
                    → {String(overview.latest_timestamp).slice(0, 10)}
                  </span>
                </span>
              ) : '—'
            }
            sublabel={dayCount ? `${dayCount} day${dayCount === 1 ? '' : 's'} with logs` : undefined}
            compact={false}
          />
          <StatCard label="Hosts" value={overview?.total_hosts} />
        </div>


        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="lg:col-span-2">
            <Reveal><TimeSeriesChart data={dashboard?.timeseries} loading={loading} refreshing={refreshing} /></Reveal>
          </div>
          <Reveal delay={60}><SeverityBreakdown rows={dashboard?.severity} loading={loading} /></Reveal>
        </div>

        <Reveal><ActivityHeatmap data={dashboard?.heatmap} loading={loading} /></Reveal>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Reveal delay={40}><ErrorRateChart data={dashboard?.error_rate} loading={loading} /></Reveal>
          <Reveal delay={80}><SeverityBySource rows={dashboard?.severity_by_source} loading={loading} /></Reveal>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Reveal>
            <DonutChart
              title="Log volume by source" subtitle="Which platforms this graph is built from"
              rows={dashboard?.sources} labelKey="source_type" valueKey="total"
              loading={loading} centerLabel="Logs"
            />
          </Reveal>
          <Reveal delay={60}>
            <DonutChart
              title="Parser coverage" subtitle="Which detector matched each record"
              rows={dashboard?.parse_coverage} labelKey="format" valueKey="total"
              loading={loading} centerLabel="Parsed"
            />
          </Reveal>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Reveal delay={40}><TopMessages rows={dashboard?.top_messages} loading={loading} /></Reveal>
          <Reveal delay={80}><EntityExplorer entities={dashboard?.entities} loading={loading} /></Reveal>
        </div>

        <Reveal><GraphComposition rows={dashboard?.graph_composition} loading={loading} /></Reveal>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <RankedBarList
            title="Hosts by errors" subtitle="Most ERROR-or-worse logs"
            rows={(dashboard?.top_hosts || []).map((h) => ({ label: h.host, value: h.errors }))}
            loading={loading}
          />
          <RankedBarList
            title="Source types" subtitle="Volume, with error count"
            rows={(dashboard?.sources || []).slice(0, 10).map((s) => ({
              label: s.source_type, value: s.total, secondary: `${s.errors || 0} err`,
            }))}
            loading={loading}
          />
          <RankedBarList
            title="Busiest hosts" subtitle="Total log volume"
            rows={(dashboard?.top_hosts_volume || []).map((h) => ({ label: h.host, value: h.total }))}
            loading={loading}
          />
          <RankedBarList
            title="Components by errors" subtitle="Which sub-component is failing"
            rows={(dashboard?.error_components || []).map((c) => ({ label: c.component, value: c.errors }))}
            loading={loading}
            emptyLabel="No error components in range"
          />
        </div>

        <InsightsFeed insights={dashboard?.insights} loading={loading} />
      </div>

    </div>
  )
}
