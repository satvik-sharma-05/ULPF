"""
api.py - FastAPI backend for the demo frontend.

    uvicorn api:app --host 0.0.0.0 --port 8000

Three endpoints:
  POST /api/ask     - ask a question; `mode` picks the retrieval strategy
  GET  /api/schema  - the graph schema the chatbot introspected (for a "what's
                       in this graph" panel)
  GET  /api/health  - Neo4j / Ollama reachability, for a status indicator

`mode` is how the frontend's manual strategy control reaches the backend:
"text2cypher" and "graphrag" force that single strategy outright (bypassing
the planner entirely, which is the point of a *manual* control for a demo -
you're showing one retrieval path on demand, not an agent's judgment).
"auto" hands the question to the LLM planner (planner.py) instead, which
decides which tool(s) to call - possibly several in sequence - and returns
its tool-call trace in the `plan` field. "hybrid" remains available too, as
the older fixed both-strategies-at-once path.

One real correctness issue this module exists to solve: chatbot.py's pieces
return raw Neo4j driver objects in row dicts (temporal types from `datetime()`
calls, or a whole Node/Relationship if a generated Cypher query ignores the
"return named columns" instruction). None of those are JSON-serializable by
default - `jsonify_rows()` below converts them recursively before anything
reaches `JSONResponse`, so a query shaped that way degrades to a readable
string instead of crashing the endpoint with a 500.
"""

import json
import logging
import os
import time
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is a convenience, not a hard requirement

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'WARNING'),
                    format='%(asctime)s [%(levelname)s] %(name)s - %(message)s')
logger = logging.getLogger("chatbot.api")

import sessions
from config import API_CONFIG, NEO4J_CONFIG
import charts
import documents
import media
from chatbot import LogChatbot
from classifier import GRAPHRAG, HYBRID, TEXT2CYPHER

_VALID_MODES = {'auto', TEXT2CYPHER, GRAPHRAG, HYBRID}


def jsonify(value: Any) -> Any:
    """Recursively converts a value into something json.dumps can handle.

    Neo4j's Python driver returns its own types for anything the database
    treats as temporal (DateTime/Date/Time/Duration - e.g. Log.created_at,
    which is set via Cypher's `datetime()`) and for whole nodes/relationships
    (possible if a generated query ignores the "return named columns, not
    whole nodes" instruction in the prompt - the read-only guard blocks
    writes, not this). Both have an `isoformat()` or mapping-like interface
    but neither is JSON-serializable out of the box.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {k: jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonify(v) for v in value]
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    try:
        return {k: jsonify(v) for k, v in dict(value).items()}
    except (TypeError, ValueError):
        pass
    return str(value)


def jsonify_rows(rows: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    return [jsonify(row) for row in rows] if rows is not None else None


# ---------------------------------------------------------------------------
# App + shared chatbot instance
# ---------------------------------------------------------------------------
app = FastAPI(title="Log Graph Chatbot API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=API_CONFIG['cors_origins'],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_bot: Optional[LogChatbot] = None

#  How long to wait before retrying a failed Neo4j connection. Long enough not
#  to hammer a database that is genuinely down on every health poll, short
#  enough that recovery is automatic rather than something an operator has to
#  notice.
_RECONNECT_COOLDOWN_S = float(os.getenv('CHATBOT_RECONNECT_COOLDOWN_S', '15'))
_last_reconnect_attempt = 0.0


def get_bot() -> LogChatbot:
    """Built lazily so a Neo4j hiccup during import doesn't crash the whole
    server - constructing LogChatbot never raises even if Neo4j is down
    (self._connect() degrades to driver=None), so this always succeeds, but
    lazily also means the very first request pays the connection cost, not
    server boot."""
    global _bot
    if _bot is None:
        _bot = LogChatbot()
    return _bot


def reconnect_if_down() -> LogChatbot:
    """Rebuilds the chatbot if it has no Neo4j driver.

    Without this the chatbot could never recover from a database that was
    briefly unavailable at startup: the driver is resolved once in
    `LogChatbot.__init__`, so a failed connection left every answer returning
    "Not connected to Neo4j" for the life of the process. `docker compose up`
    has no guaranteed start order, so that is a normal occurrence rather than
    an edge case.

    A full rebuild rather than swapping `bot.driver`: the schema introspector,
    text-to-cypher, GraphRAG and planner each captured the driver when they
    were constructed. Replacing only the attribute would leave four stale
    references and turn a clearly broken bot into a partly working one, which
    is harder to diagnose than the original fault.
    """
    global _bot, _last_reconnect_attempt
    # get_bot(), NOT reconnect_if_down() - calling itself here is infinite
    # recursion, which is exactly what a blanket search-and-replace produced
    # the first time and what testing the Neo4j-down path caught.
    bot = get_bot()
    if bot.driver is not None:
        return bot
    now = time.monotonic()
    if now - _last_reconnect_attempt < _RECONNECT_COOLDOWN_S:
        return bot
    _last_reconnect_attempt = now
    logger.info('Neo4j driver is absent; attempting to rebuild the chatbot')
    rebuilt = LogChatbot()
    if rebuilt.driver is not None:
        logger.info('reconnected to Neo4j - chatbot rebuilt')
        try:
            bot.close()
        except Exception:
            # The old bot had no working driver anyway; a failure closing it
            # must not prevent the working one from taking over.
            pass
        _bot = rebuilt
    return _bot


@app.on_event("startup")
def _warm_start():
    sessions.init()
    get_bot()


@app.on_event("shutdown")
def _shutdown():
    if _bot is not None:
        _bot.close()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    mode: str = Field('auto', description="auto | text2cypher | graphrag | hybrid")


class ProviderRequest(BaseModel):
    provider: str = Field(..., description="ollama | groq | openrouter")


class CreateSessionRequest(BaseModel):
    title: Optional[str] = Field(None, max_length=120)
    log_id: Optional[str] = Field(None, max_length=200)
    mode: str = Field('auto', description="auto | text2cypher | graphrag | hybrid")


class SessionAskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    mode: Optional[str] = Field(None, description="overrides the session's mode for this turn")


class RenameSessionRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/api/ask")
def ask(request: AskRequest) -> Dict[str, Any]:
    mode = request.mode.strip().lower()
    if mode not in _VALID_MODES:
        raise HTTPException(400, f"Unknown mode {mode!r}. Valid: {sorted(_VALID_MODES)}")

    bot = reconnect_if_down()
    force_route = None if mode == 'auto' else mode
    response = bot.ask(request.question, force_route=force_route)

    return {
        'question': response.question,
        'answer': response.answer,
        'route': {
            'strategy': response.route.strategy,
            'confidence': response.route.confidence,
            'reason': response.route.reason,
        },
        'cypher': response.cypher,
        'rows': jsonify_rows(response.rows),
        'retrieval_method': response.retrieval_method,
        'context': jsonify(response.context) if response.context else None,
        # Only present for "auto" (planner) answers: the tool calls the
        # planner made, in order - [{tool, params, ok, note}, ...]. The single
        # most useful thing to show for agentic retrieval - a wrong answer is
        # far easier to diagnose from "it called graph_neighbors on the wrong
        # entity" than from the prose alone.
        'plan': jsonify(response.plan) if response.plan else None,
        'error': response.error,
    }


# ---------------------------------------------------------------------------
# Chat sessions - persistent, resumable conversations (see sessions.py).
# ---------------------------------------------------------------------------
def _fetch_log_context(log_id: str) -> Optional[str]:
    """Pins the actual log text into a log-scoped session.

    Read directly here rather than calling the analytics service: this
    pipeline already holds a Neo4j driver, and depending on another HTTP
    service would make chat fail whenever analytics happened to be down.
    """
    bot = reconnect_if_down()
    if bot.driver is None:
        return None
    try:
        with bot.driver.session(database=bot.database) as session:
            record = session.run(
                """
                MATCH (l:Log {id: $log_id})
                OPTIONAL MATCH (l)-[r]->(e)
                WHERE NOT e:Severity AND NOT e:SourceType AND NOT e:Day
                      AND NOT e:Process AND NOT e:Component
                WITH l, collect(DISTINCT
                    type(r) + ': ' + coalesce(e.name, e.id, e.address, e.path,
                                              e.moref, e.value, e.code, e.key,
                                              e.sid, e.number, '')
                )[..25] AS entities
                RETURN l.timestamp AS timestamp, l.hostname AS hostname,
                       l.source_type AS source_type, l.process AS process,
                       l.component AS component, l.subcomponent AS subcomponent,
                       l.severity AS severity, l.pid AS pid,
                       l.message AS message, l.raw_message AS raw_message,
                       l.attributes_json AS attributes_json,
                       l.matched_format AS matched_format,
                       l.confidence AS confidence,
                       entities
                """,
                log_id=log_id,
            ).single()
    except Exception as e:
        logger.warning(f"Could not load log {log_id} for session context: {e}")
        return None
    if record is None:
        return None
    d = dict(record)

    # The whole parsed record goes in, not just the message: "what is this?"
    # and "how do I fix it?" both hinge on details that live in the attribute
    # bag and the extracted entities (the container id, the device, the opID,
    # the file path) rather than in the message text alone.
    attributes = ''
    if d.get('attributes_json'):
        try:
            parsed = json.loads(d['attributes_json'])
            if isinstance(parsed, dict) and parsed:
                attributes = '\n'.join(f"    {k}: {v}" for k, v in list(parsed.items())[:25])
        except (ValueError, TypeError):
            pass
    entities = [e for e in (d.get('entities') or []) if e and not e.endswith(': ')]

    parts = [
        "The user is asking about this specific log record from their environment:",
        f"  id: {log_id}",
        f"  timestamp: {d.get('timestamp')}",
        f"  host: {d.get('hostname')}   process: {d.get('process')}"
        f"   component: {d.get('component')}   pid: {d.get('pid')}",
        f"  source_type: {d.get('source_type')}   severity: {d.get('severity')}",
        f"  parsed_by: {d.get('matched_format')} (confidence {d.get('confidence')})",
        f"  message: {d.get('message')}",
    ]
    if attributes:
        parts.append(f"  parsed fields:\n{attributes}")
    if entities:
        parts.append(f"  linked graph entities: {', '.join(entities)}")
    parts.append(f"  original raw line:\n    {(d.get('raw_message') or '')[:2000]}")
    return '\n'.join(parts)


@app.post("/api/chat/sessions")
def create_session(request: CreateSessionRequest) -> Dict[str, Any]:
    if request.mode not in _VALID_MODES:
        raise HTTPException(400, f"Unknown mode {request.mode!r}. Valid: {sorted(_VALID_MODES)}")
    summary = None
    if request.log_id:
        summary = _fetch_log_context(request.log_id)
        if summary is None:
            raise HTTPException(404, f"No log with id {request.log_id!r}")
    return sessions.create_session(
        title=request.title, log_id=request.log_id,
        log_summary=summary, mode=request.mode,
    )


@app.get("/api/chat/sessions")
def list_sessions(limit: int = 100) -> List[Dict[str, Any]]:
    return sessions.list_sessions(limit=limit)


@app.get("/api/chat/sessions/{session_id}")
def get_session(session_id: str) -> Dict[str, Any]:
    session = sessions.get_session(session_id)
    if session is None:
        raise HTTPException(404, f"No session {session_id!r}")
    return session


@app.patch("/api/chat/sessions/{session_id}")
def rename_session(session_id: str, request: RenameSessionRequest) -> Dict[str, Any]:
    if not sessions.rename_session(session_id, request.title):
        raise HTTPException(404, f"No session {session_id!r}")
    return sessions.get_session(session_id)


@app.delete("/api/chat/sessions/{session_id}")
def delete_session(session_id: str) -> Dict[str, Any]:
    if not sessions.delete_session(session_id):
        raise HTTPException(404, f"No session {session_id!r}")
    return {'deleted': session_id}


@app.post("/api/chat/sessions/{session_id}/ask")
def session_ask(session_id: str, request: SessionAskRequest) -> Dict[str, Any]:
    session = sessions.get_session(session_id)
    if session is None:
        raise HTTPException(404, f"No session {session_id!r}")

    mode = (request.mode or session['mode'] or 'auto').strip().lower()
    if mode not in _VALID_MODES:
        raise HTTPException(400, f"Unknown mode {mode!r}. Valid: {sorted(_VALID_MODES)}")

    # History is read BEFORE the new question is stored, so the rewriter sees
    # the prior turns rather than the question it is meant to be rewriting.
    history = sessions.history_for_prompt(session_id)
    sessions.add_message(session_id, 'user', request.question)

    bot = reconnect_if_down()
    response = bot.ask(
        request.question,
        force_route=None if mode == 'auto' else mode,
        history=history,
        extra_context=session.get('log_summary'),
    )

    meta = {
        'route': {
            'strategy': response.route.strategy,
            'confidence': response.route.confidence,
            'reason': response.route.reason,
        },
        'cypher': response.cypher,
        'rows': jsonify_rows(response.rows),
        'retrieval_method': response.retrieval_method,
        'context': jsonify(response.context) if response.context else None,
        'plan': jsonify(response.plan) if response.plan else None,
        'error': response.error,
    }
    assistant = sessions.add_message(session_id, 'assistant', response.answer, meta=meta)

    return {
        'session_id': session_id,
        'message': assistant,
        'question': response.question,
        'answer': response.answer,
        **meta,
    }


@app.get("/api/schema")
def schema(refresh: bool = False) -> Dict[str, str]:
    bot = reconnect_if_down()
    if bot.schema is None:
        return {'schema': "(schema introspection is disabled - CHATBOT_INTROSPECT_SCHEMA=false)"}
    return {'schema': bot.schema.description(refresh=refresh)}


@app.get("/api/health")
def health() -> Dict[str, Any]:
    # Also the reconnect trigger. The frontend polls this continuously, so a
    # database that comes up after the chatbot did is picked up on its own
    # instead of needing a restart.
    bot = reconnect_if_down()
    neo4j_ok = bot.driver is not None
    llm_ok = bot.llm.available()
    return {
        'neo4j': neo4j_ok,
        # Kept named 'ollama' for backwards compatibility with the existing
        # frontend status dot; it now means "the ACTIVE llm provider is
        # reachable", which is what that dot was always really showing.
        'ollama': llm_ok,
        'llm': llm_ok,
        'provider': bot.llm.provider,
        'model': bot.llm.model,
        'status': 'ok' if neo4j_ok else 'degraded',
    }


@app.get("/api/llm")
def get_llm() -> Dict[str, Any]:
    """The active LLM provider, and what else this process could switch to.

    `reachable` is probed for the ACTIVE provider only - probing all three on
    every poll would cost a network round-trip per provider, and the two cloud
    ones are unreachable by definition on the airgapped VM this project
    targets, so the probe would just be a guaranteed timeout on every call.
    """
    bot = reconnect_if_down()
    return {
        'active': bot.llm.provider,
        'model': bot.llm.model,
        'reachable': bot.llm.available(),
        'providers': bot.llm.providers(),
    }


@app.post("/api/llm")
def set_llm(request: ProviderRequest) -> Dict[str, Any]:
    """Switches provider for this process, at runtime.

    Process-local and NOT persisted: a restart returns to whatever
    CHATBOT_LLM_PROVIDER says. That is deliberate - persisting it would mean
    this endpoint could silently and permanently repoint an airgapped
    deployment at a cloud API, which is exactly the change that should require
    an explicit config edit rather than a button press.
    """
    bot = reconnect_if_down()
    try:
        bot.llm.switch_provider(request.provider)
    except ValueError as e:
        raise HTTPException(400, str(e))

    info = bot.llm.providers()[bot.llm.provider]
    if not info['configured']:
        # Switched anyway - the caller asked for it, and available() will
        # report false so every stage falls back deterministically rather than
        # failing. Saying so is more useful than refusing.
        logger.warning(f"Switched to {bot.llm.provider}, which has no credentials configured.")
    return {
        'active': bot.llm.provider,
        'model': bot.llm.model,
        'reachable': bot.llm.available(),
        'configured': info['configured'],
        'providers': bot.llm.providers(),
    }


# ---------------------------------------------------------------------------
# Optional: serve the built frontend (`npm run build` in ../frontend/) so a
# demo can run as a single process/URL instead of two dev servers. Mounted
# last so it never shadows the /api/* routes above; absent entirely until you
# build, so this is a no-op during normal frontend development against
# Vite's dev server (which talks to this API via its own proxy instead).
#
# The frontend lives at LA/frontend - a sibling of this file's LA/chatbot_pipeline
# directory, not nested inside it - so the dist path goes up one level first.
# ---------------------------------------------------------------------------
_FRONTEND_DIST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'frontend', 'dist'
)
if os.path.isdir(_FRONTEND_DIST):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIST, html=True), name="frontend")
    logger.info(f"Serving built frontend from {_FRONTEND_DIST}")




def _has(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Media: speech, vision and document export
#
# Every one of these degrades to a clear 503 rather than a stack trace when its
# model is not present in the image. On an air-gapped VM a missing model cannot
# be fetched at runtime, so "this deployment was built without the Whisper
# model" is the only useful thing to say - and the UI reads /api/media/
# capabilities to avoid offering the control at all.
# ---------------------------------------------------------------------------
class PptxRequest(BaseModel):
    question: Optional[str] = None
    answer: Optional[str] = None
    cypher: Optional[str] = None
    rows: Optional[List[Dict[str, Any]]] = None
    route: Optional[str] = None


class TtsRequest(BaseModel):
    text: str


@app.get("/api/media/capabilities")
def media_capabilities() -> Dict[str, Any]:
    """What this build can do. The UI hides controls it would only fail on."""
    return media.capabilities()


@app.post("/api/export/pptx")
def export_pptx(request: PptxRequest):
    caps = media.capabilities()
    if not caps['pptx']['available']:
        raise HTTPException(503, caps['pptx']['detail'] or 'PPTX export is not available')
    try:
        blob = media.build_pptx(request.question or '', request.answer or '',
                                request.cypher, request.rows)
    except Exception as e:
        logger.exception("pptx build failed")
        raise HTTPException(500, f"Could not build the deck: {e}")
    return Response(
        content=blob,
        media_type='application/vnd.openxmlformats-officedocument.presentationml.presentation',
        headers={'Content-Disposition': 'attachment; filename="ulpf-answer.pptx"'},
    )


@app.post("/api/media/tts")
def media_tts(request: TtsRequest):
    caps = media.capabilities()
    if not caps['tts']['available']:
        raise HTTPException(503, caps['tts']['detail'] or 'Speech synthesis is not available')
    try:
        wav = media.synthesize(request.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("tts failed")
        raise HTTPException(500, f"Could not synthesize speech: {e}")
    # no-store: a spoken answer can contain the same operational detail the
    # text does, and there is no reason for it to sit in a proxy cache.
    return Response(content=wav, media_type='audio/wav',
                    headers={'Cache-Control': 'no-store'})


@app.post("/api/media/stt")
async def media_stt(file: UploadFile = File(...)):
    caps = media.capabilities()
    if not caps['stt']['available']:
        raise HTTPException(503, caps['stt']['detail'] or 'Transcription is not available')
    audio = await file.read()
    try:
        return media.transcribe(audio, file.filename or 'audio.webm')
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("stt failed")
        raise HTTPException(500, f"Could not transcribe the audio: {e}")


@app.post("/api/media/vision")
async def media_vision(file: UploadFile = File(...), question: str = Form(None)):
    caps = media.capabilities()
    if not caps['vision']['available']:
        raise HTTPException(503, caps['vision']['detail'] or 'Image understanding is not available')
    image = await file.read()
    try:
        return {'answer': media.describe_image(image, question), 'model': media.VISION_MODEL}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("vision failed")
        raise HTTPException(500, f"Could not read the image: {e}")


# ---------------------------------------------------------------------------
# Documents: decks and reports written on request
#
# "Create a 7 slide PPT on the last 7 days of logs" and "a 3 slide deck on AI
# versus the market" go through the same path; the only difference is whether
# real figures get pulled from the graph before the model writes anything.
#
# The rendered file comes back base64 in the JSON rather than as a download,
# and that is deliberate: the chat needs to show the outline it produced AND
# offer the file, and a streamed attachment can only do the second. These
# documents are tens of kilobytes, so inlining costs nothing.
# ---------------------------------------------------------------------------
class DocumentRequest(BaseModel):
    message: Optional[str] = Field(None, description="The user's request, verbatim")
    kind: Optional[str] = Field(None, description="pptx | pdf - overrides what the message implies")
    sections: Optional[int] = Field(None, ge=1, le=20)
    topic: Optional[str] = None


@app.post("/api/documents/generate")
def documents_generate(request: DocumentRequest) -> Dict[str, Any]:
    import base64

    spec = documents.parse(request.message or request.topic or '')
    # Explicit fields win over whatever was inferred from the sentence.
    if request.kind in ('pptx', 'pdf'):
        spec['kind'] = request.kind
    if request.sections:
        spec['sections'] = request.sections
    if request.topic:
        spec['topic'] = request.topic
        spec['grounded'] = bool(documents._LOG_TOPIC.search(request.topic))

    if spec['kind'] == 'pdf' and not _has('reportlab'):
        raise HTTPException(503, 'reportlab is not installed in this image')
    if spec['kind'] == 'pptx' and not _has('pptx'):
        raise HTTPException(503, 'python-pptx is not installed in this image')

    # Only a topic about this system's own data gets grounded. Asking the graph
    # about global warming would return nothing useful and would waste six
    # queries finding that out.
    bot = reconnect_if_down()
    facts = []
    if spec['grounded']:
        try:
            facts = documents.gather_facts(bot.driver, NEO4J_CONFIG.get('database', 'neo4j'))
        except Exception as e:
            # A document about the logs is still worth producing without the
            # figures; it just says less. Failing the whole request because one
            # aggregate timed out would be the wrong trade.
            logger.warning(f"grounding failed, writing ungrounded: {e}")

    doc = documents.compose(spec['topic'], spec['sections'], spec['kind'], bot.llm, facts)
    try:
        blob = documents.render(doc, spec['kind'])
    except Exception as e:
        logger.exception("document render failed")
        raise HTTPException(500, f"Could not render the document: {e}")

    return {
        'kind': spec['kind'],
        'sections': spec['sections'],
        'topic': spec['topic'],
        'grounded': doc.get('grounded', False),
        # False means the model produced nothing and the outline below is
        # placeholders. The caller has to be able to tell those apart.
        'composed': doc.get('composed', False),
        'title': doc['title'],
        'subtitle': doc.get('subtitle'),
        'outline': [{'title': s['title'], 'bullets': s['bullets']} for s in doc['sections']],
        'filename': documents.filename(doc, spec['kind']),
        'media_type': documents.MEDIA_TYPES[spec['kind']],
        'file_b64': base64.b64encode(blob).decode('ascii'),
        'bytes': len(blob),
    }


@app.post("/api/export/docx")
def export_docx(request: PptxRequest):
    """One chat answer as an editable Word document.

    Same payload as the PPTX export - the answer, its query and its rows -
    reshaped into the section structure the renderers already understand,
    rather than a fourth bespoke layout.
    """
    if not _has('docx'):
        raise HTTPException(503, 'python-docx is not installed in this image')

    sections = [{'title': 'Answer',
                 'bullets': [line.strip() for line in (request.answer or '').split('\n')
                             if line.strip()] or ['(no answer)']}]
    if request.cypher:
        sections.append({'title': 'Query', 'bullets': [request.cypher[:1500]]})
    if request.rows:
        cols = list(request.rows[0].keys())[:6]
        sections.append({
            'title': f'Results ({len(request.rows)} rows)',
            'bullets': [' · '.join(f"{c}: {r.get(c)}" for c in cols)
                        for r in request.rows[:25]],
            # Same inference the chat uses, so the exported document shows the
            # chart the user was looking at. render_docx rasterises it - Word
            # has no native chart API.
            'chart': charts.from_rows(request.rows),
        })

    doc = {'title': request.question or 'Chat answer',
           'subtitle': 'Universal Log Pre-processing Framework',
           'sections': sections}
    try:
        blob = documents.render_docx(doc)
    except Exception as e:
        logger.exception("docx build failed")
        raise HTTPException(500, f"Could not build the document: {e}")

    return Response(
        content=blob,
        media_type=documents.MEDIA_TYPES['docx'],
        headers={'Content-Disposition': 'attachment; filename="ulpf-answer.docx"'},
    )

if __name__ == '__main__':
    import uvicorn
    uvicorn.run("api:app", host=API_CONFIG['host'], port=API_CONFIG['port'], reload=False)
