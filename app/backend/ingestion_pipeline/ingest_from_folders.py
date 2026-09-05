"""
ingest_from_folders.py - Ingest records from parsed_by_folder/*.json.gz (the
new, folder-correct parse) into Neo4j, streaming across all 91 files in
sequence until --limit total records are written. Reuses the same
Neo4jWriter/batching logic as batch/ingest_to_neo4j.py - just the input side
differs (many files instead of one).

Usage:
    python ingest_from_folders.py --dir parsed_by_folder --limit 5000000 --wipe
"""

import argparse
import glob
import os
import sys
import time
from typing import Any, Dict, Iterator, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.jsonio import stream_read_records


def batched(records: Iterator[Dict[str, Any]], size: int) -> Iterator[List[Dict[str, Any]]]:
    batch: List[Dict[str, Any]] = []
    for record in records:
        batch.append(record)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def add_neo4j_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument('--neo4j-uri', default=None)
    ap.add_argument('--neo4j-user', default=None)
    ap.add_argument('--neo4j-password', default=None)
    ap.add_argument('--neo4j-database', default=None)


def apply_neo4j_overrides(args) -> None:
    for flag, env in (('neo4j_uri', 'NEO4J_URI'), ('neo4j_user', 'NEO4J_USER'),
                      ('neo4j_password', 'NEO4J_PASSWORD'),
                      ('neo4j_database', 'NEO4J_DATABASE')):
        value = getattr(args, flag, None)
        if value:
            os.environ[env] = value


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default='parsed_by_folder')
    ap.add_argument('--batch-size', type=int, default=2000)
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--wipe', action='store_true')
    add_neo4j_args(ap)
    args = ap.parse_args()

    apply_neo4j_overrides(args)

    from core.config import NEO4J_CONFIG
    from core.neo4j_writer import HAS_NEO4J_DRIVER, Neo4jWriter

    if not HAS_NEO4J_DRIVER:
        print("The neo4j driver isn't installed - run: pip install neo4j")
        sys.exit(1)

    print(f"Connecting to {NEO4J_CONFIG['uri']} (database={NEO4J_CONFIG['database']!r})...")
    writer = Neo4jWriter()
    if not writer.connect() or not writer.is_connected or writer.driver is None:
        print(f"Could not connect to {NEO4J_CONFIG['uri']}.")
        sys.exit(1)
    print("Connected. Schema constraints and indexes are in place.")

    if args.wipe:
        print(f"\n--wipe given: deleting ALL nodes in database {NEO4J_CONFIG['database']!r}...")
        with writer.driver.session(database=writer.database) as session:
            session.run(
                "MATCH (n) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 5000 ROWS"
            ).consume()
        print("Database cleared.")

    files = sorted(glob.glob(os.path.join(args.dir, '*.json.gz')))
    print(f"\nFound {len(files)} folder export(s) under {args.dir}/")

    def record_stream() -> Iterator[Dict[str, Any]]:
        seen = 0
        for fpath in files:
            for record in stream_read_records(fpath):
                if args.limit and seen >= args.limit:
                    return
                seen += 1
                yield record

    print(f"Ingesting up to {args.limit or 'ALL':,} records in batches of {args.batch_size}...\n" if args.limit
          else f"Ingesting ALL records in batches of {args.batch_size}...\n")
    total_written = 0
    start = time.time()

    try:
        for batch_index, batch in enumerate(batched(record_stream(), args.batch_size), start=1):
            total_written += writer.write_batch(batch)
            if batch_index % 20 == 0:
                elapsed = time.time() - start
                rate = total_written / elapsed if elapsed else 0
                print(f"  {total_written:,} records written | {rate:,.0f} rec/s | {elapsed:.0f}s elapsed", flush=True)
    except KeyboardInterrupt:
        print("\nInterrupted - everything written so far is committed.")

    elapsed = time.time() - start
    print(f"\nDone. Wrote {total_written:,} records in {elapsed:,.1f}s "
          f"({total_written / elapsed if elapsed else 0:,.0f} rec/s).")

    print("\nLive node counts from Neo4j:")
    for label, count in writer.node_counts():
        print(f"  {label:<20} {count:>12,}")
    print("\nLive relationship counts from Neo4j:")
    for rel, count in writer.relationship_counts():
        print(f"  {rel:<22} {count:>12,}")

    writer.close()


if __name__ == '__main__':
    main()
