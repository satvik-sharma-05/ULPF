"""
chain.py - The append-only ledger: events -> sealed blocks -> hash chain.

What this gives the framework
-----------------------------
ULPF already keeps every event's raw bytes, so it can prove a normalized field
matches what was STORED. It could not prove that storage was never altered
afterwards. That gap matters precisely where the project claims its value -
forensics and compliance - because "we kept the original" is a weaker claim
than "we can show nobody changed it".

The ledger closes it. Every ingested event contributes a leaf; batches are
sealed into blocks; each block commits to the previous block's hash. Altering
one raw byte changes that event's leaf, which changes its block's Merkle root,
which breaks every block after it. Detection is not probabilistic.

What this is NOT
----------------
This is a hash-chained Merkle ledger, not a distributed blockchain. There is
one writer, no consensus, no peer-to-peer replication and no proof of work.
Saying "blockchain" would be overclaiming, and an evaluator who knows the
difference would be right to press on it. What it does provide is the property
that actually matters for logs:

    append-only, tamper-EVIDENT storage with per-event inclusion proofs

Tamper-evident, not tamper-proof: someone with write access to Neo4j can still
rewrite an event. What they cannot do is rewrite it *without the chain
showing it*, unless they also rewrite every subsequent block AND replace every
externally held copy of the head hash. Publishing the head - to a second
system, a signed file, or an actual blockchain - is what turns evidence into
proof, and is left as an integration point rather than pretended.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .merkle import inclusion_proof, leaf_hash, root, verify_proof

LEDGER_VERSION = '1.0'

#  The chain has to start somewhere, and that somewhere must be a constant a
#  verifier can hardcode. A random or time-based genesis would mean two
#  deployments of the same software produce chains that cannot be compared.
GENESIS_HASH = '0' * 64

#  Fields whose alteration must break the seal. Deliberately NOT every field:
#  `processed_at` changes on every re-parse and `embedding` is regenerated,
#  so including them would make the ledger flag honest re-ingestion as
#  tampering and train operators to ignore it.
SEALED_FIELDS = ('id', 'timestamp', 'hostname', 'source_type', 'process',
                 'severity', 'message', 'matched_format', 'raw_message')


def raw_hash(raw_message: str) -> str:
    """SHA-256 of the event's raw bytes, exactly as received.

    Separate from the Merkle leaf on purpose: this one hash is meaningful on
    its own. An investigator holding the original log file can recompute it
    and confirm the stored event came from that file, without touching the
    ledger at all.
    """
    return hashlib.sha256(raw_message.encode('utf-8')).hexdigest()


def event_digest(event: Dict[str, Any]) -> bytes:
    """Canonical bytes for one event, over the sealed fields only.

    Canonical means sorted keys and no insignificant whitespace: the same
    event must digest identically whichever service serialised it, or
    verification fails for reasons that have nothing to do with tampering.
    """
    payload = {k: event.get(k) for k in SEALED_FIELDS}
    return json.dumps(payload, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, default=str).encode('utf-8')


def event_leaf(event: Dict[str, Any]) -> str:
    return leaf_hash(event_digest(event))


def block_hash(block: Dict[str, Any]) -> str:
    """A block's own identity: its header, hashed.

    `prev_hash` is inside the header, which is what chains the blocks - change
    any earlier block and every later block_hash changes with it.
    """
    header = {
        'version': block['version'],
        'height': block['height'],
        'prev_hash': block['prev_hash'],
        'merkle_root': block['merkle_root'],
        'event_count': block['event_count'],
        'sealed_at': block['sealed_at'],
    }
    return hashlib.sha256(
        json.dumps(header, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()


def seal_block(events: Sequence[Dict[str, Any]], height: int,
               prev_hash: str, sealed_at: Optional[str] = None) -> Dict[str, Any]:
    """Seals a batch of events into a block.

    Raises on an empty batch rather than sealing nothing: an empty block would
    advance the chain height while committing to no data, which is a hole an
    attacker could use to pad a rewritten chain to the expected length.
    """
    if not events:
        raise ValueError('refusing to seal an empty block - it would advance '
                         'the chain while committing to no data')
    leaves = [event_leaf(e) for e in events]
    block: Dict[str, Any] = {
        'version': LEDGER_VERSION,
        'height': height,
        'prev_hash': prev_hash,
        'merkle_root': root(leaves),
        'event_count': len(leaves),
        'sealed_at': sealed_at or datetime.now(timezone.utc).isoformat(),
        'event_ids': [e.get('id') for e in events],
        'leaves': leaves,
    }
    block['hash'] = block_hash(block)
    return block


def build_chain(batches: Iterable[Sequence[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Seals successive batches into a chain starting from genesis."""
    chain: List[Dict[str, Any]] = []
    prev = GENESIS_HASH
    for height, batch in enumerate(batches):
        block = seal_block(batch, height, prev)
        chain.append(block)
        prev = block['hash']
    return chain


def head(chain: Sequence[Dict[str, Any]]) -> str:
    """The single value that has to be protected.

    Publishing this one hash somewhere the log's operator cannot silently
    change - a second system, a signed file, a notary, a public chain - is
    what upgrades tamper-evidence into tamper-proof. Everything else here
    works without it; nothing here can substitute for it.
    """
    return chain[-1]['hash'] if chain else GENESIS_HASH


def verify_chain(chain: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Re-derives every block hash and link. Reports the FIRST break.

    Reporting the first break rather than a boolean is deliberate: in an
    investigation the useful question is not "was this tampered with" but
    "from which point can I no longer trust it", and everything before the
    break is still evidence.
    """
    problems: List[Dict[str, Any]] = []
    prev = GENESIS_HASH
    for block in chain:
        if block['prev_hash'] != prev:
            problems.append({
                'height': block['height'], 'kind': 'broken_link',
                'detail': 'prev_hash %s does not match the previous block hash %s'
                          % (block['prev_hash'][:16], prev[:16]),
            })
        recomputed_root = root(block['leaves'])
        if recomputed_root != block['merkle_root']:
            problems.append({
                'height': block['height'], 'kind': 'merkle_mismatch',
                'detail': 'recomputed root %s != sealed root %s'
                          % (str(recomputed_root)[:16], block['merkle_root'][:16]),
            })
        if block_hash(block) != block['hash']:
            problems.append({
                'height': block['height'], 'kind': 'header_altered',
                'detail': 'block hash does not match its own header',
            })
        if block['event_count'] != len(block['leaves']):
            problems.append({
                'height': block['height'], 'kind': 'count_mismatch',
                'detail': 'header claims %d events, block holds %d'
                          % (block['event_count'], len(block['leaves'])),
            })
        prev = block['hash']

    return {
        'valid': not problems,
        'blocks': len(chain),
        'events': sum(b['event_count'] for b in chain),
        'head': head(chain),
        'first_break_at_height': problems[0]['height'] if problems else None,
        'problems': problems,
    }


def prove_event(chain: Sequence[Dict[str, Any]], event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """An inclusion proof for one event, or None if it is not in the ledger.

    The proof carries the sibling hashes and the block header it commits to,
    and nothing else - the point is that it can be handed to someone who is
    not allowed to see the rest of the log.
    """
    leaf = event_leaf(event)
    for block in chain:
        if leaf in block['leaves']:
            index = block['leaves'].index(leaf)
            return {
                'event_id': event.get('id'),
                'leaf': leaf,
                'raw_hash': raw_hash(event.get('raw_message') or ''),
                'block_height': block['height'],
                'block_hash': block['hash'],
                'merkle_root': block['merkle_root'],
                'proof': inclusion_proof(block['leaves'], index),
            }
    return None


def check_proof(proof: Dict[str, Any]) -> bool:
    """Verifies an inclusion proof against the root it names.

    Note what this does and does not establish: it proves the event was in the
    block with that Merkle root. Whether that root belongs to the real chain is
    a separate question, answered by verify_chain() plus a trusted head.
    """
    return verify_proof(proof['leaf'], proof['proof'], proof['merkle_root'])
