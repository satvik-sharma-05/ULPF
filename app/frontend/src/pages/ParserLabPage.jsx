import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { CHROME, GLASS, SERIES, SERIES_TEXT, SEVERITY_META, STATUS, glassPanel } from '../components/analytics/theme.js'
import { parsePreview } from '../ingestApi.js'

/* ParserLabPage - paste or drop a log, watch it be parsed, store nothing.
 *
 * Every other screen shows conclusions drawn from the graph. This one shows
 * the step those conclusions rest on, and it is the only place the framework's
 * central claims can be checked rather than believed:
 *
 *   (a) lossless      the raw record sits beside the parsed one, byte for
 *                     byte, and the RAW PRESERVED check compares what the
 *                     graph would store against what was typed
 *   (b) extraction    the attributes the detector recovered, named
 *   (c) normalization the same field grid for every source, whatever it was
 *   (d) traceability  the id linking the two, computed exactly as ingestion
 *                     computes it
 *   (e) plug-and-play an unrecognised source still yields a usable record,
 *                     flagged as fallback so its limits are visible too
 *
 * Nothing here is written to Neo4j. That is what makes it usable against a
 * production deployment: you can try a vendor's sample log without the graph
 * acquiring it.
 */

const MAX_KB = 2048
const DEBOUNCE_MS = 450

const FORMATS = [
  ['auto', 'Auto-detect'],
  ['text', 'Text / Syslog'],
  ['json', 'JSON / NDJSON'],
  ['csv', 'CSV'],
  ['xml', 'XML'],
]

/* A deliberately heterogeneous sample: nine vendors, six formats, all in one
 * paste. Mixed input is the whole premise of the problem - a file that is all
 * one format proves very little - and this snippet is what caught the
 * reassembly bug where CEF, LEEF and JSON records were being welded onto
 * whatever line preceded them. */
const SAMPLE_MIXED = `<166>Sep  3 09:16:11 asa-edge-01 %ASA-6-302013: Built inbound TCP connection 129472 for outside:203.0.113.9/54321 to inside:10.20.30.43/443
date=2026-09-03 time=09:22:03 devname="FGT-EDGE-01" type="traffic" level="warning" srcip=10.20.30.41 dstip=8.8.8.8 action="blocked" msg="Denied by firewall policy"
CEF:0|Palo Alto Networks|PAN-OS|10.2|threat|url|5|rt=Sep 03 2026 09:23:22 src=10.20.30.40 dst=93.184.216.34 act=deny cs1Label=Rule cs1=Block-Malware-URL
LEEF:2.0|Check Point|Firewall|R81|Drop|src=10.20.30.42|dst=172.217.16.14|dstPort=80|proto=TCP|action=Drop|sev=6
Sep  3 09:25:10 web-srv-07 sshd[4412]: Failed password for invalid user admin from 203.0.113.101 port 52344 ssh2
2026-09-03 09:30:02.910 UTC [4415] app@billing FATAL:  password authentication failed for user "app"
{"eventTime":"2026-09-03T09:33:00Z","eventSource":"s3.amazonaws.com","eventName":"DeleteBucket","awsRegion":"ap-south-1","sourceIPAddress":"203.0.113.9","errorCode":"AccessDenied","userIdentity":{"type":"IAMUser","userName":"svc-backup"}}
{"metadata":{"eventType":"DetectionSummaryEvent"},"event":{"ComputerName":"WKS-FIN-014","SeverityName":"CRITICAL","DetectName":"Credential Dumping","FileName":"rundll32.exe"}}
{"published":"2026-09-03T09:41:30.000Z","eventType":"user.account.lock","displayMessage":"Max sign in attempts exceeded","outcome":{"result":"DENY","reason":"LOCKED_OUT"},"actor":{"alternateId":"alice@corp.example"}}
<13>Sep  3 09:43:02 iot-gw-14 modbus-bridge[771]: sensor=TEMP-0042 zone=cold-store value=8.9 unit=C threshold=8.0 state=ALARM`

/* A format with no detector at all, so the fallback path is demonstrable
 * rather than merely described. */
const SAMPLE_UNKNOWN = `2026-09-03T11:04:22Z acme-gw-04 zorblatt[9931]: SESSION_OPEN peer=198.51.100.14 tunnel=t-8821 cipher=AES256 result=ok
2026-09-03T11:04:51Z acme-gw-04 zorblatt[9931]: SESSION_DENY peer=203.0.113.77 tunnel=- reason=policy-mismatch result=fail
2026-09-03T11:05:03Z acme-gw-04 zorblatt[9931]: HEARTBEAT uptime=884721 queue_depth=3 result=ok`

const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'

/* `json_structure` is the detector a flattened CSV row or XML element goes
 * through, and it is the honest internal name - but shown beside `pri_syslog`
 * and `cef` it reads as a non-answer, because it names our mechanism rather
 * than the user's data. For those records the file format IS the answer to
 * "what was this recognised as", so say that and keep the mechanism in the
 * tooltip rather than dropping it. */
const STRUCTURAL = { CSV: 'csv row', XML: 'xml element', 'JSON / JSON Lines': 'json record' }

function detectorLabel(rec, detectedFormat) {
  if (rec.matched_format !== 'json_structure') return rec.matched_format
  return STRUCTURAL[detectedFormat] || 'structured record'
}

function Stat({ label, value, tone, hint }) {
  return (
    <div className="px-3 py-2 min-w-0" style={{ borderLeft: `2px solid ${tone || CHROME.borderStrong}` }}>
      <div className="text-[10px] uppercase tracking-wide font-semibold truncate" style={{ color: CHROME.inkMuted }}>
        {label}
      </div>
      <div className="text-lg font-bold leading-tight tabular-nums truncate" style={{ color: tone || CHROME.ink }}>
        {value}
      </div>
      {hint && <div className="text-[10px] truncate" style={{ color: CHROME.inkDim }}>{hint}</div>}
    </div>
  )
}

function Field({ name, value }) {
  if (value === null || value === undefined || value === '') return null
  return (
    <div className="flex gap-2 items-baseline min-w-0 py-[1px]">
      <span className="text-[10px] uppercase tracking-wide shrink-0 w-[104px] text-right font-semibold"
        style={{ color: CHROME.inkDim }}>{name}</span>
      <span className="text-[11px] min-w-0 break-all" style={{ color: CHROME.ink, fontFamily: MONO }}>
        {String(value)}
      </span>
    </div>
  )
}

function RecordCard({ rec, detectedFormat }) {
  const n = rec.normalized || {}
  const sev = SEVERITY_META[n.severity] || {}
  const rail = rec.is_fallback ? STATUS.warning : (sev.color || SERIES[1])
  const attrs = Object.entries(rec.attributes || {})

  return (
    <div className="mb-2" style={{ ...glassPanel(), borderLeft: `3px solid ${rail}` }}>
      <div className="flex items-center gap-2 px-3 py-1.5 flex-wrap"
        style={{ borderBottom: `1px solid ${GLASS.borderOuter}`, background: GLASS.surfaceRaised }}>
        <span className="text-[10px] tabular-nums font-bold" style={{ color: CHROME.inkDim }}>
          #{rec.ordinal}
        </span>
        <span className="text-[10px] font-bold px-1.5 py-[1px]"
          title={rec.matched_format === 'json_structure'
            ? 'Read as a structured record and mapped onto the common schema (detector: json_structure)'
            : `Detector: ${rec.matched_format}`}
          style={{ background: rec.is_fallback ? '#FFF4D6' : CHROME.surfaceActive,
                   color: rec.is_fallback ? '#836A00' : CHROME.primaryDeep, fontFamily: MONO }}>
          {detectorLabel(rec, detectedFormat)}
        </span>
        {rec.is_fallback && (
          <span className="text-[10px]" style={{ color: '#836A00' }}>
            no named detector &mdash; normalized structurally
          </span>
        )}
        {n.severity && (
          <span className="text-[10px] font-semibold ml-auto" style={{ color: sev.textColor || CHROME.inkMuted }}>
            {sev.glyph} {n.severity}
          </span>
        )}
        {n.timestamp_source === 'ingest' && (
          <span className="text-[10px]" style={{ color: CHROME.inkDim }} title="The record carried no timestamp of its own; ingest time was used.">
            no event time
          </span>
        )}
        {n.timestamp_anomalous && (
          <span className="text-[10px] font-semibold" style={{ color: STATUS.danger }}
            title="The event's own clock is far outside the ingest window. Flagged, never rewritten.">
            clock skew
          </span>
        )}
      </div>

      <div className="grid gap-0 md:grid-cols-2">
        <div className="p-3 min-w-0" style={{ borderRight: `1px solid ${GLASS.borderOuter}` }}>
          <div className="flex items-center gap-2 mb-1.5">
            <span className="text-[10px] uppercase tracking-wide font-bold" style={{ color: CHROME.inkMuted }}>
              Raw {rec.raw_lines > 1 && `(${rec.raw_lines} lines)`}
            </span>
            <span className="text-[10px] font-semibold"
              style={{ color: rec.raw_preserved ? STATUS.good : STATUS.danger }}
              title="Compares the raw_message the graph would store against the exact input.">
              {rec.raw_preserved ? '✓ preserved verbatim' : '✗ ALTERED'}
            </span>
          </div>
          <pre className="text-[11px] whitespace-pre-wrap break-all m-0"
            style={{ color: CHROME.inkSecondary, fontFamily: MONO }}>{rec.raw}</pre>
        </div>

        <div className="p-3 min-w-0">
          <div className="text-[10px] uppercase tracking-wide font-bold mb-1.5" style={{ color: CHROME.inkMuted }}>
            Normalized
          </div>
          <Field name="id" value={n.id} />
          <Field name="timestamp" value={n.timestamp} />
          <Field name="hostname" value={n.hostname} />
          <Field name="source type" value={n.source_type} />
          <Field name="process" value={n.pid ? `${n.process} [${n.pid}]` : n.process} />
          <Field name="component" value={n.component} />
          <Field name="severity" value={n.severity_score ? `${n.severity} (${n.severity_score})` : n.severity} />
          <Field name="message" value={n.message} />

          {attrs.length > 0 && (
            <div className="mt-2 pt-2" style={{ borderTop: `1px solid ${GLASS.borderOuter}` }}>
              <div className="text-[10px] uppercase tracking-wide font-bold mb-1" style={{ color: CHROME.inkMuted }}>
                Extracted attributes ({attrs.length})
              </div>
              <div className="flex flex-wrap gap-1">
                {attrs.map(([k, v]) => (
                  <span key={k} className="text-[10px] px-1.5 py-[1px]"
                    style={{ background: CHROME.surfaceCool, color: CHROME.inkSecondary, fontFamily: MONO }}>
                    <span style={{ color: CHROME.inkDim }}>{k}=</span>
                    {typeof v === 'object' ? JSON.stringify(v) : String(v)}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export default function ParserLabPage() {
  const [text, setText] = useState(SAMPLE_MIXED)
  const [format, setFormat] = useState('auto')
  const [filename, setFilename] = useState('pasted.log')
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [onlyFallback, setOnlyFallback] = useState(false)
  // Guards against an earlier request landing after a later one while you are
  // still typing, which would show output for text no longer on screen.
  const seq = useRef(0)

  const run = useCallback((value, fmt, name) => {
    if (!value.trim()) { setResult(null); setError(null); return }
    const mine = ++seq.current
    setBusy(true)
    parsePreview({ text: value, format: fmt, filename: name, limit: 200 })
      .then((r) => { if (mine === seq.current) { setResult(r); setError(null) } })
      .catch((e) => { if (mine === seq.current) { setError(e.message); setResult(null) } })
      .finally(() => { if (mine === seq.current) setBusy(false) })
  }, [])

  // Live: reparse shortly after typing stops. Short enough to feel immediate,
  // long enough not to fire a request per keystroke.
  useEffect(() => {
    const t = setTimeout(() => run(text, format, filename), DEBOUNCE_MS)
    return () => clearTimeout(t)
  }, [text, format, filename, run])

  const loadFile = useCallback((file) => {
    if (!file) return
    const reader = new FileReader()
    reader.onload = () => {
      let content = String(reader.result || '')
      if (content.length > MAX_KB * 1024) {
        // Truncated on a line boundary and said so, rather than sending a
        // request that the server would only reject.
        content = content.slice(0, MAX_KB * 1024)
        content = content.slice(0, content.lastIndexOf('\n') + 1 || content.length)
      }
      setFilename(file.name || 'upload.log')
      setText(content)
    }
    reader.readAsText(file)
  }, [])

  const s = result?.summary
  const shown = useMemo(() => {
    const rows = result?.records || []
    return onlyFallback ? rows.filter((r) => r.is_fallback) : rows
  }, [result, onlyFallback])

  const coverageTone = !s ? CHROME.inkMuted
    : s.coverage_pct >= 95 ? STATUS.good : s.coverage_pct >= 75 ? STATUS.warning : STATUS.danger

  return (
    <div className="h-full min-h-0 flex flex-col" style={{ background: CHROME.pagePlane }}>
      <div className="px-5 py-3 shrink-0" style={{ borderBottom: `1px solid ${GLASS.borderOuter}`, background: GLASS.chrome }}>
        <h1 className="text-base font-bold leading-tight" style={{ color: CHROME.ink }}>Parser Lab</h1>
        <p className="text-xs mt-0.5" style={{ color: CHROME.inkMuted }}>
          Paste or drop a log from any source. It is parsed as you type and{' '}
          <strong style={{ color: CHROME.inkSecondary }}>nothing is stored</strong> &mdash; safe to run
          against a production deployment.
        </p>
      </div>

      <div className="flex-1 min-h-0 grid gap-3 p-3 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)] overflow-hidden">
        {/* ---- input ---- */}
        <div className="min-h-0 flex flex-col gap-2 overflow-hidden">
          <div className="flex items-center gap-2 flex-wrap">
            <select value={format} onChange={(e) => setFormat(e.target.value)}
              className="text-xs px-2 py-1 outline-none"
              style={{ background: CHROME.surface, color: CHROME.ink, border: `1px solid ${CHROME.borderStrong}` }}>
              {FORMATS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
            <label className="text-xs px-2 py-1 cursor-pointer font-semibold"
              style={{ background: CHROME.primary, color: CHROME.primaryInk }}>
              Choose file
              <input type="file" className="hidden"
                onChange={(e) => loadFile(e.target.files?.[0])} />
            </label>
            <button onClick={() => { setFilename('mixed-sources.log'); setText(SAMPLE_MIXED) }}
              className="text-xs px-2 py-1" style={{ background: CHROME.surfaceActive, color: CHROME.inkSecondary }}>
              Mixed vendors
            </button>
            <button onClick={() => { setFilename('unknown-vendor.log'); setText(SAMPLE_UNKNOWN) }}
              className="text-xs px-2 py-1" style={{ background: CHROME.surfaceActive, color: CHROME.inkSecondary }}
              title="A vendor with no detector at all, to show what the fallback recovers on its own.">
              Unknown source
            </button>
            <button onClick={() => { setFilename('pasted.log'); setText('') }}
              className="text-xs px-2 py-1"
              style={{ background: 'transparent', color: CHROME.inkMuted }}>Clear</button>
          </div>

          <div
            onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => { e.preventDefault(); setDragging(false); loadFile(e.dataTransfer.files?.[0]) }}
            className="flex-1 min-h-0 flex flex-col"
            style={{ ...glassPanel(), borderStyle: dragging ? 'dashed' : 'solid',
                     borderColor: dragging ? CHROME.primary : GLASS.borderOuter }}
          >
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              spellCheck={false}
              placeholder="Paste log lines here, or drop a file&#10;&#10;Syslog, JSON, XML, CSV, CEF, LEEF, or any proprietary format."
              className="flex-1 min-h-0 w-full p-3 resize-none outline-none bg-transparent text-[11px] leading-relaxed"
              style={{ color: CHROME.ink, fontFamily: MONO }}
            />
            <div className="px-3 py-1.5 flex items-center gap-2 text-[10px] shrink-0"
              style={{ borderTop: `1px solid ${GLASS.borderOuter}`, color: CHROME.inkDim }}>
              <span style={{ fontFamily: MONO }}>{filename}</span>
              <span>{(new Blob([text]).size / 1024).toFixed(1)} KB</span>
              <span>{text ? text.split('\n').filter(Boolean).length : 0} lines</span>
              {busy && <span className="ml-auto font-semibold" style={{ color: CHROME.accentText }}>parsing&hellip;</span>}
            </div>
          </div>
        </div>

        {/* ---- output ---- */}
        <div className="min-h-0 flex flex-col overflow-hidden">
          {error && (
            <div className="p-3 mb-2 text-xs" style={{ ...glassPanel(), borderLeft: `3px solid ${STATUS.danger}`, color: STATUS.danger }}>
              {error}
            </div>
          )}

          {s && (
            <div className="shrink-0 mb-2" style={glassPanel({ raised: true })}>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5">
                <Stat label="Records" value={s.records} tone={SERIES[1]}
                  hint={s.returned < s.records
                    ? `${s.detected_format} · ${s.returned} shown below`
                    : s.detected_format} />
                <Stat label="Parsed by name" value={`${s.coverage_pct}%`} tone={coverageTone}
                  hint={`${s.named} named / ${s.fallback} fallback`} />
                <Stat label="Formats seen" value={s.distinct_formats} tone={SERIES[3]}
                  hint="distinct detectors" />
                <Stat label="Raw preserved" value={s.raw_preserved_all ? 'All' : 'FAILED'}
                  tone={s.raw_preserved_all ? STATUS.good : STATUS.danger} hint="byte-for-byte" />
                <Stat label="Throughput" value={s.records_per_sec ? `${s.records_per_sec.toLocaleString()}/s` : '—'}
                  tone={SERIES[4]} hint={`${s.elapsed_ms} ms, single core`} />
              </div>

              {/* Which detectors claimed the input, proportionally. On mixed
                  input this is the picture of heterogeneity being absorbed. */}
              <div className="px-3 pb-2 pt-1" style={{ borderTop: `1px solid ${GLASS.borderOuter}` }}>
                <div className="flex h-1.5 w-full overflow-hidden mb-1.5" style={{ background: CHROME.gridline }}>
                  {result.formats.map((f, i) => (
                    <div key={f.name} title={`${f.name}: ${f.count}`}
                      style={{ width: `${(100 * f.count) / s.records}%`,
                               background: f.name === 'generic_fallback' ? STATUS.warning : SERIES[i % SERIES.length] }} />
                  ))}
                </div>
                <div className="flex flex-wrap gap-x-3 gap-y-0.5">
                  {result.formats.slice(0, 12).map((f, i) => (
                    <span key={f.name} className="text-[10px] flex items-center gap-1"
                      style={{ color: f.name === 'generic_fallback' ? '#836A00' : SERIES_TEXT[i % SERIES_TEXT.length] }}>
                      <span style={{ width: 6, height: 6, display: 'inline-block',
                                     background: f.name === 'generic_fallback' ? STATUS.warning : SERIES[i % SERIES.length] }} />
                      <span style={{ fontFamily: MONO }}>{f.name}</span>
                      <span style={{ color: CHROME.inkDim }}>{f.count}</span>
                    </span>
                  ))}
                </div>
              </div>

              {/* Field-level normalization: a field that is null on every
                  record is an empty column, not a common taxonomy, so the
                  fill rate is stated rather than assumed. */}
              <div className="px-3 pb-2 pt-1.5" style={{ borderTop: `1px solid ${GLASS.borderOuter}` }}>
                <div className="text-[10px] uppercase tracking-wide font-bold mb-1" style={{ color: CHROME.inkMuted }}>
                  Common-schema fields populated
                </div>
                <div className="flex flex-wrap gap-x-3 gap-y-1">
                  {Object.entries(s.field_coverage).map(([field, fc]) => (
                    <span key={field} className="text-[10px] flex items-center gap-1" title={`${fc.count} of ${s.records}`}>
                      <span style={{ width: 22, height: 4, display: 'inline-block', background: CHROME.gridline }}>
                        <span style={{ width: `${fc.pct}%`, height: '100%', display: 'block',
                                       background: fc.pct >= 90 ? STATUS.good : fc.pct >= 50 ? SERIES[2] : STATUS.neutral }} />
                      </span>
                      <span style={{ color: CHROME.inkSecondary }}>{field}</span>
                      <span className="tabular-nums" style={{ color: CHROME.inkDim }}>{fc.pct}%</span>
                    </span>
                  ))}
                </div>
              </div>
            </div>
          )}

          {s && s.fallback > 0 && (
            <label className="shrink-0 mb-2 text-[11px] flex items-center gap-1.5 cursor-pointer" style={{ color: CHROME.inkMuted }}>
              <input type="checkbox" checked={onlyFallback} onChange={(e) => setOnlyFallback(e.target.checked)} />
              Show only the {s.fallback} record{s.fallback === 1 ? '' : 's'} no detector claimed
            </label>
          )}

          <div className="flex-1 min-h-0 overflow-y-auto pr-0.5">
            {!s && !error && (
              <div className="p-4 text-xs" style={{ ...glassPanel(), color: CHROME.inkMuted }}>
                Paste a log on the left. Anything works &mdash; a firewall line, a JSON
                cloud audit event, a vendor format nobody has written a parser for.
              </div>
            )}
            {shown.map((rec) => (
              <RecordCard key={`${rec.ordinal}-${rec.normalized?.id}`}
                rec={rec} detectedFormat={s?.detected_format} />
            ))}
            {s && s.returned < s.records && !onlyFallback && (
              <div className="p-2 text-[11px] text-center" style={{ color: CHROME.inkDim }}>
                Showing the first {s.returned} of {s.records} records. All {s.records} are
                counted in the figures above.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
