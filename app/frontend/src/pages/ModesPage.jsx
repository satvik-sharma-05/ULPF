import { useCallback, useEffect, useState } from 'react'
import ModeCard from '../components/modes/ModeCard.jsx'
import UploadPanel from '../components/modes/UploadPanel.jsx'
import JobList from '../components/modes/JobList.jsx'
import PageHeader from '../components/PageHeader.jsx'
import { CHROME } from '../components/analytics/theme.js'
import { fetchJobs, startSampleIngest } from '../ingestApi.js'

const POLL_MS = 3000

export default function ModesPage({ modeState, onModeChange, onRefresh, modeBusy }) {
  const [jobs, setJobs] = useState([])
  const [selected, setSelected] = useState(null)
  const [error, setError] = useState(null)
  const [sampleBusy, setSampleBusy] = useState(false)

  const loadJobs = useCallback(() => {
    fetchJobs().then(setJobs).catch(() => {})
  }, [])

  useEffect(() => { loadJobs() }, [loadJobs])

  // Follow the active mode unless the user has explicitly picked another card
  // to inspect - so arriving here always shows what is actually running.
  useEffect(() => {
    if (modeState?.current && selected === null) setSelected(modeState.current)
  }, [modeState, selected])

  const hasActiveJob = jobs.some((j) => j.status === 'queued' || j.status === 'running')
  useEffect(() => {
    if (!hasActiveJob) return undefined
    const id = setInterval(loadJobs, POLL_MS)
    return () => clearInterval(id)
  }, [hasActiveJob, loadJobs])

  async function runSampleIngest() {
    setSampleBusy(true)
    setError(null)
    try {
      const job = await startSampleIngest({ limit: 5000, embed: true })
      setJobs((prev) => [job, ...prev])
    } catch (err) {
      setError(err.message)
    } finally {
      setSampleBusy(false)
    }
  }

  const activeMode = modeState?.current
  const selectedSpec = modeState?.modes?.find((m) => m.mode === selected)

  return (
    <div className="h-full overflow-y-auto" style={{ background: CHROME.pagePlane }}>
      <PageHeader
        title="Modes"
        subtitle="How logs get into the graph — all three share the same parser, entity extraction and embeddings"
        actions={
          <button
            onClick={() => { onRefresh?.(); loadJobs() }}
            className="px-3 py-1.5 text-xs"
            style={{ border: `1px solid ${CHROME.border}`, color: CHROME.inkSecondary }}
          >
            Refresh
          </button>
        }
      />

      <div className="px-6 py-5 flex flex-col gap-4">
        {error && (
          <div className="text-xs px-3 py-2"
               style={{ border: `1px solid ${CHROME.borderStrong}`, color: CHROME.ink }}>
            {error}
          </div>
        )}

        <p className="text-[11px] px-3 py-2 leading-relaxed"
           style={{ background: CHROME.surface, border: `1px solid ${CHROME.border}`, color: CHROME.inkSecondary }}>
          Click a card to make it the active mode. The change applies to the running ingestion
          service immediately, but is <strong style={{ color: CHROME.ink }}>not persisted</strong> —
          a restart returns to the configured <code className="font-mono">APP_MODE</code>. Switching to
          production does not itself start the Kafka producer/consumer; those are long-running
          processes launched on the server.
        </p>

        {!modeState ? (
          <div className="py-16 text-center text-xs" style={{ color: CHROME.inkMuted }}>Loading modes…</div>
        ) : (
          <>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              {modeState.modes?.map((spec) => (
                <ModeCard
                  key={spec.mode}
                  spec={spec}
                  active={spec.mode === activeMode}
                  selected={spec.mode === selected}
                  busy={modeBusy}
                  onInspect={setSelected}
                  onActivate={onModeChange}
                />
              ))}
            </div>

            {selected === 'sample' && (
              <Panel title="Ingest the sample corpus">
                <p className="text-[11px] leading-relaxed" style={{ color: CHROME.inkSecondary }}>
                  Loads the first 5,000 records of the bundled parsed corpus
                  (<code className="font-mono">output.json.gz</code>) with embeddings into the configured
                  local Neo4j — enough to exercise the chatbot and every analytics panel in minutes.
                </p>
                <button
                  onClick={runSampleIngest}
                  disabled={sampleBusy}
                  className="px-4 py-2 text-xs font-medium disabled:opacity-40 self-start"
                  style={{ background: CHROME.primary, color: CHROME.primaryInk }}
                >
                  {sampleBusy ? 'Starting…' : 'Ingest 5,000 sample records'}
                </button>
              </Panel>
            )}

            {selected === 'production' && (
              <Panel title="Production setup">
                <p className="text-[11px] leading-relaxed" style={{ color: CHROME.inkSecondary }}>
                  Runs the realtime DNIF → Kafka → parse → embed → Neo4j path as long-lived producer
                  and consumer processes, started on the server. Set these, then run the two roles:
                </p>
                <pre className="text-[10px] font-mono leading-relaxed p-3 overflow-x-auto"
                     style={{ background: CHROME.surfaceRaised, color: CHROME.inkSecondary, border: `1px solid ${CHROME.border}` }}>{`NEO4J_URI=bolt://<neo4j-host>:7687
NEO4J_PASSWORD=<password>
KAFKA_BOOTSTRAP_SERVERS=<broker1:9092,broker2:9092>
DNIF_API_URL=https://<dnif-host>
DNIF_API_KEY=<key>

python -m realtime.run --role producer   # DNIF  -> Kafka
python -m realtime.run --role consumer   # Kafka -> parse -> embed -> Neo4j`}</pre>
                {selectedSpec?.problems?.length > 0 && (
                  <ul className="flex flex-col gap-1">
                    {selectedSpec.problems.map((p) => (
                      <li key={p} className="text-[10px] px-2 py-1.5 leading-relaxed"
                          style={{ background: CHROME.surfaceActive, color: CHROME.inkSecondary }}>
                        {p}
                      </li>
                    ))}
                  </ul>
                )}
              </Panel>
            )}

            {selected === 'custom' && (
              <UploadPanel onJobStarted={(job) => setJobs((prev) => [job, ...prev])} />
            )}

            <JobList jobs={jobs} />
          </>
        )}
      </div>
    </div>
  )
}

function Panel({ title, children }) {
  return (
    <section style={{ background: CHROME.surface, border: `1px solid ${CHROME.border}` }}>
      <header className="px-4 py-2.5" style={{ borderBottom: `1px solid ${CHROME.border}` }}>
        <h3 className="text-xs font-semibold uppercase tracking-wide" style={{ color: CHROME.ink }}>{title}</h3>
      </header>
      <div className="p-4 flex flex-col gap-3">{children}</div>
    </section>
  )
}
