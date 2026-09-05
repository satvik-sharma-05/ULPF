"""Backfill `timestamp_source` onto events ingested before the field existed.

`timestamp_source` records whether an event's `timestamp` came from the source
line ('event') or from the clock at read time ('ingest'). It matters because
correlating on an ingest time as though it were an event time is a real
analytical error, and because formats like the CoreDNS access log genuinely
emit no timestamp at all.

The value is DERIVED PER EVENT by re-parsing that event's own `raw_message`,
not inferred from its `matched_format`. Format is a decent proxy - a detector
that never finds a timestamp will not find one on any line - but it is only a
proxy, and a schema field that is right for most events is exactly the kind of
thing nobody notices is wrong. Re-parsing is exact and the whole corpus takes
seconds.

Reads raw_message from the graph, so it needs no access to the original files.

    python -m batch.backfill_timestamp_source
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import NEO4J_CONFIG   # noqa: E402  (needs the path above)
from core.parser import LogParser      # noqa: E402
from neo4j import GraphDatabase        # noqa: E402

READ = """
MATCH (l:Log)
WHERE l.timestamp_source IS NULL AND l.raw_message IS NOT NULL
RETURN l.id AS id, l.raw_message AS raw
LIMIT $batch
"""

WRITE = """
UNWIND $rows AS row
MATCH (l:Log {id: row.id})
SET l.timestamp_source = row.timestamp_source
RETURN count(l) AS updated
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--batch-size', type=int, default=2000)
    args = ap.parse_args()

    parser = LogParser()
    driver = GraphDatabase.driver(
        NEO4J_CONFIG['uri'],
        auth=(NEO4J_CONFIG['user'], NEO4J_CONFIG['password']),
    )
    database = NEO4J_CONFIG.get('database', 'neo4j')

    updated = 0
    counts = {'event': 0, 'ingest': 0}
    started = time.time()

    try:
        with driver.session(database=database) as session:
            while True:
                # Re-reads the "still NULL" set each pass rather than paging
                # with SKIP: the write shrinks the set, so a skip offset would
                # walk straight past rows that have not been handled yet.
                batch = list(session.run(READ, batch=args.batch_size))
                if not batch:
                    break

                rows = []
                for record in batch:
                    parsed = parser.parse(record['raw'])
                    source = parsed.get('timestamp_source') or 'event'
                    counts[source] = counts.get(source, 0) + 1
                    rows.append({'id': record['id'], 'timestamp_source': source})

                result = session.run(WRITE, rows=rows).single()
                written = result['updated'] if result else 0
                updated += written
                print(f"  {updated:,} updated | {time.time() - started:.0f}s", flush=True)

                # A batch that reads rows but writes none would loop forever -
                # stop rather than spin.
                if written == 0:
                    print("  no rows written for a non-empty batch; stopping.", file=sys.stderr)
                    break
    finally:
        driver.close()

    print(f"\nSet timestamp_source on {updated:,} events in {time.time() - started:.0f}s.")
    for key, n in sorted(counts.items()):
        print(f"  {key:8} {n:,}")


if __name__ == '__main__':
    main()
