import { useEffect, useRef, useState } from 'react'
import { CHROME } from '../analytics/theme.js'

/* PromptInputBox - the chat composer, in the shape people already know.
 *
 * Modelled on the ChatGPT composer because that is now the idiom for this kind
 * of input: a single rounded field, a `+` menu on the left holding the things
 * you do occasionally, and one round send button on the right. The previous
 * version laid every control out as a row of rectangular buttons under the
 * textarea, which meant the mode selector, attach, dictate and send all
 * competed for attention with the thing people actually came to do, which is
 * type a question.
 *
 * A deliberate departure from this project's own house style: everything else
 * here uses square corners. An input people recognise is worth more than
 * consistency with the rest of the chrome, and the composer is the one
 * component where familiarity beats house style.
 *
 * Built rather than installed - the reference component pulls framer-motion,
 * lucide-react and two Radix packages, and this ships as a Docker image to an
 * airgapped VM. Four dependencies for an input box is weight carried forever.
 * The behaviours that matter are reproduced: Enter sends, Shift+Enter breaks
 * the line, the field grows to a cap, focus is visible, send is disabled with
 * a reason, and Escape closes the menu.
 */

const RADIUS = '26px'

function PlusIcon() {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
    </svg>
  )
}

function MicIcon() {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="9" y="2.5" width="6" height="11" rx="3" fill="currentColor" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0" stroke="currentColor" strokeWidth="2"
        strokeLinecap="round" />
      <path d="M12 17.5V21" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  )
}

function SpinnerIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true"
      style={{ animation: 'ulpf-spin 0.9s linear infinite' }}>
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2.5" opacity="0.25" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="2.5"
        strokeLinecap="round" />
    </svg>
  )
}

function SendIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M12 19V5M5 12l7-7 7 7" stroke="currentColor" strokeWidth="2.4"
        strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function StopIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true">
      <rect x="6" y="6" width="12" height="12" rx="1.5" fill="currentColor" />
    </svg>
  )
}

export default function PromptInputBox({
  // Text pushed in from outside - dictation fills the composer rather than
  // sending straight away, so a mis-heard word can be corrected first.
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
  // Entries for the `+` menu: { id, label, hint, onSelect }.
  menuActions = [],
  /* Dictation gets its own button rather than a menu entry. It is the one
   * media action people reach for mid-sentence, and burying a
   * frequently-used control two clicks deep to keep the bar tidy is the
   * wrong trade. Shape: { active, disabled, hint, onSelect }. */
  micAction = null,
}) {
  const [value, setValue] = useState('')
  const [files, setFiles] = useState([])
  const [focused, setFocused] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const textareaRef = useRef(null)
  const fileRef = useRef(null)
  const menuRef = useRef(null)

  useEffect(() => {
    if (prefill) setValue((v) => (v ? `${v} ${prefill}` : prefill))
  }, [prefill])

  // Grow with the content, but stop at ~8 lines so a pasted stack trace cannot
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

  // A menu that only closes on its own button is a menu people get stuck in.
  useEffect(() => {
    if (!menuOpen) return
    function onDown(e) {
      if (menuRef.current && !menuRef.current.contains(e.target)) setMenuOpen(false)
    }
    function onKey(e) { if (e.key === 'Escape') setMenuOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [menuOpen])

  const canSend = value.trim().length > 0 && !busy

  function submit() {
    if (!canSend) return
    onSend?.(value.trim(), files)
    setValue('')
    setFiles([])
    if (fileRef.current) fileRef.current.value = ''
  }

  const items = [
    ...(allowAttachments ? [{
      id: 'attach', label: 'Add photos & files',
      hint: 'Upload from computer',
      onSelect: () => fileRef.current?.click(),
    }] : []),
    ...menuActions,
  ]

  return (
    <div className="w-full">
      <style>{`
        @keyframes ulpf-pulse {
          0%   { transform: scale(1);    opacity: 0.9; }
          100% { transform: scale(1.75); opacity: 0;   }
        }
        @keyframes ulpf-spin { to { transform: rotate(360deg); } }
        @media (prefers-reduced-motion: reduce) {
          @keyframes ulpf-pulse { 0%, 100% { transform: none; opacity: 0.9; } }
          @keyframes ulpf-spin  { 0%, 100% { transform: none; } }
        }
      `}</style>
      {/* Attachment chips sit above the field, as they do in the reference -
          inside it they push the caret around while you are typing. */}
      {files.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-2">
          {files.map((f, i) => (
            <span key={`${f.name}-${i}`}
              className="flex items-center gap-1.5 text-[11px] px-2.5 py-1"
              style={{ background: CHROME.surfaceRaised, color: CHROME.inkSecondary,
                       borderRadius: '14px' }}>
              <span className="font-mono truncate max-w-[180px]">{f.name}</span>
              <button onClick={() => setFiles((p) => p.filter((_, j) => j !== i))}
                aria-label={`Remove ${f.name}`} style={{ color: CHROME.inkMuted }}>×</button>
            </span>
          ))}
        </div>
      )}

      <div className="relative" ref={menuRef}>
        {menuOpen && items.length > 0 && (
          <div
            role="menu"
            className="absolute bottom-full left-0 mb-2 py-1.5 z-20 min-w-[280px]"
            style={{
              background: CHROME.surface,
              border: `1px solid ${CHROME.border}`,
              borderRadius: '16px',
              boxShadow: '0 8px 30px rgba(43, 33, 24, 0.14)',
            }}
          >
            {items.map((item) => (
              <button
                key={item.id}
                role="menuitem"
                disabled={item.disabled}
                onClick={() => { setMenuOpen(false); item.onSelect?.() }}
                className="w-full flex items-baseline gap-2.5 px-4 py-2 text-left transition-colors disabled:opacity-40"
                style={{ color: CHROME.ink }}
                onMouseEnter={(e) => { e.currentTarget.style.background = CHROME.surfaceRaised }}
                onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
              >
                <span className="text-[13px] font-medium whitespace-nowrap">{item.label}</span>
                {item.hint && (
                  <span className="text-[11px]" style={{ color: CHROME.inkDim }}>{item.hint}</span>
                )}
              </button>
            ))}
          </div>
        )}

        <div
          className="flex items-end gap-2 pl-2 pr-2 py-2 transition-colors"
          style={{
            background: CHROME.surface,
            border: `1px solid ${focused ? CHROME.borderStrong : CHROME.border}`,
            borderRadius: RADIUS,
            boxShadow: focused ? '0 2px 14px rgba(43, 33, 24, 0.08)' : 'none',
          }}
        >
          {items.length > 0 && (
            <button
              onClick={() => setMenuOpen((o) => !o)}
              aria-label="More actions"
              aria-expanded={menuOpen}
              className="shrink-0 flex items-center justify-center transition-colors"
              style={{
                width: 34, height: 34, borderRadius: '50%',
                color: CHROME.inkSecondary,
                background: menuOpen ? CHROME.surfaceActive : 'transparent',
              }}
            >
              <PlusIcon />
            </button>
          )}

          <input ref={fileRef} type="file" multiple className="hidden"
            onChange={(e) => setFiles((p) => [...p, ...Array.from(e.target.files || [])])} />

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
            className="flex-1 min-w-0 resize-none bg-transparent py-1.5 text-[14.5px] focus:outline-none"
            style={{ color: CHROME.ink, lineHeight: 1.5 }}
          />

          {extraControls}

          {micAction && (
            <button
              onClick={micAction.onSelect}
              disabled={micAction.disabled}
              aria-label={micAction.active ? 'Stop recording' : 'Dictate a question'}
              aria-pressed={micAction.active}
              title={micAction.hint}
              className="shrink-0 flex items-center justify-center transition-colors"
              style={{
                // relative, or the pulse ring below anchors to the composer
                // wrapper instead of to this button.
                position: 'relative',
                width: 34, height: 34, borderRadius: '50%',
                // Recording is a state people must not lose track of - it is
                // holding the microphone open - so it is filled, not outlined.
                background: micAction.active ? '#E03131' : 'transparent',
                color: micAction.active ? '#FFFFFF' : CHROME.inkSecondary,
                opacity: micAction.disabled && !micAction.transcribing ? 0.3 : 1,
                cursor: micAction.disabled ? 'default' : 'pointer',
              }}
            >
              {micAction.transcribing ? <SpinnerIcon /> : <MicIcon />}
              {micAction.active && (
                <span
                  aria-hidden="true"
                  style={{
                    position: 'absolute', width: 34, height: 34, borderRadius: '50%',
                    border: '2px solid #E03131', animation: 'ulpf-pulse 1.4s ease-out infinite',
                    pointerEvents: 'none',
                  }}
                />
              )}
            </button>
          )}

          {micAction?.transcribing && (
            <span className="shrink-0 text-[11px] whitespace-nowrap self-center"
              style={{ color: CHROME.inkMuted }} role="status" aria-live="polite">
              Transcribing…
            </span>
          )}

          {busy && onStop ? (
            <button
              onClick={onStop}
              aria-label="Stop"
              title="Stop waiting for this answer. The model keeps generating, so it may still appear later."
              className="shrink-0 flex items-center justify-center"
              style={{ width: 34, height: 34, borderRadius: '50%',
                       background: CHROME.ink, color: CHROME.surface }}
            >
              <StopIcon />
            </button>
          ) : (
            <button
              onClick={submit}
              disabled={!canSend}
              aria-label="Send"
              className="shrink-0 flex items-center justify-center transition-opacity disabled:opacity-25"
              style={{ width: 34, height: 34, borderRadius: '50%',
                       background: CHROME.primary, color: CHROME.primaryInk }}
            >
              <SendIcon />
            </button>
          )}
        </div>
      </div>

      {/* Modes stay below and small. They change how the question is answered,
          which is a setting rather than an action, and putting them in the
          field would crowd the one thing people came here to do. */}
      {(modes.length > 0) && (
        <div className="flex items-center gap-1.5 mt-2 px-1 flex-wrap">
          {modes.map((m) => {
            const active = m.value === activeMode
            return (
              <button
                key={m.value}
                onClick={() => onModeChange?.(m.value)}
                title={m.hint}
                className="px-2.5 py-1 text-[11px] transition-colors"
                style={{
                  borderRadius: '13px',
                  background: active ? CHROME.surfaceActive : 'transparent',
                  color: active ? CHROME.primaryDeep : CHROME.inkMuted,
                  border: `1px solid ${active ? CHROME.borderStrong : 'transparent'}`,
                  fontWeight: active ? 600 : 400,
                }}
              >
                {m.label}
              </button>
            )
          })}
          <span className="ml-auto text-[10px] hidden sm:inline" style={{ color: CHROME.inkDim }}>
            Enter to send · Shift+Enter for a new line
          </span>
        </div>
      )}
    </div>
  )
}
