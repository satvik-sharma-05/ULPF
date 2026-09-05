import { useCallback, useEffect, useRef, useState } from 'react'
import LlmToggle from '../components/LlmToggle.jsx'
import SessionList from '../components/chat/SessionList.jsx'
import PromptInputBox from '../components/ui/PromptInputBox.jsx'
import MediaControls from '../components/chat/MediaControls.jsx'

/* Mirrors documents.looks_like_document_request on the backend. Kept
 * deliberately broad: a false positive costs one wasted call, a false
 * negative sends "make me a deck" to the Cypher generator, which answers
 * something unrelated and confusing. */
const DOCUMENT_REQUEST =
  /\b(create|make|generate|build|draft|prepare|give me|write)\b[^.?!]{0,60}?\b(pptx?|powerpoint|deck|slides?|presentation|pdf|report|document)\b/i
import { UserMessage, BotMessage } from '../components/ChatMessage.jsx'
import { CHROME } from '../components/analytics/theme.js'
import {
  askInSession, createSession, deleteSession, getSession, listSessions, renameSession,
} from '../api.js'

const CHAT_MODES = [
  { value: 'text2cypher', label: 'Cypher', hint: 'Counts, filters, rankings — generates a query' },
  { value: 'graphrag', label: 'GraphRAG', hint: 'Why / explain — semantic retrieval over log text' },
  { value: 'auto', label: 'Auto', hint: 'Planner picks and sequences the tools itself' },
]

const EXAMPLES = {
  text2cypher: [
    'Which 10 hosts produced the most errors?',
    'How many logs are there per severity?',
    'Which users appear on more than one host?',
  ],
  graphrag: [
    'Why did storage look unhealthy?',
    'What happened around the NSX failures?',
    'Explain the repeated authentication failures',
  ],
  auto: [
    'What is connected to this datastore, and were any of them affected recently?',
    'Why is event-broker throwing errors, and how many times did it happen?',
    'What happened between 10am and 12pm on the host with the most errors?',
  ],
}

export default function ChatPage({ pendingSessionId, pendingQuestion, onConsumePendingSession, allowedProviders }) {
  const [sessions, setSessions] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [session, setSession] = useState(null)
  const [mode, setMode] = useState('text2cypher')
  const [pending, setPending] = useState(false)
  // Held so the Stop button can abandon the wait. Not in state: changing it
  // must not re-render, and the handler needs whatever the CURRENT request
  // is, not whatever it was when the handler was created.
  const abortRef = useRef(null)
  const [loadingList, setLoadingList] = useState(true)
  const bottomRef = useRef(null)

  // What this build can actually do. Probed once: the answer depends on which
  // models were baked into the image, which cannot change while it is running.
  const [mediaCaps, setMediaCaps] = useState(null)
  // Dictated text waiting to be dropped into the composer. An object rather
  // than a bare string so dictating the same words twice still fires the
  // effect that fills the box.
  const [dictated, setDictated] = useState(null)

  useEffect(() => {
    fetch('/api/media/capabilities')
      .then((r) => (r.ok ? r.json() : null))
      .then(setMediaCaps)
      .catch(() => setMediaCaps(null))
  }, [])

  /* A vision answer joins the transcript as an ordinary exchange, so it is
   * exportable, speakable and scrollable like every other message. Messages
   * live on `session`, not in their own state - appending here has to go
   * through setSession the same way send() does. */
  const addImageAnswer = useCallback(({ answer, model, filename }) => {
    const now = Date.now()
    setSession((prev) => ({
      ...(prev || { messages: [] }),
      messages: [
        ...((prev && prev.messages) || []),
        { id: `img-q-${now}`, role: 'user', content: `[image] ${filename}` },
        { id: `img-a-${now}`, role: 'assistant', content: answer,
          meta: { route: { strategy: 'vision', confidence: 1, reason: model } } },
      ],
    }))
  }, [])

  const refreshList = useCallback(async () => {
    try {
      setSessions(await listSessions())
    } catch {
      setSessions([])
    } finally {
      setLoadingList(false)
    }
  }, [])

  useEffect(() => { refreshList() }, [refreshList])

  // A session created elsewhere (the "Ask AI" button on a log) is handed over
  // here, opened, and then cleared so returning to this tab later doesn't keep
  // yanking the user back to that same conversation.
  useEffect(() => {
    if (!pendingSessionId) return
    setActiveId(pendingSessionId)
    setSession({ id: pendingSessionId, messages: [] })
    refreshList()
    const q = pendingQuestion
    onConsumePendingSession?.()
    // A question typed on the home page is sent as this session's first turn,
    // so landing here already shows the answer being produced rather than an
    // empty box the user has to retype into.
    if (q) sendInSession(pendingSessionId, q)
     
  }, [pendingSessionId])

  useEffect(() => {
    if (!activeId) { setSession(null); return }
    let cancelled = false
    getSession(activeId)
      .then((s) => {
        if (cancelled) return
        setSession(s)
        if (s.mode && s.mode !== 'auto') setMode(s.mode)
      })
      .catch(() => { if (!cancelled) setSession(null) })
    return () => { cancelled = true }
  }, [activeId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [session?.messages?.length, pending])

  async function ensureSession() {
    if (activeId) return activeId
    const created = await createSession({ mode })
    setActiveId(created.id)
    setSession({ ...created, messages: [] })
    refreshList()
    return created.id
  }

  // Extracted so the pending-question effect can send into a session id it
  // already has, without racing the `activeId` state update.
  function stop() {
    if (abortRef.current) {
      abortRef.current.abort()
      abortRef.current = null
    }
  }

  async function sendInSession(sid, q) {
    setPending(true)
    const controller = new AbortController()
    abortRef.current = controller
    setSession((prev) => (prev && prev.id === sid ? {
      ...prev,
      messages: [...(prev.messages || []), { id: `tmp-${Date.now()}`, role: 'user', content: q }],
    } : prev))
    try {
      await askInSession(sid, q, mode, controller.signal)
      setSession(await getSession(sid))
      refreshList()
    } catch (err) {
      // An abort is a deliberate act, not a failure, and the answer is still
      // being written server-side - so say what actually happened rather than
      // reporting an error the user caused on purpose.
      const stopped = err.name === 'AbortError'
      setSession((prev) => prev && {
        ...prev,
        messages: [...(prev.messages || []), {
          id: `err-${Date.now()}`, role: 'assistant',
          content: stopped
            ? 'Stopped waiting. The model was still generating, so this answer '
              + 'may still appear when you reopen this chat.'
            : `Request failed: ${err.message}`,
          meta: { isError: !stopped },
        }],
      })
    } finally {
      abortRef.current = null
      setPending(false)
    }
  }

  async function send(text) {
    const q = (text || '').trim()
    if (!q || pending) return
    setPending(true)
    // The input box comes through HERE, not sendInSession - wiring the
    // controller only into that one left Stop doing nothing for every
    // question a person actually types.
    const controller = new AbortController()
    abortRef.current = controller

    // Optimistic echo so the question appears instantly rather than after a
    // round trip that can take tens of seconds on a local LLM.
    setSession((prev) => prev && {
      ...prev,
      messages: [...(prev.messages || []), { id: `tmp-${Date.now()}`, role: 'user', content: q }],
    })

    try {
      // A document request goes to the generator, not to Cypher. Handled here
      // rather than server-side inside /api/ask so the transcript is not
      // rewritten by a route that returns a file instead of an answer - the
      // document lives in local state as one assistant message.
      if (DOCUMENT_REQUEST.test(q)) {
        const res = await fetch('/api/documents/generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: q }),
          // Document generation is the slowest call in the app - minutes on a
          // local model - so it needs Stop more than anything else does.
          signal: controller.signal,
        })
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText)
        const doc = await res.json()
        setSession((prev) => prev && {
          ...prev,
          messages: [...(prev.messages || []), {
            id: `doc-${Date.now()}`,
            role: 'assistant',
            content: `Here is your ${doc.sections}-${doc.kind === 'pptx' ? 'slide deck' : 'page report'}`
              + ` on **${doc.title}**.`
              + (doc.grounded ? ' The figures come from the live graph.' : ''),
            meta: { document: doc, route: { strategy: 'document', confidence: 1, reason: doc.kind } },
          }],
        })
        return
      }

      const sid = await ensureSession()
      await askInSession(sid, q, mode, controller.signal)
      // Re-read from the server rather than appending locally: the stored
      // transcript is the source of truth, and it also carries the assistant
      // message's meta (cypher, rows, plan) that the bubble renders.
      setSession(await getSession(sid))
      refreshList()
    } catch (err) {
      const stopped = err.name === 'AbortError'
      setSession((prev) => prev && {
        ...prev,
        messages: [...(prev.messages || []), {
          id: `err-${Date.now()}`, role: 'assistant',
          content: stopped
            ? 'Stopped waiting. The model was still generating, so this answer '
              + 'may still appear when you reopen this chat.'
            : `Request failed: ${err.message}`,
          meta: { isError: !stopped },
        }],
      })
    } finally {
      abortRef.current = null
      setPending(false)
    }
  }

  async function handleNew() {
    const created = await createSession({ mode })
    setActiveId(created.id)
    setSession({ ...created, messages: [] })
    refreshList()
  }

  async function handleDelete(id) {
    await deleteSession(id).catch(() => {})
    if (id === activeId) { setActiveId(null); setSession(null) }
    refreshList()
  }

  async function handleRename(id, title) {
    await renameSession(id, title).catch(() => {})
    refreshList()
    if (id === activeId) setSession((prev) => prev && { ...prev, title })
  }

  const messages = session?.messages || []
  const examples = EXAMPLES[mode] || EXAMPLES.auto

  return (
    <div className="h-full flex" style={{ background: CHROME.pagePlane }}>
      <SessionList
        sessions={sessions}
        activeId={activeId}
        onSelect={setActiveId}
        onNew={handleNew}
        onRename={handleRename}
        onDelete={handleDelete}
        loading={loadingList}
      />

      <div className="flex-1 min-w-0 flex flex-col">
        <header className="px-6 py-3.5 shrink-0" style={{ borderBottom: `1px solid ${CHROME.border}` }}>
          <h1 className="text-base font-semibold tracking-tight" style={{ color: CHROME.ink }}>
            {session?.title || 'Chat'}
          </h1>
          <p className="text-xs mt-0.5" style={{ color: CHROME.inkMuted }}>
            {session?.log_id ? (
              <>Scoped to log <span className="font-mono">{session.log_id}</span> — it stays in context for every question</>
            ) : (
              'Ask questions about the log graph — follow-ups keep their context'
            )}
          </p>
        </header>

        <div
          className="px-6 py-3 flex items-end justify-between gap-4 flex-wrap shrink-0"
          style={{ borderBottom: `1px solid ${CHROME.border}` }}
        >
          <span className="text-[11px]" style={{ color: CHROME.inkMuted }}>
            Strategy and model apply to the next question you send.
          </span>
          <LlmToggle allowedProviders={allowedProviders} />
        </div>

        <main className="flex-1 overflow-y-auto px-6 py-4">
          <div className="max-w-3xl mx-auto flex flex-col gap-3">
            {messages.length === 0 && !pending && (
              <div className="text-center mt-12">
                <p className="mb-3 text-xs" style={{ color: CHROME.inkMuted }}>
                  {session?.log_id
                    ? 'Ask anything about this log — why it happened, what else it touched, what to check next.'
                    : 'Ask a question about the log graph.'}
                </p>
                {!session?.log_id && (
                  <div className="flex flex-wrap justify-center gap-2">
                    {examples.map((ex) => (
                      <button
                        key={ex}
                        onClick={() => send(ex)}
                        className="px-2.5 py-1.5 text-[11px] transition-colors"
                        style={{ border: `1px solid ${CHROME.border}`, color: CHROME.inkSecondary }}
                      >
                        {ex}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}

            {messages.map((m, i) =>
              m.role === 'user' ? (
                <UserMessage key={m.id} text={m.content} />
              ) : (
                <BotMessage
                  key={m.id}
                  // The question this answered, for the exported document -
                  // a PDF of an answer with no question on it is not a report.
                  question={[...messages.slice(0, i)].reverse()
                    .find((p) => p.role === 'user')?.content}
                  mediaCaps={mediaCaps}
                  response={{
                    answer: m.content,
                    isError: m.meta?.isError,
                    ...(m.meta || {}),
                  }}
                />
              ),
            )}
            {pending && <BotMessage pending />}
            <div ref={bottomRef} />
          </div>
        </main>

        <footer className="px-6 py-4 shrink-0" style={{ borderTop: `1px solid ${CHROME.border}` }}>
          <div className="max-w-3xl mx-auto">
            <MediaControls
              capabilities={mediaCaps}
              disabled={pending}
              onTranscript={setDictated}
              onImageAnswer={addImageAnswer}
            />
            <PromptInputBox
              prefill={dictated}
              onSend={(text) => send(text)}
              busy={pending}
              onStop={stop}
              allowAttachments={false}
              modes={CHAT_MODES}
              activeMode={mode}
              onModeChange={setMode}
              placeholder={session?.log_id ? 'Ask about this log…' : 'Ask about the log graph…'}
            />
          </div>
        </footer>
      </div>
    </div>
  )
}
