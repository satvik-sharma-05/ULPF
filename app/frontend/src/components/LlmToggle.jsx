import { useEffect, useState } from 'react'
import { CHROME, STATUS } from './analytics/theme.js'
import { fetchLlm, setLlmProvider } from '../api.js'

// Ollama vs a cloud API, switchable at runtime. Ollama is the airgapped
// choice (a model on the VM itself); Groq/OpenRouter need outbound internet,
// which is exactly what an airgapped deployment does not have - so the
// network requirement is stated on the control rather than left implicit.
const ORDER = ['ollama', 'groq', 'openrouter']
const SHORT = { ollama: 'Ollama', groq: 'Groq', openrouter: 'OpenRouter' }

export default function LlmToggle({ allowedProviders }) {
  const [state, setState] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetchLlm().then(setState).catch(() => setState(null))
  }, [])

  async function pick(provider) {
    if (busy || provider === state?.active) return
    setBusy(true)
    setError(null)
    try {
      setState(await setLlmProvider(provider))
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (!state) return null
  // In an AIRGAPPED mode the cloud providers are unreachable as a matter of
  // network topology, so they are shown disabled with the reason rather than
  // hidden - hiding them would make the airgap look like a missing feature.
  const allowed = allowedProviders && allowedProviders.length ? new Set(allowedProviders) : null
  const available = ORDER.filter((p) => state.providers?.[p])

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-[10px] uppercase tracking-wide" style={{ color: CHROME.inkMuted }}>LLM</span>
      <div className="flex items-center">
        {available.map((p) => {
          const info = state.providers[p]
          const active = state.active === p
          const blocked = allowed ? !allowed.has(p) : false
          return (
            <button
              key={p}
              onClick={() => pick(p)}
              disabled={busy || blocked}
              title={
                `${info.label} — ${info.model}` +
                (info.configured ? '' : ' (no API key set)') +
                (blocked
                  ? ' — unavailable in this mode: no outbound internet'
                  : info.needs_network ? ' — needs internet access' : ' — works airgapped')
              }
              className="px-2.5 py-1 text-[10px] font-medium transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
              style={{
                background: active ? CHROME.primary : 'transparent',
                color: active ? CHROME.primaryInk : CHROME.inkSecondary,
                border: `1px solid ${active ? CHROME.primary : CHROME.border}`,
                marginLeft: -1,
              }}
            >
              {SHORT[p]}
              {/* An unconfigured provider is marked with a glyph, not a hue. */}
              {!info.configured && <span className="ml-1" aria-hidden="true">·</span>}
            </button>
          )
        })}
      </div>

      <span className="flex items-center gap-1.5 text-[10px] font-mono" style={{ color: CHROME.inkMuted }}>
        <span
          className="inline-block w-2 h-2"
          style={{
            background: state.reachable ? STATUS.good : STATUS.danger,
          }}
          aria-hidden="true"
        />
        {state.model}
      </span>

      {state.configured === false && (
        <span className="text-[10px]" style={{ color: CHROME.inkSecondary }}>no API key set</span>
      )}
      {error && <span className="text-[10px]" style={{ color: CHROME.ink }}>{error}</span>}
    </div>
  )
}
