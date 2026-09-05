import { useState } from 'react'
import { CHROME, GLASS } from './analytics/theme.js'
import { chartSvg } from './chartSvg.js'

/* Export one chat answer as PDF, or hand it to the backend to become a deck.
 *
 * PDF is generated in the BROWSER, via a print window rather than a bundled
 * PDF library. Three reasons, in order of how much they matter here:
 *
 *  - It adds nothing to the air-gapped deployment. ReportLab on the backend
 *    would mean a new dependency, a new endpoint and a rebuild of two images
 *    to produce a document the client already has all the content for.
 *  - Print CSS reuses the page's own typography, so the PDF looks like the
 *    product rather than like a generated report.
 *  - The browser's print dialog is where people already expect "save as PDF"
 *    to live, including "which printer" and "landscape".
 *
 * PPTX is different: a .pptx is a zip of XML with a rigid schema, and there is
 * no honest way to build one client-side without shipping a library that is
 * larger than python-pptx. That one is a backend call.
 */

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ))
}

/* The answer is markdown. Escaping it and stopping there printed a literal
 * "**16,791 INFO**" into the exported PDF - the emphasis markers came out as
 * asterisks. Escape first, because the text is untrusted, then re-introduce
 * only the few inline tags a one-page report actually needs. */
function inlineMarkdown(text) {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>')
    .replace(/\n/g, '<br>')
}

function rowsTable(rows) {
  if (!rows || rows.length === 0) return ''
  const cols = Object.keys(rows[0])
  const head = cols.map((c) => `<th>${escapeHtml(c)}</th>`).join('')
  // Capped: a print of ten thousand rows is not a report, and the browser will
  // sit paginating it for a long time before anyone finds that out.
  const capped = rows.slice(0, 200)
  const body = capped
    .map((r) => `<tr>${cols.map((c) => `<td>${escapeHtml(r[c])}</td>`).join('')}</tr>`)
    .join('')
  const note = rows.length > capped.length
    ? `<p class="note">Showing the first ${capped.length} of ${rows.length} rows.</p>`
    : ''
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>${note}`
}

function buildPrintable({ question, answer, cypher, rows, route }) {
  const when = new Date().toLocaleString()
  return `<!doctype html><html><head><meta charset="utf-8">
<title>ULPF — ${escapeHtml((question || 'answer').slice(0, 60))}</title>
<style>
  @page { margin: 18mm; }
  body { font: 12px/1.55 "Segoe UI", system-ui, sans-serif; color: #2B2118; }
  h1 { font-size: 17px; margin: 0 0 2px; }
  .meta { font-size: 10px; color: #6B5B4D; margin-bottom: 16px; }
  .q { background: #FFF4E6; border-left: 4px solid #F76707; padding: 8px 10px; margin-bottom: 14px; }
  .q b { display: block; font-size: 10px; text-transform: uppercase; letter-spacing: .04em; color: #6B5B4D; }
  pre { background: #FFF8F0; border: 1px solid #F0E4D4; padding: 8px; font-size: 10px;
        white-space: pre-wrap; word-break: break-word; }
  svg { margin: 8px 0 4px; break-inside: avoid; }
  table { border-collapse: collapse; width: 100%; font-size: 10px; margin-top: 6px; }
  th, td { border: 1px solid #E4D5C2; padding: 4px 6px; text-align: left; }
  th { background: #FFF4E6; }
  tr { break-inside: avoid; }
  h2 { font-size: 12px; margin: 16px 0 4px; }
  code { background: #FFF4E6; padding: 1px 3px; font-size: 10px; }
  .note { font-size: 10px; color: #6B5B4D; }
  footer { margin-top: 20px; border-top: 1px solid #E4D5C2; padding-top: 6px;
           font-size: 9px; color: #94826F; }
</style></head><body>
<h1>Universal Log Pre-processing Framework</h1>
<div class="meta">Chat answer &middot; ${escapeHtml(when)}${route ? ` &middot; ${escapeHtml(route)}` : ''}</div>
<div class="q"><b>Question</b>${escapeHtml(question || '—')}</div>
<div>${inlineMarkdown(answer || '')}</div>
${cypher ? `<h2>Query</h2><pre>${escapeHtml(cypher)}</pre>` : ''}
${rows && rows.length ? `<h2>Results</h2>${chartSvg(rows)}${rowsTable(rows)}` : ''}
<footer>Generated offline by ULPF. Figures come from the graph at query time.</footer>
</body></html>`
}

export default function ExportAnswer({ question, response }) {
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  const payload = {
    question,
    answer: response?.answer,
    cypher: response?.cypher,
    rows: response?.rows,
    route: response?.route?.strategy,
  }

  /* Prints through a hidden iframe, not a pop-up window.
   *
   * The pop-up version was broken in two ways at once. Browsers block
   * window.open outside a few trusted gestures, so most of the time nothing
   * appeared at all; and when it did open, `onload` was assigned AFTER
   * document.write/close, by which point the load event had usually already
   * fired - so print() never ran and the user was left staring at a blank
   * tab. An iframe has no pop-up blocker to satisfy, and srcdoc gives a load
   * event that is guaranteed to arrive after the handler is attached.
   */
  const toPdf = () => {
    setError(null)
    try {
      const frame = document.createElement('iframe')
      frame.setAttribute('aria-hidden', 'true')
      // Off-screen rather than display:none - a display:none iframe has no
      // layout in some engines and prints an empty page.
      frame.style.cssText =
        'position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden'
      frame.onload = () => {
        try {
          const win = frame.contentWindow
          win.focus()
          win.print()
        } catch (e) {
          setError(`Could not open the print dialog: ${e.message}`)
        } finally {
          // Leave it long enough for the dialog to take its snapshot; removing
          // it immediately cancels the print on some browsers.
          setTimeout(() => frame.remove(), 60000)
        }
      }
      frame.srcdoc = buildPrintable(payload)
      document.body.appendChild(frame)
    } catch (e) {
      setError(e.message)
    }
  }

  const toFile = async (kind) => {
    setBusy(kind)
    setError(null)
    try {
      const res = await fetch(`/api/export/${kind}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText)
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `ulpf-answer.${kind === 'pptx' ? 'pptx' : 'docx'}`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(null)
    }
  }

  const btn = {
    border: `1px solid ${GLASS.borderOuter}`,
    color: CHROME.inkSecondary,
  }

  return (
    <div className="mt-2 flex items-center gap-1.5 flex-wrap">
      <button type="button" onClick={toPdf} className="text-[10px] px-2 py-1" style={btn}>
        Export PDF
      </button>
      <button type="button" onClick={() => toFile('pptx')} disabled={busy === 'pptx'}
              className="text-[10px] px-2 py-1 disabled:opacity-50" style={btn}>
        {busy === 'pptx' ? 'Building…' : 'Export PPTX'}
      </button>
      <button type="button" onClick={() => toFile('docx')} disabled={busy === 'docx'}
              className="text-[10px] px-2 py-1 disabled:opacity-50" style={btn}>
        {busy === 'docx' ? 'Building…' : 'Export Word'}
      </button>
      {error && (
        <span className="text-[10px]" style={{ color: CHROME.inkMuted }}>{error}</span>
      )}
    </div>
  )
}
