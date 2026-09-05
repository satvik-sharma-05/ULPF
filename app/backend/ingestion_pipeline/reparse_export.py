"""
reparse_export.py - Re-parse every record's raw_message with the CURRENT
(fixed) parser, group by source_file, write out in the same streaming JSON
format as output.json.gz (core/jsonio.py's StreamingJSONArrayWriter).

Single fast reader (I/O-bound, ~55K rec/s) feeds a multiprocessing.Pool of
CPU-bound parse workers (~3.7K rec/s single-threaded), order-preserved via
imap so records stay in file order within each source_file's output.

Usage:
    python reparse_export.py --limit 2000000 --out parsed_export --workers 10
    python reparse_export.py --out parsed_export --workers 10   # full corpus
"""

import argparse
import gzip
import json
import os
import sys
import time
from contextlib import ExitStack
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.parser import LogParser
from core.jsonio import StreamingJSONArrayWriter

INPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output.json.gz')

_parser = None


def _init_worker():
    global _parser
    _parser = LogParser()


def _parse_one(args):
    old_id, source_file, raw_message, old_entity_count = args
    global _parser
    if not raw_message:
        return (old_id, source_file, None, old_entity_count, 0)
    parsed = _parser.parse(raw_message)
    new_entity_count = len(parsed.get('entities') or [])
    return (old_id, source_file, parsed, old_entity_count, new_entity_count)


def stream_input(path, limit=None):
    n = 0
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line in ('[', ']'):
                continue
            if line.endswith(','):
                line = line[:-1]
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            old_entity_count = len(rec.get('entities') or [])
            yield (rec.get('id'), rec.get('source_file') or 'unknown_source',
                   rec.get('raw_message') or '', old_entity_count)
            n += 1
            if limit and n >= limit:
                return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--out', default='parsed_export')
    ap.add_argument('--workers', type=int, default=10)
    ap.add_argument('--chunksize', type=int, default=200)
    ap.add_argument('--progress-every', type=int, default=200_000)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    total = 0
    old_entity_total = 0
    new_entity_total = 0
    records_gained_entities = 0
    records_lost_entities = 0
    start = time.time()

    with ExitStack() as stack:
        writers = {}
        pool = stack.enter_context(Pool(args.workers, initializer=_init_worker))
        for old_id, source_file, parsed, old_c, new_c in pool.imap(
                _parse_one, stream_input(INPUT, args.limit), chunksize=args.chunksize):
            if parsed is None:
                continue
            key = os.path.splitext(source_file)[0].replace('/', '_').replace('\\', '_')
            if key not in writers:
                path = os.path.join(args.out, f'parsed_{key}.json.gz')
                w = StreamingJSONArrayWriter(path)
                w.__enter__()
                writers[key] = w
            writers[key].write(parsed)
            total += 1
            old_entity_total += old_c
            new_entity_total += new_c
            if new_c > old_c:
                records_gained_entities += 1
            elif new_c < old_c:
                records_lost_entities += 1
            if total % args.progress_every == 0:
                elapsed = time.time() - start
                print(f"{total:,} records re-parsed | {total/elapsed:,.0f} rec/s | "
                      f"{len(writers)} source_file groups open | {elapsed:.0f}s elapsed | "
                      f"entities so far: old={old_entity_total:,} new={new_entity_total:,}", flush=True)

        for w in writers.values():
            w.__exit__(None, None, None)

    elapsed = time.time() - start
    print(f"\nDONE: {total:,} records re-parsed into {len(writers)} files under {args.out}/ "
          f"in {elapsed:.0f}s ({total/elapsed:,.0f} rec/s)")
    print(f"\nPARSER COMPARISON (old parse in output.json.gz vs. this fresh re-parse):")
    print(f"  Total entities - old: {old_entity_total:,}  new: {new_entity_total:,}  "
          f"(+{new_entity_total - old_entity_total:,}, {(new_entity_total/old_entity_total - 1)*100:+.1f}%)"
          if old_entity_total else "  (no old entities to compare)")
    print(f"  Records that gained entities under the new parser: {records_gained_entities:,}")
    print(f"  Records that lost entities under the new parser:   {records_lost_entities:,}")


if __name__ == '__main__':
    main()
