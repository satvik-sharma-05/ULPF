// api.js - thin fetch wrappers around the FastAPI backend.
//
// Calls are relative ('/api/...'), not absolute URLs: in dev, vite.config.js
// proxies /api to the backend so no CORS setup is needed; in a production
// build served by api.py's static-file mount, /api is already same-origin.

const BASE = '/api'

async function handle(res) {
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail || detail
    } catch {
      // response body wasn't JSON - keep the status text
    }
    throw new Error(detail)
  }
  return res.json()
}

export function askQuestion(question, mode, signal) {
  return fetch(`${BASE}/ask`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, mode }),
    signal,
  }).then(handle)
}

export function fetchHealth() {
  return fetch(`${BASE}/health`).then(handle)
}

export function fetchSchema(refresh = false) {
  return fetch(`${BASE}/schema${refresh ? '?refresh=true' : ''}`).then(handle)
}

export function fetchLlm() {
  return fetch(`${BASE}/llm`).then(handle)
}

export function setLlmProvider(provider) {
  return fetch(`${BASE}/llm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider }),
  }).then(handle)
}

// --- chat sessions -------------------------------------------------------
// Persistent, resumable conversations (chatbot_pipeline/sessions.py). A
// session optionally pins one log, which is what the "Ask AI" button on a log
// row creates.

export function createSession({ title = null, logId = null, mode = 'auto' } = {}) {
  return fetch(`${BASE}/chat/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, log_id: logId, mode }),
  }).then(handle)
}

export function listSessions() {
  return fetch(`${BASE}/chat/sessions`).then(handle)
}

export function getSession(sessionId) {
  return fetch(`${BASE}/chat/sessions/${encodeURIComponent(sessionId)}`).then(handle)
}

/* `signal` abandons the client's wait, it does not cancel the server's work:
 * the query completes and the model finishes generating, and the answer is
 * still written to the session store. That is why the UI says "stopped
 * waiting" rather than "cancelled" - the second would be untrue. */
export function askInSession(sessionId, question, mode, signal) {
  return fetch(`${BASE}/chat/sessions/${encodeURIComponent(sessionId)}/ask`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, mode }),
    signal,
  }).then(handle)
}

export function renameSession(sessionId, title) {
  return fetch(`${BASE}/chat/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  }).then(handle)
}

export function deleteSession(sessionId) {
  return fetch(`${BASE}/chat/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  }).then(handle)
}
