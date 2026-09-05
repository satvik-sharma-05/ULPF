# Architecture

Three independent services sharing one graph, plus a React frontend. They are
separate deployables on purpose: ingestion **writes**, analytics and chat
**read**, and the integrity ledger checks storage from a process that did not
write it.

```
                    ┌──────────────┐
  DNIF API ────────►│  producer    │──── raw, unparsed ───►┌───────────────┐
                    └──────────────┘                       │ Kafka         │
                                                           │ raw-logs      │
  log files ──┐                                            └───────┬───────┘
              │                                                    │
              ▼                                                    ▼
       ┌──────────────┐                                   ┌────────────────┐
       │ reassemble   │  physical lines → logical records │  consumer      │
       └──────┬───────┘                                   └───────┬────────┘
              │                                                   │
              └──────────────────┬────────────────────────────────┘
                                 ▼
                    ┌────────────────────────┐
                    │ LogParser.parse()      │  54 detectors, first match wins
                    │  ├ named detector      │
                    │  └ generic_fallback    │  never drops a record
                    └───────────┬────────────┘
                                ▼
                    ┌────────────────────────┐
                    │ _enrich()              │  severity, entities, id hash,
                    │                        │  raw_message + raw_hash
                    └───────────┬────────────┘
                                ▼
                  ┌─────────────────────────────┐
                  │ bge-m3 embedding (optional) │
                  └─────────────┬───────────────┘
                                ▼
                    ┌────────────────────────┐
                    │  Neo4j                 │  46 labels, 62 relationship
                    │  + native VECTOR INDEX │  types, 1024-dim COSINE
                    └───┬──────────┬─────────┘
                        │          │
         ┌──────────────┘          └───────────────┐
         ▼                                         ▼
  ┌─────────────┐                          ┌──────────────┐
  │ analytics   │ :8010                    │ chatbot      │ :8000
  │  aggregates │                          │  text2cypher │
  │  exports    │──► NDJSON/ECS/OCSF/CEF   │  GraphRAG    │
  │  forwarder  │──► syslog/HTTP/file      │  speech      │
  │  ledger     │──► Merkle chain          │  vision      │
  └──────┬──────┘                          │  documents   │
         │                                 └──────┬───────┘
         └────────────────┬───────────────────────┘
                          ▼
                   React frontend :5173
```

## Why raw is buffered before parsing

Kafka sits **between fetching and parsing**, holding unparsed events. That only
works if fetching and parsing are separate processes, which is why the realtime
pipeline ships as two roles. The consequence is the one that matters: the parse
stage can crash, restart, or scale to several replicas without losing a log
that was already pulled.

In the batch path there is no Kafka — raw lives in the source file on disk, and
Neo4j receives it only as part of the parsed record. If parsing fails there, the
record still lands with `matched_format: generic_fallback` and its raw intact.
**Nothing is dropped in either path.**

## The parsing decision

```
record ─► is it JSON?  ──yes──► 8 JSON-aware detectors ──► structural reader
   │
   no
   ▼
 54 detectors in priority order ──► first match wins
   │
   nothing matched
   ▼
 generic_fallback ──► positional host (RFC 3164/5424), proc[pid], key=value,
                      binary guard. Always returns a schema-valid record.
```

**Order is the most fragile thing here.** A broad new format placed too early
takes lines from a specific one, and nothing fails — the data just gets quietly
worse. That happened when `logfmt` was added and it claimed FortiGate traffic
logs, so `test_source_coverage.py` now pins which detector must claim which
format.

## Where the record boundary is

`core/multiline.py` decides where one record ends and the next begins: a line
starts a new record only if it matches a known header signature; anything else
is a continuation.

**Every format with a detector needs a signature here**, and the two lists must
be kept in step. A detector without a matching record-start pattern works only
on files containing nothing else — on a mixed feed its records are silently
welded onto whatever preceded them.

## Schema

28 fields, 18 required, each carrying its ECS name and OCSF path. Coverage is
measured against the live graph (`GET /api/analytics/schema/coverage`), so the
document can be proved wrong rather than only asserted.

Traceability fields:

| Field | Purpose |
|---|---|
| `raw_message` | The original event, byte for byte |
| `raw_hash` | SHA-256 of those bytes — recomputable from the source file with no access to this system |
| `id` | MD5-16 of `timestamp\|host\|process\|raw`, stable across re-ingestion |
| `matched_format` | Which detector claimed it |
| `confidence` | Per-detector constant, 1.0 vendor-specific → lower for fallback |
| `source_file` / `source_record` | Origin file and ordinal |
| `timestamp_source` | `event` or `ingest` |
| `timestamp_anomalous` | Clock-skew flag — flagged, never corrected |

`raw_hash` deserves a note: its value is that it is verifiable **without this
system**. An investigator holding the original log file can recompute SHA-256
over a line and find the matching stored event, with no ledger, no database and
no need to trust us.

## The integrity ledger

```
event  ─► leaf   = SHA256(0x00 ‖ canonical(event))     RFC 6962 prefixes
leaves ─► root   = Merkle tree, odd nodes promoted (not duplicated)
block  ─► hash   = SHA256(header including prev_hash ‖ merkle_root)
blocks ─► chain  = each block commits to the previous
chain  ─► head   = the one value that must be protected
```

Only nine fields are sealed — identity, time, origin, content — deliberately
**not** `processed_at` or `embedding`, which change legitimately on re-parse.
Sealing those would make the ledger flag honest re-ingestion as tampering, and
a control that cries wolf is a control that gets switched off.

See [`app/backend/analytics_pipeline/ledger/README.md`](../app/backend/analytics_pipeline/ledger/README.md).

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

## Configuration

Each service reads its own `.env`, loaded by `config.py` so that **every** entry
point honours it — not just the FastAPI app. That was a real bug: only `api.py`
called `load_dotenv()`, so scripts, tests and CLIs silently used the built-in
defaults and failed with a network timeout that named nothing.

Site-specific values — domain suffixes, hostname conventions, the speech
vocabulary — live in `.env.example`, never as defaults in code. A framework
that claims to be vendor-agnostic should not ship one customer's domain as its
built-in assumption.
