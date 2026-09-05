import { CHROME, STATUS } from './analytics/theme.js'

/* Says out loud what selecting a mode did and did not do.
 *
 * This exists because the mode selector was quietly dishonest. Switching to
 * "Production (airgapped)" flips APP_MODE inside the ingestion control service
 * and nothing else: the analytics and chatbot processes hold a Neo4j driver
 * built from the NEO4J_URI they were STARTED with, and the realtime
 * DNIF -> Kafka -> parse path is a separate long-running process that a mode
 * switch does not start. So the pages carried on showing the sample sandbox
 * while the sidebar said "Production", which is the worst possible outcome -
 * sample data wearing a production label.
 *
 * Two things are surfaced, and neither is decoration:
 *  - every problem the backend's validate() found for this mode, verbatim
 *    (KAFKA_BOOTSTRAP_SERVERS unset, DNIF_API_URL still a placeholder, ...);
 *  - the Neo4j address the read services are actually reading, so a number on
 *    screen can always be traced to the database it came from.
 */
export default function ModeBanner({ mode, spec, graphUri, graphDatabase }) {
  const problems = spec?.problems || []
  if (!spec || problems.length === 0) return null

  const isProduction = mode === 'production'

  return (
    <div
      role="status"
      className="px-6 py-3 flex flex-col gap-1.5 shrink-0"
      style={{
        background: CHROME.surfaceRaised,
        borderBottom: `1px solid ${CHROME.borderStrong}`,
        boxShadow: `inset 4px 0 0 ${STATUS.warning}`,
      }}
    >
      <div className="flex items-baseline gap-2 flex-wrap">
        <span className="text-xs font-bold" style={{ color: CHROME.ink }}>
          {spec.label} is selected but not fully configured
        </span>
        {isProduction && (
          <span className="text-[11px]" style={{ color: CHROME.inkSecondary }}>
            — the realtime Kafka path is not running, so nothing new is being ingested.
          </span>
        )}
      </div>

      <ul className="flex flex-col gap-0.5">
        {problems.map((p) => (
          <li key={p} className="text-[11px] leading-snug" style={{ color: CHROME.inkSecondary }}>
            • {p}
          </li>
        ))}
      </ul>

      {graphUri && (
        <div className="text-[11px]" style={{ color: CHROME.inkMuted }}>
          Everything on screen is read from{' '}
          <span className="font-mono" style={{ color: CHROME.inkSecondary }}>
            {graphUri}
          </span>
          {graphDatabase ? (
            <>
              {' '}/{' '}
              <span className="font-mono" style={{ color: CHROME.inkSecondary }}>
                {graphDatabase}
              </span>
            </>
          ) : null}
          {isProduction && ' — not a production graph. Point NEO4J_URI at one and restart the services.'}
        </div>
      )}
    </div>
  )
}
