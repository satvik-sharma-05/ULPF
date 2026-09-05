"""
batch/run_pipeline.py - logs -> parse -> embed -> Neo4j, in one command.

    python -m batch.run_pipeline --path ./logs
    python -m batch.run_pipeline --path ./logs --embed
    python -m batch.run_pipeline --path ./logs --limit 50000 --wipe

The same work as `batch.parse_logs` followed by `batch.ingest_to_neo4j`, minus
the intermediate output.json - records stream straight from the parser into
batched Neo4j writes.

Use the two-step form when you want the parsed data kept: parsing is CPU-bound
and needs no network, ingestion needs a reachable database, and they fail for
unrelated reasons. Keeping output.json means a Neo4j problem doesn't cost you a
multi-hour reparse. Use this one-step form when you only want the graph.

Parsing runs across every CPU core; the main process handles the database
writes, since a single Neo4j connection pool ingests faster than several
processes contending on the same locks.
"""

import argparse
import os
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from batch.ingest_to_neo4j import (
    add_neo4j_args,
    apply_neo4j_overrides,
    connect_or_exit,
    report_graph,
    wipe_database,
)
from batch.parse_logs import find_log_files, parse_one_file


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('--path', default='./logs',
                    help="Log file, or a folder of them (searched recursively). Default: ./logs")
    ap.add_argument('--pattern', default='*.txt', help="Filename pattern. Default: *.txt")
    ap.add_argument('--workers', type=int, default=os.cpu_count(),
                    help="Parser worker processes. Default: one per CPU core")
    ap.add_argument('--batch-size', type=int, default=None,
                    help="Records per Neo4j transaction (default: NEO4J_BATCH_SIZE, 500)")
    ap.add_argument('--limit', type=int, default=None, help="Stop after this many records")
    ap.add_argument('--max-records-per-file', type=int, default=None,
                    help="Only take the first N records of each file")
    ap.add_argument('--embed', action='store_true',
                    help="Also generate + store embeddings (slower, loads BAAI/bge-m3)")
    ap.add_argument('--wipe', action='store_true',
                    help="DESTRUCTIVE - delete every node in the database before loading")
    add_neo4j_args(ap)
    args = ap.parse_args()

    apply_neo4j_overrides(args)

    try:
        files = find_log_files(args.path, args.pattern)
    except FileNotFoundError:
        print(f"No such path: {args.path}")
        sys.exit(1)
    if not files:
        print(f"No files matching {args.pattern!r} found under {args.path}")
        sys.exit(1)

    print(f"Found {len(files):,} file(s) under {args.path}")

    writer, neo4j_config = connect_or_exit()
    batch_size = args.batch_size or neo4j_config.get('batch_size', 500)

    if args.wipe:
        wipe_database(writer, neo4j_config)

    embedder = None
    if args.embed:
        from core.embeddings import EmbeddingGenerator
        print("Loading embedding model (BAAI/bge-m3)...")
        embedder = EmbeddingGenerator()

    print(f"\nParsing with {args.workers} worker(s), writing in batches of {batch_size}...\n")

    format_counts: Counter = Counter()
    source_counts: Counter = Counter()
    entity_counts: Counter = Counter()
    total_written = 0
    total_parsed = 0
    files_done = 0
    stop = False
    start = time.time()

    pending: List[Dict[str, Any]] = []

    def flush(force: bool = False) -> None:
        """Writes whole batches out of the pending buffer."""
        nonlocal pending, total_written
        while len(pending) >= batch_size or (force and pending):
            chunk, pending = pending[:batch_size], pending[batch_size:]
            if embedder:
                texts = [r.get('normalized_message') or r.get('message') or '' for r in chunk]
                for record, vector in zip(chunk, embedder.generate_batch(texts)):
                    record['embedding'] = vector
            total_written += writer.write_batch(chunk)

    work = [(f, args.max_records_per_file, False) for f in files]

    try:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(parse_one_file, item): item[0] for item in work}
            for future in as_completed(futures):
                (_path, records, _fmt, _src, _ents, _samples, _errors) = future.result()

                # Counted per record actually queued, not from the worker's own
                # tallies: a worker parses its whole file, but --limit can stop
                # us mid-file, and using the worker counters would report more
                # records than were written (percentages over 100%).
                for record in records:
                    if args.limit and total_parsed >= args.limit:
                        stop = True
                        break
                    pending.append(record)
                    total_parsed += 1
                    format_counts[record.get('matched_format', 'generic_fallback')] += 1
                    source_counts[record.get('source_type', 'Unknown')] += 1
                    for entity in record.get('entities') or []:
                        entity_counts[entity.get('type', '?')] += 1
                flush()

                files_done += 1
                if files_done % 50 == 0 or files_done == len(files):
                    elapsed = time.time() - start
                    print(f"  {files_done:,}/{len(files):,} files | "
                          f"{total_written:,} written | "
                          f"{total_written / elapsed if elapsed else 0:,.0f} rec/s", flush=True)

                if stop:
                    for p in futures:
                        p.cancel()
                    break
    except KeyboardInterrupt:
        print("\nInterrupted - everything written so far is committed.")

    flush(force=True)

    elapsed = time.time() - start
    recognized = total_parsed - format_counts.get('generic_fallback', 0)
    print(f"\nDone. Parsed {total_parsed:,}, wrote {total_written:,} records "
          f"in {elapsed:,.1f}s ({total_written / elapsed if elapsed else 0:,.0f} rec/s).")
    if total_parsed:
        print(f"Recognized by a specific detector: {recognized:,} "
              f"({recognized / total_parsed * 100:.2f}%)")
        print(f"Entities extracted: {sum(entity_counts.values()):,} "
              f"({sum(entity_counts.values()) / total_parsed:.1f} per log)")

    print("\nTop source types:")
    for name, count in source_counts.most_common(15):
        print(f"  {name:<26} {count:>12,}")

    report_graph(writer, neo4j_config)
    writer.close()


if __name__ == '__main__':
    main()
