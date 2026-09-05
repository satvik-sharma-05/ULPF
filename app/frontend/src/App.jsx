import { useCallback, useEffect, useMemo, useState } from 'react'
import Sidebar from './components/Sidebar.jsx'
import ModeBanner from './components/ModeBanner.jsx'
import ModeGate from './components/ModeGate.jsx'
import AuroraBackdrop from './components/ui/AuroraBackdrop.jsx'
import ChatPage from './pages/ChatPage.jsx'
import AnalyticsPage from './pages/AnalyticsPage.jsx'
import AlertsPage from './pages/AlertsPage.jsx'
import ModesPage from './pages/ModesPage.jsx'
import HomePage from './pages/HomePage.jsx'
import LiveLogsPage from './pages/LiveLogsPage.jsx'
import SchemaPage from './pages/SchemaPage.jsx'
import ParserLabPage from './pages/ParserLabPage.jsx'
import { CHROME } from './components/analytics/theme.js'
import { fetchHealth } from './api.js'
import { fetchAnalyticsHealth } from './analyticsApi.js'
import { fetchModes, setMode as setModeApi } from './ingestApi.js'

export default function App() {
  const [page, setPage] = useState('home')
  const [health, setHealth] = useState(null)
  const [modeState, setModeState] = useState(null)
  const [modeBusy, setModeBusy] = useState(false)
  // Remembered across reloads: someone who collapsed the sidebar to get
  // screen space does not want it back every time they refresh. Wrapped
  // because localStorage throws outright in some privacy modes.
  const [navCollapsed, setNavCollapsed] = useState(() => {
    try { return localStorage.getItem('ulpf.nav') === 'collapsed' } catch { return false }
  })

  const toggleNav = useCallback(() => {
    setNavCollapsed((c) => {
      const next = !c
      try { localStorage.setItem('ulpf.nav', next ? 'collapsed' : 'open') } catch { /* not available */ }
      return next
    })
  }, [])
  // Which Neo4j the read services are actually on. Polled alongside chat
  // health so the mode banner can name the graph the numbers come from.
  const [graph, setGraph] = useState(null)
  // Set when "Ask AI" creates a log-scoped session on another page; the Chat
  // page opens it and then clears it, so navigating back later doesn't keep
  // re-opening the same conversation.
  const [pendingSessionId, setPendingSessionId] = useState(null)
  // A question asked from the home page's prompt box: the session is created
  // there, then this carries the first question so Chat can send it on open.
  const [pendingQuestion, setPendingQuestion] = useState(null)

  const openSession = useCallback((sessionId, question = null) => {
    setPendingSessionId(sessionId)
    setPendingQuestion(question)
    setPage('chat')
  }, [])

  // Health and modes live here rather than per-page: the sidebar shows both on
  // every page, and hoisting them means one poll for the whole app instead of
  // one per page, and a mode change made in the sidebar is instantly visible
  // to the Modes page without either component knowing about the other.
  useEffect(() => {
    const load = () => {
      fetchHealth().then(setHealth).catch(() => setHealth({ neo4j: false, llm: false }))
      fetchAnalyticsHealth().then(setGraph).catch(() => setGraph(null))
    }
    load()
    const id = setInterval(load, 15000)
    return () => clearInterval(id)
  }, [])

  const loadModes = useCallback(() => {
    fetchModes().then(setModeState).catch(() => setModeState(null))
  }, [])

  useEffect(() => { loadModes() }, [loadModes])

  const changeMode = useCallback(async (mode) => {
    setModeBusy(true)
    try {
      setModeState(await setModeApi(mode))
    } catch {
      // Leave the previous state on screen; the select snaps back to it.
    } finally {
      setModeBusy(false)
    }
  }, [])

  const activeSpec = modeState?.modes?.find((m) => m.mode === modeState?.current) || null

  /* Should the data pages be withheld?
   *
   * Two independent conditions, either of which means the numbers on screen
   * would not be what the selected mode claims they are:
   *
   *  1. The backend's own validate() found unmet requirements for this mode -
   *     no Kafka brokers, no DNIF endpoint, no Neo4j address. Nothing is being
   *     ingested, so whatever is in the graph did not come from this mode.
   *
   *  2. Production is selected but the read services are pointed at a local
   *     sandbox. This is the case that actually bit: production "worked",
   *     because analytics was quietly serving the sample corpus. The mode
   *     switch never repoints an already-running service - it builds its Neo4j
   *     driver once at startup - so the address has to be checked, not assumed.
   *
   * Sample and custom are never gated: a local sandbox is exactly what they
   * are for, so pointing at one is correct rather than a misconfiguration.
   */
  const gate = useMemo(() => {
    if (!activeSpec) return null
    const isProduction = activeSpec.mode === 'production'
    if (!isProduction) return null

    const reasons = [...(activeSpec.problems || [])]
    const uri = graph?.neo4j_uri || ''
    const localGraph = /localhost|127\.0\.0\.1|sandbox-neo4j/.test(uri)
    if (localGraph) {
      reasons.push(
        `The analytics service is reading ${uri}, a local sandbox — production mode has no `
        + 'default Neo4j address and must be given a real one.',
      )
    }
    if (reasons.length === 0) return null

    return {
      modeLabel: activeSpec.label,
      reasons,
      graphUri: localGraph ? uri : null,
      graphDatabase: localGraph ? graph?.neo4j_database : null,
      summary:
        'Production mode reads live events from Kafka, fed by the DNIF producer, into a '
        + 'production Neo4j. None of that is wired up here, so there is nothing for this '
        + 'page to show. The sample corpus is still in the local graph, but it is not this '
        + "mode's data and is not shown under this mode's label.",
      onUseSample: () => changeMode('sample'),
    }
  }, [activeSpec, changeMode, graph])

  return (
    <div className="h-screen flex relative" style={{ background: CHROME.pagePlane }}>
      {/* The layer everything else is glass over - see AuroraBackdrop. */}
      <AuroraBackdrop />
      <Sidebar
        active={page}
        onNavigate={setPage}
        health={health}
        mode={modeState?.current}
        modes={modeState?.modes}
        onModeChange={changeMode}
        modeBusy={modeBusy}
        collapsed={navCollapsed}
        onToggle={toggleNav}
      />
      <main className="flex-1 min-w-0 min-h-0 flex flex-col relative z-10">
        {/* Above every page, not just Modes: the whole point is that it is
            visible while you are looking at the numbers it qualifies. */}
        <ModeBanner
          mode={gate ? null : modeState?.current}
          spec={gate ? null : activeSpec}
          graphUri={graph?.neo4j_uri}
          graphDatabase={graph?.neo4j_database}
        />
        <div className="flex-1 min-h-0">
        {page === 'home' && (
          <HomePage onNavigate={setPage} />
        )}
        {/* Home and Modes stay reachable: Modes is where the missing
            configuration gets fixed, so gating it would be a trap. */}
        {page === 'analytics' && (
          <ModeGate gate={gate} onNavigate={setPage}><AnalyticsPage /></ModeGate>
        )}
        {page === 'alerts' && (
          <ModeGate gate={gate} onNavigate={setPage}><AlertsPage onAskAI={openSession} /></ModeGate>
        )}
        {page === 'live' && (
          <ModeGate gate={gate} onNavigate={setPage}><LiveLogsPage onAskAI={openSession} /></ModeGate>
        )}
        {page === 'schema' && (
          <ModeGate gate={gate} onNavigate={setPage}><SchemaPage /></ModeGate>
        )}
        {/* Deliberately outside ModeGate: the Lab neither reads from the graph
            nor writes to it, so it works on a fresh install before any mode is
            configured - and gating the one screen that needs no data would be
            the wrong way round. */}
        {page === 'lab' && (
          <ParserLabPage />
        )}
        {page === 'chat' && (
          <ModeGate gate={gate} onNavigate={setPage}>
            <ChatPage
              pendingSessionId={pendingSessionId}
              pendingQuestion={pendingQuestion}
              onConsumePendingSession={() => { setPendingSessionId(null); setPendingQuestion(null) }}
              allowedProviders={activeSpec?.allowed_llm_providers}
            />
          </ModeGate>
        )}
        {page === 'modes' && (
          <ModesPage modeState={modeState} onModeChange={changeMode} onRefresh={loadModes} modeBusy={modeBusy} />
        )}
        </div>
      </main>
    </div>
  )
}
