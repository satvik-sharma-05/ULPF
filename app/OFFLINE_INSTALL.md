# Air-gapped installation

For a Linux x86_64 VM with **no internet access**. Nothing here downloads
anything.

## What you need on the VM

- Docker Engine 24+ and the Compose plugin (`docker compose version`)
- ~60GB free disk
- 16GB RAM recommended (the LLM and the embedding model are the consumers)

That is the entire prerequisite list. No Python, no `pip`, no Node, no `npm`,
no Hugging Face download - every dependency is already inside the images.

## Assembling the package before transfer

The images are in `images/*.tar` (~11.5GB). One thing is **not** in them and
has to be copied in on the build machine before the folder is transferred:

```powershell
# the Ollama weights, from the build machine's model store
robocopy "$env:USERPROFILE\.ollama\models" ".ackend\ollama\models" /E
```

They are mounted rather than baked because baking produced a 26GB image *and*
a 26GB tar - the same weights shipped twice. Mounted, they travel once. The
trade is that they can be left behind during a copy, which is why
`load_images.sh` checks for them and says so rather than letting the chat fail
on the first question.

## Install

```bash
cd final_project
./load_images.sh                   # docker load + model-store preflight
```

Set the one value that is genuinely yours:

```bash
# backend/*/.env  - every line marked CHANGE_ME
NEO4J_PASSWORD=<your Neo4j password>
```

Compose needs it too, for the bundled Neo4j container:

```bash
export NEO4J_PASSWORD='<same password>'
docker compose up -d
```

Open **http://\<vm\>:3000**.

## What is already configured

| Setting | Value |
|---|---|
| Neo4j | `bolt://neo4j.internal.example:7687` |
| Kafka brokers | `kafka-1.internal.example:9092, kafka-2.internal.example:9092, kafka-3.internal.example:9092` |
| Kafka topics | `raw-logs`, `parsed-logs`, `insights` |
| Consumer group | `log-ingestor-group` |
| LLM provider | `ollama` - the only one reachable air-gapped |
| Embedding model | `BAAI/bge-m3`, baked into the image |
| Reranker | `BAAI/bge-reranker-v2-m3`, baked in |
| Speech to text | `faster-whisper` base, beam 5 + domain vocabulary |
| Text to speech | Piper `en_US-lessac-medium` |

**Not shipped, by design:** `NEO4J_PASSWORD` (the only one held here is for a
local development instance - wrong for your cluster and a credential leak),
`GROQ_API_KEY` / `OPEN_ROUTER_API_KEY` (cloud LLMs, unreachable air-gapped and
belonging to the originating developer), and `DNIF_API_KEY` / `DNIF_API_URL`.

If you already run Neo4j at `neo4j.internal.example`, comment the `neo4j` service out of
`docker-compose.yml`. For the self-contained deployment instead, point
`NEO4J_URI` at `bolt://neo4j:7687`.

## Why images rather than a dependency bundle

A Docker image is a Linux image whatever built it, so images built on a Windows
laptop with Docker Desktop run unchanged on a Linux VM. Everything resolves at
build time on a machine that has internet. That is why this package contains no
Python wheelhouse and no `node_modules` - they would be a second, redundant
copy of what is already inside the images.

The one thing that does *not* travel is CPU **architecture**. These images are
`linux/amd64`; on an ARM host, rebuild with `--platform linux/amd64`.

## Performance on CPU

Measured on this hardware, so you can size expectations:

| Operation | Time |
|---|---|
| Parsing | **4,702 records/sec/core** (~406M events/day/core) |
| Full-corpus export, 24,515 events | 12s, 31.5MB |
| Chat answer, local `qwen3:14b` | 45s+ |
| Speech transcription, 5s of audio | ~4s |
| Image understanding, one screenshot | 35-40s |

`qwen3:14b` on CPU is slow for chat and too slow for document generation. A
smaller local model (`qwen3:4b`, `llama3.2:3b`) is worth considering if
response time matters more than answer quality.

## Rebuilding the images

On an internet-connected Windows machine with Docker Desktop:

```powershell
.\build_images.ps1
```

Writes `images/*.tar`. Rebuild whenever code under `backend/` or `frontend/`
changes - the tars are a snapshot, not a link to the source.

## Ingesting logs

Drop files into `data/logs/` (mounted into the ingestion container):

```bash
docker compose exec ingestion python -m batch.run_pipeline --path /app/logs --embed
```

Or use **Modes -> Custom logs** in the UI to upload a single file.

For a format nothing recognises, draft a detector from the data itself:

```bash
docker compose exec ingestion \
  python -m batch.synth_parser --file /app/logs/unknown.log --only-unmatched
```

## Verifying the install

```bash
curl http://localhost:8010/api/analytics/health
curl http://localhost:8000/api/health
curl http://localhost:8020/api/ingest/health
curl http://localhost:8010/api/analytics/schema/coverage   # violations: 0
```

The **Schema & export** page is the quickest end-to-end check: it reads the
live graph, measures it against the Universal Event Schema, and reports any
required field not present on every event.
