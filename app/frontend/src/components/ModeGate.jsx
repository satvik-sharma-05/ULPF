import { CHROME, SERIES, STATUS, glassPanel } from './analytics/theme.js'

/* Refuses to render data pages when the active mode is not actually connected
 * to the data source it claims.
 *
 * The bug this fixes: selecting "Production (airgapped)" showed the full
 * dashboard - 24.5K events, alerts, hosts - even though no Kafka broker and no
 * DNIF endpoint were configured and nothing was being ingested. Those numbers
 * were the SAMPLE sandbox, because the analytics and chatbot processes hold a
 * Neo4j driver built from the NEO4J_URI they were started with and a mode
 * switch does not repoint them.
 *
 * A banner saying so was not enough. Sample data rendered under a production
 * label is the kind of thing someone makes a decision on, and "we warned them
 * at the top of the page" is not a defence. So the data is withheld and the
 * page says exactly what is missing instead.
 *
 * What is NOT gated: Home, and Modes - you need Modes to fix the very
 * configuration this is complaining about, so locking it would be a trap.
 */
export default function ModeGate({ gate, onNavigate, children }) {
  if (!gate) return children

  return (
    <div className="h-full overflow-y-auto" style={{ background: CHROME.pagePlane }}>
      <div className="max-w-3xl mx-auto px-6 py-16 flex flex-col gap-6">
        <div>
          <div className="text-[11px] uppercase tracking-wide font-bold mb-2" style={{ color: STATUS.warning }}>
            No data source
          </div>
          <h1 className="text-3xl font-extrabold leading-tight" style={{ color: CHROME.ink }}>
            {gate.modeLabel} is selected, but it is not connected to anything
          </h1>
          <p className="mt-3 text-sm leading-relaxed" style={{ color: CHROME.inkSecondary }}>
            {gate.summary}
          </p>
        </div>

        <section className="overflow-hidden" style={glassPanel()}>
          <header className="px-4 py-2.5" style={{ borderBottom: `1px solid ${CHROME.border}` }}>
            <h2 className="text-xs font-bold uppercase tracking-wide" style={{ color: CHROME.ink }}>
              What is missing
            </h2>
          </header>
          <ul className="px-4 py-3 flex flex-col gap-2">
            {gate.reasons.map((r) => (
              <li key={r} className="text-xs leading-snug flex gap-2" style={{ color: CHROME.inkSecondary }}>
                <span style={{ color: STATUS.danger }}>•</span>
                <span>{r}</span>
              </li>
            ))}
          </ul>
        </section>

        {gate.graphUri && (
          <section className="overflow-hidden" style={glassPanel()}>
            <div className="px-4 py-3">
              <h2 className="text-xs font-bold uppercase tracking-wide mb-1.5" style={{ color: CHROME.ink }}>
                Why the numbers are withheld
              </h2>
              <p className="text-xs leading-relaxed" style={{ color: CHROME.inkSecondary }}>
                The analytics and chat services are reading{' '}
                <span className="font-mono" style={{ color: CHROME.accentText }}>
                  {gate.graphUri}
                </span>
                {gate.graphDatabase ? (
                  <> / <span className="font-mono" style={{ color: CHROME.accentText }}>{gate.graphDatabase}</span></>
                ) : null}
                {' '}— the local sample sandbox, not a production graph. They build their Neo4j
                driver once at startup, so switching mode here does not repoint them. Showing
                you those events under a production label would be showing you the wrong data.
              </p>
            </div>
          </section>
        )}

        <section className="overflow-hidden" style={glassPanel()}>
          <div className="px-4 py-3">
            <h2 className="text-xs font-bold uppercase tracking-wide mb-1.5" style={{ color: CHROME.ink }}>
              To bring this mode up
            </h2>
            <ol className="flex flex-col gap-1.5 text-xs leading-snug" style={{ color: CHROME.inkSecondary }}>
              <li>1. Set <span className="font-mono">NEO4J_URI</span>,{' '}
                  <span className="font-mono">KAFKA_BOOTSTRAP_SERVERS</span> and{' '}
                  <span className="font-mono">DNIF_API_URL</span> to your real infrastructure.</li>
              <li>2. Restart the ingestion, analytics and chat services so they pick up the new address.</li>
              <li>3. Start the realtime producer and consumer to begin ingesting from Kafka.</li>
            </ol>
          </div>
        </section>

        <div className="flex flex-wrap gap-2.5">
          <button
            onClick={() => onNavigate?.('modes')}
            className="px-5 py-2.5 text-sm font-semibold"
            style={{ background: CHROME.primary, color: CHROME.primaryInk }}
          >
            Open Modes
          </button>
          <button
            onClick={() => gate.onUseSample?.()}
            className="px-5 py-2.5 text-sm font-semibold"
            style={{ background: CHROME.surface, border: `1px solid ${CHROME.borderStrong}`, color: CHROME.ink }}
          >
            Switch back to Sample
          </button>
        </div>
      </div>
    </div>
  )
}
