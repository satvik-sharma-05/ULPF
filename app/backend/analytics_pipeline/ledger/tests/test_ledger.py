"""
Tests for the tamper-evident ledger.

The important cases here are the adversarial ones. A ledger that only proves
"unmodified data verifies" has demonstrated nothing - the whole point is that
modified data must FAIL, including the modifications an attacker would
actually attempt: editing a message, swapping two events, deleting one,
re-sealing a block to cover the edit, and forging an inclusion proof for an
event that was never present.

Run:  python -m tests.test_ledger
"""

import copy
import os
import sys

# analytics_pipeline/ - two levels up from ledger/tests/. Inserting only
# one level put ledger/ itself on the path, so `from ledger import ...`
# could not resolve when the file was run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from ledger import (GENESIS_HASH, block_hash, build_chain, check_proof,  # noqa: E402
                    event_leaf, head, node_hash, prove_event, raw_hash,
                    root, seal_block, verify_chain)
from ledger.merkle import (build_levels, inclusion_proof, leaf_hash,  # noqa: E402
                           verify_proof)

FAILURES = []
CHECKS = 0


def check(condition, message):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(message)


def events(n, start=0):
    return [{
        'id': 'log-%04d' % i,
        'timestamp': '2026-09-04T10:%02d:00+00:00' % (i % 60),
        'hostname': 'fw-edge-%02d' % (i % 4),
        'source_type': 'Firewall',
        'process': 'policy-engine',
        'severity': 'WARNING' if i % 3 else 'INFO',
        'message': 'connection %d denied by rule DMZ-DENY-07' % i,
        'matched_format': 'pri_syslog',
        'raw_message': '<134>Sep  4 10:%02d:00 fw-edge-%02d policy-engine: '
                       'connection %d denied' % (i % 60, i % 4, i),
    } for i in range(start, start + n)]


# --------------------------------------------------------------- merkle
def test_merkle():
    check(root([]) is None, 'an empty tree must have no root')

    one = [leaf_hash(b'a')]
    check(root(one) == one[0], 'a single-leaf root is that leaf')

    # Domain separation: a leaf and an internal node over the same bytes must
    # not collide, or an internal node can be replayed as a leaf (RFC 6962).
    a, b = leaf_hash(b'a'), leaf_hash(b'b')
    check(node_hash(a, b) != leaf_hash(bytes.fromhex(a) + bytes.fromhex(b)),
          'leaf and node hashing must be domain-separated')

    # Order matters, or a proof verifies with siblings swapped.
    check(node_hash(a, b) != node_hash(b, a), 'node hashing must not commute')

    # Odd counts are promoted, not duplicated (CVE-2012-2459).
    three = [leaf_hash(bytes([i])) for i in range(3)]
    four_dup = three + [three[-1]]
    check(root(three) != root(four_dup),
          'promoting an odd node must not equal duplicating it')

    # Every leaf in a range of sizes must prove against the root.
    for n in (1, 2, 3, 4, 5, 7, 8, 9, 16, 17, 33):
        leaves = [leaf_hash(bytes([i % 251])) for i in range(n)]
        r = root(leaves)
        for i in range(n):
            ok = verify_proof(leaves[i], inclusion_proof(leaves, i), r)
            check(ok, 'inclusion proof failed for leaf %d of %d' % (i, n))

    # A proof must not verify a leaf that is not in the tree.
    leaves = [leaf_hash(bytes([i])) for i in range(8)]
    r = root(leaves)
    forged = leaf_hash(b'never-ingested')
    check(not verify_proof(forged, inclusion_proof(leaves, 0), r),
          'a foreign leaf must not verify against the root')


# --------------------------------------------------------------- chain
def test_chain_valid():
    chain = build_chain([events(4), events(4, 4), events(3, 8)])
    result = verify_chain(chain)
    check(result['valid'], 'an untampered chain must verify: %s' % result['problems'])
    check(result['blocks'] == 3, 'expected 3 blocks, got %d' % result['blocks'])
    check(result['events'] == 11, 'expected 11 events, got %d' % result['events'])
    check(chain[0]['prev_hash'] == GENESIS_HASH, 'block 0 must link to genesis')
    check(chain[1]['prev_hash'] == chain[0]['hash'], 'block 1 must link to block 0')
    check(head(chain) == chain[-1]['hash'], 'head must be the last block hash')


def test_empty_block_refused():
    try:
        seal_block([], 0, GENESIS_HASH)
        check(False, 'sealing an empty block must raise')
    except ValueError:
        check(True, '')


# --------------------------------------------------------------- tampering
def test_message_edited():
    """The base case: someone rewrites a log message after the fact."""
    chain = build_chain([events(4), events(4, 4)])
    batch = events(4)
    batch[2]['message'] = 'connection 2 ALLOWED by rule DMZ-DENY-07'
    # Re-derive the leaf as the store would after the edit.
    chain[0]['leaves'][2] = event_leaf(batch[2])
    result = verify_chain(chain)
    check(not result['valid'], 'an edited message must break the ledger')
    check(result['first_break_at_height'] == 0,
          'the break must be reported at the block that was edited')
    check(any(p['kind'] == 'merkle_mismatch' for p in result['problems']),
          'an edited event must surface as a merkle mismatch')


def test_events_reordered():
    """Reordering changes nothing about the events themselves, and must
    still be caught - order is evidence in an incident timeline."""
    chain = build_chain([events(4)])
    chain[0]['leaves'][0], chain[0]['leaves'][1] = \
        chain[0]['leaves'][1], chain[0]['leaves'][0]
    check(not verify_chain(chain)['valid'], 'reordering events must be caught')


def test_event_deleted():
    chain = build_chain([events(4)])
    del chain[0]['leaves'][1]
    result = verify_chain(chain)
    check(not result['valid'], 'deleting an event must be caught')
    check(any(p['kind'] in ('merkle_mismatch', 'count_mismatch')
              for p in result['problems']),
          'a deletion must surface as a merkle or count mismatch')


def test_block_reseal_still_caught():
    """The attacker who knows how the ledger works.

    Editing an event breaks the Merkle root, so a competent attacker re-seals
    the block to match. That fixes the root and the block's own hash - and
    breaks the LINK to every block after it, which is the entire reason the
    blocks are chained rather than independently signed.
    """
    chain = build_chain([events(4), events(4, 4), events(4, 8)])
    tampered = copy.deepcopy(chain)
    batch = events(4)
    batch[1]['message'] = 'nothing to see here'
    resealed = seal_block(batch, 0, GENESIS_HASH,
                          sealed_at=tampered[0]['sealed_at'])
    tampered[0] = resealed

    result = verify_chain(tampered)
    check(not result['valid'], 're-sealing a block must not launder the edit')
    check(any(p['kind'] == 'broken_link' for p in result['problems']),
          're-sealing must surface as a broken link to the next block')
    check(result['first_break_at_height'] == 1,
          'the break must appear at the FOLLOWING block, which is what the '
          'chain exists to detect')


def test_full_rewrite_changes_head():
    """The attacker who re-seals the whole chain.

    Nothing in the ledger can stop this - they control every block. What they
    cannot control is a head hash already published elsewhere, which is why
    head() exists and why DESIGN.md insists on publishing it.
    """
    original = build_chain([events(4), events(4, 4)])
    rewritten_batches = [events(4), events(4, 4)]
    rewritten_batches[0][1]['message'] = 'redacted'
    rewritten = build_chain(rewritten_batches)
    check(verify_chain(rewritten)['valid'],
          'a fully rewritten chain is internally consistent - that is the point')
    check(head(rewritten) != head(original),
          'a rewritten chain must produce a different head, so an externally '
          'held head detects it')


def test_header_tampering():
    chain = build_chain([events(4), events(4, 4)])
    chain[0]['event_count'] = 99
    result = verify_chain(chain)
    check(not result['valid'], 'editing a block header must be caught')
    check(any(p['kind'] == 'header_altered' for p in result['problems']),
          'a header edit must surface as header_altered')


# --------------------------------------------------------------- proofs
def test_inclusion_proofs():
    batches = [events(5), events(5, 5)]
    chain = build_chain(batches)

    for batch in batches:
        for ev in batch:
            proof = prove_event(chain, ev)
            check(proof is not None, 'event %s must be provable' % ev['id'])
            if proof:
                check(check_proof(proof),
                      'inclusion proof for %s must verify' % ev['id'])
                check(proof['raw_hash'] == raw_hash(ev['raw_message']),
                      'proof must carry the raw-bytes hash for %s' % ev['id'])

    # An event that was never ingested must not be provable.
    outsider = events(1, 999)[0]
    check(prove_event(chain, outsider) is None,
          'an event never ingested must not be provable')

    # A proof whose root has been swapped must fail.
    proof = prove_event(chain, batches[0][0])
    proof['merkle_root'] = '0' * 64
    check(not check_proof(proof), 'a proof against a forged root must fail')


def test_raw_hash_is_independent():
    """raw_hash must be recomputable from the original file alone, with no
    reference to the ledger - that is what makes it useful to an investigator
    who has the log but not the system."""
    ev = events(1)[0]
    check(raw_hash(ev['raw_message']) == raw_hash(ev['raw_message']),
          'raw_hash must be deterministic')
    check(raw_hash('a') != raw_hash('a '),
          'raw_hash must distinguish a trailing space - that difference is '
          'exactly what the losslessness guarantee is about')


def main():
    for fn in (test_merkle, test_chain_valid, test_empty_block_refused,
               test_message_edited, test_events_reordered, test_event_deleted,
               test_block_reseal_still_caught, test_full_rewrite_changes_head,
               test_header_tampering, test_inclusion_proofs,
               test_raw_hash_is_independent):
        fn()

    print('%d checks across %d ledger tests' % (CHECKS, 11))
    if FAILURES:
        print('\nFAILED:')
        for f in FAILURES:
            print('  ' + f)
        return 1
    print('All passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
