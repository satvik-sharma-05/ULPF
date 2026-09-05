# ULPF — requirement checklist

Every clause of the problem statement, checked against the running system on
2 September 2026. Figures are measured, not asserted; where something is
partial or missing it says so, because a checklist that only contains ticks is
not a checklist.

**Legend** — ✅ met · 🟡 partial · ❌ not met

---

## 1. Background — source and format diversity

### 1.1 Source categories named in the statement

| Source category | Status | Evidence |
|---|---|---|
| Network devices | ✅ | `pri_syslog`, `fortinet_kv`, CEF, LEEF, Squid — 10/10 perimeter samples parsed |
| Servers / operating systems | ✅ | `linux_syslog`, `vmkernel`, `esxi_*`, `windows_security_event`, auditd |
| Applications | ✅ | `aria_automation_springboot`, `vrlcm_classic_logback`, `spring_actuator_access`, `jvm_gc` |
| Databases | ✅ | `postgres_log`, `postgres_native_log` |
| Cloud services | ✅ | `aws_cloudtrail`, `azure_activity`, `gcp_audit`, `k8s_audit`, plus the private-cloud stack (vCenter, vROps, NSX, Horizon) — 4/4 provider samples parsed with identity, resource and outcome |
| Containers | ✅ | `kubelet_glog`, `k8s_access`, `coredns`, `envoy_access` |
| Endpoint security tools | ✅ | `crowdstrike_falcon`, `defender_alert`, plus CEF/LEEF for the rest — severity escalates correctly (credential dumping -> CRITICAL) |
| Identity & access management | ✅ | `okta_system_log`, `duo_auth`, `windows_security_event`, `vrops_session_audit`, `horizon_view_audit`, PAM/sudo — failed sign-in -> WARNING, lockout -> ERROR |
| IoT devices | 🟡 | Most IoT gateways emit RFC 3164 syslog, now handled by `pri_syslog`. **Not specifically tested** |
| Other hardware / software | ✅ | `generic_fallback` normalizes structurally rather than rejecting |

**51 detectors registered**, 8 of them JSON-aware. Verified: `len(DETECTORS) == 51`.

### 1.2 Formats named in the statement

| Format | Status | Where |
|---|---|---|
| Syslog (RFC 3164) | ✅ | `rfc3164_wrapper`, `pri_syslog` (with `<PRI>` decoding) |
| Syslog (RFC 5424) | ✅ | `linux_syslog` structured-data handling |
| JSON / JSONL | ✅ | `structured_readers.iter_records` |
| XML | ✅ | `structured_readers._iter_xml` |
| CSV | ✅ | `structured_readers._iter_csv` |
| CEF | ✅ | `detect_cef` — Palo Alto sample parsed, `src`/`dst`/`dpt` extracted |
| LEEF | ✅ | `detect_leef` — Check Point sample parsed |
| Proprietary vendor formats | ✅ | 51 detectors, most written for one vendor's shape |
| Application-specific schemas | ✅ | `attributes_json` holds any field with no home in the taxonomy |

`SUPPORTED_FORMATS = ('auto', 'text', 'json', 'csv', 'xml', 'cef', 'leef')` with
format sniffing when `auto`.

---

## 2. Detailed Description

| Requirement | Status | Evidence |
|---|---|---|
| Ingest, parse, normalize, standardize | ✅ | Three ingest paths (batch, realtime Kafka, upload) share one `core/` pipeline |
| Any hardware or software system | ✅ | 51 detectors, a structural fallback, and a synthesizer that drafts a detector for a format nobody has seen — see (i) |
| Preserve original for forensic / compliance | ✅ | `raw_message` on **24,515 / 24,515** events (100%) |
| Unified schema | ✅ | 27 declared fields, 17 guaranteed, **0 contract violations** |
| — consistent analytics | ✅ | One filter threaded through 15 aggregations |
| — correlation | ✅ | 46 node labels, 62 relationship types |
| — visualization | ✅ | Analytics, Alerts, Live logs, heatmap, trends; native charts in chat, PPTX, PDF and Word exports |
| — threat hunting | ✅ | Text-to-Cypher, GraphRAG, vector search, full-text |
| — anomaly detection | ✅ | `anomalies.py` — modified z-score on MAD; **10 findings** on live data |
| — machine learning | ✅ | 24,515 / 24,515 embedded (100%), native vector index |
| Scalable | ✅ | **Measured: 4,702 records/sec/core** = 406M events/day/core. Indexed properties, keyset pagination, streaming export, `--scale consumer=N` |
| Extensible | ✅ | New source = one module + one registry line; nothing else changes |
| Vendor-agnostic | ✅ | `source_type` is the technology; no vendor in the schema |
| Big Data / billions of events per day | ✅ | Measured parse throughput extrapolates to **3.25 B/day on 8 cores**, 13 B on 32, 52 B on 128. Kafka decouples fetch from parse so consumers scale horizontally |

---

## 3. Expected Solutions (a)–(k)

### (a) Preserve complete raw event data without information loss ✅

| Check | Value |
|---|---|
| Events carrying `raw_message` | **24,515 / 24,515 (100%)** |
| Truncation cap | `RAW_MESSAGE_MAX_CHARS`, default 32,768 |
| Truncation flagged | `raw_truncated` + `raw_length` |

`message` is a cleaned derivative and never the only copy.

### (b) Extract and parse source-specific attributes ✅

| Check | Value |
|---|---|
| Detectors matching the corpus | 20 of 51 registered |
| `generic_fallback` share | **18 / 24,515 = 0.07%** |
| Events with an attributes bag | 100% |
| Typed entity labels | 46 |

### (c) Normalize fields into a common event taxonomy ✅

Universal Event Schema: **27 fields, 17 required, 0 contract violations**, each
mapped to its **ECS** and **OCSF** equivalent.
`GET /api/analytics/schema` · `GET /api/analytics/schema/coverage` · **Schema & export** page.

Coverage is measured against the live graph, so the document can be proved
wrong rather than only asserted.

### (d) Maintain traceability between normalized and original events ✅

| Field | Role |
|---|---|
| `id` | md5 of `timestamp\|hostname\|process\|raw`, deterministic |
| `raw_message` | the original bytes |
| `source_file` | path relative to the corpus root |
| `source_record` | ordinal within that file |
| `timestamp_source` | `event` or `ingest` — whether the time is real |
| `timestamp_anomalous` | flags appliance clock skew without rewriting the timestamp |

**24,496 / 24,515 (99.9%)** carry the file pointer. The last 19 are raw lines
that occur in more than one source file, so no single origin exists to record -
skipped rather than guessed, because inventing provenance is worse than
admitting there is none. **300/300** sampled ids reproduce exactly on re-parse.

### (e) Plug-and-play onboarding of new log sources ✅

Three paths, none needing a code change in the common case: upload with format
sniffing, structural `generic_fallback`, or one new detector module.

**Demonstrated:** adding `network_devices.py` took perimeter coverage from
**3/10 to 10/10** — one new file, two registry lines, and **0/500** existing
records changed detector.

### (f) Unified visibility across enterprise environments ✅

One graph over 20 source technologies. Analytics, Alerts (P1–P3), Live logs,
Schema & export, and natural-language Chat. A dashboard-wide filter runs
through all 15 aggregations so no two panels describe different populations.

### (g) Efficient SIEM and Data Lake integration ✅

| Format | Destination |
|---|---|
| NDJSON | Data lake — Spark, DuckDB, BigQuery, object store |
| ECS JSON | Elasticsearch / OpenSearch, no mapping work |
| CEF | ArcSight CEF — what most SIEMs accept over syslog |
| CSV | Spreadsheet / relational staging |

**Full corpus: 24,515 events, 31.5 MB, 12 s**, flat memory (generator +
`StreamingResponse`). Same filter as the dashboard. Lineage fields travel by
default; dropping the raw original is an explicit opt-out.

### (h) AI/ML-ready security and operational analytics ✅

| Check | Value |
|---|---|
| Events embedded | **24,515 / 24,515 (100%)** |
| Model | BAAI/bge-m3, 1024-dim |
| Index | native Neo4j vector index, cosine |
| Reranker | BAAI/bge-reranker-v2-m3 |
| Anomaly detection | modified z-score on MAD, **10 findings live** |

Semantic retrieval verified: *"kubernetes container log could not be reopened"*
retrieves `ReopenContainerLog from runtime service failed` at cosine **0.885**
with no shared keywords.

### (i) Reduced parser development effort ✅

`core/parser_synth.py` **writes the detector**. Given sample lines it tokenizes
each into typed slots — timestamp, ipv4, quoted, bracketed, int, word —
aligns the sequences, and emits a regex where the constants are structure and
the variables are named capture groups.

    python -m batch.synth_parser --file unknown.log --name acme_fw --only-unmatched

**Measured on HAProxy, a format no detector handles:** 5 sample lines →
**23 fields discovered**, 100% coverage, and the pattern matches held-out lines
from *different hosts, pids and backends*.

Deterministic alignment rather than an LLM: it runs in milliseconds, needs no
model on an air-gapped VM, and the output is a plain regex a human can read and
correct. A generated parser nobody can audit is worse than no generated parser.

It prints a **draft**, never self-registers — the reviewer decides which
captured field is the hostname. It also warns when a constant in a small sample
is probably a variable, which is the failure that made the first version emit a
detector matching exactly one machine.

Alongside: the structural fallback keeps an unknown source usable immediately,
and the **Parser coverage** panel ranks `matched_format` live so "where would a
detector pay off" is answered by data.

### (j) Deployable in an air-gapped network ✅

Self-hosted fonts, no runtime CDN, Ollama on the box, embedding/reranker/Whisper/
Piper weights baked into images, `HF_HUB_OFFLINE=1`. `core/modes.py::validate()`
reports what is missing rather than starting and silently ingesting nothing —
and **production mode withholds data entirely** when it is not connected,
rather than showing the sample sandbox under a production label.

### (k) Container packaging ✅

Dockerfile per deployable (ingestion, analytics, chatbot, frontend, ollama),
plus a top-level `docker-compose.yml`, `build_images.ps1` and `load_images.sh`.
Target VM does `docker load && docker compose up` — no pip, npm or model
download.

---

## 4. Current Scope — perimeter network devices ✅

Measured against real vendor samples:

| Device | Detector | Hostname | Severity |
|---|---|---|---|
| Palo Alto NGFW (CEF) | `cef` | — | WARNING |
| Fortinet FortiGate | `fortinet_kv` | `FGT-EDGE-01` | WARNING |
| Check Point (LEEF) | `leef` | — | INFO |
| Cisco ASA firewall | `pri_syslog` | `asa-edge-01` | INFO |
| Snort IDS | `pri_syslog` | `ids-sensor-01` | **ALERT** |
| Cisco IOS switch | `pri_syslog` | `sw-core-02` | NOTICE |
| F5 BIG-IP WAF | `pri_syslog` | `bigip-waf-01` | INFO |
| OpenVPN | `pri_syslog` | `vpn-gw-01` | INFO |
| pfSense | `pri_syslog` | `pfsense-01` | INFO |
| Squid proxy | `squid_access` | — | WARNING |

**10/10 matched, 0 fell to `generic_fallback`.** Severity comes from the
device's own `<PRI>` value rather than being guessed from the message text.

Before this work it was **3/10, and every one lost its hostname** — including
the RFC 3164 lines where the hostname was in the line, because the existing
syslog detector anchored on the month and never matched a `<PRI>` prefix.

---

## 5. Verification method

Nothing above is asserted from reading the code. Each figure came from running
the system: detector counts from `len(DETECTORS)`, coverage from the live
`/schema/coverage` endpoint, throughput from timing 3,000 real records,
perimeter and cloud coverage from feeding it real vendor samples, and the
synthesizer from training on two lines and testing on held-out ones.

Where a check contradicted an earlier claim of mine, the check won — the
perimeter gap, the JSON short-circuit that stopped cloud detectors firing, and
the vision model's OCR failure were all found this way rather than reasoned
about.

---

## 6. Open items

| Item | Impact |
|---|---|
| 141 events carry 2012 timestamps | **Resolved as a data-quality finding, not a bug.** The raw NSX lines really do say 2012 — an appliance with an unset clock. Now flagged `timestamp_anomalous` and never rewritten, because correcting it would put the normalized record at odds with the raw line and break requirement (a) |
| Vision model swapped after testing | `llava:7b` could not read **30px black-on-white Consolas** ("the image is too blurry") and on a smaller capture *fabricated* an error about a host that was not in the picture. Replaced with **`minicpm-v`**, which transcribed the same images exactly, hostname included. Resolved. |
