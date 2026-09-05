"""
merkle.py - Merkle tree over event hashes, with inclusion proofs.

A plain hash chain proves the sequence has not been altered, but to check one
event you have to replay every event before it. A Merkle tree lets a single
event be proved against a sealed root using only log2(n) sibling hashes, which
is what makes the guarantee usable in an investigation: an analyst can hand
over one event plus a short proof, and the recipient verifies it against a
root they already trust - without being given the rest of the log, which may
be classified, enormous, or both.

That property is the reason this is worth building rather than just storing a
SHA-256 per record.

Second-preimage resistance
--------------------------
Leaves and internal nodes are hashed with different prefixes (0x00 and 0x01).
Without that, an attacker can present an internal node as if it were a leaf
and produce a valid proof for data that was never in the tree - the classic
Merkle second-preimage attack, and the reason Certificate Transparency
(RFC 6962) specifies exactly this domain separation. We follow RFC 6962.

Odd node counts
---------------
An odd node is promoted to the next level unchanged rather than duplicated.
Duplicating the last node is what made Bitcoin's CVE-2012-2459 possible: two
different trees produce the same root, so "the root matches" stops meaning
"the data matches".
"""

import hashlib
from typing import Dict, List, Optional, Sequence, Tuple

#  RFC 6962 domain separation: a leaf and an internal node must never hash the
#  same way, or an internal node can be replayed as a leaf.
LEAF_PREFIX = b'\x00'
NODE_PREFIX = b'\x01'


def _h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def leaf_hash(payload: bytes) -> str:
    """Hash of one event's bytes, as a Merkle leaf."""
    return _h(LEAF_PREFIX + payload)


def node_hash(left: str, right: str) -> str:
    """Hash of two child hashes, as an internal node."""
    return _h(NODE_PREFIX + bytes.fromhex(left) + bytes.fromhex(right))


def build_levels(leaves: Sequence[str]) -> List[List[str]]:
    """Every level of the tree, leaves first, root last.

    Returns [[]] for no leaves - an empty tree has no root, and inventing one
    (the hash of nothing) would let an empty block claim to seal data.
    """
    if not leaves:
        return [[]]
    levels = [list(leaves)]
    while len(levels[-1]) > 1:
        cur = levels[-1]
        nxt = []
        for i in range(0, len(cur) - 1, 2):
            nxt.append(node_hash(cur[i], cur[i + 1]))
        if len(cur) % 2:
            # Promoted, not duplicated - see the module note on CVE-2012-2459.
            nxt.append(cur[-1])
        levels.append(nxt)
    return levels


def root(leaves: Sequence[str]) -> Optional[str]:
    levels = build_levels(leaves)
    return levels[-1][0] if levels[-1] else None


def inclusion_proof(leaves: Sequence[str], index: int) -> List[Dict[str, str]]:
    """The sibling hashes needed to recompute the root from one leaf.

    Each step records which side the sibling was on, because
    node_hash(a, b) != node_hash(b, a) and a proof that forgot the order would
    verify only half the time.
    """
    if not 0 <= index < len(leaves):
        raise IndexError('leaf index %d out of range for %d leaves'
                         % (index, len(leaves)))
    proof: List[Dict[str, str]] = []
    levels = build_levels(leaves)
    idx = index
    for level in levels[:-1]:
        if idx % 2 == 0:
            sibling_idx = idx + 1
            side = 'right'
        else:
            sibling_idx = idx - 1
            side = 'left'
        if sibling_idx < len(level):
            proof.append({'side': side, 'hash': level[sibling_idx]})
            idx //= 2
        else:
            # This node was promoted rather than paired; its position in the
            # next level is not idx//2.
            idx = (idx + 1) // 2
    return proof


def verify_proof(leaf: str, proof: Sequence[Dict[str, str]], expected_root: str) -> bool:
    """Recomputes the root from a leaf and its siblings."""
    current = leaf
    for step in proof:
        sibling = step['hash']
        if step['side'] == 'right':
            current = node_hash(current, sibling)
        else:
            current = node_hash(sibling, current)
    return current == expected_root


def tree_summary(leaves: Sequence[str]) -> Tuple[Optional[str], int, int]:
    """(root, leaf count, depth) - for reporting on a sealed block."""
    levels = build_levels(leaves)
    return (levels[-1][0] if levels[-1] else None,
            len(leaves),
            len(levels) - 1 if leaves else 0)
