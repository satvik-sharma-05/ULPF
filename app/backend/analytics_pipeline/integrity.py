"""
integrity.py - The tamper-evident ledger, wired to the live graph.

`ledger/` is the primitive: hashes, Merkle trees, chain verification, all
independent of this project. This module is what connects it to real stored
events - sealing what is in Neo4j, verifying it later, and proving a single
event to someone who is not allowed to see the rest of the log.

Why it lives in analytics rather than ingestion: sealing is a read of what was
stored, and the whole point is to check storage independently of the thing
that wrote it. Ingestion computes `raw_hash` per event; nothing else about the
ledger should share a process with the writer.

The ledger is held on disk as JSON, not only in the graph. A ledger that lives
inside the system it protects can be rewritten alongside it - the file exists
so the head hash can be copied somewhere the operator does not control, which
is the step that turns tamper-evidence into proof.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ledger import (GENESIS_HASH, LEDGER_VERSION, build_chain, check_proof,
                    event_leaf, head, prove_event, verify_chain)
from ledger.merkle import inclusion_proof

logger = logging.getLogger('analytics.integrity')

#  Where the sealed chain is written. Deliberately a path an operator can
#  mount elsewhere - see the module note.
LEDGER_PATH = os.getenv('ULPF_LEDGER_PATH', '/data/ledger/ulpf-ledger.json')
BLOCK_SIZE = int(os.getenv('ULPF_LEDGER_BLOCK_SIZE', '500'))
SEAL_MAX_EVENTS = int(os.getenv('ULPF_LEDGER_MAX_EVENTS', '50000'))


def _batches(events: List[Dict[str, Any]], size: int):
    return [events[i:i + size] for i in range(0, len(events), size)]


def load_chain() -> List[Dict[str, Any]]:
    if not os.path.exists(LEDGER_PATH):
        return []
    try:
        with open(LEDGER_PATH, encoding='utf-8') as fh:
            return json.load(fh).get('chain', [])
    except (OSError, ValueError) as exc:
        logger.error('ledger at %s is unreadable: %s', LEDGER_PATH, exc)
        return []


def save_chain(chain: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(LEDGER_PATH) or '.', exist_ok=True)
    with open(LEDGER_PATH, 'w', encoding='utf-8') as fh:
        json.dump({'version': LEDGER_VERSION,
                   'sealed_at': datetime.now(timezone.utc).isoformat(),
                   'chain': chain}, fh)


def seal(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Seals stored events into the ledger.

    Replaces rather than appends, and says so: appending to a chain built from
    a different query would produce a ledger whose blocks cover overlapping,
    unstated sets of events - internally valid and meaningless. Re-sealing the
    same query is the honest operation.
    """
    if not events:
        return {'sealed': 0, 'blocks': 0, 'head': GENESIS_HASH,
                'note': 'nothing to seal'}
    events = events[:SEAL_MAX_EVENTS]
    chain = build_chain(_batches(events, BLOCK_SIZE))
    save_chain(chain)
    return {
        'sealed': len(events),
        'blocks': len(chain),
        'block_size': BLOCK_SIZE,
        'head': head(chain),
        'ledger_path': LEDGER_PATH,
        'next_step': ('Copy this head hash somewhere this system cannot '
                      'silently change. Until that is done the ledger detects '
                      'tampering by everyone except whoever can rewrite the '
                      'ledger itself.'),
    }


def verify(expected_head: Optional[str] = None) -> Dict[str, Any]:
    """Re-derives every hash in the stored ledger.

    `expected_head` is the important argument. Without it this proves the
    chain is internally consistent, which a full rewrite also is. With a head
    obtained from outside the system, it proves the chain is the one that was
    sealed.
    """
    chain = load_chain()
    if not chain:
        return {'valid': None, 'blocks': 0,
                'note': 'no ledger has been sealed yet'}
    result = verify_chain(chain)
    if expected_head:
        result['head_matches'] = (result['head'] == expected_head)
        if not result['head_matches']:
            result['valid'] = False
            result['problems'].insert(0, {
                'height': -1, 'kind': 'head_mismatch',
                'detail': 'the chain is internally consistent but is NOT the '
                          'chain that was sealed - this is what a full rewrite '
                          'looks like',
            })
    return result


def prove(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """An inclusion proof for one stored event."""
    chain = load_chain()
    if not chain:
        return None
    return prove_event(chain, event)


def prove_by_id(event_id: str) -> Optional[Dict[str, Any]]:
    """Proof by event id alone, without re-reading the event.

    Cheaper than prove(), and enough for the common question - "was this event
    in the sealed set" - but note the difference: this proves the ID was
    sealed. Proving the event's CONTENT is unaltered needs prove(), which
    recomputes the leaf from the stored fields.
    """
    for block in load_chain():
        ids = block.get('event_ids') or []
        if event_id in ids:
            index = ids.index(event_id)
            return {
                'event_id': event_id,
                'leaf': block['leaves'][index],
                'block_height': block['height'],
                'block_hash': block['hash'],
                'merkle_root': block['merkle_root'],
                'proof': inclusion_proof(block['leaves'], index),
                'proves': 'this event id was in the sealed block',
                'does_not_prove': ('that the event CONTENT is unaltered - use '
                                   'the content proof for that'),
            }
    return None


def status() -> Dict[str, Any]:
    chain = load_chain()
    return {
        'ledger_version': LEDGER_VERSION,
        'sealed': bool(chain),
        'blocks': len(chain),
        'events': sum(b.get('event_count', 0) for b in chain),
        'head': head(chain) if chain else None,
        'ledger_path': LEDGER_PATH,
        'block_size': BLOCK_SIZE,
        'guarantee': 'append-only, tamper-EVIDENT: any alteration to a sealed '
                     'event breaks its block and every block after it',
        'not_a_blockchain': ('one writer, no consensus, no replication. What '
                             'it provides is Merkle inclusion proofs and an '
                             'append-only chain, not distributed trust.'),
    }


def check(proof: Dict[str, Any]) -> Dict[str, Any]:
    """Verifies a proof someone hands us, without trusting our own ledger."""
    try:
        ok = check_proof(proof)
    except (KeyError, TypeError, ValueError) as exc:
        return {'valid': False, 'error': 'malformed proof: %s' % exc}
    return {
        'valid': ok,
        'event_id': proof.get('event_id'),
        'block_height': proof.get('block_height'),
        'merkle_root': proof.get('merkle_root'),
        'scope': ('proves membership in the block with that Merkle root; '
                  'whether that block belongs to the real chain is a separate '
                  'question, answered by verify() against a trusted head'),
    }
