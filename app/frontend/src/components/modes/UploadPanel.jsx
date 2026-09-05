import { useRef, useState } from 'react'
import { CHROME } from '../analytics/theme.js'
import { uploadLogFile } from '../../ingestApi.js'

const FORMATS = [
  ['auto', 'Auto-detect'],
  ['text', 'Syslog / plain text'],
  ['json', 'JSON / JSON Lines'],
  ['csv', 'CSV / TSV'],
  ['xml', 'XML'],
  ['cef', 'CEF'],
  ['leef', 'LEEF'],
]

export default function UploadPanel({ onJobStarted }) {
  const [file, setFile] = useState(null)
  const [format, setFormat] = useState('auto')
  const [embed, setEmbed] = useState(true)
  const [wipe, setWipe] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef(null)

  async function submit() {
    if (!file || busy) return
    setBusy(true)
    setError(null)
    try {
      const job = await uploadLogFile(file, { format, embed, wipe })
      onJobStarted?.(job)
      setFile(null)
      if (inputRef.current) inputRef.current.value = ''
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  function onDrop(e) {
    e.preventDefault()
    setDragging(false)
    const dropped = e.dataTransfer.files?.[0]
    if (dropped) setFile(dropped)
  }

  const controlStyle = {
    background: CHROME.surfaceRaised,
    border: `1px solid ${CHROME.border}`,
    color: CHROME.ink,
  }

  return (
    <section style={{ background: CHROME.surface, border: `1px solid ${CHROME.border}` }}>
      <header className="px-4 py-2.5" style={{ borderBottom: `1px solid ${CHROME.border}` }}>
        <h3 className="text-xs font-semibold uppercase tracking-wide" style={{ color: CHROME.ink }}>
          Upload your logs
        </h3>
        <p className="text-[10px]" style={{ color: CHROME.inkMuted }}>
          Syslog, JSON, JSONL, XML, CSV, CEF, LEEF, or any plain-text vendor format — parsed with the
          same detectors, entity extraction and embeddings as the bundled corpus.
        </p>
      </header>

      <div className="p-4 flex flex-col gap-3">
        <div
          onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          onClick={() => inputRef.current?.click()}
          className="px-4 py-8 text-center cursor-pointer transition-colors"
          style={{
            border: `1px dashed ${dragging ? CHROME.ink : CHROME.borderStrong}`,
            background: dragging ? CHROME.surfaceRaised : 'transparent',
          }}
        >
          <input ref={inputRef} type="file" className="hidden"
                 onChange={(e) => setFile(e.target.files?.[0] || null)} />
          {file ? (
            <div className="text-xs font-mono" style={{ color: CHROME.ink }}>
              {file.name}
              <span className="ml-2" style={{ color: CHROME.inkMuted }}>
                {(file.size / 1024).toFixed(1)} KB
              </span>
            </div>
          ) : (
            <div className="text-xs" style={{ color: CHROME.inkSecondary }}>
              Drop a log file here, or click to browse
            </div>
          )}
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-wide" style={{ color: CHROME.inkMuted }}>Format</span>
            <select value={format} onChange={(e) => setFormat(e.target.value)}
                    className="px-2 py-1.5 text-[11px]" style={controlStyle}>
              {FORMATS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>

          <div className="flex flex-col gap-2 justify-end">
            <label className="flex items-center gap-2 text-[11px] cursor-pointer" style={{ color: CHROME.inkSecondary }}>
              <input type="checkbox" checked={embed} onChange={(e) => setEmbed(e.target.checked)} />
              Generate embeddings (enables semantic search)
            </label>
            <label className="flex items-center gap-2 text-[11px] cursor-pointer" style={{ color: CHROME.inkSecondary }}>
              <input type="checkbox" checked={wipe} onChange={(e) => setWipe(e.target.checked)} />
              Wipe the graph first (deletes everything)
            </label>
          </div>
        </div>

        {!embed && (
          <p className="text-[10px] px-2.5 py-2 leading-relaxed"
             style={{ background: CHROME.surfaceActive, color: CHROME.inkSecondary }}>
            <strong style={{ color: CHROME.ink }}>Note — </strong>
            without embeddings the chatbot falls back to full-text search on this data; semantic
            &ldquo;why did X fail&rdquo; questions will be much weaker.
          </p>
        )}

        {wipe && (
          <p className="text-[10px] px-2.5 py-2 leading-relaxed"
             style={{ background: CHROME.surfaceActive, color: CHROME.ink, border: `1px solid ${CHROME.borderStrong}` }}>
            <strong>Destructive — </strong>
            every node in the target Neo4j will be deleted before this file is ingested.
          </p>
        )}

        {error && (
          <p className="text-[10px] px-2.5 py-2" style={{ border: `1px solid ${CHROME.borderStrong}`, color: CHROME.ink }}>
            {error}
          </p>
        )}

        <button
          onClick={submit}
          disabled={!file || busy}
          className="px-4 py-2 text-xs font-medium disabled:opacity-40"
          style={{ background: CHROME.ink, color: CHROME.pagePlane }}
        >
          {busy ? 'Uploading…' : 'Ingest file'}
        </button>
      </div>
    </section>
  )
}
