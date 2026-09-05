import { CHROME, GLASS, SERIES, STATUS } from '../analytics/theme.js'

/* A generated deck or report, shown in the transcript with its outline.
 *
 * The outline is not decoration. The file is produced by a language model, and
 * the fastest way to know whether it is worth opening is to read the section
 * titles - so they are on screen before anyone downloads anything.
 *
 * `grounded` says the content was written from real figures pulled out of the
 * graph rather than from the model's general knowledge, and `composed` says
 * the model actually returned something. A document that is structurally
 * perfect and entirely empty is a real outcome when the local LLM times out,
 * and it has to be visible rather than discovered on slide three.
 */
export default function DocumentCard({ doc }) {
  if (!doc) return null

  const download = () => {
    const bytes = Uint8Array.from(atob(doc.file_b64), (c) => c.charCodeAt(0))
    const url = URL.createObjectURL(new Blob([bytes], { type: doc.media_type }))
    const a = document.createElement('a')
    a.href = url
    a.download = doc.filename
    a.click()
    URL.revokeObjectURL(url)
  }

  const kb = Math.max(1, Math.round(doc.bytes / 1024))

  return (
    <div className="mt-2" style={{ border: `1px solid ${GLASS.borderOuter}` }}>
      <div className="px-3 py-2 flex items-center justify-between gap-3 flex-wrap"
           style={{ background: CHROME.surfaceRaised, borderBottom: `1px solid ${GLASS.borderOuter}` }}>
        <div className="min-w-0">
          <div className="text-xs font-bold truncate" style={{ color: CHROME.ink }}>{doc.title}</div>
          <div className="text-[10px]" style={{ color: CHROME.inkMuted }}>
            {doc.kind.toUpperCase()} · {doc.sections} {doc.kind === 'pptx' ? 'slides' : 'pages'} · {kb} KB
            {doc.grounded && ' · figures from the graph'}
          </div>
        </div>
        <button
          type="button"
          onClick={download}
          className="text-[11px] px-3 py-1.5 font-semibold shrink-0"
          style={{ background: CHROME.primary, color: CHROME.primaryInk }}
        >
          Download ↓
        </button>
      </div>

      {doc.composed === false && (
        <div className="px-3 py-1.5 text-[10px]"
             style={{ color: CHROME.ink, background: CHROME.surfaceActive,
                      boxShadow: `inset 3px 0 0 ${STATUS.warning}` }}>
          The model returned nothing, so this document has placeholder sections.
          A local model on CPU can take minutes for a document this long — try a
          faster provider, or fewer sections.
        </div>
      )}

      <ol className="px-3 py-2 flex flex-col gap-1.5">
        {(doc.outline || []).map((section, i) => (
          <li key={i} className="text-[11px]">
            <span className="font-semibold" style={{ color: CHROME.ink }}>
              <span className="font-mono mr-1.5" style={{ color: SERIES[i % SERIES.length] }}>
                {String(i + 1).padStart(2, '0')}
              </span>
              {section.title}
            </span>
            <ul className="mt-0.5 ml-6 list-disc" style={{ color: CHROME.inkMuted }}>
              {(section.bullets || []).slice(0, 3).map((b, j) => (
                <li key={j} className="leading-snug">{b}</li>
              ))}
            </ul>
          </li>
        ))}
      </ol>
    </div>
  )
}
