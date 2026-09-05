"""Backfill `timestamp_anomalous` onto events ingested before the flag existed.

Computed in Python rather than Cypher on purpose: `datetime(l.timestamp)`
rejects the space-separated variant this corpus contains
("2026-06-12 04:12:37.717+0000") and takes the whole statement down with it.
Python's parser handles both spellings, and one malformed value costs its own
row instead of the run.

    python -m batch.backfill_clock_skew
"""

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import NEO4J_CONFIG        # noqa: E402
from core.parser import CLOCK_SKEW_TOLERANCE_DAYS  # noqa: E402
from neo4j import GraphDatabase             # noqa: E402

READ = """
MATCH (l:Log)
WHERE l.timestamp IS NOT NULL AND l.timestamp_anomalous IS NULL
RETURN l.id AS id, l.timestamp AS ts
LIMIT $batch
"""

WRITE = """
UNWIND $rows AS row
MATCH (l:Log {id: row.id})
SET l.timestamp_anomalous = row.flag
RETURN count(l) AS n
"""


def parse_ts(value: str):
    text = str(value).strip().replace('Z', '+00:00')
    if ' ' in text and 'T' not in text:
        text = text.replace(' ', 'T', 1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--batch-size', type=int, default=5000)
    args = ap.parse_args()

    driver = GraphDatabase.driver(NEO4J_CONFIG['uri'],
                                  auth=(NEO4J_CONFIG['user'], NEO4J_CONFIG['password']))
    database = NEO4J_CONFIG.get('database', 'neo4j')
    now = datetime.now(timezone.utc)
    total = flagged = unparseable = 0

    try:
        with driver.session(database=database) as session:
            while True:
                batch = list(session.run(READ, batch=args.batch_size))
                if not batch:
                    break
                rows = []
                for rec in batch:
                    dt = parse_ts(rec['ts'])
                    if dt is None:
                        unparseable += 1
                        # Still write False, or the row is read again forever.
                        rows.append({'id': rec['id'], 'flag': False})
                        continue
                    flag = abs((now - dt).days) > CLOCK_SKEW_TOLERANCE_DAYS
                    flagged += flag
                    rows.append({'id': rec['id'], 'flag': bool(flag)})
                written = session.run(WRITE, rows=rows).single()['n']
                total += written
                if written == 0:
                    break
                print(f"  {total:,} processed", flush=True)
    finally:
        driver.close()

    print(f"\n{total:,} events checked against a {CLOCK_SKEW_TOLERANCE_DAYS}-day tolerance.")
    print(f"  {flagged:,} flagged as clock-skewed")
    if unparseable:
        print(f"  {unparseable:,} had an unparseable timestamp and were left unflagged")


if __name__ == '__main__':
    main()
