# Universal Log Pre-processing Framework (ULPF)

Ingests logs from any source or format, normalizes them into one declared
event schema without discarding the original, and makes the result queryable,
analysable and exportable to a SIEM or Data Lake.

Conformance against each ULPF requirement (a)-(k), with measured evidence:
**[ULPF_CONFORMANCE.md](ULPF_CONFORMANCE.md)**

Three independent pipelines that share nothing but a Neo4j database.

```
                    ┌─────────────────────────────────────────┐
   DNIF API ──────▶ │  ingestion_pipeline/realtime/           │
                    │  DNIF → Kafka → parse → embed → Neo4j   │──┐
                    └─────────────────────────────────────────┘  │
                                                                 ▼
                    ┌─────────────────────────────────────────┐ ┌────────┐
   logs/ ─────────▶ │  ingestion_pipeline/batch/              │▶│ Neo4j  │
   (33GB on disk)   │  logs → parse → embed → Neo4j           │ └────────┘
                    └─────────────────────────────────────────┘   │    │
                                                                   ▼    ▼
      ┌──────────────────────────────────────┐   ┌──────────────────────────────────┐
      │  chatbot_pipeline/                    │   │  analytics_pipeline/             │
      │  classifier → text2Cypher / GraphRAG  │   │  volume/rate/severity + insights │
      └──────────────────────────────────────┘   └──────────────────────────────────┘
        ▲ "why did X fail?"                          ▲ "how many logs per day?"
```

| Folder | What it does | Needs |
|---|---|---|
| [`ingestion_pipeline/`](ingestion_pipeline/) | Builds the graph. Two entry paths, one shared core. | Neo4j; Kafka+DNIF for the realtime path only |
| [`chatbot_pipeline/`](chatbot_pipeline/) | Answers questions about the graph. | A populated Neo4j. Nothing else. |
| [`analytics_pipeline/`](analytics_pipeline/) | Dashboard: volume, rate, severity, alerts, and rule-based insights over the graph. | A populated Neo4j. Nothing else. |
| [`frontend/`](frontend/) | React UI with two tabs - **Analytics** (dashboard) and **Chat** - talking to the two backends above. | Both backends running. |

They are deliberately separate deployables: neither `chatbot_pipeline` nor
`analytics_pipeline` imports the ingestion code or each other, so any one of
them can be restarted, scaled or redeployed without touching the others.
Their only contract is the graph itself - `chatbot_pipeline` and
`analytics_pipeline` never write to it, only read.

---

## The two ingestion pipelines

Both share `ingestion_pipeline/core/` — the same parser, entity extraction,
embeddings and graph writer — so **both produce an identical graph**. They
differ only in where records come from.

### 1. Realtime — `ingestion_pipeline/realtime/`

`DNIF API → Kafka → parse → embed → Neo4j`

For the live stream. Kafka sits between fetching and parsing as a durable
buffer, so the parse/write stage can crash or scale to several replicas without
losing a log that was already pulled from DNIF. Runs as two roles:

```bash
cd ingestion_pipeline
python -m realtime.run --role producer     # DNIF  → Kafka
python -m realtime.run --role consumer     # Kafka → parse → embed → Neo4j

docker compose up -d --scale consumer=3    # or containerized
```

### 2. Batch — `ingestion_pipeline/batch/`

`logs/ → parse → embed → Neo4j`

For log files already on disk (the 33GB / 9,186-file corpus in
`ingestion_pipeline/logs/`). No Kafka, no DNIF.

```bash
cd ingestion_pipeline

# one step: straight into the graph
python -m batch.run_pipeline --path ./logs --embed

# or two steps, keeping the parsed data
python -m batch.parse_logs      --path ./logs --out output.json
python -m batch.ingest_to_neo4j --input output.json --embed
```

---

## Quick start

```bash
# 1. A Neo4j to write into (skip if you have one)
cd ingestion_pipeline
docker compose --profile sandbox up -d sandbox-neo4j

# 2. Build the graph from the corpus
pip install neo4j
export NEO4J_URI=bolt://localhost:7687 NEO4J_PASSWORD=sandboxpassword NEO4J_DATABASE=neo4j
python -m batch.run_pipeline --path ./logs --limit 50000

# 3. Ask it questions
cd ../chatbot_pipeline
python chat.py --ask "which hosts produced the most errors?"

# 4. Or see it as a dashboard
cd ../analytics_pipeline
pip install -r requirements.txt
uvicorn api:app --port 8010
# then run the frontend (see frontend/README below) and open its Analytics tab
```

See each pipeline's own README for details, deployment and troubleshooting.

## Embeddings

BAAI/bge-m3, 1024-dim, stored on `(:Log).embedding` in Neo4j behind a native
`VECTOR INDEX`. There is no second datastore — PostgreSQL was removed, since
the graph already holds the logs, the relationships *and* the vectors.

Embeddings are opt-in on the batch path (`--embed`, it roughly triples ingest
time) and on by default on the realtime path (`ENABLE_EMBEDDINGS`). Without
them the chatbot still works — GraphRAG falls back to the full-text index —
but semantic search is what makes "why did X fail" questions work well.

**The model must match on both sides.** `EMBEDDING_MODEL`/`EMBEDDING_DIM` in
`chatbot_pipeline` must be whatever produced the stored vectors; the vector
index accepts any 1024-float query and cannot tell you it came from a
different embedding space.
