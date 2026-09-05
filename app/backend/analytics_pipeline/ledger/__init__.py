"""Tamper-evident ledger for ULPF events.

See docs/DESIGN.md for what this guarantees and, more importantly, what it
does not. Short version: append-only, tamper-EVIDENT storage with per-event
Merkle inclusion proofs. Not a distributed blockchain - one writer, no
consensus, no replication.
"""

from .chain import (GENESIS_HASH, LEDGER_VERSION, SEALED_FIELDS, block_hash,
                    build_chain, check_proof, event_digest, event_leaf, head,
                    prove_event, raw_hash, seal_block, verify_chain)
from .merkle import (inclusion_proof, leaf_hash, node_hash, root,
                     tree_summary, verify_proof)

__all__ = [
    'GENESIS_HASH', 'LEDGER_VERSION', 'SEALED_FIELDS',
    'raw_hash', 'event_digest', 'event_leaf', 'seal_block', 'block_hash',
    'build_chain', 'head', 'verify_chain', 'prove_event', 'check_proof',
    'leaf_hash', 'node_hash', 'root', 'inclusion_proof', 'verify_proof',
    'tree_summary',
]
