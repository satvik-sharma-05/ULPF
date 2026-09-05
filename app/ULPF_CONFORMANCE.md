# ULPF Conformance

Universal Log Pre-processing Framework — how this implementation meets each
expected solution (a)–(k) from the NTRO problem statement, with the evidence
for each claim and the gaps stated plainly.

Every figure below was measured against the live graph, not asserted. The
corpus is 24,515 events drawn from 9,186 files of real VMware SDDC /
Kubernetes / Linux infrastructure logs across 20 source technologies. Anything
still incomplete is marked **Partial** with the specific reason.

---

## (a) Preserve complete raw event data without information loss — **Met**

Every event stores `raw_message`: the original line byte for byte, before any
cleaning. `message` is a *derivative* (BOM stripped, octal escapes unescaped,
whitespace trimmed) and is never the only copy.

| Evidence | Value |
|---|---|
| Events with `raw_message` | 24,515 / 24,515 — **100%** |
| Truncation cap | `RAW_MESSAGE_MAX_CHARS`, default 32,768 |
| Truncation flagged | `raw_truncated` + `raw_length`, so a clipped value can never be mistaken for a whole one |

Code: `ingestion_pipeline/core/neo4j_writer.py::_prepare_log`.

The cap exists because a reassembled multi-line record (a Windows Security
event body, a Java stack trace) routinely exceeded the previous hard-coded
4,000 characters and was being silently clipped. It is now bounded, tunable
and reported.

## (b) Extract and parse source-specific attributes — **Met**

A detector chain claims each line and extracts its native fields. Anything
that has no home in the common taxonomy goes into `attributes_json` — so
nothing parsed is discarded for want of a column.

| Evidence | Value |
|---|---|
| Detectors that matched the corpus | 20 |
| `generic_fallback` share | 18 / 24,515 — **0.07%** |
| Events with an attributes bag | 24,515 / 24,515 — **100%** |
| Typed entity labels extracted | 46 (`User`, `IPAddress`, `Pod`, `VM`, `Datastore`, `Syscall`, …) |
| Relationship types | 62 |

Code: `ingestion_pipeline/core/parsers/` (registry + 14 detector modules),
`core/entity_extraction.py`.

## (c) Normalize fields into a common event taxonomy — **Met**

The **Universal Event Schema** is declared in one place, served over the API
and rendered in the UI, with each field's **ECS** (Elastic Common Schema) and
**OCSF** equivalent. A universal schema that is universal only to itself would
just be a 34th proprietary format.

| Evidence | Value |
|---|---|
| Declared fields | 25 |
| Guaranteed on every event | 16 |
| Contract violations (required field not at 100%) | **0** |
| Standards mapped | ECS, OCSF |

- Definition: `analytics_pipeline/taxonomy.py`
- API: `GET /api/analytics/schema`, `GET /api/analytics/schema/coverage`
- UI: **Schema & export** page

`schema/coverage` measures the live graph against the schema's own claims
rather than restating them. A required field below 100% is reported as a
contract violation — the document is designed to be falsifiable.

Severity is one example of the normalization: numeric syslog priorities,
CEF/LEEF severities and bare vendor words all collapse onto a single 9-level
ladder with an ordered `severity_score` (1 = most severe).

## (d) Maintain traceability between normalized and original events — **Met**

Four fields together form the lineage contract:

| Field | Role |
|---|---|
| `id` | md5 of `timestamp\|hostname\|process\|raw` — deterministic, so re-ingesting a line MERGEs onto the same event rather than duplicating it |
| `raw_message` | the original bytes |
| `source_file` | path of the originating file, **relative to the corpus root** |
| `source_record` | ordinal of the record within that file, counting records that failed to parse so it stays a faithful index |

| Evidence | Value |
|---|---|
| Events with a file+record pointer | 23,403 / 24,515 — **95.5%** |
| Ids reproduced exactly on re-parse | 300 / 300 sampled |

**Known gap, and the defect behind it.** The 1,112 events without a file
pointer are all CoreDNS and `vracli` records, and investigating why the
backfill could not match them found a real bug rather than a missing file:

Those formats emit **no timestamp at all**. `_enrich` fell back to
`datetime.now()`, and because the id hashes the timestamp, the id was a
function of *when the line happened to be read*. Every re-ingest minted a new
node and `MERGE` silently stopped being idempotent — the exact opposite of the
determinism this requirement depends on.

Fixed in `core/parser.py`: when no timestamp is parsed the id hashes a
constant marker instead of the clock, and the new `timestamp_source` field
records whether `timestamp` is an event time or a read time. Verified:

- 300 / 300 events **with** a real timestamp still reproduce their stored id —
  no existing identity changed.
- Events **without** one are now stable across repeated parses (50 / 50),
  where previously 0 / 50 were.

The affected events keep `raw_message` and remain traceable by content; a
re-ingest now gives them stable ids and the file pointer, which it could not
have done before this fix.

`source_file` is a relative path, not a basename, deliberately: this corpus has
2,501 folders that each contain an `export10.txt`, so a basename identified
essentially nothing.

## (e) Plug-and-play onboarding of new log sources — **Met**

Three independent paths, none requiring a code change for the common case:

1. **Upload** — Syslog, JSON, JSONL, XML, CSV, CEF, LEEF or plain text, with
   format auto-sniffing (`format=auto`). `core/structured_readers.py`.
2. **Generic fallback** — an unrecognized format still normalizes structurally
   (timestamp, host, severity, message) instead of being rejected.
3. **New detector** — one module in `core/parsers/`, registered in
   `registry.py`. No change to the writer, schema, analytics or UI.

## (f) Unified visibility across enterprise environments — **Met**

One graph over 20 source technologies, queried the same way regardless of
origin: Analytics (volume, severity, heatmap, error rate, rankings, insights),
Alerts (P1/P2/P3 triage queue), Live logs, and natural-language Chat over the
graph. A dashboard-wide filter (date range, severity, source, host, full-text
search) is threaded through all 15 aggregations, so no two panels can describe
different populations.

## (g) Efficient SIEM and Data Lake integration — **Met**

`GET /api/analytics/export` streams normalized events in four wire formats:

| Format | Destination |
|---|---|
| **NDJSON** | Data lake — loads directly into Spark, DuckDB, BigQuery, any object store |
| **ECS JSON** | Elasticsearch / OpenSearch, no field mapping work |
| **CEF** | ArcSight Common Event Format — what most SIEMs accept over syslog |
| **CSV** | Spreadsheet or relational staging table |

| Evidence | Value |
|---|---|
| Full-corpus export | 24,515 events, 31.5 MB, 12 s |
| Memory profile | flat — generator + `StreamingResponse`, one line at a time |
| Filter support | the same filter as the dashboard, same query string |
| Lineage in output | `raw_message`, `source_file`, `source_record` included by default |

Dropping the raw original is an explicit opt-out (`include_raw=false`), not the
default — an event that reaches a SIEM without its original has been stripped
of the thing that makes it admissible for a forensic question.

Code: `analytics_pipeline/exporters.py`, `queries.py::stream_export`,
`neo4j_client.py::run_read_stream`.

## (h) AI/ML-ready security and operational analytics — **Met**

| Evidence | Value |
|---|---|
| Events embedded | 24,515 / 24,515 — **100%** |
| Model | BAAI/bge-m3, 1024-dim |
| Index | native Neo4j `VECTOR INDEX`, cosine |
| Reranker | BAAI/bge-reranker-v2-m3 |

Vectors are computed over `normalized_message` — the canonical
`[source] host/process: message` rendering — so semantic search compares like
with like across every source format. Verified semantically, not just
structurally: *"kubernetes container log could not be reopened"* retrieves
`ReopenContainerLog from runtime service failed` at cosine **0.885** with no
shared keywords.

This powers GraphRAG in the chatbot, and the same vectors are directly usable
for clustering or anomaly detection without a separate feature pipeline.

## (i) Reduced parser development effort — **Partial**

What exists: the generic fallback means an unknown source is usable
immediately; the detector registry means a new source is one self-contained
module; and the **Parser coverage** panel ranks `matched_format` live, so the
question "where would a new detector actually pay off" is answered by data
rather than guesswork.

**Gap:** there is no automatic parser *generation* — inferring a grammar from
sample lines. A genuinely new proprietary format still needs a human to write
a detector, even though the fallback keeps its events queryable meanwhile.

## (j) Deployable in an air-gapped network — **Met**

| Concern | How it is handled |
|---|---|
| Fonts | self-hosted via npm (`@fontsource-variable/*`), emitted into `dist/` — no `fonts.googleapis.com` |
| JS/CSS | no runtime CDN references anywhere in `frontend/src` |
| LLM | Ollama on the box; production mode restricts providers to `['ollama']` because cloud APIs are unreachable by topology, not policy |
| Embedding / reranker weights | baked into the images |
| Mode validation | `core/modes.py::validate()` reports what is missing (Kafka brokers, DNIF URL, Neo4j address) instead of starting and silently ingesting nothing |

The UI surfaces this: selecting an unconfigured mode shows a banner naming
every unmet requirement and the exact Neo4j the numbers on screen came from —
so sample data can never be mistaken for production data.

## (k) Container packaging — **Met**

Dockerfile per deployable, plus compose files:

```
ingestion_pipeline/Dockerfile   analytics_pipeline/Dockerfile
chatbot_pipeline/Dockerfile     frontend/Dockerfile
ollama/Dockerfile
```

The three backends are standalone deployables sharing only Neo4j, so each
scales and restarts independently.

---

## Architecture in one paragraph

Logs enter through one of three paths (bundled corpus, realtime
DNIF → Kafka, or user upload) into a shared `core/` pipeline: multi-line
reassembly → detector chain → normalization into the Universal Event Schema →
typed entity extraction → bge-m3 embedding → Neo4j. The graph holds `:Log`
nodes with the full schema plus a 1024-dim vector, connected to 46 typed
entity labels by 62 relationship types. Three FastAPI services read it —
ingestion control (:8020), analytics (:8010), chatbot (:8000) — behind one
React frontend. Nothing but Neo4j is shared between them.

## Summary

| Requirement | Status |
|---|---|
| (a) Lossless raw preservation | Met — 100% |
| (b) Source-specific attribute extraction | Met — 0.07% fallback |
| (c) Common event taxonomy | Met — 25 fields, ECS + OCSF, 0 violations |
| (d) Traceability | Met — 95.5% file+record, 100% by content |
| (e) Plug-and-play onboarding | Met |
| (f) Unified visibility | Met — 20 source types |
| (g) SIEM / Data Lake integration | Met — 4 streaming formats |
| (h) AI/ML-ready | Met — 100% embedded |
| (i) Reduced parser effort | **Partial** — no automatic parser generation |
| (j) Air-gapped deployment | Met |
| (k) Container packaging | Met |
