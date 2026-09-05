import { useState } from 'react'
import { CHROME } from '../analytics/theme.js'

function relativeTime(epochSeconds) {
  if (!epochSeconds) return ''
  const diff = Date.now() / 1000 - epochSeconds
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

// The session rail, in the shape people already know from ChatGPT/Claude:
// newest first, titled from the first question, inline rename and delete.
export default function SessionList({ sessions, activeId, onSelect, onNew, onRename, onDelete, loading }) {
  const [editingId, setEditingId] = useState(null)
  const [draft, setDraft] = useState('')

  function startRename(session, e) {
    e.stopPropagation()
    setEditingId(session.id)
    setDraft(session.title)
  }

  function commitRename(e) {
    e?.preventDefault()
    if (draft.trim()) onRename(editingId, draft.trim())
    setEditingId(null)
  }

  return (
    <aside
      className="w-60 shrink-0 flex flex-col"
      style={{ background: CHROME.surface, borderRight: `1px solid ${CHROME.border}` }}
    >
      <div className="p-3" style={{ borderBottom: `1px solid ${CHROME.border}` }}>
        <button
          onClick={onNew}
          className="w-full px-3 py-2 text-xs font-medium"
          style={{ background: CHROME.primary, color: CHROME.primaryInk }}
        >
          + New chat
        </button>
      </div>

      <div className="flex-1 overflow-y-auto py-1">
        {loading && sessions.length === 0 ? (
          <div className="px-3 py-6 text-center text-[11px]" style={{ color: CHROME.inkMuted }}>Loading…</div>
        ) : sessions.length === 0 ? (
          <div className="px-3 py-6 text-center text-[11px] leading-relaxed" style={{ color: CHROME.inkMuted }}>
            No chats yet.<br />Start one here, or click “Ask AI” on any log.
          </div>
        ) : (
          sessions.map((s) => {
            const active = s.id === activeId
            return (
              <div
                key={s.id}
                onClick={() => onSelect(s.id)}
                className="group px-3 py-2 cursor-pointer transition-colors"
                style={{
                  background: active ? CHROME.surfaceActive : 'transparent',
                  boxShadow: active ? `inset 2px 0 0 ${CHROME.primary}` : 'none',
                }}
                onMouseEnter={(e) => { if (!active) e.currentTarget.style.background = CHROME.surfaceRaised }}
                onMouseLeave={(e) => { if (!active) e.currentTarget.style.background = 'transparent' }}
              >
                {editingId === s.id ? (
                  <form onSubmit={commitRename}>
                    <input
                      autoFocus
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      onBlur={commitRename}
                      onKeyDown={(e) => {
                        if (e.key === 'Escape') { e.preventDefault(); setEditingId(null) }
                      }}
                      onClick={(e) => e.stopPropagation()}
                      className="w-full text-[11px] px-1.5 py-1"
                      style={{ background: CHROME.pagePlane, border: `1px solid ${CHROME.primary}`, color: CHROME.ink }}
                    />
                  </form>
                ) : (
                  <>
                    <div className="flex items-start gap-1.5">
                      <span
                        className="text-[11px] leading-snug flex-1 line-clamp-2"
                        style={{ color: active ? CHROME.ink : CHROME.inkSecondary }}
                        title={`${s.title}
(double-click to rename)`}
                        onDoubleClick={(e) => startRename(s, e)}
                      >
                        {s.title}
                      </span>
                      <span className="flex gap-1 shrink-0 transition-opacity opacity-60 group-hover:opacity-100">
                        <button
                          onClick={(e) => startRename(s, e)}
                          title="Rename this chat"
                          className="text-[11px] px-1 leading-none"
                          style={{ color: CHROME.inkSecondary }}
                        >
                          ✎
                        </button>
                        <button
                          onClick={(e) => { e.stopPropagation(); onDelete(s.id) }}
                          title="Delete this chat"
                          className="text-[11px] px-1 leading-none"
                          style={{ color: CHROME.inkSecondary }}
                        >
                          ✕
                        </button>
                      </span>
                    </div>
                    <div className="flex items-center gap-2 mt-0.5 text-[9px]" style={{ color: CHROME.inkMuted }}>
                      {/* A pinned log is the single most useful thing to see in
                          this list - it says what the chat is *about*. */}
                      {s.log_id && (
                        <span
                          className="px-1 font-mono"
                          style={{ background: CHROME.surfaceRaised, color: CHROME.primary }}
                          title={s.log_id}
                        >
                          log
                        </span>
                      )}
                      <span>{s.message_count || 0} msg</span>
                      <span className="ml-auto">{relativeTime(s.updated_at)}</span>
                    </div>
                  </>
                )}
              </div>
            )
          })
        )}
      </div>
    </aside>
  )
}
