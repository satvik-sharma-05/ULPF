"""
ledger/demo.py - Seal real events, tamper with one, watch the ledger catch it.

    python -m ledger.demo                       # uses ../../../../sample_logs.txt
    python -m ledger.demo --logs /path/to/logs  # or any file or directory

Runs in about a second and needs no database. It exists because "tamper-evident"
is a claim that should be demonstrated rather than asserted, and because the
demonstration that matters is the FAILING case: a ledger that only shows
unmodified data verifying has proved nothing.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import (build_chain, event_leaf, head,  # noqa: E402
                    prove_event, verify_chain)

#  Small blocks so a short sample still produces a chain with several links -
#  the point being demonstrated is that a break propagates ACROSS blocks.
BLOCK_SIZE = 10


def _load_events(path):
    """Parses logs with the ingestion pipeline if it is present.

    The ledger itself does not depend on the parser - it seals dictionaries -
    so this import is local and its absence is reported rather than raised.
    """
    ingestion = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        '..', '..', 'ingestion_pipeline'))
    if not os.path.isdir(ingestion):
        raise SystemExit('ingestion_pipeline not found at %s - the demo needs '
                         'it to parse raw logs.' % ingestion)
    sys.path.insert(0, ingestion)
    from core.multiline import reassemble
    from core.parser import LogParser

    parser = LogParser()
    files = [path] if os.path.isfile(path) else [
        os.path.join(r, f) for r, _d, fs in os.walk(path) for f in fs]

    events = []
    for f in files[:200]:
        try:
            with open(f, encoding='utf-8', errors='replace') as fh:
                lines = [l.rstrip('\n') for i, l in enumerate(fh) if i < 60]
        except OSError:
            continue
        events.extend(parser.parse(r) for r in reassemble(lines) if r.strip())
        if len(events) >= 400:
            break
    return events[:400]


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    default = os.path.abspath(os.path.join(here, '..', '..', '..', '..',
                                           'sample_logs.txt'))
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--logs', default=default)
    args = ap.parse_args()

    print('=' * 70)
    print('TAMPER-EVIDENCE DEMONSTRATION')
    print('=' * 70)

    events = _load_events(args.logs)
    if len(events) < 2:
        raise SystemExit('need at least 2 events; got %d from %s'
                         % (len(events), args.logs))

    batches = [events[i:i + BLOCK_SIZE] for i in range(0, len(events), BLOCK_SIZE)]
    chain = build_chain(batches)
    print('\n1. Sealed %d real events into %d blocks' % (len(events), len(chain)))
    print('   head %s' % head(chain))

    result = verify_chain(chain)
    print('\n2. Verifying the untouched ledger')
    print('   valid: %s   (%d blocks, %d events re-derived)'
          % (result['valid'], result['blocks'], result['events']))

    proof = prove_event(chain, events[3])
    if proof:
        print('\n3. Proving ONE event without revealing the others')
        print('   event %s' % proof['event_id'])
        print('   %d sibling hashes prove it was in block %d'
              % (len(proof['proof']), proof['block_height']))
        print('   the other %d events in that block are not disclosed'
              % (chain[proof['block_height']]['event_count'] - 1))

    victim = events[2]
    print('\n4. Tampering: changing ONE character in one stored event')
    print('   before : %s' % str(victim.get('message'))[:60])
    edited = dict(victim)
    edited['message'] = str(victim.get('message') or '') + '.'
    print('   after  : %s' % edited['message'][:60])
    chain[0]['leaves'][2] = event_leaf(edited)

    result = verify_chain(chain)
    print('\n5. Re-verifying')
    print('   valid  : %s' % result['valid'])
    print('   first break at block height: %s' % result['first_break_at_height'])
    for p in result['problems'][:2]:
        print('   %-16s %s' % (p['kind'], p['detail']))

    print('\n' + '=' * 70)
    print('Detection is deterministic, not probabilistic: one byte changes the')
    print('leaf, which changes the Merkle root, which breaks every later block.')
    print('=' * 70)
    return 0 if not result['valid'] else 1


if __name__ == '__main__':
    sys.exit(main())
