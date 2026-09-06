import { useCallback, useEffect, useRef, useState } from 'react'
import { CHROME, GLASS, STATUS } from '../analytics/theme.js'

/* Microphone and image input for the chat composer.
 *
 * Both controls are hidden unless the backend says the model is actually
 * present - `/api/media/capabilities` reports per-feature availability, and on
 * an air-gapped VM a missing model cannot be fetched to recover. Offering a
 * microphone that always returns 503 is worse than not offering one.
 *
 * Dictation records with MediaRecorder and posts the blob to faster-whisper on
 * the backend. The browser's own SpeechRecognition API would be less code and
 * is not usable here: every implementation of it streams audio to a vendor
 * cloud service, which an air-gapped deployment cannot reach and a security
 * product should not be doing with an operator's voice regardless.
 */

const MAX_SECONDS = 60
// Below this a recording is a click, not speech. Rejecting locally avoids a
// round trip to a CPU transcriber that will return an empty string anyway.
const MIN_BLOB_BYTES = 1200

/* Safari cannot record webm. Feature-detect rather than assuming, and keep the
 * chosen type - Whisper picks its demuxer from the FILENAME, so the extension
 * has to match the container or the audio is rejected as unreadable. */
const AUDIO_TYPES = [
  { mime: 'audio/webm', ext: 'webm' },
  { mime: 'audio/mp4', ext: 'm4a' },
  { mime: 'audio/ogg', ext: 'ogg' },
]

function pickAudioType() {
  if (typeof MediaRecorder === 'undefined') return null
  return AUDIO_TYPES.find((t) => MediaRecorder.isTypeSupported?.(t.mime)) || null
}

/* `onActions` hands the composer a description of what this component can
 * do - label, hint, handler, disabled - so the actions can appear as entries
 * in the `+` menu while every piece of logic (MediaRecorder lifecycle,
 * transcription, vision upload) stays here. Lifting that logic into the
 * composer would have duplicated the fiddliest code in the app. */
export default function MediaControls({ capabilities, onTranscript, onImageAnswer, disabled, onActions }) {
  const [recording, setRecording] = useState(false)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const recorderRef = useRef(null)
  // The MediaStream is held separately from the recorder. `MediaRecorder.stream`
  // is a read-only accessor, so assigning to it throws "Cannot set property
  // stream of #<MediaRecorder> which has only a getter" - which killed the
  // whole handler before recording ever started.
  const streamRef = useRef(null)
  // The container actually negotiated, so the upload filename can match it.
  const typeRef = useRef(null)
  const chunksRef = useRef([])
  const stopTimerRef = useRef(null)
  const fileRef = useRef(null)
  const actionsRef = useRef(null)

  const sttOn = capabilities?.stt?.available
  const visionOn = capabilities?.vision?.available

  const cleanup = useCallback(() => {
    if (stopTimerRef.current) { clearTimeout(stopTimerRef.current); stopTimerRef.current = null }
    // Stopping every track is what actually releases the microphone and turns
    // the browser's recording indicator off.
    streamRef.current?.getTracks().forEach((t) => t.stop())
    streamRef.current = null
    recorderRef.current = null
  }, [])

  // A recorder left running after the component goes away keeps the microphone
  // light on, which reads as the app listening when it is not.
  useEffect(() => cleanup, [cleanup])

  const startRecording = async () => {
    setError(null)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const chosen = pickAudioType()
      const rec = new MediaRecorder(stream, chosen ? { mimeType: chosen.mime } : undefined)
      typeRef.current = chosen
      streamRef.current = stream
      chunksRef.current = []
      rec.ondataavailable = (e) => { if (e.data.size) chunksRef.current.push(e.data) }
      rec.onstop = async () => {
        cleanup()
        setRecording(false)
        const chosen = typeRef.current
        const blob = new Blob(chunksRef.current, { type: chosen?.mime || 'audio/webm' })
        if (blob.size < MIN_BLOB_BYTES) {
          setError(blob.size === 0 ? 'Nothing was recorded.' : 'Too short - hold the button and speak.')
          return
        }
        setBusy('stt')
        try {
          const form = new FormData()
          form.append('file', blob, `dictation.${chosen?.ext || 'webm'}`)
          const res = await fetch('/api/media/stt', { method: 'POST', body: form })
          if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText)
          const data = await res.json()
          if (data.text) onTranscript?.(data.text)
          else setError('No speech detected.')
        } catch (e) {
          setError(e.message)
        } finally {
          setBusy(null)
        }
      }
      rec.start()
      recorderRef.current = rec
      setRecording(true)
      // Hard stop: a recorder left running by a forgotten click would post a
      // very large blob to a CPU-only transcriber.
      stopTimerRef.current = setTimeout(() => {
        if (recorderRef.current?.state === 'recording') recorderRef.current.stop()
      }, MAX_SECONDS * 1000)
    } catch (e) {
      setError(e.name === 'NotAllowedError' ? 'Microphone permission denied.' : e.message)
    }
  }

  const stopRecording = () => {
    if (recorderRef.current?.state === 'recording') recorderRef.current.stop()
    else { cleanup(); setRecording(false) }
  }

  const onPickImage = async (e) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    setError(null)
    setBusy('vision')
    try {
      const form = new FormData()
      form.append('file', file, file.name)
      const res = await fetch('/api/media/vision', { method: 'POST', body: form })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText)
      const data = await res.json()
      onImageAnswer?.({ answer: data.answer, model: data.model, filename: file.name })
    } catch (e2) {
      setError(e2.message)
    } finally {
      setBusy(null)
    }
  }

  // Publish the actions upward so the composer's + menu can render them,
  // then render nothing visible ourselves. The recording state still lives
  // here, which is why the hidden file input below is still mounted - the
  // menu entry only triggers a click on it.
  useEffect(() => {
    if (!onActions) return
    const actions = []
    if (sttOn) {
      actions.push({
        id: 'stt',
        active: recording,
        label: recording ? 'Stop recording' : 'Dictate a question',
        hint: recording ? `Stop and transcribe — auto-stops at ${MAX_SECONDS}s`
              : (busy === 'stt' ? 'Transcribing…' : 'Dictate a question'),
        disabled: disabled || busy === 'stt',
        onSelect: recording ? stopRecording : startRecording,
      })
    }
    if (visionOn) {
      actions.push({
        id: 'vision',
        label: 'Add a screenshot',
        hint: busy === 'vision' ? 'Reading…' : 'Ask about an image of a log',
        disabled: disabled || busy === 'vision',
        onSelect: () => fileRef.current?.click(),
      })
    }
    onActions(actions)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sttOn, visionOn, recording, busy, disabled])

  if (!sttOn && !visionOn) return null

  // When the composer is hosting the actions, this component contributes only
  // the hidden input and the error line - the buttons would duplicate the menu.
  if (onActions) {
    return (
      <>
        <input ref={fileRef} type="file" accept="image/*" onChange={onPickImage} className="hidden" />
        {(recording || error) && (
          <div className="text-[11px] mb-2 px-1" style={{ color: recording ? STATUS.danger : STATUS.danger }}>
            {recording ? '● Recording — press the microphone again to stop' : error}
          </div>
        )}
      </>
    )
  }


  const btn = {
    border: `1px solid ${GLASS.borderOuter}`,
    color: CHROME.inkSecondary,
    background: CHROME.surface,
  }

  return (
    <div className="flex items-center gap-1.5">
      {sttOn && (
        <button
          type="button"
          onClick={recording ? stopRecording : startRecording}
          disabled={disabled || busy === 'stt'}
          title={recording ? `Stop and transcribe (auto-stops at ${MAX_SECONDS}s)` : 'Dictate a question'}
          aria-label={recording ? 'Stop recording' : 'Start recording'}
          className="px-2 py-1 text-[11px] disabled:opacity-50"
          style={recording
            ? { ...btn, background: STATUS.danger, color: '#FFFFFF', borderColor: STATUS.danger }
            : btn}
        >
          {busy === 'stt' ? 'Transcribing…' : recording ? '■ Stop' : '🎤 Speak'}
        </button>
      )}

      {visionOn && (
        <>
          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            disabled={disabled || busy === 'vision'}
            title="Attach a screenshot and ask about it"
            className="px-2 py-1 text-[11px] disabled:opacity-50"
            style={btn}
          >
            {busy === 'vision' ? 'Reading…' : '🖼 Image'}
          </button>
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            onChange={onPickImage}
            className="hidden"
          />
        </>
      )}

      {error && (
        <span className="text-[10px]" style={{ color: CHROME.inkMuted }}>{error}</span>
      )}
    </div>
  )
}
