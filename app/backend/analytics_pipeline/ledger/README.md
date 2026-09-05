# Tamper-evident log ledger

The integrity half of the Blockchain & Cybersecurity theme. Run
`python -m ledger.demo` from `analytics_pipeline/` to see it seal real logs,
tamper with one byte, and locate the damage.

## Why this exists

ULPF's central claim is that an investigator can always prove what a device
actually said. Before this module, the system could prove a normalized field
matched what it had **stored** — it could not prove that storage had not been
altered afterwards.

That gap sits exactly where the project claims its value. *"We kept the
original bytes"* is a weaker statement than *"we can show nobody changed
them"*, and in a forensic or compliance review it is the second one that
counts.

## What it is

An **append-only, hash-chained Merkle ledger** over ingested events.

```
event ──► leaf   = SHA256(0x00 ‖ canonical(event))
leaves ─► root   = Merkle tree, RFC 6962 domain separation
block  ─► hash   = SHA256(header incl. prev_hash ‖ merkle_root)
blocks ─► chain  = each block commits to the previous block's hash
chain  ─► head   = the single hash that has to be protected
```

Alter one raw byte → that event's leaf changes → its block's Merkle root
changes → the block's own hash changes → **every block after it breaks.**
Detection is deterministic, not probabilistic.

## What it is NOT

This is not a distributed blockchain, and the deck should not call it one.

| Blockchain has | This has |
|---|---|
| Many writers, consensus | One writer, no consensus |
| Peer-to-peer replication | A single ledger file + graph copy |
| Proof of work / stake | None — no mining, no tokens |
| Trustless verification | Verification against a head you must obtain honestly |

What it *does* provide is the primitive that actually matters for logs:
**append-only, tamper-evident storage with per-event inclusion proofs.**

**Tamper-evident, not tamper-proof.** Someone with write access to the store
can still rewrite an event. What they cannot do is rewrite it *without the
chain showing it* — unless they also re-seal every subsequent block **and**
replace every externally held copy of the head hash.

Publishing the head somewhere the log's operator does not control is what
upgrades evidence into proof. That is an integration point, and it is named
rather than pretended:

- a second system under different administrative control
- a signed file held by the auditor
- a timestamping authority (RFC 3161)
- a public chain, if one is available — the only place a real blockchain
  earns its place in this design

## Design decisions worth defending

**RFC 6962 domain separation.** Leaves are hashed with a `0x00` prefix and
internal nodes with `0x01`. Without this an attacker can present an internal
node as a leaf and produce a valid proof for data that was never in the tree —
the classic Merkle second-preimage attack. Certificate Transparency specifies
exactly this; we follow it.

**Odd nodes are promoted, not duplicated.** Duplicating the last node when a
level has an odd count is what made Bitcoin's CVE-2012-2459 possible: two
different trees yield the same root, so *"the root matches"* stops meaning
*"the data matches"*. An odd node is carried up unchanged.

**Only nine fields are sealed.** `SEALED_FIELDS` covers identity, time, origin
and content — not `processed_at` or `embedding`, which legitimately change on
re-parse and re-embedding. Sealing those would make the ledger flag honest
re-ingestion as tampering, and a control that cries wolf is a control that
gets switched off.

**Canonical JSON.** Sorted keys, no insignificant whitespace. The same event
must digest identically whichever service serialised it, or verification fails
for reasons that have nothing to do with tampering.

**Empty blocks are refused.** An empty block would advance the chain height
while committing to no data — a hole an attacker could use to pad a rewritten
chain to the expected length.

**`raw_hash` is separate from the Merkle leaf.** It is the plain SHA-256 of
the raw bytes and is meaningful on its own: an investigator holding the
original log file can recompute it and confirm the stored event came from that
file, without touching the ledger or trusting this system at all.

**The verifier does not import ULPF.** `verify` and `check` work with no ULPF
code present. A verifier should not have to run the system it is auditing.

## Threat model

| Attacker | Outcome |
|---|---|
| Edits a message in the store | **Caught** — Merkle mismatch at that block |
| Reorders events | **Caught** — order is part of the tree |
| Deletes an event | **Caught** — merkle/count mismatch |
| Re-seals the block to hide an edit | **Caught** — breaks the link to the next block |
| Re-seals the *entire chain* | **Not caught internally** — the rewritten chain is self-consistent. Caught only by an externally held head |
| Has no store access, tampers with the log file on the device | Out of scope — the ledger attests to what was ingested, not to what the device chose to emit |

The fifth row is the honest limit of any single-writer ledger, and the reason
`head()` exists as a first-class concept.

## Cost

Per event: one SHA-256 over ~200–2,000 bytes, plus one tree-level hash
amortised. Measured: **sealing 5,000 real events into 10 blocks is sub-second** on the same i5-12450H used for the other ULPF benchmarks, against
a parse stage that runs at 3,513 events/sec/core. Storage is 64 hex chars per
event leaf plus a small block header — roughly 0.5% of the normalized record
size.

Inclusion proof size is log₂(block size): a 500-event block yields a
**9-hash proof**, measured.
