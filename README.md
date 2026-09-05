# Universal Log Pre-processing Framework (ULPF)

**Smart India Hackathon 2026** · Problem Statement by **NTRO** · Theme: Blockchain & Cybersecurity

Turns a log from *any* device into one common schema — without losing a byte of
the original, and with a hash chain that proves it was never altered afterwards.

```
firewall syslog ─┐
Okta JSON        ├─►  61 parsers  ─►  28-field schema  ─►  Neo4j  ─►  search · export · alert
Windows XML      │         │              │                  │
CEF / LEEF       │         │              │                  └─►  Merkle ledger
containerd       ┘         │              └─►  raw bytes + SHA-256 on every event
                           └─►  nothing recognised? structural fallback, never dropped
```

---

## Contents

1. [Screenshots](#screenshots)
2. [The problem](#the-problem)
3. [What is actually built](#what-is-actually-built)
4. [Quick start](#quick-start)
5. [For evaluators — a guided tour](#for-evaluators--a-guided-tour)
6. [Architecture](#architecture)
7. [The schema](#the-schema)
8. [The tamper-evident ledger](#the-tamper-evident-ledger)
9. [Parser synthesis](#parser-synthesis)
10. [Integrations](#integrations)
11. [Local AI](#local-ai)
12. [Measured numbers, and how to reproduce them](#measured-numbers-and-how-to-reproduce-them)
13. [Repository map](#repository-map)
14. [Honest scope](#honest-scope)
15. [Licences](#licences)

---

## Screenshots

<!-- Drop the images into docs/screenshots/ with these filenames. -->

### Parser Lab — paste any vendor's log, watch it parse live
Left: the raw pasted log. Right: the normalised record with a byte-for-byte
"preserved" check. **Nothing is stored** — safe to run against production.

![Parser Lab](docs/screenshots/parser-lab.png)

### Analytics — volume, severity, hosts, anomalies
![Analytics](docs/screenshots/analytics.png)

### Chat — ask the graph in English, by voice, or with a screenshot
![Chat](docs/screenshots/chat.png)

### Integrity — seal, verify, prove a single event
![Integrity ledger](docs/screenshots/integrity.png)

### Schema & export — the common taxonomy and what it exports to
![Schema and export](docs/screenshots/schema-export.png)

---

## The problem

Every firewall, server, cloud service and security tool writes logs in its own
private language. Before anyone can search them, correlate them, or spot an
attack, an engineer has to hand-write a translator for each one — and that work
is repeated at every organisation.

Worse: once logs are normalised, most pipelines can prove what they **stored**,
not that storage was never changed. In a forensic or compliance review, the
second one is what counts.

---

## What is actually built

| | |
|---|---|
| **61 parsers** | Cisco ASA, FortiGate, Palo Alto (CEF), Check Point (LEEF), Snort, F5 BIG-IP, pfSense, Squid, Linux syslog, Windows Security, VMware (ESXi/NSX/vCenter/vROps/Horizon), Kubernetes, Envoy, CoreDNS, PostgreSQL, AWS CloudTrail, Azure, GCP, CrowdStrike, Defender, Okta, Duo, logfmt, JVM, Apache, IoT gateways |
| **Structural fallback** | An unknown vendor still yields hostname, process, severity and key=value fields — and is never dropped |
| **Parser synthesis** | Points at unmatched lines and writes a working parser in ~3 ms |
| **28-field schema** | 18 required, 0 violations on live data; ECS and OCSF 1.3.0 exports |
| **Tamper-evident ledger** | Merkle hash chain; 9 sibling hashes prove one event without revealing the rest |
| **Local AI** | Text-to-Cypher, GraphRAG, speech in/out, image understanding, PPT/PDF/Word — all offline |
| **Air-gapped** | Verified with `docker run --network none` |

---

## Quick start

### The parser alone — no installation

It is pure Python standard library. On a bare Python 3.10+:

```bash
cd app/backend/ingestion_pipeline
python -m batch.test_custom_formats      # 50 checks — do detectors extract the right FIELDS
python -m batch.test_source_coverage     # 61 + 84 checks — coverage, formats, OCSF, ordering
python -m batch.test_unseen_vendors      # 28 vendors that appear in NO fixture
```

### The whole system

```bash
# 1. a graph to write into
docker run -d --name neo4j -p 7687:7687 -p 7474:7474 \
  -e NEO4J_AUTH=neo4j/yourpassword neo4j:5-community

# 2. configure — each service has its own .env.example
cp app/backend/ingestion_pipeline/.env.example app/backend/ingestion_pipeline/.env
cp app/backend/analytics_pipeline/.env.example app/backend/analytics_pipeline/.env
cp app/backend/chatbot_pipeline/.env.example   app/backend/chatbot_pipeline/.env

# 3. load logs (sample_logs.txt is in this repo)
cd app/backend/ingestion_pipeline
pip install -r requirements.txt
python -m batch.parse_logs      --path ../../../sample_logs.txt --out parsed.json
python -m batch.ingest_to_neo4j --input parsed.json

# 4. the three services
python -m uvicorn api:app --port 8020                          # ingestion
cd ../analytics_pipeline && python -m uvicorn api:app --port 8010
cd ../chatbot_pipeline   && python -m uvicorn api:app --port 8000

# 5. the UI
cd ../../frontend && npm install && npm run dev                # → localhost:5173
```

Offline / air-gapped deployment: build images on a connected machine with
`app/build_images.ps1`, copy the folder across, then `./load_images.sh &&
docker compose up -d`. No runtime downloads, telemetry or licence checks.

---

## For evaluators — a guided tour

### The claims, and where each is enforced

| Claim | Enforced in | Checked by |
|---|---|---|
| Raw bytes preserved exactly | `core/parser.py` — `raw_message`, `raw_hash` | `_check_verbatim` |
| An unknown vendor is never dropped | `core/parsers/generic_fallback.py` | `_check_fallback` (incl. binary guard) |
| Format detected from content, not filename | `core/structured_readers.py` | `_check_sniff`, both directions |
| CEF/LEEF timestamps normalise | `core/parsers/cef_leef.py` | `_check_cef_leef` |
| The **right** detector claims each format | `core/parsers/registry.py` ordering | `_check_attribution`, 10 cases |
| OCSF classes correct | `analytics_pipeline/ocsf.py` | `_check_ocsf` |
| The ledger detects tampering | `analytics_pipeline/ledger/` | 165 adversarial checks |

All of these live in
[`app/backend/ingestion_pipeline/batch/test_source_coverage.py`](app/backend/ingestion_pipeline/batch/test_source_coverage.py)
— readable in one sitting, with comments saying *why* each case exists, which is
usually a bug it caught.

### The part we are most pleased with

We built **Parser Lab** to demonstrate the parser, and then tested the parser
against vendors it had never seen. Between them they found **five real bugs in
our own work** — the last one in the tests themselves:

1. **`reassemble()` didn't know half the formats we parse.** CEF, LEEF, JSON and
   PRI-prefixed syslog were not recognised as record *starts*, so on a mixed
   feed unrelated events were welded together — 38 lines arrived as 17 records.
   It hid because our development corpus was homogeneous.
2. **A `.log` extension overrode the file's contents.** `sniff_format()` returned
   on the first extension match and never read the data, so a CSV named
   `export.log` — how such exports actually arrive — was parsed as syslog.
3. **Raw was not verbatim.** `parse()` stored the *stripped* form, so a record
   ending `request content = ` lost its trailing space — 5 of 305 in one real
   export. Trailing whitespace distinguishes an empty field from an absent one.
4. **A broad new detector silently stole another's lines.** Adding `logfmt` took
   FortiGate traffic logs (Fortinet also emits `msg=`), and the suite **stayed
   green** because it only asserted that *a* detector matched. Now 10 attribution
   cases pin which detector must claim which format.

5. **Our own fixtures were flattering us.** `sample_logs.txt` was written for
   this parser, so testing against it proved the two agreed — not that the
   parser handled the world. Re-testing with **28 vendors that appear nowhere
   in the repository** (Juniper, SonicWall, Sophos, Zeek, Cloudflare, Zscaler,
   SentinelOne, SailPoint, nginx, HAProxy, Redis, MongoDB, MySQL, Tomcat,
   Veeam, BACnet, Zigbee…) dropped coverage from 97% to **75%**. Worse,
   **RFC 5424 — a standard the problem statement names — was parsed wrong**:
   `pri_syslog` read the version digit as the hostname. Seven detectors later
   it is 28/28. `python -m batch.test_unseen_vendors` keeps it honest.

The tool that demonstrates the system also audits it, and the tests grew from
real defects rather than from imagination.

### Where the interesting code is

| File | Why |
|---|---|
| `core/parser.py` | The whole normalisation path. Note `stored_raw`, bound once so the stored bytes and their hash cannot disagree |
| `core/parsers/generic_fallback.py` | How an *unknown* vendor still yields a usable event — positional (RFC 3164/5424), not vendor-specific, with a binary guard that refuses to invent a hostname from noise |
| `core/parser_synth.py` | Typed-token alignment — writes a regex from samples |
| `core/parser_semantics.py` | Infers what a synthesised field *means*, and reports the evidence |
| `core/multiline.py` | Where one record ends and the next begins |
| `analytics_pipeline/ledger/merkle.py` | RFC 6962 domain separation; why odd nodes are promoted, not duplicated |
| `analytics_pipeline/ocsf.py` | Conservative classification — a wrong `class_uid` is worse than a generic one, because a SIEM routes on it |
| `analytics_pipeline/forwarder.py` | Push integration; delivery stated as *at-least-once*, not pretended to be exactly-once |

---

## Architecture

Three independent services sharing one graph, plus a React frontend. Separate
deployables on purpose: ingestion **writes**, analytics and chat **read**, and
the ledger checks storage from a process that did not write it.

```
  DNIF API ──► producer ──► Kafka (raw-logs, unparsed) ──► consumer ──┐
                                                                      │
  log files ──► reassemble (physical lines → logical records) ────────┤
                                                                      ▼
                                              ┌──────────────────────────┐
                                              │ LogParser.parse()        │
                                              │  ├ 54 detectors          │
                                              │  └ generic_fallback      │
                                              └────────────┬─────────────┘
                                                           ▼
                                              ┌──────────────────────────┐
                                              │ _enrich(): severity,     │
                                              │ entities, id, raw+hash   │
                                              └────────────┬─────────────┘
                                                           ▼
                                        bge-m3 embedding (optional) ──► Neo4j
                                                                        │
              ┌─────────────────────────────────────────────────────────┤
              ▼                                                         ▼
    analytics :8010                                            chatbot :8000
      aggregates · exports (NDJSON/ECS/OCSF/CEF/CSV)             text-to-Cypher
      forwarder (syslog/HTTP/file) · Merkle ledger               GraphRAG · speech
              └──────────────────► React frontend :5173 ◄───────  vision · documents
```

**Raw is buffered before parsing.** Kafka holds unparsed events, so the parse
stage can crash, restart, or scale to several replicas without losing a log that
was already fetched. In the batch path there is no Kafka — raw lives in the
source file — and if parsing fails the record still lands with
`matched_format: generic_fallback` and its raw intact. **Nothing is dropped in
either path.**

**Detector order is the most fragile thing here.** A broad new format placed too
early takes lines from a specific one, and nothing fails — the data just gets
quietly worse. Hence the attribution tests.

**Record boundaries** are decided by `core/multiline.py`: a line starts a new
record only if it matches a known header signature. Every format with a detector
needs a signature there, and the two lists must be kept in step.

---

## The schema

28 fields, 18 required, each carrying its ECS name and OCSF path. Coverage is
measured against the live graph (`GET /api/analytics/schema/coverage`), so the
document can be proved wrong rather than only asserted.

Traceability fields:

| Field | Purpose |
|---|---|
| `raw_message` | The original event, byte for byte |
| `raw_hash` | SHA-256 of those bytes — **recomputable from the source file with no access to this system** |
| `id` | MD5-16 of `timestamp\|host\|process\|raw`, stable across re-ingestion |
| `matched_format` | Which detector claimed it |
| `confidence` | Per-detector constant: 1.0 vendor-specific, lower for shape matches and the fallback |
| `source_file` / `source_record` | Origin file and ordinal |
| `timestamp_source` | `event` or `ingest` |
| `timestamp_anomalous` | Clock-skew flag — flagged, **never corrected** (rewriting would break losslessness) |

`raw_hash` matters because it is verifiable *without us*: an investigator holding
the original log file can recompute SHA-256 over a line and find the matching
stored event, with no ledger, no database and no need to trust this system.

---

## The tamper-evident ledger

We do **not** use a blockchain, and saying we did would be overclaiming. We use
the primitive underneath one, where it earns its place.

```
event  ─► leaf   = SHA256(0x00 ‖ canonical(event))     RFC 6962 prefixes
leaves ─► root   = Merkle tree, odd nodes promoted (not duplicated)
block  ─► hash   = SHA256(header including prev_hash ‖ merkle_root)
blocks ─► chain  = each block commits to the previous
chain  ─► head   = the one value that must be protected
```

```bash
cd app/backend/analytics_pipeline
python -m ledger.demo                    # seal real logs, edit one byte, watch it caught
python ledger/tests/test_ledger.py       # 165 adversarial checks
```

| Attack | Result |
|---|---|
| Edit a message | Caught — Merkle mismatch |
| Reorder events | Caught |
| Delete an event | Caught |
| **Re-seal the block to hide the edit** | Caught — breaks the link to the next block |
| Re-seal the **entire** chain | **Not caught internally.** Self-consistent; caught only by an externally held head hash |
| Forge an inclusion proof | Rejected |

Design decisions worth defending:

- **RFC 6962 domain separation** (`0x00` leaves, `0x01` nodes) — without it an
  internal node can be replayed as a leaf, the classic Merkle second-preimage
  attack.
- **Odd nodes promoted, not duplicated** — duplicating is what made Bitcoin's
  CVE-2012-2459 possible: two different trees yielding the same root.
- **Only nine fields are sealed** — not `processed_at` or `embedding`, which
  change legitimately on re-parse. Sealing those would flag honest re-ingestion
  as tampering, and a control that cries wolf gets switched off.
- **9 sibling hashes** prove one event was in a 500-event block *without
  revealing the other 499* — which matters when the rest of the log is classified.

**Tamper-evident, not tamper-proof.** Publishing the head hash somewhere the
operator does not control is what turns evidence into proof — and is the one
place a real blockchain, or an RFC 3161 timestamp authority, would genuinely
belong.

---

## Parser synthesis

```bash
python -m batch.synth_parser --file unknown.log --name acme --only-unmatched
```

Measured on a format that exists nowhere in the codebase:

| | |
|---|---|
| Before | 4/4 lines unclaimed by all 54 detectors |
| Synthesis | **3 ms**, confidence 0.67, **no TODO in the output** |
| After | 4/4 parsed with **zero human edits** — hostname, ISO timestamp, 10 attributes |

It infers field *meaning* from three kinds of evidence — syslog **position**
(RFC 3164/5424 fix the host and program slots), **key name** (a vocabulary drawn
from the formats our detectors already handle), and **value shape** — and the
generated module records the evidence for each field as a comment, so a reviewer
can disagree with one inference rather than distrust the file.

It will not guess whether an unnamed IP is source or destination. Putting wrong
evidence into an investigation is the failure this project exists to prevent.

---

## Integrations

**Pull** — streaming exports, flat memory, same filter as the dashboard:
NDJSON · ECS · **OCSF 1.3.0** · CEF · CSV

**Push** — `analytics_pipeline/forwarder.py`:

| Sink | For |
|---|---|
| syslog (RFC 5424 + RFC 6587 octet framing, CEF payload) | ArcSight, QRadar, appliance-era collectors |
| HTTP / NDJSON batches | Splunk HEC, Elastic, OpenSearch, Sentinel, any webhook |
| Rotating NDJSON files | Object storage or a data-lake loader |

Bounded queue, exponential backoff, dead-letter counter. Delivery is
**at-least-once** — a retried batch can arrive twice; deduplicate on the stable
event `id`.

**OCSF scope, stated honestly:** three classes implemented — Authentication
(3002), Network Activity (4001), Application Lifecycle (1008). An event that
does not clearly belong to the first two goes to 1008 rather than being forced
into a class whose required fields we would have to invent. On our
infrastructure corpus 95% lands in 1008, which is correct — it genuinely is
application lifecycle activity. On heterogeneous perimeter data the split is
74 / 16 / 11.

---

## Local AI

Everything runs on the air-gapped box. No cloud speech, vision or rendering API.

| | |
|---|---|
| **Ollama + Qwen3 14B** | Text-to-Cypher, GraphRAG. Groq / OpenRouter optional when a network exists |
| **BAAI/bge-m3** | 1024-dim embeddings, native Neo4j vector index |
| **BAAI/bge-reranker-v2-m3** | Retrieval reranking |
| **faster-whisper 1.2** | Speech to text — beam 5 + seeded domain vocabulary took WER **25% → 10%** |
| **Piper TTS 1.7** | Text to speech — any answer read aloud, ~50 MB ONNX voice |
| **minicpm-v** | Image understanding — paste a screenshot of a log and ask about it |
| **python-pptx / reportlab / python-docx** | "Make a 7-slide deck on the last 7 days" — with native charts |

A larger Whisper model was *not* the fix for accuracy: `small` scored 15% and ran
2.5× slower. Conditioning on the vocabulary that actually occurs was.

---

## Measured numbers, and how to reproduce them

**Intel i5-12450H, 8 cores / 12 threads, no GPU, Python 3.11.**

| | | Condition |
|---|---|---|
| **3,513 events/sec** | normalisation | **1 core, parse stage only** |
| **16,826 events/sec** | normalisation | 8 cores — 5.83× measured |
| **229 events/sec** | parse + graph write | semantic search off |
| **3 events/sec** | full pipeline | with embeddings — 94.5% of wall clock |
| p50 0.24 ms · p99 0.82 ms | parse latency | per record |
| **97.4%** | named-detector coverage | live infrastructure logs |
| **3 ms** | unseen vendor → working parser | zero human edits |
| **9 hashes** | inclusion proof | 500-event block |
| **25% → 10%** | speech-to-text WER | after vocabulary seeding |

**Please do not read 3,513/sec as system throughput.** It is normalisation on one
core. Use **229/sec** for a SIEM pre-processor; embeddings buy semantic search
and are optional per deployment.

Parsing scales with **CPU cores**, not a GPU — the work is Python's `re` engine,
branchy backtracking over short strings:

| workers | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| records/sec | 2,887 | 6,517 | 11,605 | **16,826** |
| speedup | 1.00× | 2.26× | 4.02× | 5.83× |

**Storage** over 10,000 records (JSON-serialised, not Neo4j on-disk):

| | | vs raw |
|---|---|---|
| raw only | 3.09 MB | 1.00× |
| normalised, raw included | 14.33 MB | **4.64×** |
| + 1024-dim embedding | 53.39 MB | 17.27× |

**Air-gap:**

```bash
docker run --rm --network none ulpf-ingestion:latest python -c "
import socket; socket.setdefaulttimeout(4)
try: socket.create_connection(('8.8.8.8',53)); print('REACHABLE - FAIL')
except Exception as e: print('no network:', type(e).__name__)
from core.parsers.registry import DETECTORS; print('detectors:', len(DETECTORS))"
```
```
no network: OSError
detectors: 61
```

---

## Repository map

```
sample_logs.{txt,xml,csv}      synthetic logs covering every source category and
                               format the problem statement names — RFC 2606
                               documentation domains, RFC 5737 addresses

app/
  backend/
    ingestion_pipeline/        parsers, schema, synthesis  ← start here
      core/parsers/            the 61 detectors
      core/parser.py           normalisation, raw preservation, id + hash
      core/parser_synth.py     writes a parser from samples
      core/parser_semantics.py infers what a synthesised field MEANS
      core/multiline.py        record boundaries
      batch/                   CLI: parse, ingest, embed, synthesise, test
    analytics_pipeline/        aggregations, exports, integrity
      ledger/                  Merkle tree, hash chain, demo, 165 tests
      ocsf.py                  OCSF 1.3.0 emitter
      forwarder.py             push to syslog / HTTP / file
      taxonomy.py              the 28-field schema with ECS + OCSF mappings
    chatbot_pipeline/          text-to-Cypher, GraphRAG, speech, vision, docs
  frontend/                    React UI — 8 screens
```

**Not in this repository, deliberately:** the 33 GB development corpus (real
captured logs, not ours to publish) and the deployment's internal addresses —
the public tree carries placeholders and `localhost` defaults, and the real
values are supplied to the operator separately.

---

## Honest scope

- **Tamper-evident, not tamper-proof.** A full chain rewrite is self-consistent; it is caught only by a head hash published outside the system.
- **OCSF covers 3 classes of ~70.**
- **Neo4j is the wrong store beyond single-VM scale.** Streaming exports to a SIEM or data lake are the scale path.
- **Semantic search runs at ~3 events/sec on CPU** and is optional per deployment.
- **The synthesiser drafts; a human still reviews.** It gets shape and most field meanings right; it does not know your business.
- **Never run against the real Kafka cluster or a live DNIF instance.** Both clients exist; neither has talked to production infrastructure.
- **No enrichment** — no GeoIP, no threat intel, no asset lookup.
- **Our corpus is VMware/Kubernetes infrastructure, not perimeter traffic.** The perimeter claim rests on 10 hand-checked vendor samples, not on volume. Given a real firewall feed, that is the number we would most like to re-measure.
- **No violin or box plots** in the chat charts: query results are aggregates, so a distribution plot would be inventing spread the data does not contain.

We would rather state the boundary than be found at it.

---

## Licences

| Licence | Projects |
|---|---|
| **GPLv3** | Neo4j Community — communicated with over Bolt via an Apache-2.0 driver |
| Apache-2.0 | confluent-kafka, sentence-transformers, transformers, bge-reranker-v2-m3, Qwen3, requests |
| MIT | FastAPI, React, Vite, Ollama, bge-m3, python-pptx |
| BSD-3-Clause | PyTorch, Uvicorn, python-dotenv, reportlab |
| LGPL / MIT | faster-whisper, Piper TTS, python-docx |

The Neo4j line is worth knowing before anyone plans redistribution: linking over
a network protocol with a permissively licensed driver is the normal
arrangement, but bundling is a question for a lawyer, not an assumption.

## Standards implemented

RFC 3164 · RFC 5424 (syslog) · RFC 6587 (octet framing) · RFC 4180 (CSV) ·
**RFC 6962** (Certificate Transparency — Merkle domain separation) · RFC 2606
(documentation domains) · ArcSight CEF · IBM QRadar LEEF 2.0 · Elastic Common
Schema 8.11 · OCSF 1.3.0

**Algorithms:** typed-token alignment (our own) · Merkle hash chain (RFC 6962) ·
modified z-score on median absolute deviation for anomalies (Iglewicz & Hoaglin,
1993).

We do **not** use Drain, Spell, LogPai or IPLoM, and do not cite them.
