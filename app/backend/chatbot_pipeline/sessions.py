"""
sessions.py - Persistent chat sessions and their message history.

Backed by SQLite (stdlib `sqlite3`, no new dependency) in a single file next
to this pipeline. Three reasons it is SQLite rather than the alternatives:

  - **Not in-memory.** Sessions the user is expected to "come back to any
    time" cannot live in a process dict - a backend restart would silently
    erase every conversation, which is the one behaviour that would make the
    feature untrustworthy.
  - **Not Neo4j.** The graph is the *log corpus*; chat transcripts are
    application state about it, not part of it. Writing them there would put
    user-authored text into the same store every analytics aggregate and
    text-to-Cypher query scans, and this pipeline is otherwise strictly
    read-only against Neo4j - a property worth keeping.
  - **Not a new service.** Redis/Postgres would add a container to an
    airgapped deployment for a few kilobytes of text.

Concurrency: FastAPI runs sync endpoints in a threadpool, so connections are
opened per call (SQLite objects are not shareable across threads) with WAL
enabled so a reader never blocks the writer.
"""

import json
import logging
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.getenv(
    'CHATBOT_SESSIONS_DB',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chat_sessions.db'),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    log_id      TEXT,
    log_summary TEXT,
    mode        TEXT NOT NULL DEFAULT 'auto',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    meta_json  TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL so a long read can't block the write that follows an answer.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
    logger.info(f"Chat session store ready at {DB_PATH}")


def _now() -> float:
    return time.time()


def _row_to_session(row: sqlite3.Row, message_count: Optional[int] = None) -> Dict[str, Any]:
    out = {
        'id': row['id'],
        'title': row['title'],
        'log_id': row['log_id'],
        'log_summary': row['log_summary'],
        'mode': row['mode'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
    }
    if message_count is not None:
        out['message_count'] = message_count
    return out


def create_session(title: Optional[str] = None, log_id: Optional[str] = None,
                   log_summary: Optional[str] = None, mode: str = 'auto') -> Dict[str, Any]:
    session_id = uuid.uuid4().hex[:16]
    now = _now()
    # A log-scoped session names itself after the log so the list is scannable
    # without opening anything.
    resolved_title = title or (f"Log {log_id[:18]}" if log_id else 'New chat')
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, log_id, log_summary, mode, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (session_id, resolved_title, log_id, log_summary, mode, now, now),
        )
    return {
        'id': session_id, 'title': resolved_title, 'log_id': log_id,
        'log_summary': log_summary, 'mode': mode,
        'created_at': now, 'updated_at': now, 'message_count': 0,
    }


def list_sessions(limit: int = 100) -> List[Dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*, (SELECT count(*) FROM messages m WHERE m.session_id = s.id) AS n
            FROM sessions s ORDER BY s.updated_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_row_to_session(r, r['n']) for r in rows]


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        messages = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        ).fetchall()
    session = _row_to_session(row, len(messages))
    session['messages'] = [
        {
            'id': m['id'],
            'role': m['role'],
            'content': m['content'],
            'created_at': m['created_at'],
            'meta': json.loads(m['meta_json']) if m['meta_json'] else None,
        }
        for m in messages
    ]
    return session


def history_for_prompt(session_id: str, max_turns: int = 8) -> List[Dict[str, str]]:
    """Recent turns as plain {role, content} - what the follow-up rewriter
    consumes. Capped because a long session would otherwise grow the rewrite
    prompt without bound."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ?"
            " ORDER BY created_at DESC LIMIT ?",
            (session_id, max_turns),
        ).fetchall()
    return [{'role': r['role'], 'content': r['content']} for r in reversed(rows)]


def add_message(session_id: str, role: str, content: str,
                meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    message_id = uuid.uuid4().hex[:16]
    now = _now()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, meta_json, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (message_id, session_id, role, content,
             json.dumps(meta, default=str) if meta else None, now),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        # Title the session from its first user message, the way every chat UI
        # does - "New chat" everywhere makes the list useless.
        if role == 'user':
            row = conn.execute(
                "SELECT title, (SELECT count(*) FROM messages WHERE session_id = ?) AS n"
                " FROM sessions WHERE id = ?",
                (session_id, session_id),
            ).fetchone()
            if row and row['n'] == 1 and row['title'] in ('New chat', None):
                title = content.strip().split('\n')[0][:60]
                conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
    return {'id': message_id, 'role': role, 'content': content, 'created_at': now, 'meta': meta}


def rename_session(session_id: str, title: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title[:120], _now(), session_id),
        )
    return cur.rowcount > 0


def delete_session(session_id: str) -> bool:
    with _connect() as conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return cur.rowcount > 0
