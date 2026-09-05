import { useCallback, useEffect, useRef, useState } from 'react'
import LogTable from '../components/logs/LogTable.jsx'
import LogDetailPanel from '../components/logs/LogDetailPanel.jsx'
import FilterBar from '../components/logs/FilterBar.jsx'
import PageHeader from '../components/PageHeader.jsx'
import useDebounced from '../hooks/useDebounced.js'
import { CHROME } from '../components/analytics/theme.js'
import { fetchFilters, fetchLogs } from '../analyticsApi.js'

const POLL_MS = 4000
const MAX_ROWS = 500     // ring-buffer cap: a tail left open all day must not grow without bound
const PAGE_SIZE = 150

export default function LiveLogsPage({ onAskAI }) {
  const [rows, setRows] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [live, setLive] = useState(true)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [filters, setFilters] = useState(null)
  const [newCount, setNewCount] = useState(0)

  const [severity, setSeverity] = useState('')
  const [host, setHost] = useState('')
  const [source, setSource] = useState('')
  const [searchInput, setSearchInput] = useState('')
  // Debounced so typing fires one query, not one per keystroke.
  const search = useDebounced(searchInput, 350)

  // Monotonic request id. Only the newest request is allowed to write state,
  // so a slow earlier response can never overwrite a newer one - the race
  // that made filtering look broken.
  const reqIdRef = useRef(0)
  // Newest timestamp on screen; the poll's keyset cursor.
  const newestRef = useRef(null)

  useEffect(() => {
    fetchFilters().then(setFilters).catch(() => setFilters(null))
  }, [])

  const reload = useCallback(async () => {
    const myReq = ++reqIdRef.current
    setLoading(true)
    setError(null)
    try {
      const data = await fetchLogs({
        severity, hostname: host, source_type: source, search,
        limit: PAGE_SIZE, order: 'desc',
      })
      if (myReq !== reqIdRef.current) return   // superseded - drop this response
      setRows(data)
      newestRef.current = data.length ? data[0].timestamp : null
      setNewCount(0)
    } catch (e) {
      if (myReq === reqIdRef.current) setError(e.message)
    } finally {
      if (myReq === reqIdRef.current) setLoading(false)
    }
  }, [severity, host, source, search])

  useEffect(() => { reload() }, [reload])

  useEffect(() => {
    if (!live) return undefined
    const id = setInterval(async () => {
      // Don't poll on top of an in-flight reload: the poll prepends to rows
      // the reload is about to replace, briefly mixing old and new filters.
      if (loading) return
      const cursorAtSend = newestRef.current
      const reqAtSend = reqIdRef.current
      try {
        const fresh = await fetchLogs({
          severity, hostname: host, source_type: source, search,
          limit: 100, order: 'desc',
          after_timestamp: cursorAtSend || undefined,
        })
        // A filter changed while this was in flight - its rows belong to the
        // previous filter set, so discard them.
        if (reqAtSend !== reqIdRef.current) return
        if (fresh.length) {
          newestRef.current = fresh[0].timestamp
          setNewCount((n) => n + fresh.length)
          setRows((prev) => {
            // Dedupe by id: two logs can share a timestamp, so a strictly
            // greater-than cursor can still redeliver a sibling row.
            const seen = new Set(fresh.map((r) => r.id))
            return [...fresh, ...prev.filter((r) => !seen.has(r.id))].slice(0, MAX_ROWS)
          })
        }
      } catch {
        // A failed poll isn't worth surfacing - the next tick retries and the
        // already-loaded rows stay on screen.
      }
    }, POLL_MS)
    return () => clearInterval(id)
  }, [live, severity, host, source, search, loading])

  function clearAll() {
    setSeverity(''); setHost(''); setSource(''); setSearchInput('')
  }

  const pendingSearch = searchInput !== search

  return (
    <div className="h-full flex flex-col" style={{ background: CHROME.pagePlane }}>
      <PageHeader
        title="Live logs"
        subtitle={`Newest first, polled every ${POLL_MS / 1000}s · click any row for the full record`}
        actions={
          <>
            {newCount > 0 && (
              <span className="text-xs font-mono px-2 py-1"
                    style={{ background: CHROME.surfaceActive, color: CHROME.primary }}>
                +{newCount}
              </span>
            )}
            <button
              onClick={() => setLive((l) => !l)}
              className="px-3 py-1.5 text-xs font-medium"
              style={{
                background: live ? CHROME.primary : 'transparent',
                color: live ? CHROME.primaryInk : CHROME.inkSecondary,
                border: `1px solid ${live ? CHROME.primary : CHROME.border}`,
              }}
            >
              {live ? 'Live' : 'Paused'}
            </button>
            <button
              onClick={reload}
              className="px-3 py-1.5 text-xs"
              style={{ border: `1px solid ${CHROME.border}`, color: CHROME.inkSecondary }}
            >
              Reload
            </button>
          </>
        }
      />

      <FilterBar
        search={searchInput} onSearch={setSearchInput}
        severity={severity} onSeverity={setSeverity}
        source={source} onSource={setSource}
        host={host} onHost={setHost}
        filters={filters}
        onClear={clearAll}
        rightSlot={
          <span className="text-xs font-mono" style={{ color: CHROME.inkMuted }}>
            {pendingSearch ? 'filtering…' : `${rows.length} row${rows.length === 1 ? '' : 's'}`}
          </span>
        }
      />

      {error && (
        <div className="mx-6 mt-3 text-xs px-3 py-2"
             style={{ border: `1px solid ${CHROME.borderStrong}`, color: CHROME.ink }}>
          {error}
        </div>
      )}

      <div className="flex-1 overflow-y-auto px-6 py-4">
        <LogTable
          rows={rows}
          loading={loading}
          onSelect={setSelectedId}
          selectedId={selectedId}
          dense
          emptyLabel="No logs match these filters"
        />
      </div>

      <LogDetailPanel logId={selectedId} onClose={() => setSelectedId(null)} onAskAI={onAskAI} />
    </div>
  )
}
