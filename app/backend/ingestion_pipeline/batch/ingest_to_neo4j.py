"""
batch/ingest_to_neo4j.py - Load parsed logs from output.json into Neo4j.

    python -m batch.parse_logs      --path ./logs --out output.json
    python -m batch.ingest_to_neo4j --input output.json

    # with embeddings, so the chatbot's vector search works
    python -m batch.ingest_to_neo4j --input output.json --embed

Streams output.json (never loading it all - the full corpus is tens of millions
of records), writes in batched transactions, and reports live node and
relationship counts read back from Neo4j itself at the end, so the numbers are
the database's rather than this script's tally.

Connection settings come from .env / core/config.py and can be overridden:

    python -m batch.ingest_to_neo4j --input output.json \\
        --neo4j-uri bolt://neo4j.internal.example:7687 --neo4j-password secret

There is no local-file fallback: if Neo4j can't be reached this exits with the
specific reason rather than pretending to have succeeded.
"""

import argparse
import os
import sys
import time
from typing import Any, Dict, Iterator, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
    ap.add_argument('--neo4j-uri', default=None, help="Override NEO4J_URI")
    ap.add_argument('--neo4j-user', default=None, help="Override NEO4J_USER")
    ap.add_argument('--neo4j-password', default=None, help="Override NEO4J_PASSWORD")
    ap.add_argument('--neo4j-database', default=None, help="Override NEO4J_DATABASE")


def apply_neo4j_overrides(args) -> None:
    """core/config.py reads the environment exactly once, at first import - so
    every override has to be applied before anything imports it, directly or
    transitively."""
    for flag, env in (('neo4j_uri', 'NEO4J_URI'), ('neo4j_user', 'NEO4J_USER'),
                      ('neo4j_password', 'NEO4J_PASSWORD'),
                      ('neo4j_database', 'NEO4J_DATABASE')):
        value = getattr(args, flag, None)
        if value:
            os.environ[env] = value


def connect_or_exit():
    from core.config import NEO4J_CONFIG
    from core.neo4j_writer import HAS_NEO4J_DRIVER, Neo4jWriter

    if not HAS_NEO4J_DRIVER:
        print("The neo4j driver isn't installed - run: pip install neo4j")
        sys.exit(1)

    print(f"Connecting to {NEO4J_CONFIG['uri']} (database={NEO4J_CONFIG['database']!r})...")
    writer = Neo4jWriter()
    if not writer.connect() or not writer.is_connected or writer.driver is None:
        print(
            f"\nCould not connect to {NEO4J_CONFIG['uri']}.\n"
            f"  - Is the host reachable from this machine (VPN/subnet/firewall)?\n"
            f"  - Are NEO4J_USER/NEO4J_PASSWORD correct?\n"
            f"  - Neo4j Community Edition only has a database named 'neo4j';\n"
            f"    NEO4J_DATABASE is currently {NEO4J_CONFIG['database']!r}."
        )
        sys.exit(1)
    print("Connected. Schema constraints and indexes are in place.")
    return writer, NEO4J_CONFIG


def wipe_database(writer, config) -> None:
    print(f"\n--wipe given: deleting ALL nodes in database "
          f"{config['database']!r} at {config['uri']}...")
    with writer.driver.session(database=writer.database) as session:
        # `CALL { WITH n ... }` rather than the newer `CALL (n) { ... }` scoped
        # form, which only parses on Neo4j 5.23+. Batching in transactions keeps
        # a multi-million-node delete from exhausting heap.
        session.run(
            "MATCH (n) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS"
        ).consume()
    print("Database cleared.")


def report_graph(writer, config) -> None:
    print("\nLive node counts from Neo4j:")
    for label, count in writer.node_counts():
        print(f"  {label:<20} {count:>12,}")

    print("\nLive relationship counts from Neo4j:")
    for rel, count in writer.relationship_counts():
        print(f"  {rel:<22} {count:>12,}")

    host = config['uri'].split('://')[-1].split(':')[0]
    print(f"\nOpen Neo4j Browser: http://{host}:7474")
    print("  MATCH p=()-->() RETURN p LIMIT 300")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('--input', default='output.json',
                    help="Parsed-log file from batch.parse_logs. Default: output.json")
    ap.add_argument('--batch-size', type=int, default=None,
                    help="Records per transaction (default: NEO4J_BATCH_SIZE, 500)")
    ap.add_argument('--limit', type=int, default=None, help="Stop after this many records")
    ap.add_argument('--embed', action='store_true',
                    help="Also generate + store embeddings (slower, loads BAAI/bge-m3)")
    ap.add_argument('--wipe', action='store_true',
                    help="DESTRUCTIVE - delete every node in the database before loading")
    add_neo4j_args(ap)
    args = ap.parse_args()

    apply_neo4j_overrides(args)

    from core.jsonio import stream_read_records

    if not os.path.isfile(args.input):
        print(f"No such file: {args.input}\n"
              f"Run `python -m batch.parse_logs --path ./logs` first to produce it.")
        sys.exit(1)

    writer, neo4j_config = connect_or_exit()
    batch_size = args.batch_size or neo4j_config.get('batch_size', 500)

    if args.wipe:
        wipe_database(writer, neo4j_config)

    embedder = None
    if args.embed:
        from core.embeddings import EmbeddingGenerator
        print("Loading embedding model (BAAI/bge-m3)...")
        embedder = EmbeddingGenerator()

    print(f"\nIngesting {args.input} in batches of {batch_size}...\n")
    total_written = 0
    total_seen = 0
    start = time.time()

    def record_stream() -> Iterator[Dict[str, Any]]:
        nonlocal total_seen
        for record in stream_read_records(args.input):
            if args.limit and total_seen >= args.limit:
                return
            total_seen += 1
            yield record

    try:
        for batch_index, batch in enumerate(batched(record_stream(), batch_size), start=1):
            if embedder:
                texts = [r.get('normalized_message') or r.get('message') or '' for r in batch]
                for record, vector in zip(batch, embedder.generate_batch(texts)):
                    record['embedding'] = vector

            total_written += writer.write_batch(batch)

            if batch_index % 20 == 0:
                elapsed = time.time() - start
                rate = total_written / elapsed if elapsed else 0
                print(f"  {total_written:,} records written | {rate:,.0f} rec/s", flush=True)
    except KeyboardInterrupt:
        print("\nInterrupted - everything written so far is committed.")

    elapsed = time.time() - start
    print(f"\nDone. Wrote {total_written:,} of {total_seen:,} records in {elapsed:,.1f}s "
          f"({total_written / elapsed if elapsed else 0:,.0f} rec/s).")

    report_graph(writer, neo4j_config)
    writer.close()


if __name__ == '__main__':
    main()
