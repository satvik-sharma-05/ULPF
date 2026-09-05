import { useEffect, useRef, useState } from 'react'
import { CHROME } from '../analytics/theme.js'

/* PromptInputBox - the AI chat input from the 21st.dev spec (auto-growing
 * textarea, attachment chips, a toolbar of modes, a send button that arms
 * only when there is something to send).
 *
 * Built here rather than installed: that component pulls framer-motion,
 * lucide-react and two Radix packages, and this app ships as a Docker image
 * to an airgapped VM - four dependencies for an input box is weight we'd
 * carry forever. The behaviours that make it good (Enter to send / Shift+Enter
 * for newline, height that tracks content up to a cap, visible focus, a
 * disabled send with a reason) are all reproduced.
 */
export default function PromptInputBox({
  // Text pushed in from outside - dictation fills the composer rather
  // than sending straight away, so a mis-heard word can be corrected
  // before it becomes a question.
  prefill,
  extraControls,
  onSend,
  placeholder = 'Ask anything about your logs…',
  busy = false,
  onStop,
  autoFocus = false,
  modes = [],
  activeMode,
  onModeChange,
  allowAttachments = true,
}) {
  const [value, setValue] = useState('')

  useEffect(() => {
    if (prefill) setValue((v) => (v ? `${v} ${prefill}` : prefill))
  }, [prefill])
  const [files, setFiles] = useState([])
  const [focused, setFocused] = useState(false)
  const textareaRef = useRef(null)
  const fileRef = useRef(null)

  // Grow with the content, but stop at ~8 lines so a pasted stack trace can't
  // push the send button off screen.
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`
  }, [value])

  useEffect(() => {
    if (autoFocus) textareaRef.current?.focus()
  }, [autoFocus])

  const canSend = value.trim().length > 0 && !busy

  function submit() {
    if (!canSend) return
    onSend?.(value.trim(), files)
    setValue('')
    setFiles([])
    if (fileRef.current) fileRef.current.value = ''
  }

  return (
    <div
      className="transition-colors"
      style={{
        background: CHROME.surface,
        border: `1px solid ${focused ? CHROME.primary : CHROME.border}`,
      }}
    >
      {files.length > 0 && (
        <div className="flex flex-wrap gap-1.5 px-3 pt-3">
          {files.map((f, i) => (
            <span
              key={`${f.name}-${i}`}
              className="flex items-center gap-1.5 text-[11px] px-2 py-1"
              style={{ background: CHROME.surfaceRaised, color: CHROME.inkSecondary }}
            >
              <span className="font-mono truncate max-w-[180px]">{f.name}</span>
              <button
                onClick={() => setFiles((prev) => prev.filter((_, j) => j !== i))}
                aria-label={`Remove ${f.name}`}
                style={{ color: CHROME.inkMuted }}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      <textarea
        ref={textareaRef}
        value={value}
        rows={1}
        placeholder={placeholder}
        onChange={(e) => setValue(e.target.value)}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() }
        }}
        className="w-full resize-none bg-transparent px-3.5 py-3 text-sm focus:outline-none"
        style={{ color: CHROME.ink }}
      />

      <div className="flex items-center gap-2 px-3 pb-2.5 flex-wrap">
        {allowAttachments && (
          <>
            <input
              ref={fileRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => setFiles((prev) => [...prev, ...Array.from(e.target.files || [])])}
            />
            <button
              onClick={() => fileRef.current?.click()}
              title="Attach a file"
              className="px-2 py-1 text-[11px]"
              style={{ border: `1px solid ${CHROME.border}`, color: CHROME.inkMuted }}
            >
              Attach
            </button>
          </>
        )}

        {modes.map((m) => {
          const active = m.value === activeMode
          return (
            <button
              key={m.value}
              onClick={() => onModeChange?.(m.value)}
              title={m.hint}
              className="px-2 py-1 text-[11px] transition-colors"
              style={{
                background: active ? CHROME.primary : 'transparent',
                color: active ? CHROME.primaryInk : CHROME.inkMuted,
                border: `1px solid ${active ? CHROME.primary : CHROME.border}`,
              }}
            >
              {m.label}
            </button>
          )
        })}

        <span className="ml-auto flex items-center gap-2">
          <span className="text-[10px] hidden sm:inline" style={{ color: CHROME.inkDim }}>
            Enter to send · Shift+Enter for a new line
          </span>
          {/* While a question is in flight the primary action is to stop
              waiting for it, not to send another - so the button becomes Stop
              rather than sitting there disabled saying "Thinking…". On a local
              model an answer can take tens of seconds, which is a long time to
              offer someone no way out. */}
          {busy && onStop ? (
            <button
              onClick={onStop}
              className="px-3.5 py-1.5 text-xs font-medium"
              style={{ background: CHROME.surfaceActive, color: CHROME.inkSecondary,
                       border: `1px solid ${CHROME.borderStrong}` }}
              title="Stop waiting for this answer. The model keeps generating, so it may still appear later."
            >
              ■ Stop
            </button>
          ) : (
            <button
              onClick={submit}
              disabled={!canSend}
              className="px-3.5 py-1.5 text-xs font-medium disabled:opacity-40"
              style={{ background: CHROME.primary, color: CHROME.primaryInk }}
            >
              {busy ? 'Thinking…' : 'Send'}
            </button>
          )}
        </span>
      </div>
    </div>
  )
}
