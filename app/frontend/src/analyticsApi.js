// analyticsApi.js - thin fetch wrappers around the analytics_pipeline backend.
//
// Calls are relative ('/analytics-api/...'), not absolute URLs: in dev,
// vite.config.js proxies /analytics-api to the analytics backend so no CORS
// setup is needed; in a production build served behind the same reverse
// proxy as the chatbot API, /analytics-api is already same-origin. This is a
// distinct prefix from '/api' (the chatbot backend) because the two are
// separate deployables on separate ports - see analytics_pipeline/README.md.

const BASE = '/analytics-api'

async function handle(res) {
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail || detail
    } catch {
      // response body wasn't JSON - keep the status text
    }
    throw new Error(detail)
  }
  return res.json()
}

function get(path) {
  return fetch(`${BASE}${path}`).then(handle)
}

/* The dashboard takes an optional filter, and every panel in the response
 * honours it - the backend threads one LogFilter through all 15 aggregations
 * so the tiles can never describe a different population from the charts.
 * Empty values are dropped rather than sent blank, so an untouched filter bar
 * produces the same request (and the same warm cache entry) as before. */
export function fetchDashboard(filter) {
  const params = new URLSearchParams()
  for (const [k, v] of Object.entries(filter || {})) {
    if (v !== undefined && v !== null && String(v).trim() !== '') params.set(k, String(v).trim())
  }
  const qs = params.toString()
  return get(`/dashboard${qs ? `?${qs}` : ''}`)
}

export function fetchOverview() {
  return get('/overview')
}

export function fetchTimeseries(days) {
  return get(`/timeseries${days ? `?days=${days}` : ''}`)
}

export function fetchTimeseriesDay(day, granularity = 'hour') {
  return get(`/timeseries/day?day=${encodeURIComponent(day)}&granularity=${granularity}`)
}

export function fetchSeverity() {
  return get('/severity')
}

export function fetchSources() {
  return get('/sources')
}

export function fetchHosts(limit = 10, metric = 'total') {
  return get(`/hosts?limit=${limit}&metric=${metric}`)
}

export function fetchProcesses(limit = 10) {
  return get(`/processes?limit=${limit}`)
}

export function fetchErrorComponents(limit = 10) {
  return get(`/error-components?limit=${limit}`)
}

export function fetchInsights() {
  return get('/insights')
}

export function fetchAnalyticsHealth() {
  return get('/health')
}

function qs(params) {
  const sp = new URLSearchParams()
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') sp.set(k, String(v))
  })
  const s = sp.toString()
  return s ? `?${s}` : ''
}

export function fetchLogs(params) {
  return get(`/logs${qs(params)}`)
}

export function fetchAlerts(params) {
  return get(`/alerts${qs(params)}`)
}

export function fetchLogDetail(logId) {
  return get(`/logs/${encodeURIComponent(logId)}`)
}

export function fetchFilters() {
  return get('/filters')
}

export function fetchAlertLevels() {
  return get('/alert-levels')
}

/* ---- Universal Event Schema + SIEM / Data Lake export (ULPF c, d, g) ---- */

export function fetchSchema() {
  return get('/schema')
}

export function fetchSchemaCoverage() {
  return get('/schema/coverage')
}

export function fetchExportFormats() {
  return get('/export/formats')
}

/* The export is a file download, not a fetch: the response streams and can be
 * gigabytes, so it must never be pulled into a JS string. Handing the URL to
 * the browser lets it write straight to disk, and Content-Disposition on the
 * response names the file. Same filter shape as fetchDashboard, so "export
 * what I am looking at" is the same query string. */
export function exportUrl(filter, { format = 'ndjson', includeRaw = true, limit = null } = {}) {
  const params = new URLSearchParams({ format, include_raw: String(includeRaw) })
  for (const [k, v] of Object.entries(filter || {})) {
    if (v !== undefined && v !== null && String(v).trim() !== '') params.set(k, String(v).trim())
  }
  if (limit) params.set('limit', String(limit))
  return `${BASE}/export?${params.toString()}`
}

/* Tamper-evident ledger. The one screen where the framework's forensic claim
 * can be checked rather than believed: seal what is stored, re-derive every
 * hash, and prove a single event with a handful of sibling hashes. */
export function fetchIntegrityStatus() {
  return get('/integrity/status')
}

export function sealLedger(limit = 5000) {
  return fetch(`${BASE}/integrity/seal`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ limit }),
  }).then((r) => (r.ok ? r.json() : r.json().then((b) => Promise.reject(new Error(b.detail || r.statusText)))))
}

export function verifyLedger(expectedHead) {
  const q = expectedHead ? `?expected_head=${encodeURIComponent(expectedHead)}` : ''
  return get(`/integrity/verify${q}`)
}

export function proveEvent(eventId) {
  return get(`/integrity/prove/${encodeURIComponent(eventId)}`)
}
