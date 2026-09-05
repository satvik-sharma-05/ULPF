// ingestApi.js - thin fetch wrappers around the ingestion control API
// (ingestion_pipeline/api.py), which owns modes and custom-log uploads.
//
// Relative '/ingest-api/...' paths, same reasoning as api.js and
// analyticsApi.js: vite.config.js proxies it in dev, nginx.conf proxies it in
// the container, so the frontend never needs to know the backend's address
// and no CORS setup is needed in either setup. A third distinct prefix
// because this is a third separate deployable on its own port.

const BASE = '/ingest-api'

async function handle(res) {
  if (!res.ok) {
    // A 404 from these endpoints is almost never a missing resource - it is a
    // backend older than the frontend calling it, which "Not Found" does
    // nothing to convey. Worth naming, because the fix (restart the service)
    // is not one anybody guesses from the status text.
    if (res.status === 404) {
      throw new Error(
        `The ingestion service does not have ${new URL(res.url, location.origin).pathname}. ` +
        'It is most likely running an older build - restart it and try again.')
    }
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

export function fetchModes() {
  return fetch(`${BASE}/modes`).then(handle)
}

export function fetchIngestHealth() {
  return fetch(`${BASE}/health`).then(handle)
}

export function setMode(mode) {
  return fetch(`${BASE}/mode`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
  }).then(handle)
}

export function uploadLogFile(file, { format = 'auto', embed = true, wipe = false } = {}) {
  const body = new FormData()
  body.append('file', file)
  body.append('format', format)
  body.append('embed', String(embed))
  body.append('wipe', String(wipe))
  return fetch(`${BASE}/upload`, { method: 'POST', body }).then(handle)
}

export function fetchJobs() {
  return fetch(`${BASE}/jobs`).then(handle)
}

export function fetchJob(jobId) {
  return fetch(`${BASE}/jobs/${encodeURIComponent(jobId)}`).then(handle)
}

export function startSampleIngest({ limit = null, embed = true, wipe = false } = {}) {
  const params = new URLSearchParams({ embed: String(embed), wipe: String(wipe) })
  if (limit) params.set('limit', String(limit))
  return fetch(`${BASE}/sample?${params}`, { method: 'POST' }).then(handle)
}

// Dry-run parse for the Parser Lab: text in, normalized records out, nothing
// stored. Separate from uploadCustomLogs() on purpose - that one ingests, this
// one deliberately does not touch Neo4j, so a new source can be tried against
// a production deployment without contaminating the graph.
export function parsePreview({ text, format = 'auto', filename = 'pasted.log', limit = 200 }) {
  return fetch(`${BASE}/parse`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, format, filename, limit }),
  }).then(handle)
}
