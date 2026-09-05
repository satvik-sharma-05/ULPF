"""Backfill `source_file` onto events the id-based backfill could not match.

1,112 events came from formats that carry no timestamp of their own (CoreDNS,
vracli). Their ids were derived from the ingest clock before that was fixed, so
re-parsing the corpus now produces different ids and the id join finds nothing.

Matching on `raw_message` instead sidesteps the whole problem: the raw line is
what actually ties a graph node to a source line, and it did not change. Only
events that are still missing the pointer are touched, so this cannot disturb
the 23,403 that already have one.

Lines that appear more than once in the corpus are skipped rather than guessed
at - if a raw line occurs in three files, no single file is *the* origin, and
recording one of them would be inventing provenance.

    python -m batch.backfill_lineage_by_raw --input backfill.json
"""

import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import NEO4J_CONFIG          # noqa: E402
from core.jsonio import stream_read_records   # noqa: E402
from neo4j import GraphDatabase               # noqa: E402

READ_MISSING = """
MATCH (l:Log)
WHERE l.source_file IS NULL AND l.raw_message IS NOT NULL
RETURN l.id AS id, l.raw_message AS raw
"""

WRITE = """
UNWIND $rows AS row
MATCH (l:Log {id: row.id})
SET l.source_file = row.source_file,
    l.source_record = row.source_record
RETURN count(l) AS n
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--input', default='backfill.json')
    ap.add_argument('--batch-size', type=int, default=2000)
    args = ap.parse_args()

    driver = GraphDatabase.driver(NEO4J_CONFIG['uri'],
                                  auth=(NEO4J_CONFIG['user'], NEO4J_CONFIG['password']))
    database = NEO4J_CONFIG.get('database', 'neo4j')

    with driver.session(database=database) as session:
        missing = [(r['id'], r['raw']) for r in session.run(READ_MISSING)]
    print(f"{len(missing):,} event(s) still without a source_file.")
    if not missing:
        driver.close()
        return

    wanted = {raw for _, raw in missing}

    # Only the raw lines we actually need are held, so this stays bounded even
    # though the parse output is ~120MB.
    found = defaultdict(list)
    scanned = 0
    for rec in stream_read_records(args.input):
        scanned += 1
        raw = rec.get('raw_message')
        if raw in wanted and rec.get('source_file'):
            found[raw].append((rec['source_file'], rec.get('source_record') or 0))
    print(f"scanned {scanned:,} parsed record(s); "
          f"{len(found):,} of the wanted raw lines were located.")

    rows, ambiguous = [], 0
    for log_id, raw in missing:
        hits = found.get(raw)
        if not hits:
            continue
        distinct_files = {f for f, _ in hits}
        if len(distinct_files) > 1:
            # The same line in several files - no single origin to record.
            ambiguous += 1
            continue
        source_file, source_record = hits[0]
        rows.append({'id': log_id, 'source_file': source_file,
                     'source_record': int(source_record)})

    updated = 0
    try:
        with driver.session(database=database) as session:
            for i in range(0, len(rows), args.batch_size):
                chunk = rows[i:i + args.batch_size]
                updated += session.run(WRITE, rows=chunk).single()['n']
                print(f"  {updated:,} updated", flush=True)
    finally:
        driver.close()

    print(f"\nSet source_file on {updated:,} event(s).")
    if ambiguous:
        print(f"{ambiguous:,} skipped: the raw line appears in more than one file, "
              f"so no single origin could be recorded.")
    remaining = len(missing) - updated - ambiguous
    if remaining > 0:
        print(f"{remaining:,} still unmatched - those raw lines are not in this parse "
              f"output at all (a different ingest, or a file the pattern skipped).")


if __name__ == '__main__':
    main()
