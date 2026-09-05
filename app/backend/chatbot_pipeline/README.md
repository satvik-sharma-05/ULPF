# Chatbot Pipeline

Answers natural-language questions about the log graph the ingestion pipeline
built. Two ways a question gets answered:

- **Manual mode** (`text2cypher` / `graphrag` / `hybrid`) — one fixed strategy
  runs, no planning. What the frontend's manual mode buttons use, so a demo
  can show each retrieval path on demand.
- **Auto mode** — an **LLM planner** (qwen3:14b) decides which tool(s) to call,
  possibly several in sequence, and fuses whatever it gathers into one answer.
  A compound question like *"why did storage fail, and which hosts were
  affected"* genuinely needs graph traversal **and** vector search together -
  a single fixed strategy picked up front can't do that.

Standalone by design: the only dependency is a populated Neo4j. It does not
import `../ingestion_pipeline`, so it deploys, scales and restarts on its own.

```bash
pip install neo4j requests
cp .env.example .env && nano .env      # NEO4J_URI, NEO4J_PASSWORD

python chat.py --ask "which hosts produced the most errors?"
python chat.py                          # interactive REPL - planner decides what to do
```

For the web demo (backend + React frontend), see [Demo frontend](#demo-frontend) below.

---

## How a question is answered

```
                                LLM Planner  <-------------------+
                                     |                            |
                                     v                            |
question ──▶ [manual mode?] ──▶ pick & execute tool(s) ───────────+  (more rounds if needed)
                    |                |
                    |                v
                    |          Context Fusion
                    |                |
                    v                v
              single strategy    qwen3:14b
                    |                |
                    +───────▶ final answer
```

### The tools the planner can call

| Tool | For | Example |
|---|---|---|
| `vector_search` | Semantic search over log text, with optional host/day/severity pre-filter | *"why did storage fail"* |
| `fulltext_search` | Exact keyword/error-code search over log text | *"find NMP APD detected"* |
| `graph_neighbors` | What's structurally connected to a named entity (host, VM, user, device, datastore, …) | *"what depends on this datastore"* |
| `path_traversal` | How two named entities are connected | *"did the same user touch both hosts"* |
| `temporal_window` | Logs in a time range, in chronological order | *"what happened between 2pm and 4pm"* |
| `parent_child_retrieval` | Expand one already-known log into every other log sharing its operation/trace/task id | *"show me the rest of that incident"* (needs a log id from an earlier step) |
| `query_template` | Deterministic, pre-written Cypher for common stats questions (`top_hosts_by_severity`, `count_by_severity`, `host_inventory`) | *"top hosts by error count"* |
| `pattern_match` | Curated search terms for known vSphere/VMware failure signatures (`ha_restart`, `apd`, `host_isolation`, `network_partition`, `snapshot_failure`) | *"find HA restart events"* |
| `text2cypher` | Structural/counting/filtering/ranking questions with no matching template, translated directly into Cypher | *"which host has the most errors"* |

The planner's prompt is generated from the same `TOOL_SPECS` registry that
dispatches these calls (`tools.py`), not a hand-maintained copy - so it can
never advertise a tool name that doesn't actually exist. Each planning round
returns `intent`, `confidence`, `entities`, `filters` and a `retrieval_plan`
(tool + one-line `reason` + parameters per step), which the frontend's plan
trace displays verbatim so a wrong answer is diagnosable ("it called
`graph_neighbors` on the wrong entity") instead of opaque.

`graph_neighbors` and `path_traversal` traverse **only** the low-cardinality
*structural* relationships (`ACCESSED`, `ATTACHED_TO`, `HOSTED_ON`, `RUNS_ON`, …)
— never the six relationship types that touch `:Log` directly
(`EMITTED`, `EMITTED_BY`, `FROM_COMPONENT`, `HAS_SEVERITY`, `FROM_SOURCE`, `ON_DAY`).
`Host-[:EMITTED]->Log` alone is ~117M edges in the reference corpus; an
unrestricted traversal that could route through `:Log` would explore that
fan-out before hitting any hop limit. Log content is what
`vector_search`/`fulltext_search`/`temporal_window` are for — a deliberate
separation of concerns, not a missing feature (see `tools.py`'s module
docstring for the full reasoning).

Named entities (`"VM-125"`, `"MD-H-SY2-BL07"`) are resolved to their actual
graph label via `entity_resolver.py`, which reads Neo4j's own uniqueness
constraints (`SHOW CONSTRAINTS`) rather than hardcoding a second copy of the
schema — consistent with how `schema_introspect.py` already avoids that for
text-to-Cypher. **Real limitation, not a resolver bug:** `VM.moref` is a
vSphere managed-object reference pulled from log text (e.g. `vm-1203`), not a
human-assigned name — a question about `"VM-125"` only resolves if that exact
string appears somewhere in the corpus.

### Reliability

Planning uses a raw JSON-completion prompt against qwen3:14b, not native
function-calling, so it fails more often than a larger model would - every
failure degrades rather than breaks:

- an unknown tool name or bad parameters → that one step is skipped, logged, planning continues
- the planner produces nothing usable across every round → a fixed,
  deterministic fallback (`vector_search` + `text2cypher`) runs instead
- hard caps (`CHATBOT_PLANNER_MAX_ROUNDS`, `CHATBOT_PLANNER_MAX_TOOL_CALLS`)
  bound the worst-case latency/cost of a plan that never converges

```bash
python chat.py --ask "..." --route graphrag     # skip the planner, force one strategy
python chat.py --explain-route "how many errors yesterday?"   # the OLD cheap heuristic router - not what the planner does, just a fast sanity check
```

If manual text-to-Cypher returns no usable rows, the answer falls back to
GraphRAG rather than reporting an empty result.

### Text-to-Cypher

The schema in the prompt is **introspected from the live database**
(`schema_introspect.py`), not copied from the ingestion pipeline. Two copies
would drift the first time either side changed; introspection also describes
the graph *as actually loaded*, so a partial ingest doesn't leave the model
promising labels with no data.

Generated Cypher passes a **read-only guard** before execution. An LLM asked
for a "query" will occasionally emit `DETACH DELETE`, and this runs against the
real log store. The guard rejects rather than sanitizes — silently rewriting a
destructive query into a different one would answer the user's question wrongly
and confidently. String literals are stripped before the check, so a log
containing the word "delete" isn't a false positive.

### GraphRAG

Graph-aware, not plain vector RAG. After the vector index finds seed logs, it
walks one hop out to the entities they touch — host, user, device, task,
operation — and pulls in other logs sharing those entities. The log that
*explains* a failure rarely contains the question's words, but it almost always
shares an opID, a device or a host with the log that does.

Retrieval degrades in three steps, so it stays useful as capability drops:

1. **vector** — native index on `(:Log).embedding` *(needs `--embed` at ingest)*
2. **full-text** — index on `(:Log).message` *(always available)*
3. **substring** — plain `CONTAINS` *(last resort)*

### Reranking (Context Fusion)

The planner can call several tools per question - `pattern_match` might return
15 logs, `parent_child_retrieval` another 20 correlated ones, some overlapping.
Before those reach the final-answer prompt, `reranker.py` deduplicates by log
id and scores the pool with a cross-encoder (`BAAI/bge-reranker-v2-m3`) against
the actual question, so the evidence that survives `CHATBOT_MAX_CONTEXT_CHARS`
truncation is the most relevant, not just whatever a tool call happened to
return first. This only reorders free-text log evidence (`vector_search`,
`fulltext_search`, `temporal_window`, `pattern_match`, `parent_child_retrieval`)
- a cross-encoder has nothing to score tabular/entity results
(`graph_neighbors`, `path_traversal`, `query_template`, `text2cypher`) against,
so those keep their own tool's ordering. Same degrade-gracefully rule as
embeddings: if the model isn't downloaded or torch is broken, the pool just
keeps its retrieval order - never a crash, never a blocked answer.

---

## What this doesn't do (yet)

Being upfront about scope: an "AI ops engineer" vision (root-cause reports,
incident timelines, "have we seen this before", conversational follow-up)
needs some things this graph and API genuinely don't have yet - these aren't
silently faked, they're just not built:

| Would need | Why it's not here |
|---|---|
| `Alert` / `Incident` / `Event` as distinct node types | The graph only has `:Log` with a `severity` property - there's no separate "this is an incident" entity to query, cluster, or embed. `temporal_window` + `max_severity_score` approximates "what happened during this incident" without one. |
| VMware KB / documentation RAG (`"explain NMP APD detected"` citing vendor docs) | No documentation corpus is ingested anywhere in this project - only the log graph. Adding it would mean a second embedding index over a different (chunked-document) source, not a change to this chatbot. |
| Conversation memory / follow-up (`"what about the other affected VMs?"`) | `/api/ask` is stateless per call - no session id, no server-side history. A real implementation needs a conversation id threaded from the frontend and either a history buffer or an entity-tracking layer, not just a bigger prompt. |
| "Similar incident" search via incident-level embeddings | Embeddings exist per-*log*, not per-*incident* - there's no defined "cluster of correlated logs = one incident" to embed as a unit. `vector_search` finds semantically similar individual log lines today, which is a real but narrower capability. |
| Global/community summarization (Microsoft GraphRAG-style, "give me an executive health summary") | Needs community detection (e.g. Leiden clustering) plus per-community LLM summaries, regenerated on ingest - a real engineering investment. `text2cypher` already handles per-host/per-day/per-severity aggregates directly and cheaply; nothing built so far asks a totally unscoped "summarize everything" question. |

If any of these are the actual next priority, they're addable - each is a
scoped, standalone piece of work, not a rewrite of what's here.

---

## Demo frontend

A React (plain JS, no TypeScript) + Tailwind chat UI, backed by a small
FastAPI wrapper around `LogChatbot`. **The frontend lives at `LA/frontend/`
- a sibling of this directory, not nested inside it** - because it's a
separate deployable (its own Dockerfile, its own nginx container) even though
it only ever talks to this pipeline's API.

```
LA/
├── ingestion_pipeline/
├── chatbot_pipeline/   ← this directory (api.py, chatbot.py, ...)
└── frontend/           ← the React app
```

### Local dev (two processes)

```bash
# 1. Backend - from chatbot_pipeline/
pip install -r requirements.txt        # adds fastapi, uvicorn, python-dotenv
uvicorn api:app --reload --port 8000

# 2. Frontend - from ../frontend/ (i.e. LA/frontend/)
cd ../frontend
npm install
npm run dev                             # http://localhost:5173
```

Vite's dev server proxies `/api/*` to `http://localhost:8000` (see
`frontend/vite.config.js`), so the frontend calls relative paths and never
needs CORS configured for the normal dev workflow. `api.py`'s CORS middleware
(`API_CORS_ORIGINS`, default `*`) only matters if you open the frontend from a
different host than the one running uvicorn.

**The mode selector is a manual classifier, not a display of the automatic
one.** For a demo you want to show each retrieval strategy on demand rather
than leave it to the router's judgment, so picking **Text → Cypher** or
**GraphRAG** in the UI calls `bot.ask(question, force_route=mode)` directly,
bypassing `classifier.py` entirely. **Auto (classifier)** is still there as a
third option so the automatic routing can be demonstrated too — it's just not
the default.

Each answer shows: the route badge (strategy + confidence), the answer text,
a collapsible generated-Cypher block, a collapsible results table, and for
GraphRAG/hybrid, a collapsible evidence panel listing exactly which log lines
were retrieved and which related logs were pulled in via shared entities -
the retrieval is the part worth being transparent about in a demo, since a
wrong answer originates there, not in the final prose.

### Single-process demo (optional, no Docker)

`npm run build` (from `LA/frontend/`) produces `frontend/dist/`; if that
directory exists, `api.py` serves it directly at `/`, so `uvicorn api:app`
alone becomes the whole demo - no second process, no CORS, one URL:

```bash
cd ../frontend && npm run build && cd ../chatbot_pipeline
uvicorn api:app --port 8000
# open http://localhost:8000
```

### Full offline / airgapped deployment (Docker)

**Assumes Ollama is already running natively on the airgapped VM** (systemd /
`ollama serve`, port 11434, model `qwen3:14b` already pulled) - that's the
common case if the VM was set up for other LLM work before this pipeline
existed, and it means one less multi-GB image to build and transfer. If you
don't have that yet, install Ollama on the VM and `ollama pull qwen3:14b`
there directly (it needs internet for that one-time pull, same as any other
package install on that VM would).

An internet-connected Ubuntu PC builds two Docker images (backend, frontend),
you carry the tars across the air gap, and the airgapped VM runs both with
zero network access - reaching the native Ollama and the ingestion stack's
Neo4j via the VM's own `localhost`.

**1. On a connected Ubuntu PC** (needs internet + Docker):

```bash
cd chatbot_pipeline
./build_offline_bundle.sh
```

This builds `chatbot-backend` (bakes in **both** `BAAI/bge-m3` and
`BAAI/bge-reranker-v2-m3` at build time, same as the ingestion image, since
GraphRAG has to embed the user's *question* with the identical model that
embedded the logs, and the planner's reranker needs its own weights present
too) and `chatbot-frontend` (React build + nginx). Produces two `.tar` files.
Fully self-contained - the resulting image never depends on anything already
present on whatever machine runs it.

**2. Transfer to the airgapped VM**: this whole `chatbot_pipeline/` folder
(with the two new `.tar` files) plus `LA/frontend/` plus - if not already
there - `LA/ingestion_pipeline/` with the graph already ingested.

**3. On the airgapped VM**, with Ollama running and the ingestion stack's
Neo4j already up (`cd ingestion_pipeline && ./offline_ingest_structure.sh` if
not):

```bash
cd chatbot_pipeline
./offline_start_chatbot.sh
```

Checks the native Ollama is reachable and has `qwen3:14b`, loads the two
images, starts backend + frontend, waits for the backend to report healthy,
and prints the URLs:

```
Frontend : http://<vm-ip>:3000
Backend  : http://<vm-ip>:8000/api/health
```

**Why `network_mode: host`** (see the comments in `docker-compose.yml` for
the full reasoning): `ollama serve` binds to `127.0.0.1` by default, which an
ordinarily-networked container can't reach even via `host.docker.internal`
(that only routes to the host's external address, not literally `127.0.0.1`).
Host networking sidesteps that without requiring you to reconfigure the
existing Ollama - "localhost" inside the container really is the VM's
localhost. The trade-off, accepted deliberately for a single-purpose demo VM,
is giving up container network isolation.

### API endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/ask` | `{"question": "...", "mode": "auto\|text2cypher\|graphrag\|hybrid"}` → answer, route, cypher, rows, evidence |
| `GET /api/schema` | The introspected schema text (`?refresh=true` to bypass the cache) |
| `GET /api/health` | `{"neo4j": bool, "ollama": bool, "model": "..."}` — drives the header status dots |

---

## Files

```
LA/
├── ingestion_pipeline/          (see its own README)
├── frontend/                    React + Tailwind chat UI (Vite, plain JS) - SIBLING, not nested
│   ├── src/App.jsx                  layout, message state, example prompts
│   ├── src/components/              ModeSelector, StatusBar, RouteBadge,
│   │                                 CypherBlock, RowsTable, EvidencePanel
│   ├── Dockerfile                   multi-stage: npm build -> nginx
│   └── nginx.conf                   serves the build + proxies /api to the backend
└── chatbot_pipeline/
    ├── chat.py                  CLI: REPL, --ask, --route, --explain-route, --show-schema
    ├── api.py                   FastAPI backend for the frontend
    ├── chatbot.py                orchestrator: manual mode -> one strategy; auto -> the planner
    ├── planner.py               LLM planning loop - the "auto" mode's brain
    ├── tools.py                 the tool registry the planner calls (vector/fulltext/graph/path/temporal/text2cypher)
    ├── entity_resolver.py       "VM-125" -> its actual (label, key, value) in the graph
    ├── classifier.py            cheap heuristic router - used by --explain-route only, not by "auto" anymore
    ├── text_to_cypher.py        NL → Cypher (qwen3:14b), with the read-only guard
    ├── graph_rag.py             vector/full-text retrieval + 1-hop entity expansion (manual GraphRAG mode)
    ├── schema_introspect.py     reads the live graph schema from Neo4j
    ├── embeddings.py            embeds the question for vector search (shared singleton - loaded at most once)
    ├── llm.py                   thin Ollama client with offline fallbacks
    ├── config.py  .env.example  requirements.txt
    ├── Dockerfile               backend image (bakes in BAAI/bge-m3 + BAAI/bge-reranker-v2-m3)
    ├── docker-compose.yml       backend + frontend, network_mode: host (reaches native Ollama + Neo4j via localhost)
    ├── build_offline_bundle.sh  run on a CONNECTED Ubuntu PC - builds + saves the 2 images
    └── offline_start_chatbot.sh run on the AIRGAPPED VM - checks native Ollama, loads images, starts the stack

(Ollama itself is not part of this project - it's assumed already installed
natively on the target VM with `qwen3:14b` pulled.)
```

---

## Without an LLM

Everything still runs. Routing is deterministic, retrieval is unchanged, and
answers come back as the retrieved rows and log lines instead of prose. The
retrieval is the genuinely hard part, and it doesn't need a model.

To get natural-language answers, point at any Ollama instance:

```bash
export OLLAMA_HOST=http://localhost:11434
export OLLAMA_MODEL=qwen3:14b
```

## Embeddings

Vector search only works if the graph was ingested **with `--embed`**, and if
`EMBEDDING_MODEL` / `EMBEDDING_DIM` here match whatever produced those vectors.

This is worth being careful about: the vector index accepts any 1024-float
query and **cannot tell you** the query came from a different embedding space —
you get plausible-looking, meaningless neighbours rather than an error. Unlike
the ingestion side there is deliberately no hash-based fallback here, so a
missing model degrades to full-text search instead of inventing vectors.

Vector search needs the optional extras:

```bash
pip install sentence-transformers
pip install torch --index-url https://download.pytorch.org/whl/cpu
```

## Troubleshooting

| Problem | Fix |
|---|---|
| "Not connected to Neo4j" | Check `NEO4J_URI`/credentials; `nc -zv <host> 7687` |
| `database 'logs_db' not found` | Community Edition has only `neo4j` — set `NEO4J_DATABASE=neo4j` |
| Answers are raw rows, not prose | No LLM reachable — start Ollama or set `OLLAMA_HOST` |
| Retrieval says `fulltext`, not `vector` | Graph ingested without `--embed`, or `sentence-transformers` not installed |
| Cypher looks right but returns nothing | `python chat.py --show-schema` — check the graph has the labels the query used |
| "Refused to run generated query" | Working as intended: the model emitted a write clause |
| Frontend loads but every question fails | Backend not running, or wrong port — check `uvicorn` is up and `VITE_API_URL` (default `http://localhost:8000`) matches |
| Status dots both red but Neo4j/Ollama are actually up | `GET /api/health` is cached per-call, not per-server; wait 15s (`_AVAILABILITY_TTL_SECONDS` for Ollama) or refresh |
| `npm run build` output not served | `api.py` only mounts `frontend/dist/` if it exists at import time — restart uvicorn after building |
