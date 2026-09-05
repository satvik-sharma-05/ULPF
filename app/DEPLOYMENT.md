# Offline / Airgapped Deployment Guide

How to get from "code on a Windows laptop" to "a running chatbot on the
airgapped VM, backed by the fully ingested graph." Three machines are
involved:

| Machine | Role | Internet? |
|---|---|---|
| Windows laptop | Builds all Docker images | Yes (needed here) |
| Airgapped Ubuntu VM | Runs Neo4j, Ollama (native), the ingestion job, and the chatbot | No, never |

Ollama + `qwen3:14b` are assumed already installed natively on the VM
(systemd / `ollama serve`, port 11434) — nothing to build or transfer for that.

---

## 0. Confirmed answers to two setup questions

**Can `output.json.gz` be ingested with embeddings?**
Yes — confirmed in code, not assumed. `core/jsonio.py` transparently gzip-reads
any path ending in `.gz` (`stream_read_records`), and `batch/ingest_to_neo4j.py`
accepts that same `.gz` path via `--input`. There are two ways to add
embeddings:

1. `ingest_to_neo4j.py --input output.json.gz --embed` — loads BAAI/bge-m3 and
   embeds inline during the same pass that writes structure. **Not
   recommended for this corpus size**: there is no checkpoint/resume logic in
   this script at all (verified — no `checkpoint`/`resume` code path exists in
   `ingest_to_neo4j.py`). If it dies at record 80,000,000 of ~117,000,000, a
   rerun starts over from record 1, re-embedding everything already done.
2. **Structure now, embeddings later, resumable** (what the deployment steps
   below actually use): ingest structure first with `ingest_to_neo4j.py`
   (no `--embed`, minutes not days), then run `batch/embed_logs.py` — a
   separate pass that queries Neo4j directly for `:Log` nodes still missing
   `.embedding`, in keyset-paginated batches, checkpointed to
   `state/embed_checkpoint.json` after every batch. Kill it, reboot the VM,
   rerun the same command — it resumes exactly where it stopped.

Use path 2. It's what `offline_ingest_structure.sh` +
`offline_ingest_embeddings.sh` already do.

**Do the Docker images have every dependency they need?**
Yes — confirmed by reading both Dockerfiles and requirements.txt files, not
assumed:

| Image | Installs | Bakes in (weights) |
|---|---|---|
| `log-ingestion-pipeline` | CPU-only torch, `requirements.txt` (`requests`, `confluent-kafka`, `neo4j`, `sentence-transformers`) | `BAAI/bge-m3` |
| `chatbot-backend` | CPU-only torch, `sentence-transformers` (explicit `RUN`, since it's commented-optional in `requirements.txt` for non-Docker use), `requirements.txt` (`neo4j`, `requests`, `fastapi`, `uvicorn`, `python-dotenv`) | `BAAI/bge-m3` **and** `BAAI/bge-reranker-v2-m3` |
| `chatbot-frontend` | Node build stage → nginx runtime | n/a (static assets) |
| `neo4j:5-community` | Official image, pulled as-is | n/a |

Every one of these is self-contained. Once loaded, the VM needs **zero**
network access — not to Hugging Face, not to PyPI, not to Docker Hub — for
anything covered here.

**A note on where the data actually lives:** Neo4j is disk-backed, not an
in-memory store. RAM (heap + page cache) is only a cache for speed — every
node, relationship and embedding vector is persisted to Neo4j's own data
files on disk, inside the `sandbox_neo4j_data` Docker volume. The sizing table
below is that on-disk footprint, not the size of `output.json.gz` (which is
just a temporary input file, separate from wherever Neo4j itself stores data).

### Hardware sizing: three scenarios

Based on the actual measured corpus (116,889,850 records) and the confirmed
fact that embeddings are stored as 1024 × 8-byte doubles per log (Neo4j's
`FLOAT` is always 64-bit, no 32-bit option):

| Scenario | Disk | RAM | CPU | Time |
|---|---|---|---|---|
| **Structure only** (no `--embed`) | ~150-200GB *(uncompressed corpus is ~146GB; Neo4j stores `message`+`normalized_message`+`raw_message` as three separate properties per log)* | 16-32GB (6-10G heap + 6-10G pagecache) | 8+ cores | **~4-12 hours** *(estimate)* |
| **Full corpus + embeddings** | **~1.3-1.6TB** *(confirmed: 116,889,850 × 1024 × 8 bytes ≈ 958GB for the embedding property alone, before the vector index's own HNSW structure and the ~150-200GB of structure data)* | 32GB minimum, 64GB comfortable | 16+ cores (torch CPU threading scales roughly linearly) | **~1.5-3 weeks** *(estimate — consistent with `embed_logs.py`'s own docstring: "realistically days, not hours, even on 8-16 cores")* |
| **Quick test** (`--limit 1000 --embed`) | <50MB | Still needs ~4-6GB free to load BAAI/bge-m3 — the model's RAM cost doesn't scale down with record count, only the compute time does | Any modern multi-core CPU | **~1-2 minutes** |

**On a 16-core/32GB/200GB box**: structure-only fits comfortably; full corpus
+ embeddings does not (short by roughly 6-8x on disk alone, independent of
the multi-week runtime); the quick test fits trivially. If full-scale
embeddings are ever needed, plan for ≥1.5TB disk and ≥64GB RAM on whichever
machine actually holds it — and if any GPU is available, even a modest one,
running just the embedding pass there instead of on CPU is typically
10-50x faster.

---

## 1. Build the images (Windows laptop, needs internet)

```powershell
cd C:\Users\usernkn\Downloads\LA_transfer\LA_transfer\LA
.\build_offline_images.ps1
```

Requires Docker Desktop, running. It builds real Linux containers via its
WSL2/Hyper-V backend regardless of the Windows host, so the images run
identically on the Ubuntu VM — no intermediate Linux build machine needed.

What it does, in order:

1. Builds `log-ingestion-pipeline:latest` (downloads ~3GB: torch + BAAI/bge-m3)
2. Pulls `neo4j:5-community`
3. Builds `chatbot-backend:latest` (downloads ~4GB: torch + BAAI/bge-m3 +
   BAAI/bge-reranker-v2-m3)
4. Builds `chatbot-frontend:latest` (React build + nginx — also serves the
   Analytics dashboard tab, same SPA)
5. Builds `analytics-backend:latest` (no baked models — just FastAPI + the
   Neo4j driver, so this one builds in seconds)
6. Saves all five as `.tar` files **in place**, inside each pipeline's own
   folder — not a separate output directory — so the VM-side scripts below
   find them exactly where they already expect, unmodified:
   - `ingestion_pipeline\ingestion_image.tar`
   - `ingestion_pipeline\neo4j_image.tar`
   - `chatbot_pipeline\chatbot_backend_image.tar`
   - `chatbot_pipeline\chatbot_frontend_image.tar`
   - `analytics_pipeline\analytics_backend_image.tar`

Expect this to take a while and use significant bandwidth (torch +
sentence-transformers + two ~2GB models, downloaded twice — once per Python
image). Rerun it whenever code under `ingestion_pipeline/`, `chatbot_pipeline/`,
`analytics_pipeline/` or `frontend/` changes; the tars are a snapshot, not a
live link.

---

## 2. Transfer to the airgapped VM

Copy across the air gap:

- The **whole `LA/` folder** (now containing the 4 `.tar` files from step 1,
  plus all the code and `docker-compose.yml` files)
- `output.json.gz` (the parsed corpus) — placed inside `ingestion_pipeline/`

---

## 3. On the VM — ingest the graph

Neo4j is not yet running at this point; the scripts below start it. Ollama
must already be running natively with `qwen3:14b` pulled.

### 3a. Full corpus (production)

```bash
cd LA/ingestion_pipeline

# Loads ingestion_image.tar + neo4j_image.tar, starts Neo4j (sized for this
# VM's RAM), ingests output.json.gz - structure, typed entities, all
# relationships - WITHOUT embeddings. ~4-12 hours, not days. The graph is
# queryable via text2cypher / query_template / pattern_match / graph_neighbors
# / path_traversal / parent_child_retrieval the moment this finishes - see
# the sizing table above and chatbot_pipeline/FEATURES.md for exactly what
# works without embeddings (short answer: everything except true semantic
# search, which auto-degrades to fulltext instead of failing).
./offline_ingest_structure.sh

# Adds BAAI/bge-m3 embeddings to every :Log node, working directly against
# Neo4j. This is the long one - realistically 1.5-3 weeks on CPU for ~117M
# logs, and needs ~1.3-1.6TB of disk (see the sizing table above) - do NOT
# start this on a 200GB disk. Safely interruptible and resumable
# (state/embed_checkpoint.json). Run it under screen/tmux so it survives an
# SSH disconnect:
screen -S embed
./offline_ingest_embeddings.sh
# [Ctrl-A then D to detach; `screen -r embed` to reattach later]
```

`vector_search` and full GraphRAG quality improve progressively as the
embeddings pass makes headway — nothing is blocked in the meantime, it
automatically falls back to fulltext search until embeddings exist.

### 3b. Quick test-scale ingestion (validate the chatbot in minutes, not hours)

Skip both scripts above and ingest just a slice of the corpus instead — the
right choice when the goal is testing the chatbot, not running production
scale. `--embed`'s lack of checkpointing (the reason it's unsuitable for the
full corpus) doesn't matter at this size; it finishes before it could ever
need to resume.

**Measured, not estimated:** on the CPU-only build machine used to test this
project, embedding + writing 5,000 records took 2,477s (~0.5s/record) end to
end (model load + embed + Neo4j write) — noticeably slower than earlier
guidance suggested. Budget roughly `0.5s * LIMIT` on similar hardware (so
1,000 records ≈ 8 minutes, 5,000 ≈ 40 minutes); faster or slower CPUs will
shift this, but treat "1-2 minutes" as optimistic, not a stall indicator.

```bash
cd LA/ingestion_pipeline

# Wraps the docker compose run below: starts Neo4j, wipes existing data,
# ingests the first LIMIT records of output.json.gz WITH embeddings.
./offline_ingest_sample.sh                  # first 5000 records (default)
LIMIT=1000 ./offline_ingest_sample.sh       # or any other slice size
```

Equivalent by hand, if you want to see/adjust each step:

```bash
docker compose --profile sandbox up -d sandbox-neo4j
docker compose run --rm \
    -v "$(pwd)/output.json.gz:/app/data/output.json.gz:ro" \
    consumer \
    python -m batch.ingest_to_neo4j \
        --input /app/data/output.json.gz \
        --limit 1000 \
        --embed \
        --wipe \
        --neo4j-uri bolt://sandbox-neo4j:7687 \
        --neo4j-user neo4j \
        --neo4j-password "${NEO4J_PASSWORD:-sandboxpassword}" \
        --neo4j-database neo4j
```

Caveat: `--limit` takes records in file order, so a small slice likely spans
only 1-2 time windows and a handful of hosts — plenty to validate
`vector_search`, `fulltext_search`, `text2cypher`, `temporal_window`, and the
planner end-to-end, but traversal-heavy tools may return sparse results
simply because that slice doesn't have much cross-entity structure. Rerun
with a larger `LIMIT` (still wipes first) if richer traversal results are
needed. A 5,000-record run produced 4,667 `:Log` nodes (some records merge
via the stable `id` field), all with embeddings, plus a full spread of typed
entities (`Thread`, `Trace`, `Host`, `Component`, ...) and their relationships
— confirmed by direct Neo4j query, not just the ingest script's own report.

---

## 4. On the VM — start the chatbot

Can be done immediately after step 3's *structure* ingest finishes; doesn't
need to wait for the embeddings pass to complete.

```bash
cd ../chatbot_pipeline
./offline_start_chatbot.sh
```

Loads `chatbot_backend_image.tar` + `chatbot_frontend_image.tar`, checks the
native Ollama is reachable with `qwen3:14b`, starts both containers
(`network_mode: host`, so they reach Neo4j and Ollama via the VM's own
`localhost`), and waits for the backend health check.

---

## 4b. On the VM — start the analytics dashboard

Independent of step 4 — can be started before, after, or without the chatbot
stack running at all, since it's a separate deployable that only ever reads
Neo4j (no LLM, no Ollama check):

```bash
cd ../analytics_pipeline
./offline_start_analytics.sh
```

Loads `analytics_backend_image.tar`, starts it (`network_mode: host`, same
reasoning as the chatbot stack), and waits for its health check. The frontend
container from step 4 already proxies `/analytics-api/*` to it (see
`frontend/nginx.conf`), so once both are up the dashboard is the **Analytics**
tab at the same frontend URL below — no separate URL to remember.

---

## 5. Chat & Analytics

```
Frontend         : http://<vm-ip>:3000   (Analytics tab + Chat tab)
Chatbot backend  : http://<vm-ip>:8000/api/health
Analytics backend: http://<vm-ip>:8010/api/analytics/health
```

Pick a mode (`text2cypher`, `graphrag`, `hybrid`, or `Auto (planner)`) and
ask a question. See `chatbot_pipeline/FEATURES.md` for exactly what each mode
and tool can and can't do. The **Analytics** tab needs no interaction — it
loads volume/rate/severity charts and rule-based insights as soon as
structure ingest (step 3a/3b) has written any `:Log` nodes.

---

## Rebuilding / updating later

- Code changed → rerun `build_offline_images.ps1` on the Windows laptop,
  re-transfer the updated `.tar` files, `docker compose down` then re-run the
  relevant offline script on the VM (it reloads and restarts automatically).
- Embeddings pass interrupted → just rerun `./offline_ingest_embeddings.sh`
  on the VM; it resumes from `state/embed_checkpoint.json`.
- New logs to add → rerun `parse_logs.py` to regenerate `output.json.gz`,
  transfer it, rerun `./offline_ingest_structure.sh` (existing `:Log` nodes
  are matched and updated via `ON MATCH SET`, not duplicated).
