# Universal Log Pre-processing Framework (ULPF)

Ingests logs from any source, vendor or format; normalizes them into one
declared event schema **without discarding the original**; and makes the result
queryable, analysable, and exportable to a SIEM or Data Lake.

Built for the NTRO problem statement under **Blockchain & Cybersecurity**.
Conformance against each expected solution (a)–(k), with measured evidence
rather than claims: **[ULPF_CONFORMANCE.md](ULPF_CONFORMANCE.md)**

---

## Layout

```
final_project/
├── frontend/                     React 18 + Vite UI
│   └── src/
│       ├── pages/                Home, Analytics, Alerts, Live logs,
│       │                         Schema & export, Chat, Modes
│       └── components/           analytics/ logs/ chat/ modes/ ui/
│
└── backend/
    ├── ingestion_pipeline/       Builds the graph
    │   ├── core/                 parser, detectors, entity extraction,
    │   │                         embeddings, Neo4j writer  (shared by all paths)
    │   ├── batch/                files on disk  → parse → embed → Neo4j
    │   ├── realtime/             DNIF → Kafka   → parse → embed → Neo4j
    │   └── api.py                ingestion control API   :8020
    │
    ├── analytics_pipeline/       Reads the graph
    │   ├── taxonomy.py           the Universal Event Schema   (ULPF c)
    │   ├── exporters.py          NDJSON / ECS / CEF / CSV     (ULPF g)
    │   ├── filters.py            one filter, threaded through every aggregation
    │   ├── queries.py            every Cypher aggregation
    │   └── api.py                analytics API           :8010
    │
    ├── chatbot_pipeline/         Asks questions of the graph
    │   ├── text2cypher.py        natural language → Cypher (read-only guard)
    │   ├── graph_rag.py          vector retrieval + rerank + synthesis
    │   ├── sessions.py           resumable chat sessions (SQLite)
    │   └── api.py                chat API                :8000
    │
    └── ollama/                   local LLM image for air-gapped deployment
```

The three backends are **separate deployables that share only Neo4j**. Neither
`analytics_pipeline` nor `chatbot_pipeline` imports the ingestion code or each
other, and neither ever writes to the graph — so any one can be restarted,
scaled or redeployed without touching the others.

---

## Setup

### Prerequisites

- Python 3.12, Node 18+
- Neo4j 5.11+ (5.11 is the floor — earlier versions have no native vector index)
- Optional: Docker, Ollama (air-gapped LLM), Kafka (realtime path only)

### 1. Configure

Each backend service reads its own `.env`. Copy the example and fill it in:

```bash
cd backend/ingestion_pipeline && cp .env.example .env
cd ../analytics_pipeline      && cp .env.example .env
cd ../chatbot_pipeline        && cp .env.example .env
```

All three must point at the **same** `NEO4J_URI` and `NEO4J_DATABASE`, or the
dashboard and the chatbot will describe different graphs.

> `.env` files are not in this tree by design — they hold real credentials.
> Only `.env.example` ships.

### 2. Install

```bash
# backend  (one venv is fine for all three)
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r backend/ingestion_pipeline/requirements.txt
pip install -r backend/analytics_pipeline/requirements.txt
pip install -r backend/chatbot_pipeline/requirements.txt

# frontend
cd frontend && npm ci
```

### 3. Ingest

```bash
cd backend/ingestion_pipeline

# straight into the graph
python -m batch.run_pipeline --path /path/to/logs --embed

# or in two steps, keeping the parsed output
python -m batch.parse_logs --path /path/to/logs --out output.json
python -m batch.ingest_to_neo4j --input output.json --embed
```

`--max-records-per-file N` and `--limit N` bound a large corpus.

### 4. Run

Four processes, four terminals:

```bash
cd backend/chatbot_pipeline   && python -m uvicorn api:app --port 8000
cd backend/analytics_pipeline && python -m uvicorn api:app --port 8010
cd backend/ingestion_pipeline && python -m uvicorn api:app --port 8020
cd frontend                   && npm run dev          # http://localhost:5173
```

Vite proxies `/api` → 8000, `/analytics-api` → 8010, `/ingest-api` → 8020, so
the browser never needs to know a backend address and there is no CORS setup.

### Containers

```bash
cd backend/chatbot_pipeline && docker compose up -d
```

A Dockerfile per deployable; the compose file builds the frontend image from
`../../frontend`.

---

## Operating modes

Selected in the sidebar; the active mode is validated live and the UI reports
what is missing rather than starting and silently doing nothing.

| Mode | Source | Network | LLM |
|---|---|---|---|
| **Sample** | bundled corpus in a local Neo4j | online | Groq / OpenRouter / Ollama |
| **Production** | DNIF → Kafka, real Neo4j | **air-gapped** | Ollama only |
| **Custom** | your uploaded file | online | Groq / OpenRouter / Ollama |

Production mode will not render data until it is actually connected: showing
the sample sandbox under a production label is how someone ends up making a
decision on the wrong data.

---

## The Universal Event Schema

26 fields, 17 guaranteed on every event, each mapped to its **ECS** and **OCSF**
equivalent — a schema universal only to itself would just be one more
proprietary format.

Served at `GET /api/analytics/schema`, rendered on the **Schema & export** page.
`GET /api/analytics/schema/coverage` measures the live graph against the
schema's own claims and flags any required field below 100%, so the document
can be proved wrong rather than merely asserted.

## SIEM / Data Lake export

```
GET /api/analytics/export?format=ndjson|ecs|cef|csv
```

Streams one event at a time — memory is flat whether the export is 200 events
or 200 million. Takes the same filter as the dashboard, and carries the lineage
fields (`raw_message`, `source_file`, `source_record`) by default, because an
event that reaches a SIEM without its original has lost what makes it usable
for a forensic question.

---

## Docs

| | |
|---|---|
| [ULPF_CONFORMANCE.md](ULPF_CONFORMANCE.md) | Requirement-by-requirement evidence, and the gaps |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Production deployment and scaling notes |
