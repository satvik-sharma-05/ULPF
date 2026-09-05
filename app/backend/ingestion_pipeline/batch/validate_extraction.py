"""
batch/validate_extraction.py - Check that extracted entities are actually valid.

    python -m batch.validate_extraction --path ./logs --sample 20000

Counting nodes proves the graph is *big*, not that it is *right*. A regex that
mistakes an opID for a username inflates the node count and quietly corrupts
every answer built on it. This script samples the corpus and reports what the
extractor is really producing, so the output can be eyeballed before a
multi-hour ingest:

  - how many entities per type, and how many distinct values each has
  - the most common values per type, with the log line each came from
  - automatic suspicion checks: values that look like the wrong type, near-
    duplicate values, types with pathological cardinality

Read the "SUSPICIOUS" section first. Everything in it is a candidate bug in
core/parsers/entities.py, not a fact about the logs.
"""

import argparse
import os
import random
import re
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from batch.parse_logs import find_log_files
from core.graph_schema import ENTITY_SPECS, entity_spec
from core.multiline import reassemble
from core.parser import LogParser

# Shape each type is expected to satisfy. A value failing its own type's check
# is the clearest signal of a mis-typed extraction.
_EXPECTED_SHAPE: Dict[str, re.Pattern] = {
    'ip': re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$'),
    'port': re.compile(r'^\d{1,5}$'),
    'mac_address': re.compile(r'^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$'),
    'uuid': re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                       r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'),
    'security_id': re.compile(r'^S-1-(?:\d+-){1,8}\d+$'),
    'device': re.compile(r'^(?:naa|eui|t10|mpx)\.'),
    'managed_object': re.compile(r'^(?:host|task|group|datastore|network|resgroup|dvportgroup)-'),
    'vm': re.compile(r'^vm-'),
    'gc_cycle': re.compile(r'^\d+$'),
    'event_id': re.compile(r'^\d+$'),
    'error_code': re.compile(r'^[A-Z]{2,6}\d{4,6}$'),
    'network_interface': re.compile(r'^(?:vmnic|vmk|vmhba|eth|ens|eno|bond)\d{1,3}$'),
    'systemd_unit': re.compile(r'\.(?:service|socket|target|timer|mount|path|slice)$'),
    'file': re.compile(r'^(?:/|[A-Za-z]:\\)'),
    'executable': re.compile(r'^(?:/|[A-Za-z]:\\|[\w.\-]+$)'),
    'url': re.compile(r'^(?:https?|amqp|bolt|ftp)://'),
    'dns_name': re.compile(r'^[\w.\-]+$'),
    'pod': re.compile(r'^[a-z0-9][a-z0-9\-]{2,}$'),
}

# A username that is really an IP, a path, or a UUID means _split_user() was
# handed something that was never an account.
_MISTYPE_CHECKS: List[Tuple[str, re.Pattern, str]] = [
    ('user', re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$'), 'looks like an IP address'),
    ('user', re.compile(r'^/'), 'looks like a file path'),
    ('user', re.compile(r'^[0-9a-f]{16,}$'), 'looks like a hex id, not an account'),
    ('operation', re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$'), 'looks like an IP address'),
    ('session', re.compile(r'^/'), 'looks like a file path'),
    ('logger', re.compile(r'^\d+$'), 'is purely numeric'),
    ('thread', re.compile(r'^/'), 'looks like a file path'),
]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('--path', default='./logs', help="Corpus root. Default: ./logs")
    ap.add_argument('--pattern', default='*.txt')
    ap.add_argument('--sample', type=int, default=20000,
                    help="How many records to check. Default: 20000")
    ap.add_argument('--files', type=int, default=40,
                    help="How many files to draw the sample from. Default: 40")
    ap.add_argument('--examples', type=int, default=5,
                    help="Example values to show per type. Default: 5")
    ap.add_argument('--seed', type=int, default=0,
                    help="Sampling seed, so a run is reproducible. Default: 0")
    ap.add_argument('--report', default=None, help="Also write the report to this file")
    args = ap.parse_args()

    try:
        all_files = find_log_files(args.path, args.pattern)
    except FileNotFoundError:
        print(f"No such path: {args.path}")
        sys.exit(1)
    if not all_files:
        print(f"No files matching {args.pattern!r} under {args.path}")
        sys.exit(1)

    # Spread the sample across the corpus rather than reading the first N files:
    # each folder is one day, and formats vary between them.
    rng = random.Random(args.seed)
    chosen = rng.sample(all_files, min(args.files, len(all_files)))

    parser = LogParser()
    per_type_values: Dict[str, Counter] = defaultdict(Counter)
    per_type_example_log: Dict[str, Dict[str, str]] = defaultdict(dict)
    total_records = 0
    logs_with_entities = 0

    print(f"Sampling up to {args.sample:,} records from {len(chosen)} of "
          f"{len(all_files):,} files...\n")

    for file_path in chosen:
        if total_records >= args.sample:
            break
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as handle:
            for record in reassemble(handle):
                if total_records >= args.sample:
                    break
                total_records += 1
                parsed = parser.parse(record)
                entities = parsed.get('entities') or []
                if entities:
                    logs_with_entities += 1
                for entity in entities:
                    etype, value = entity.get('type'), entity.get('value')
                    if not etype or not value:
                        continue
                    per_type_values[etype][value] += 1
                    if value not in per_type_example_log[etype]:
                        per_type_example_log[etype][value] = parsed.get('message', '')[:160]

    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("ENTITY EXTRACTION VALIDATION")
    lines.append("=" * 78)
    lines.append(f"Records sampled      : {total_records:,}")
    lines.append(f"Records with entities: {logs_with_entities:,} "
                 f"({logs_with_entities / total_records * 100 if total_records else 0:.1f}%)")
    total_entities = sum(sum(c.values()) for c in per_type_values.values())
    lines.append(f"Entities extracted   : {total_entities:,} "
                 f"({total_entities / total_records if total_records else 0:.1f} per log)")
    lines.append(f"Distinct types seen  : {len(per_type_values)} of {len(ENTITY_SPECS)} declared")

    # --- coverage -----------------------------------------------------------
    lines.append("")
    lines.append("-" * 78)
    lines.append("PER-TYPE BREAKDOWN  (node label <- entity type)")
    lines.append("-" * 78)
    lines.append(f"{'type':<20}{'label':<20}{'total':>10}{'distinct':>10}  top values")
    for etype, counter in sorted(per_type_values.items(),
                                 key=lambda kv: -sum(kv[1].values())):
        label = entity_spec(etype).label
        top = ', '.join(v for v, _ in counter.most_common(args.examples))
        lines.append(f"{etype:<20}{label:<20}{sum(counter.values()):>10,}"
                     f"{len(counter):>10,}  {top[:70]}")

    declared_but_unseen = sorted(set(ENTITY_SPECS) - set(per_type_values))
    if declared_but_unseen:
        lines.append("")
        lines.append("Declared but not seen in this sample (may simply be rare, or the "
                     "extractor for them is broken):")
        lines.append("  " + ", ".join(declared_but_unseen))

    # --- validity -----------------------------------------------------------
    suspicious: List[str] = []

    for etype, counter in per_type_values.items():
        shape = _EXPECTED_SHAPE.get(etype)
        if shape:
            bad = [v for v in counter if not shape.match(v)]
            if bad:
                pct = len(bad) / len(counter) * 100
                suspicious.append(
                    f"[shape]  {etype}: {len(bad):,}/{len(counter):,} distinct values "
                    f"({pct:.1f}%) don't match the expected shape. e.g. {bad[:4]}"
                )

        for check_type, pattern, why in _MISTYPE_CHECKS:
            if check_type != etype:
                continue
            hits = [v for v in counter if pattern.match(v)]
            if hits:
                suspicious.append(
                    f"[mistype] {etype}: {len(hits):,} distinct value(s) {why}. e.g. {hits[:4]}"
                )

        # A type whose values are nearly all unique across the sample is
        # usually a per-line id being modelled as a shared entity - it makes
        # huge numbers of nodes that nothing else will ever connect to.
        distinct, total = len(counter), sum(counter.values())
        if total >= 200 and distinct / total > 0.95 and etype not in (
                'uuid', 'trace', 'operation', 'task', 'session', 'container'):
            suspicious.append(
                f"[cardinality] {etype}: {distinct:,} distinct of {total:,} occurrences "
                f"- almost every value is unique, so these nodes will not link anything."
            )

    lines.append("")
    lines.append("-" * 78)
    lines.append("SUSPICIOUS  (each is a candidate bug in core/parsers/entities.py)")
    lines.append("-" * 78)
    if suspicious:
        lines.extend("  " + s for s in suspicious)
    else:
        lines.append("  Nothing flagged - every value matched its type's expected shape.")

    # --- examples in context ------------------------------------------------
    lines.append("")
    lines.append("-" * 78)
    lines.append("EXAMPLES IN CONTEXT  (does the value make sense for the line it came from?)")
    lines.append("-" * 78)
    for etype, counter in sorted(per_type_values.items(),
                                 key=lambda kv: -sum(kv[1].values())):
        lines.append("")
        lines.append(f"  {etype}  ->  (:{entity_spec(etype).label})")
        for value, count in counter.most_common(min(args.examples, 3)):
            lines.append(f"    {value!r}  (x{count:,})")
            lines.append(f"      from: {per_type_example_log[etype].get(value, '')!r}")

    report = "\n".join(lines)
    print(report)
    if args.report:
        with open(args.report, 'w', encoding='utf-8') as handle:
            handle.write(report + "\n")
        print(f"\nReport written to {args.report}")


if __name__ == '__main__':
    main()
