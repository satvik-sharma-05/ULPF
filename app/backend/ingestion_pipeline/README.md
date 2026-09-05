# Ingestion Pipeline

Parses VMware SDDC / Kubernetes / Windows / Linux logs into a **rich Neo4j
knowledge graph** — typed entity nodes, entity-to-entity relationships, and
1024-dim embeddings for semantic search.

**Two pipelines, one shared core.**

```
core/       parser, entity extraction, embeddings, graph writer   ← shared
├── realtime/   DNIF → Kafka → parse → embed → Neo4j              PIPELINE 1
└── batch/      logs/ → parse → embed → Neo4j                     PIPELINE 2
```

Both use the same `core/`, so both produce an identical graph. They differ only
in where records come from — a live stream, or files on disk.

---

## Layout

```
ingestion_pipeline/
├── core/                     ← SHARED BY BOTH PIPELINES
│   ├── config.py               all tunables, env-var driven
│   ├── graph_schema.py         SINGLE SOURCE OF TRUTH for the graph model
│   ├── parser.py               raw log → normalized record
│   ├── multiline.py            reassembles multi-line records
│   ├── embeddings.py           BAAI/bge-m3, 1024-dim
│   ├── neo4j_writer.py         batched, 3-layer graph writes
│   ├── jsonio.py               streaming JSON reader/writer
│   └── parsers/                one detector per log family
│       ├── entities.py           raw log → TYPED graph entities
│       ├── registry.py           priority-ordered detector list
│       └── nsx / esxi / vmacore / aria_automation / vrops_casa_horizon /
│           windows_security / linux_syslog / platform_services / generic
│
├── realtime/                 ← PIPELINE 1: live stream
│   ├── run.py                  entrypoint: --role producer|consumer
│   ├── producer_service.py     DNIF → Kafka
│   ├── consumer_service.py     Kafka → parse → embed → Neo4j
│   ├── dnif_api.py  kafka_producer.py  kafka_consumer.py  base_service.py
│
├── batch/                    ← PIPELINE 2: files on disk
│   ├── run_pipeline.py         logs → Neo4j in one command
│   ├── parse_logs.py           logs → output.json
│   ├── ingest_to_neo4j.py      output.json → Neo4j
│   └── validate_extraction.py  check the entities are actually correct
│
├── logs/                     the corpus (91 folders × ~107 files, ~33GB)
├── Dockerfile  docker-compose.yml  entrypoint.sh  .env.example
└── requirements.txt
```

---

## PIPELINE 2 — batch (start here)

The parser has **zero third-party dependencies** — pure standard library — so
parsing runs on a bare Python 3.10+ install.

```bash
# One step: parse and load straight into the graph
python -m batch.run_pipeline --path ./logs --embed

# Two steps: keep the parsed data in between
python -m batch.parse_logs      --path ./logs --out output.json
python -m batch.ingest_to_neo4j --input output.json --embed
```

Prefer the two-step form for a big run: parsing is CPU-bound and needs no
network, ingestion needs a reachable database, and they fail for unrelated
reasons. Keeping `output.json` means a Neo4j problem doesn't cost you a
multi-hour reparse.

| Flag | `parse_logs` | `ingest_to_neo4j` | `run_pipeline` |
|---|:-:|:-:|:-:|
| `--path` / `--input` | ✓ | ✓ | ✓ |
| `--limit`, `--workers` | ✓ | ✓ | ✓ |
| `--embed` | | ✓ | ✓ |
| `--drop-raw` (halves output size) | ✓ | | |
| `--wipe` (**destructive**) | | ✓ | ✓ |
| `--neo4j-uri/-user/-password/-database` | | ✓ | ✓ |

`--path` is a variable — the full corpus, one export folder, or a single file.

Both `parse_logs` and `run_pipeline` finish with a **coverage report**: records
per detector, per source type, entity counts by type, and a sample of anything
that fell through to `generic_fallback`. That sample is the to-do list for the
next detector.

> `output.json` is a real JSON array written **one record per line**, so it
> stays valid for any JSON tool while remaining streamable — the corpus is tens
> of millions of records and `json.load()` on it would exceed RAM. It can also
> exceed the size of the logs themselves: use `--drop-raw` if disk is tight.

### Checking the extraction is *correct*, not just large

```bash
python -m batch.validate_extraction --path ./logs --sample 20000
```

Node counts prove the graph is big, not that it's right — a regex that mistakes
an opID for a username inflates the count and quietly corrupts every answer
built on it. This samples the corpus and reports per-type value counts,
examples in the context of the line they came from, and a **SUSPICIOUS**
section flagging values that fail their own type's expected shape, look
mis-typed, or have pathological cardinality. Read that section first.

---

## PIPELINE 1 — realtime

```bash
python -m realtime.run --role producer     # DNIF  → Kafka (raw-logs)
python -m realtime.run --role consumer     # Kafka → parse → embed → Neo4j
```

Kafka is a durable buffer between fetching and parsing, so the consumer can
crash, restart or scale out without losing a log already pulled from DNIF. The
consumer commits its Kafka offset only *after* a log is persisted, so a crash
mid-batch causes a safe re-delivery rather than silent loss.

```bash
cp .env.example .env && nano .env      # NEO4J_PASSWORD, DNIF credentials
docker compose up -d                    # producer + consumer
docker compose up -d --scale consumer=3 # scale the parse/write side
docker compose --profile sandbox up -d  # local Kafka + Neo4j for dev
```

If DNIF isn't configured yet, the producer replays real lines from
`DNIF_REPLAY_PATH` (default `./logs`) so the path is exercised with real log
shapes rather than invented ones.

Run the batch pipeline inside the same image:

```bash
docker compose run --rm consumer python -m batch.run_pipeline --path /app/logs
```

---

## The graph

`core/graph_schema.py` is the single source of truth — the writer builds from
it, and the chatbot introspects the live database, so nothing can drift.

**Structural backbone** (every log):

```
(:Host)-[:EMITTED]->(:Log)-[:HAS_SEVERITY]->(:Severity)
(:Log)-[:FROM_SOURCE]->(:SourceType)     (:Log)-[:ON_DAY]->(:Day)
(:Log)-[:EMITTED_BY]->(:Process)         (:Process)-[:RUNS_ON]->(:Host)
(:Log)-[:FROM_COMPONENT]->(:Component)   (:Component)-[:PART_OF]->(:Process)
```

**Typed entities** — 34 labels instead of one opaque `:Entity`:

| Domain | Labels |
|---|---|
| Identity | `:User` `:SecurityID` `:Domain` `:Session` |
| Network | `:IPAddress` `:Port` `:Endpoint` `:URL` `:DnsName` `:MacAddress` `:NetworkInterface` |
| vSphere | `:Device` `:Datastore` `:VM` `:ManagedObject` `:ApiMethod` `:Package` |
| Work tracking | `:Task` `:Operation` `:Trace` `:UUID` `:Thread` `:Logger` |
| Kubernetes | `:Pod` `:Namespace` `:K8sService` `:Container` |
| Files/processes | `:File` `:Executable` `:Syscall` `:ApparmorProfile` `:SystemdUnit` |
| Storage/messaging | `:Bucket` `:ObjectKey` `:Queue` |
| Diagnostics | `:ErrorCode` `:EventID` `:GCCycle` |

**Derived relationships** — entity-to-entity edges, so a path between two facts
doesn't have to detour through a `:Log`:

```
(:User)-[:ACCESSED]->(:Host)          (:Session)-[:BELONGS_TO]->(:User)
(:User)-[:MEMBER_OF]->(:Domain)       (:SecurityID)-[:IDENTIFIES]->(:User)
(:Device)-[:ATTACHED_TO]->(:Host)     (:Datastore)-[:MOUNTED_ON]->(:Host)
(:VM)-[:HOSTED_ON]->(:Host)           (:Task)-[:EXECUTED_ON]->(:Host)
(:Executable)-[:RAN_ON]->(:Host)      (:Operation)-[:SEEN_ON]->(:Host)
(:Pod)-[:IN_NAMESPACE]->(:Namespace)  (:Pod)-[:OF_SERVICE]->(:K8sService)
(:ObjectKey)-[:STORED_IN]->(:Bucket)  (:MacAddress)-[:ASSIGNED_TO]->(:NetworkInterface)
(:Package)-[:INSTALLED_ON]->(:Host)   (:ApparmorProfile)-[:ENFORCED_ON]->(:Host)
```

Only the *first* entity of each type per log takes part in entity-to-entity
edges — a log with 8 users and 8 devices would otherwise emit 64 edges the log
doesn't actually support.

Low-cardinality values (HTTP status, method) stay as `:Log` **properties**, not
nodes: a `(:HttpStatus {code:200})` over this corpus would accumulate tens of
millions of relationships — a supernode that slows every traversal touching it
and answers nothing a property can't.

### Example queries

```cypher
// Users seen on more than one host
MATCH (u:User)-[:ACCESSED]->(h:Host)
WITH u, collect(h.name) AS hosts
WHERE size(hosts) > 1
RETURN u.name, hosts ORDER BY size(hosts) DESC LIMIT 25;

// Storage devices named in errors on one day
MATCH (l:Log)-[:ON_DAY]->(:Day {date: '2026-06-15'})
MATCH (l)-[:REFERENCES_DEVICE]->(d:Device)
WHERE l.severity_score <= 3
RETURN d.id, count(l) AS mentions ORDER BY mentions DESC LIMIT 25;

// Everything that happened under one operation id
MATCH (o:Operation {id: '4e9e0d7b'})<-[:PART_OF_OPERATION]-(l:Log)
RETURN l.timestamp, l.hostname, l.severity, l.message ORDER BY l.timestamp;

// What a host has attached to it
MATCH (h:Host {name: 'MD-H-SY2-BL07'})<-[r]-(e)
RETURN type(r), labels(e)[0], count(*) ORDER BY count(*) DESC;
```

---

## The parser

One small detector per log family in `core/parsers/`, tried in
`registry.py`'s priority order (specific before generic, so a rare shape is
never shadowed). Covers NSX, ESXi (hostd/vpxa/fdm/vmkernel/envoy), Site
Recovery Manager, vCenter, Aria Automation, vROps, CASA/Horizon, MinIO,
Kubernetes/CoreDNS/kubelet/Squid, PostgreSQL, Windows Security auditing, JVM
GC, and Linux syslog/auditd/kernel.

Anything matching nothing still gets a best-effort generic extraction — nothing
is dropped, and nothing raises past `parser.parse()`.

`multiline.py` reassembles records that span physical lines (a Windows Security
Event body, a Java stack trace) before parsing, using the rule syslog shippers
use: a line starts a new record only if it looks like a record header.

Every log gets a `confidence` score (0–1) reflecting *only* what was genuinely
extracted — defaulted fields never contribute, so it's a trustworthy triage
signal rather than a padded number.

### Normalized record

| Field | Meaning |
|---|---|
| `id` | `log-<md5>` of timestamp+host+process+raw — stable, so re-ingest MERGEs |
| `timestamp`, `day`, `hostname` | best-effort; `unknown-host` only if truly absent |
| `source_type` | `NSX`, `ESXi`, `vCenter`, `SRM`, `AriaAutomation`, `vROps`, `CASA`, `Horizon`, `MinIO`, `Kubernetes`, `WindowsSecurityAudit`, `JVM_GC`, `LinuxAudit`, `LinuxKernel`, `PostgreSQL`, `Squid`, `Syslog`, `HMS`, `Unknown` |
| `process`, `pid`, `component`, `subcomponent` | which daemon/service/class emitted it |
| `severity`, `severity_score` | EMERGENCY..DEBUG / 1..7 |
| `message`, `normalized_message` | cleaned text / embedding-ready summary |
| `attributes` | free-form dict of format-specific fields |
| `entities` | **typed**: `{'type': 'user', 'value': 'root'}` |
| `confidence`, `matched_format` | triage signal + which detector matched |
| `raw_message` | untouched original |

---

## Embeddings

BAAI/bge-m3, 1024-dim, on `(:Log).embedding` behind a native `VECTOR INDEX`.
Neo4j is the only datastore — PostgreSQL was removed, since the graph already
holds the logs, the relationships and the vectors.

- **Batch**: opt-in via `--embed` (roughly triples ingest time).
- **Realtime**: on by default via `ENABLE_EMBEDDINGS`.

Without them the chatbot still works — GraphRAG falls back to the full-text
index — but semantic search is what makes "why did X fail" questions work.

The model is baked into the Docker image at build time (`HF_HUB_OFFLINE=1`), so
the pipeline needs no network at runtime.

---

## Deployment

### Fresh VM, no Docker

```bash
python3 --version                    # 3.10+
python3 -m batch.parse_logs --path ./logs --out output.json
pip install neo4j
python3 -m batch.ingest_to_neo4j --input output.json
```

### Docker

```bash
cp .env.example .env && nano .env
docker compose --profile sandbox up -d --build   # local Kafka + Neo4j
docker compose ps
docker compose logs -f consumer
docker compose down          # add -v to also wipe sandbox data
```

First build downloads CPU-only PyTorch and bakes in the embedding model
(~2–3GB); needs internet during build, 5–15 min, one time only.

### Air-gapped

Build the image on a connected machine, `docker save` it, transfer, then
`docker load`. Every service shares the `log-ingestion-pipeline:latest` tag, so
Compose starts containers instead of rebuilding. Note the batch pipeline needs
none of this — the parser is pure standard library.

---

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `database 'logs_db' not found` | Community Edition has only one database | `NEO4J_DATABASE=neo4j` |
| Cannot connect to Neo4j | No route to that subnet | `nc -zv <host> 7687` |
| Ingest slows as it runs | Missing constraints | Auto-created on connect; verify with `SHOW CONSTRAINTS` |
| `output.json` filled the disk | Corpus is 33GB | Re-run with `--drop-raw` or `--limit` |
| Vector search returns nothing | Ingested without `--embed` | Re-run ingest with `--embed` |
| Many `generic_fallback` records | Format not covered | Find it in the parse report's sample list, add a detector in `core/parsers/` |
| Entity values look wrong | Over-broad regex | `python -m batch.validate_extraction`, read the SUSPICIOUS section |
| `permission denied: entrypoint.sh` | Executable bit lost in transfer | `chmod +x entrypoint.sh` |
