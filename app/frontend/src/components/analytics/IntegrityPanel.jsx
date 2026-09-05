import { useCallback, useEffect, useState } from 'react'
import { CHROME, GLASS, SERIES, STATUS, glassPanel } from './theme.js'
import { fetchIntegrityStatus, proveEvent, sealLedger, verifyLedger } from '../../analyticsApi.js'

/* IntegrityPanel - the tamper-evident ledger, made visible.
 *
 * Every other panel reports what the data says. This one reports whether the
 * data can still be trusted, which is a different question and the one a
 * forensic or compliance reviewer actually asks.
 *
 * The three actions map to the three things a reviewer needs to do:
 *   Seal    commit the stored events to an append-only hash chain
 *   Verify  re-derive every hash and find the first break, if any
 *   Prove   produce ~9 sibling hashes that prove one event was in the sealed
 *           set, without revealing any other event
 *
 * The head hash is given the most visual weight on purpose. It is the single
 * value that has to be copied somewhere the operator cannot silently change,
 * and the guarantee is only as strong as that step - so the UI says so rather
 * than letting a green tick imply more than it means.
 */

const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'

function Row({ label, children }) {
  return (
    <div className="flex gap-3 items-baseline py-1 min-w-0">
      <span className="text-[10px] uppercase tracking-wide font-semibold shrink-0 w-[112px] text-right"
        style={{ color: CHROME.inkDim }}>{label}</span>
      <span className="text-[11px] min-w-0 break-all" style={{ color: CHROME.ink, fontFamily: MONO }}>
        {children}
      </span>
    </div>
  )
}

export default function IntegrityPanel() {
  const [status, setStatus] = useState(null)
  const [result, setResult] = useState(null)
  const [proof, setProof] = useState(null)
  const [eventId, setEventId] = useState('')
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  const load = useCallback(() => {
    fetchIntegrityStatus().then(setStatus).catch((e) => setError(e.message))
  }, [])
  useEffect(load, [load])

  const run = (name, fn) => {
    setBusy(name); setError(null)
    fn().then((r) => { setResult({ name, r }); load() })
      .catch((e) => setError(e.message))
      .finally(() => setBusy(null))
  }

  const verdict = result?.name === 'verify' ? result.r : null
  const tone = verdict == null ? CHROME.inkMuted
    : verdict.valid ? STATUS.good : STATUS.danger

  return (
    <div style={glassPanel()} className="p-4">
      <div className="flex items-baseline gap-2 mb-1 flex-wrap">
        <h3 className="text-sm font-bold" style={{ color: CHROME.ink }}>
          Tamper-evident ledger
        </h3>
        <span className="text-[10px] px-1.5 py-[1px] font-semibold"
          style={{ background: CHROME.surfaceActive, color: CHROME.primaryDeep }}>
          append-only · Merkle
        </span>
      </div>
      <p className="text-xs mb-3" style={{ color: CHROME.inkMuted }}>
        Every stored event contributes a leaf; batches are sealed into blocks; each block
        commits to the previous one. Altering one byte breaks its block and every block after
        it &mdash; detection is deterministic, not probabilistic.
      </p>

      {status && (
        <div className="mb-3 pb-3" style={{ borderBottom: `1px solid ${GLASS.borderOuter}` }}>
          <Row label="sealed">{status.sealed ? 'yes' : 'not yet sealed'}</Row>
          {status.sealed && <Row label="blocks">{status.blocks} · {status.events} events</Row>}
          {status.head && (
            <div className="mt-2 p-2" style={{ background: CHROME.surfaceCool }}>
              <div className="text-[10px] uppercase tracking-wide font-bold mb-1"
                style={{ color: CHROME.inkDim }}>Head hash &mdash; protect this value</div>
              <div className="text-[11px] break-all font-bold"
                style={{ color: CHROME.ink, fontFamily: MONO }}>{status.head}</div>
              <div className="text-[10px] mt-1" style={{ color: CHROME.inkMuted }}>
                Copy it somewhere this system cannot change. Until then the ledger detects
                tampering by everyone except whoever can rewrite the ledger itself.
              </div>
            </div>
          )}
        </div>
      )}

      <div className="flex gap-2 flex-wrap mb-3">
        <button onClick={() => run('seal', () => sealLedger(5000))} disabled={busy}
          className="text-xs px-2.5 py-1 font-semibold"
          style={{ background: CHROME.primary, color: CHROME.primaryInk, opacity: busy ? 0.6 : 1 }}>
          {busy === 'seal' ? 'Sealing…' : 'Seal stored events'}
        </button>
        <button onClick={() => run('verify', () => verifyLedger())} disabled={busy || !status?.sealed}
          className="text-xs px-2.5 py-1 font-semibold"
          style={{ background: CHROME.surfaceActive, color: CHROME.inkSecondary,
                   opacity: busy || !status?.sealed ? 0.5 : 1 }}>
          {busy === 'verify' ? 'Verifying…' : 'Verify chain'}
        </button>
      </div>

      {status?.sealed && (
        <div className="flex gap-2 items-center mb-3 flex-wrap">
          <input value={eventId} onChange={(e) => setEventId(e.target.value)}
            placeholder="event id, e.g. log-4f72b738014d80d6"
            className="text-[11px] px-2 py-1 flex-1 min-w-[220px] outline-none"
            style={{ fontFamily: MONO, background: CHROME.surface,
                     border: `1px solid ${CHROME.borderStrong}`, color: CHROME.ink }} />
          <button onClick={() => { setProof(null); run('prove', () => proveEvent(eventId).then((p) => { setProof(p); return p })) }}
            disabled={busy || !eventId.trim()}
            className="text-xs px-2.5 py-1 font-semibold"
            style={{ background: CHROME.surfaceActive, color: CHROME.inkSecondary,
                     opacity: busy || !eventId.trim() ? 0.5 : 1 }}>
            Prove one event
          </button>
        </div>
      )}

      {error && (
        <div className="p-2 mb-2 text-[11px]"
          style={{ borderLeft: `3px solid ${STATUS.danger}`, color: STATUS.danger,
                   background: CHROME.surface }}>{error}</div>
      )}

      {verdict && (
        <div className="p-2.5 mb-2" style={{ borderLeft: `3px solid ${tone}`, background: CHROME.surface }}>
          <div className="text-xs font-bold" style={{ color: tone }}>
            {verdict.valid === null ? 'Nothing sealed yet'
              : verdict.valid ? 'VALID — no tampering detected'
              : `TAMPERED — first break at block ${verdict.first_break_at_height}`}
          </div>
          <div className="text-[11px] mt-1" style={{ color: CHROME.inkMuted }}>
            {verdict.blocks} blocks · {verdict.events} events re-derived
          </div>
          {(verdict.problems || []).slice(0, 4).map((p, i) => (
            <div key={i} className="text-[10px] mt-1" style={{ color: STATUS.danger, fontFamily: MONO }}>
              height {p.height} · {p.kind} · {p.detail}
            </div>
          ))}
        </div>
      )}

      {result?.name === 'seal' && (
        <div className="p-2.5 mb-2" style={{ borderLeft: `3px solid ${SERIES[4]}`, background: CHROME.surface }}>
          <div className="text-xs font-bold" style={{ color: CHROME.ink }}>
            Sealed {result.r.sealed} events into {result.r.blocks} blocks
          </div>
          <div className="text-[10px] mt-1" style={{ color: CHROME.inkMuted }}>{result.r.next_step}</div>
        </div>
      )}

      {proof && (
        <div className="p-2.5" style={{ borderLeft: `3px solid ${STATUS.good}`, background: CHROME.surface }}>
          <div className="text-xs font-bold mb-1" style={{ color: CHROME.ink }}>
            Inclusion proof &mdash; {proof.proof.length} sibling hashes
          </div>
          <Row label="event">{proof.event_id}</Row>
          <Row label="block">{proof.block_height}</Row>
          <Row label="merkle root">{proof.merkle_root}</Row>
          <div className="text-[10px] mt-2" style={{ color: CHROME.inkMuted }}>
            These {proof.proof.length} hashes prove the event was in that block without
            revealing any other event in it &mdash; which is what makes the proof shareable
            when the rest of the log is not.
          </div>
        </div>
      )}
    </div>
  )
}
