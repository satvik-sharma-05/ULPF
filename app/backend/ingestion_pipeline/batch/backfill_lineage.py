"""Backfill the lineage back-pointer onto Log nodes that predate it.

`source_file` and `source_record` say which file an event came from and which
record within that file it was - the link from a normalized (:Log) back to the
original line in the untouched corpus. They were being attached to parsed
records but never written to Neo4j, so every node already in the graph has the
raw bytes with no idea where they came from.

This walks a parse output and applies those two properties to logs that are
ALREADY in the graph.

It uses MATCH, never MERGE, and that is the whole point: the parse output can
legitimately contain more records than were ingested (a smaller --limit was
used, or the corpus grew), and a MERGE would quietly create thousands of new
partial nodes with no embedding, silently wrecking the graph. MATCH touches
only what is already there and ignores the rest.

Ids are derived from timestamp|hostname|process|raw, none of which this
changes, so re-parsing the same corpus reproduces the same ids and the join
lands on the right nodes.

    python -m batch.backfill_lineage --input backfill.json
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import NEO4J_CONFIG            # noqa: E402  (needs the path above)
from core.jsonio import stream_read_records     # noqa: E402
from neo4j import GraphDatabase                 # noqa: E402

CYPHER = """
UNWIND $rows AS row
MATCH (l:Log {id: row.id})
SET l.source_file = row.source_file,
    l.source_record = row.source_record
RETURN count(l) AS updated
"""


def batched(iterable, size):
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--input', default='backfill.json', help="Parse output to read")
    ap.add_argument('--batch-size', type=int, default=2000)
    args = ap.parse_args()

    driver = GraphDatabase.driver(
        NEO4J_CONFIG['uri'],
        auth=(NEO4J_CONFIG['user'], NEO4J_CONFIG['password']),
    )
    database = NEO4J_CONFIG.get('database', 'neo4j')

    seen = 0
    updated = 0
    skipped_no_id = 0
    started = time.time()

    def rows():
        nonlocal seen, skipped_no_id
        for record in stream_read_records(args.input):
            seen += 1
            log_id = record.get('id')
            if not log_id:
                skipped_no_id += 1
                continue
            yield {
                'id': log_id,
                'source_file': str(record.get('source_file') or ''),
                'source_record': int(record.get('source_record') or 0),
            }

    try:
        with driver.session(database=database) as session:
            for batch in batched(rows(), args.batch_size):
                result = session.run(CYPHER, rows=batch).single()
                updated += (result['updated'] if result else 0)
                elapsed = time.time() - started
                print(f"  {seen:,} read | {updated:,} updated | "
                      f"{seen / max(elapsed, 0.001):,.0f} rec/s", flush=True)
    finally:
        driver.close()

    print(f"\nRead {seen:,} parsed records, updated {updated:,} existing Log nodes "
          f"in {time.time() - started:.0f}s.")
    if skipped_no_id:
        print(f"{skipped_no_id:,} records had no id and were skipped.")
    print("Records with no matching node were ignored - they were never ingested.")


if __name__ == '__main__':
    main()
