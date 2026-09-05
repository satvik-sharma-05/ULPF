import { useEffect, useRef, useState } from 'react'
import { CHROME, GLASS } from './analytics/theme.js'

/* Reads one answer aloud, via Piper on the backend.
 *
 * Not the browser's speechSynthesis API: its voices come from the host OS, so
 * a minimal Linux VM typically has none at all and the call succeeds silently
 * while producing no sound. Piper ships inside the image, which means the
 * voice is the same on every deployment and is actually present.
 *
 * Hidden entirely when the voice is not in this build - see MediaControls for
 * the same reasoning.
 */
/* Markdown is written to be read, not heard. Without stripping it the voice
 * says "asterisk asterisk Total events asterisk asterisk", reads table pipes
 * aloud, and spells out code fences. */
function speakable(markdown) {
  return String(markdown || '')
    .replace(/```[\s\S]*?```/g, ' ')          // fenced blocks: never spoken
    .replace(/`([^`]*)`/g, '$1')               // inline code
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')  // links, keep the label
    .replace(/^\s{0,3}#{1,6}\s*/gm, '')        // headings
    .replace(/(\*\*|__|\*|_)/g, '')            // emphasis
    .replace(/^\s*[-*+]\s+/gm, '')             // bullets
    .replace(/^\s*>\s?/gm, '')                 // quotes
    .replace(/\|/g, ' ')                       // table pipes
    .replace(/\s{2,}/g, ' ')
    .trim()
}

export default function SpeakButton({ text, available }) {
  const [state, setState] = useState('idle')   // idle | loading | playing
  const [error, setError] = useState(null)
  const audioRef = useRef(null)
  const urlRef = useRef(null)

  // Stop the audio and release the blob if the message unmounts mid-playback -
  // otherwise a scrolled-away answer keeps talking.
  useEffect(() => () => {
    audioRef.current?.pause()
    if (urlRef.current) URL.revokeObjectURL(urlRef.current)
  }, [])

  if (!available || !text) return null

  const stop = () => {
    audioRef.current?.pause()
    audioRef.current = null
    setState('idle')
  }

  const speak = async () => {
    if (state === 'playing') { stop(); return }
    setError(null)
    setState('loading')
    try {
      const res = await fetch('/api/media/tts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: speakable(text) }),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText)
      const blob = await res.blob()
      if (urlRef.current) URL.revokeObjectURL(urlRef.current)
      urlRef.current = URL.createObjectURL(blob)
      const audio = new Audio(urlRef.current)
      audio.onended = () => setState('idle')
      audio.onerror = () => { setError('Playback failed.'); setState('idle') }
      audioRef.current = audio
      await audio.play()
      setState('playing')
    } catch (e) {
      setError(e.message)
      setState('idle')
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={speak}
        disabled={state === 'loading'}
        title={state === 'playing' ? 'Stop' : 'Read this answer aloud'}
        className="text-[10px] px-2 py-1 disabled:opacity-50"
        style={{ border: `1px solid ${GLASS.borderOuter}`, color: CHROME.inkSecondary }}
      >
        {state === 'loading' ? 'Synthesizing…' : state === 'playing' ? '■ Stop' : '🔊 Listen'}
      </button>
      {error && <span className="text-[10px]" style={{ color: CHROME.inkMuted }}>{error}</span>}
    </>
  )
}
