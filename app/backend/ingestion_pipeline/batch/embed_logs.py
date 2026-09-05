"""
batch/embed_logs.py - Add BAAI/bge-m3 embeddings to :Log nodes that don't have
one yet, working directly against Neo4j.

This is the "add embeddings later" half of a two-step ingest:

    python -m batch.ingest_to_neo4j --input output.json.gz   # structure, fast
    python -m batch.embed_logs                                # embeddings, slow

It never re-reads output.json.gz - it queries Neo4j for Log nodes with
`embedding IS NULL`, embeds their message text, and writes the vector back.
That means it can be run at any time after the structural ingest, independent
of whether output.json.gz is even still around, and it's naturally idempotent:
run it twice and the second run finds nothing left to do.

Embedding ~117M records on CPU is a genuinely long job (realistically days,
not hours), so **being interruptible is the main design goal here**, not raw
throughput:

  - Progress is paginated by a keyset (l.id > $after_id), not SKIP/LIMIT, so
    each page costs the same regardless of how far into the run you are.
  - After every batch, the last id processed is written to a checkpoint file
    with an atomic rename (os.replace), so a crash, Ctrl-C, VM reboot, or
    `docker compose down` mid-batch can never corrupt the checkpoint - the
    worst case is redoing the one batch that was in flight.
  - Rerunning the exact same command resumes from that checkpoint
    automatically. There is no separate "--resume" flag to remember.

Refuses to run if the real model fails to load, rather than silently writing
core/embeddings.py's deterministic hash-based fallback vectors into the graph
- those carry no semantic meaning, and writing hundreds of millions of them
into a permanent index would quietly break vector search everywhere with no
error to point at later.
"""

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING, format='%(asctime)s [%(levelname)s] %(message)s')

# Keyset pagination: strictly increasing by id, so a page's cost doesn't grow
# as the run progresses (unlike SKIP n, which re-walks n rows every time).
_FETCH_QUERY = """
MATCH (l:Log)
WHERE l.embedding IS NULL AND l.id > $after_id
RETURN l.id AS id, coalesce(l.normalized_message, l.message, '') AS text
ORDER BY l.id
LIMIT $limit
"""

_WRITE_QUERY = """
UNWIND $rows AS row
MATCH (l:Log {id: row.id})
SET l.embedding = row.embedding
"""

_COUNT_REMAINING_QUERY = "MATCH (l:Log) WHERE l.embedding IS NULL RETURN count(l) AS c"
_COUNT_TOTAL_QUERY = "MATCH (l:Log) RETURN count(l) AS c"


def _load_checkpoint(path: str) -> Dict[str, Any]:
    if os.path.isfile(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {'after_id': '', 'lifetime_done': 0}


def _save_checkpoint(path: str, state: Dict[str, Any]) -> None:
    # Write-then-rename: os.replace is atomic on both POSIX and Windows, so a
    # process killed mid-write leaves either the old checkpoint or the new one
    # intact, never a half-written, unparseable file.
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('--batch-size', type=int, default=256,
                    help="Log nodes embedded per round-trip. Default: 256")
    ap.add_argument('--checkpoint', default='embed_checkpoint.json',
                    help="Resume-state file. Default: embed_checkpoint.json")
    ap.add_argument('--limit', type=int, default=None,
                    help="Stop after embedding this many nodes this run (for a quick test)")
    ap.add_argument('--threads', type=int, default=None,
                    help="torch CPU threads. Default: all cores")
    ap.add_argument('--neo4j-uri', default=None, help="Override NEO4J_URI")
    ap.add_argument('--neo4j-user', default=None, help="Override NEO4J_USER")
    ap.add_argument('--neo4j-password', default=None, help="Override NEO4J_PASSWORD")
    ap.add_argument('--neo4j-database', default=None, help="Override NEO4J_DATABASE")
    args = ap.parse_args()

    for flag, env in (('neo4j_uri', 'NEO4J_URI'), ('neo4j_user', 'NEO4J_USER'),
                      ('neo4j_password', 'NEO4J_PASSWORD'),
                      ('neo4j_database', 'NEO4J_DATABASE')):
        value = getattr(args, flag, None)
        if value:
            os.environ[env] = value

    from core.config import NEO4J_CONFIG
    from core.neo4j_writer import HAS_NEO4J_DRIVER, Neo4jWriter

    if not HAS_NEO4J_DRIVER:
        print("The neo4j driver isn't installed - run: pip install neo4j")
        sys.exit(1)

    try:
        import torch
        torch.set_num_threads(args.threads or os.cpu_count() or 4)
    except Exception:
        pass  # embeddings.py's own guard handles a broken/missing torch below

    print("Loading embedding model (BAAI/bge-m3)...")
    from core.embeddings import EmbeddingGenerator
    embedder = EmbeddingGenerator()
    if not embedder.is_loaded:
        print(
            "\nThe real embedding model did not load, so this refuses to continue: "
            "writing the deterministic fallback vectors into 100M+ permanent graph "
            "nodes would silently make vector search meaningless everywhere, with "
            "no error to point at afterwards. Fix the model/torch install (see "
            "sentence_transformers' warning above) and re-run - nothing has been "
            "written to Neo4j."
        )
        sys.exit(1)
    print(f"Model loaded on device: {embedder.device}")

    print(f"Connecting to {NEO4J_CONFIG['uri']} (database={NEO4J_CONFIG['database']!r})...")
    writer = Neo4jWriter()
    if not writer.connect() or not writer.is_connected or writer.driver is None:
        print(f"Could not connect to {NEO4J_CONFIG['uri']}. Check NEO4J_URI/credentials.")
        sys.exit(1)
    print("Connected.")

    with writer.driver.session(database=writer.database) as session:
        total = session.run(_COUNT_TOTAL_QUERY).single()['c']
        remaining_at_start = session.run(_COUNT_REMAINING_QUERY).single()['c']
    print(f"Log nodes total: {total:,}  |  without an embedding: {remaining_at_start:,}")

    if remaining_at_start == 0:
        print("Nothing to do.")
        writer.close()
        return

    state = _load_checkpoint(args.checkpoint)
    after_id = state.get('after_id', '')
    lifetime_done = state.get('lifetime_done', 0)
    if after_id:
        print(f"Resuming from checkpoint {args.checkpoint!r}: "
              f"after_id={after_id!r}, {lifetime_done:,} embedded across all runs so far")

    session_done = 0
    start = time.time()

    try:
        while True:
            with writer.driver.session(database=writer.database) as session:
                rows = [dict(r) for r in session.run(
                    _FETCH_QUERY, after_id=after_id, limit=args.batch_size
                )]
            if not rows:
                print("\nNo Log nodes without an embedding remain. Done.")
                break

            texts = [r['text'] for r in rows]
            vectors = embedder.generate_batch(texts)
            write_rows = [{'id': r['id'], 'embedding': v} for r, v in zip(rows, vectors)]

            with writer.driver.session(database=writer.database) as session:
                session.run(_WRITE_QUERY, rows=write_rows).consume()

            after_id = rows[-1]['id']
            session_done += len(rows)
            lifetime_done += len(rows)
            _save_checkpoint(args.checkpoint, {'after_id': after_id, 'lifetime_done': lifetime_done})

            elapsed = time.time() - start
            rate = session_done / elapsed if elapsed else 0
            remaining = remaining_at_start - session_done
            eta_hours = (remaining / rate / 3600) if rate else 0
            print(f"  session: {session_done:,} | lifetime: {lifetime_done:,}/{total:,} | "
                  f"{rate:.1f} rec/s | ETA {eta_hours:.1f}h", flush=True)

            if args.limit and session_done >= args.limit:
                print(f"\nReached --limit of {args.limit:,} for this run. "
                      f"Checkpoint saved - rerun the same command to continue.")
                break
    except KeyboardInterrupt:
        print(f"\nInterrupted. Checkpoint saved at after_id={after_id!r} "
              f"({lifetime_done:,} embedded across all runs). "
              f"Rerun the same command any time to continue exactly here.")

    writer.close()


if __name__ == '__main__':
    main()
