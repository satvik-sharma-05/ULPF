# A guided tour, for evaluation

Written for someone with limited time who wants to know **which claims are real
and where to check them**. Every figure here was produced by running the code;
each one names the command that produces it, so nothing has to be taken on
trust.

If you read one thing: **[§3, the three bugs the tool found in itself](#3-the-part-we-are-most-pleased-with)**.

---

## 1. Ten minutes, no installation

The parser has **no third-party dependencies** — it is pure Python standard
library. On a bare Python 3.10+:

```bash
cd app/backend/ingestion_pipeline
python -m batch.test_custom_formats      # 50 checks — do detectors extract the right FIELDS
python -m batch.test_source_coverage     # 61 checks — coverage, fallback, formats, OCSF, ordering
```

Then watch it parse a genuinely heterogeneous file — nine vendors, six formats:

```bash
python -m batch.parse_logs --path ../../../sample_logs.txt --out /tmp/out.json
```

`sample_logs.txt` was written to cover **every source category and format the
problem statement names**: network devices, servers, OS, applications,
databases, cloud, containers, endpoint security, IAM, IoT — across Syslog,
JSON, XML, CSV, CEF, LEEF and proprietary vendor formats.

---

## 2. The claims, and where each is checked

| Claim | Where it is enforced | How to check |
|---|---|---|
| Raw bytes preserved exactly | `core/parser.py` — `raw_message` and `raw_hash` | `test_source_coverage.py` → `_check_verbatim` |
| An unknown vendor is never dropped | `core/parsers/generic_fallback.py` | `_check_fallback` — includes a binary-input guard |
| Format detected from content, not filename | `core/structured_readers.py` | `_check_sniff` — both directions |
| CEF/LEEF timestamps normalise | `core/parsers/cef_leef.py` | `_check_cef_leef` |
| The **right** detector claims each format | `core/parsers/registry.py` ordering | `_check_attribution` — 10 cases |
| OCSF classes are correct | `analytics_pipeline/ocsf.py` | `_check_ocsf` — `type_uid = class_uid × 100 + activity_id` |
| The ledger detects tampering | `analytics_pipeline/ledger/` | see §4 |

Each of those is a function in
[`app/backend/ingestion_pipeline/batch/test_source_coverage.py`](../app/backend/ingestion_pipeline/batch/test_source_coverage.py)
— readable in one sitting, and the comments say *why* each case exists, which
is usually a bug it caught.

---

## 3. The part we are most pleased with

We built a screen called **Parser Lab** to demonstrate the parser: paste a log,
watch it parse, nothing stored.

**It found three real bugs in our own parser within two days**, and a fourth
later:

1. **`reassemble()` didn't know half the formats we parse.** CEF, LEEF, JSON and
   PRI-prefixed syslog were not recognised as record *starts*, so on a mixed
   feed unrelated events were welded onto whatever line preceded them — 38 lines
   arrived as 17 records. It hid because our development corpus was homogeneous.

2. **A `.log` extension overrode the file's contents.** `sniff_format()` returned
   on the first extension match and never looked at the data, so a CSV named
   `export.log` — which is how such exports actually arrive — was parsed as
   syslog, header row and all.

3. **Raw was not verbatim.** `parse()` stored the *stripped* form, so a record
   ending `request content = ` lost its trailing space. Five of 305 records in
   one real export. Trailing whitespace distinguishes an empty field from an
   absent one, and it is exactly what a forensic hash of the original file
   disagrees with.

4. **A new broad detector silently stole another's lines.** Adding `logfmt`
   took FortiGate traffic logs (Fortinet also emits `msg=`) — and the test suite
   **stayed green**, because it only asserted that *a* named detector matched.
   Fixed, and 10 attribution cases now pin which detector must claim which
   format.

We think this is the most useful thing to know about the project: the tool that
demonstrates the system also audits it, and the tests grew from real defects
rather than from imagination.

---

## 4. The Blockchain & Cybersecurity angle

We do **not** use a blockchain, and saying we did would be overclaiming. We use
the primitive underneath one, where it earns its place: an **append-only Merkle
hash chain** over ingested events.

```bash
cd app/backend/analytics_pipeline
python -m ledger.demo          # seals real logs, edits one byte, shows detection
```

What it gives you:

- Alter one byte → that event's leaf changes → its block's Merkle root changes →
  **every block after it breaks**. Detection is deterministic, not probabilistic.
- **9 sibling hashes** prove one event was in a 500-event block *without
  revealing the other 499* — which matters when the rest of the log is classified.
- RFC 6962 domain separation (`0x00` leaves, `0x01` nodes) against second-preimage
  attacks; odd nodes **promoted, not duplicated**, avoiding Bitcoin CVE-2012-2459.

The adversarial tests are the point — a ledger that only shows unmodified data
verifying has demonstrated nothing:

| Attack | Result |
|---|---|
| Edit a message | Caught — Merkle mismatch |
| Reorder events | Caught |
| Delete an event | Caught |
| **Re-seal the block to hide the edit** | Caught — breaks the link to the next block |
| Re-seal the **entire** chain | **Not caught internally.** Self-consistent; caught only by an externally held head hash |
| Forge an inclusion proof | Rejected |

That fifth row is the honest limit of any single-writer ledger, and the reason
`head()` exists as a first-class concept. Publishing the head somewhere the
operator does not control is what turns evidence into proof — and is the one
place a real blockchain, or an RFC 3161 timestamp authority, would genuinely
belong.

---

## 5. Where the interesting code is

| File | Why it is worth a look |
|---|---|
| `core/parser.py` | The whole normalisation path in one file. Note `stored_raw` — bound once so the stored bytes and their hash cannot disagree |
| `core/parsers/generic_fallback.py` | How an *unknown* vendor still yields a usable event. Positional (RFC 3164/5424) rather than vendor-specific, plus a binary guard that refuses to invent a hostname from noise |
| `core/parser_synth.py` | Typed-token alignment — writes a regex from samples |
| `core/parser_semantics.py` | Infers what a synthesised field *means*, from position, key name and value shape. Reports the evidence for each |
| `core/multiline.py` | Where one record ends and the next begins. Comment explains why this list must be kept in step with the detector registry |
| `analytics_pipeline/ledger/merkle.py` | RFC 6962 domain separation, and why odd nodes are promoted not duplicated |
| `analytics_pipeline/ocsf.py` | Conservative classification — a wrong `class_uid` is worse than a generic one, because a SIEM routes on it |
| `analytics_pipeline/forwarder.py` | Push integration. Delivery is stated as *at-least-once*, not pretended to be exactly-once |

---

## 6. What we did not build

Stated plainly, because the boundary is part of the design:

- **No OCSF beyond 3 classes** of roughly 70.
- **No forwarder daemon** — pushing is a one-shot operation, not a continuously
  running agent.
- **Never run against the real Kafka cluster or a live DNIF instance.** Both
  clients exist; neither has talked to production infrastructure.
- **No enrichment** — no GeoIP, no threat intel, no asset lookup.
- **Our corpus is VMware/Kubernetes infrastructure, not perimeter traffic.** The
  perimeter claim rests on 10 hand-checked vendor samples, not on volume. If
  you have a real firewall feed, that is the number we would most like to
  re-measure.
- **Violin and box plots** are not offered in the chat charts: the query results
  are aggregates, so a distribution plot would be inventing spread the data does
  not contain.

---

## 7. Numbers, with their conditions

Measured on an **Intel i5-12450H, 8 cores, no GPU**. See
[VERIFY.md](VERIFY.md) for the command behind each.

| | | Condition |
|---|---|---|
| 3,513 events/sec | normalisation | **1 core, parse stage only** |
| 16,826 events/sec | normalisation | 8 cores — 5.83× measured |
| 229 events/sec | parse + graph write | semantic search off |
| p50 0.24 ms · p99 0.82 ms | parse latency | per record |
| 97.4% | named-detector coverage | on live infrastructure logs |
| 3 ms | unseen vendor → working parser | zero human edits |
| 25% → 10% | speech-to-text word error rate | after vocabulary seeding |

**Please do not read 3,513/sec as system throughput.** It is the normalisation
stage on one core. The end-to-end figure with semantic search enabled is
**3 events/sec**, because embedding on CPU is 94.5% of the wall clock — which
is why embeddings are optional per deployment.
