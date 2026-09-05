"""
batch/parse_logs.py - Parse a whole log corpus into one output.json.

    python -m batch.parse_logs --path ./logs --out output.json

    # a slice first, to eyeball the output before the full run
    python -m batch.parse_logs --path ./logs --out sample.json --limit 5000

    # smaller file: drop the verbatim raw line (roughly halves it)
    python -m batch.parse_logs --path ./logs --out output.json --drop-raw

--path is a variable, so this works on the full corpus, one export folder, or a
single file. Parsing is pure CPU (regex only, no I/O waits, no shared state), so
it runs one worker process per core. Output is streamed a record at a time -
the full corpus is tens of millions of records and building that list in memory
first would need more RAM than the machine has.

It ends with a coverage report: records per detector, per source type, entity
counts by type, and a sample of anything that fell through to generic_fallback.
That sample is the to-do list for the next detector.
"""

import argparse
import itertools
import os
import sys
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.jsonio import StreamingJSONArrayWriter
from core.multiline import reassemble
from core.parser import LogParser

# One parser per worker process, not one per file - a worker handles hundreds
# of files and rebuilding the detector list for each would be wasted work.
_PARSER = None

# Dropped from each record when --drop-raw is given. raw_message is by far the
# largest field; processed_at is a re-run timestamp, not data.
_BULKY_FIELDS = ('raw_message', 'processed_at')


def _get_parser() -> LogParser:
    global _PARSER
    if _PARSER is None:
        _PARSER = LogParser()
    return _PARSER


def find_log_files(root: str, pattern: str) -> List[str]:
    """Every matching file under root, at any depth.

    The corpus nests one level (logs/<export>.txt.tar/export1.txt), but a flat
    folder and a single file both need to work too.
    """
    import fnmatch

    if os.path.isfile(root):
        return [root]
    if not os.path.isdir(root):
        raise FileNotFoundError(root)

    matches: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if fnmatch.fnmatch(filename, pattern):
                matches.append(os.path.join(dirpath, filename))
    return sorted(matches)


def parse_one_file(args: Tuple[str, int, bool, str]):
    """Worker: parse one file, return its records plus local statistics."""
    file_path, max_records, drop_raw, root = args

    # The lineage pointer is the path RELATIVE TO THE CORPUS ROOT, not the
    # basename. This corpus has 2,501 folders that each contain an
    # 'export10.txt', so a basename alone identifies roughly nothing - it
    # cannot be walked back to a single original file, which is the entire
    # job of the field. Forward slashes so the value reads the same whether
    # it was produced on Windows or Linux.
    try:
        rel = os.path.relpath(file_path, root) if root else os.path.basename(file_path)
    except ValueError:
        # Different drive on Windows - no relative path exists.
        rel = os.path.basename(file_path)
    source_file = rel.replace(os.sep, '/')
    parser = _get_parser()

    records: List[Dict[str, Any]] = []
    format_counts: Counter = Counter()
    source_counts: Counter = Counter()
    entity_counts: Counter = Counter()
    fallback_samples: List[str] = []
    error_count = 0

    try:
        # Some export files contain stray NUL bytes; errors='ignore' plus the
        # BOM/NUL stripping inside the parser keeps those readable rather than
        # aborting a whole file on one UnicodeDecodeError.
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as handle:
            # `ordinal` counts every reassembled record the file yields,
            # including ones that fail to parse, so it stays a faithful index
            # into the source rather than a count of successes.
            for ordinal, record in enumerate(reassemble(handle), start=1):
                if max_records and len(records) >= max_records:
                    break
                try:
                    parsed = parser.parse(record)
                except Exception:
                    # parse() is contractually non-raising; count it loudly
                    # rather than losing records silently if that ever breaks.
                    error_count += 1
                    continue

                parsed['source_file'] = source_file
                parsed['source_record'] = ordinal
                if drop_raw:
                    for field in _BULKY_FIELDS:
                        parsed.pop(field, None)

                matched = parsed.get('matched_format', 'generic_fallback')
                format_counts[matched] += 1
                source_counts[parsed.get('source_type', 'Unknown')] += 1
                for entity in parsed.get('entities') or []:
                    entity_counts[entity.get('type', '?')] += 1
                if matched == 'generic_fallback' and len(fallback_samples) < 5:
                    fallback_samples.append(record.split('\n', 1)[0][:200])
                records.append(parsed)
    except Exception as e:
        error_count += 1
        print(f"  [warn] could not read {file_path}: {e}", file=sys.stderr)

    return (file_path, records, format_counts, source_counts,
            entity_counts, fallback_samples, error_count)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('--path', default='./logs',
                    help="Log file, or a folder of them (searched recursively). Default: ./logs")
    ap.add_argument('--out', default='output.json',
                    help="Where to write the parsed records. Default: output.json")
    ap.add_argument('--pattern', default='*.txt',
                    help="Filename pattern to include. Default: *.txt")
    ap.add_argument('--workers', type=int, default=os.cpu_count(),
                    help="Parallel worker processes. Default: one per CPU core")
    ap.add_argument('--limit', type=int, default=None,
                    help="Stop after this many records in total")
    ap.add_argument('--max-records-per-file', type=int, default=None,
                    help="Only take the first N records of each file")
    ap.add_argument('--drop-raw', action='store_true',
                    help="Omit raw_message/processed_at to shrink the output file")
    ap.add_argument('--jsonl', action='store_true',
                    help="Write JSON Lines instead of a JSON array")
    ap.add_argument('--report', default=None,
                    help="Also write the coverage report to this text file")
    args = ap.parse_args()

    try:
        files = find_log_files(args.path, args.pattern)
    except FileNotFoundError:
        print(f"No such path: {args.path}")
        sys.exit(1)

    if not files:
        print(f"No files matching {args.pattern!r} found under {args.path}")
        sys.exit(1)

    folders = {os.path.dirname(f) for f in files}
    print(f"Found {len(files):,} file(s) across {len(folders):,} folder(s) under {args.path}")
    print(f"Parsing with {args.workers} worker process(es) -> {args.out}\n")

    format_counts: Counter = Counter()
    source_counts: Counter = Counter()
    entity_counts: Counter = Counter()
    fallback_samples: List[str] = []
    total_records = 0
    total_errors = 0
    files_done = 0
    start = time.time()

    corpus_root = args.path if os.path.isdir(args.path) else os.path.dirname(args.path)
    work = [(f, args.max_records_per_file, args.drop_raw, corpus_root) for f in files]

    # Only ever keep a few files in flight. Submitting all 9,186 at once would
    # let workers run far ahead of the main process (which does the JSON
    # encoding and gzip compression), and every completed-but-unconsumed result
    # holds a whole file's records - tens of thousands of dicts - in memory.
    # On a corpus this size that grows without bound and OOMs the machine.
    # A window of a few per worker keeps every core busy while capping memory.
    max_inflight = max(2, args.workers * 2)
    pending_work = iter(work)
    stop_early = False

    with StreamingJSONArrayWriter(args.out, jsonl=args.jsonl) as writer:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(parse_one_file, item)
                for item in itertools.islice(pending_work, max_inflight)
            }
            try:
                while futures:
                    done, futures = wait(futures, return_when=FIRST_COMPLETED)
                    for future in done:
                        (_path, records, _fmt, _src, _ents,
                         samples, errors) = future.result()

                        # Statistics are counted per record actually written,
                        # not from the worker's own tallies. A worker parses its
                        # whole file, but --limit can stop us mid-file, so the
                        # worker counters would describe more records than were
                        # written - and the percentages would exceed 100%.
                        for record in records:
                            if args.limit and total_records >= args.limit:
                                break
                            writer.write(record)
                            total_records += 1
                            format_counts[record.get('matched_format', 'generic_fallback')] += 1
                            source_counts[record.get('source_type', 'Unknown')] += 1
                            for entity in record.get('entities') or []:
                                entity_counts[entity.get('type', '?')] += 1

                        # Drop the reference before pulling in the next file, so
                        # this file's records can be freed rather than being held
                        # alive until the loop variable is rebound.
                        del records
                        total_errors += errors
                        if len(fallback_samples) < 200:
                            fallback_samples.extend(samples)

                        files_done += 1
                        if files_done % 25 == 0 or files_done == len(files):
                            elapsed = time.time() - start
                            rate = total_records / elapsed if elapsed else 0
                            remaining = len(files) - files_done
                            eta = (elapsed / files_done) * remaining if files_done else 0
                            print(f"  {files_done:,}/{len(files):,} files | "
                                  f"{total_records:,} records | {rate:,.0f} rec/s | "
                                  f"ETA {eta / 3600:.1f}h", flush=True)

                        if args.limit and total_records >= args.limit:
                            print(f"\nReached --limit of {args.limit:,} records; stopping.")
                            stop_early = True
                            break

                    if stop_early:
                        for future in futures:
                            future.cancel()
                        break

                    # Top the window back up, one new file per completed file.
                    for item in itertools.islice(pending_work, len(done)):
                        futures.add(pool.submit(parse_one_file, item))
            except KeyboardInterrupt:
                print("\nInterrupted - the output file is still valid up to this point.")
                for future in futures:
                    future.cancel()

    elapsed = time.time() - start
    recognized = total_records - format_counts.get('generic_fallback', 0)
    total_entities = sum(entity_counts.values())

    lines: List[str] = []
    lines.append("=" * 74)
    lines.append("PARSE SUMMARY")
    lines.append("=" * 74)
    lines.append(f"Input path       : {args.path}")
    lines.append(f"Files parsed     : {files_done:,} of {len(files):,}")
    lines.append(f"Records written  : {total_records:,}  ->  {args.out}")
    lines.append(f"Read/parse errors: {total_errors:,}")
    lines.append(f"Elapsed          : {elapsed:,.1f}s "
                 f"({total_records / elapsed if elapsed else 0:,.0f} records/sec)")
    if total_records:
        lines.append(f"Recognized by a specific detector : {recognized:,} "
                     f"({recognized / total_records * 100:.2f}%)")
        lines.append(f"Entities extracted : {total_entities:,} "
                     f"({total_entities / total_records:.1f} per log)")

    lines.append("")
    lines.append("By source_type:")
    for name, count in source_counts.most_common():
        lines.append(f"  {name:<26} {count:>12,}")

    lines.append("")
    lines.append("Entities by type (these become graph nodes):")
    for name, count in entity_counts.most_common():
        lines.append(f"  {name:<26} {count:>12,}")

    lines.append("")
    lines.append("By matched_format:")
    for name, count in format_counts.most_common():
        share = count / total_records * 100 if total_records else 0
        lines.append(f"  {name:<34} {count:>12,}  ({share:5.2f}%)")

    if fallback_samples:
        unique = sorted(set(fallback_samples))
        lines.append("")
        lines.append(f"Unmatched samples ({len(unique)} unique) - each is a candidate "
                     f"for a new detector in core/parsers/:")
        for sample in unique[:40]:
            lines.append(f"  {sample!r}")

    report = "\n".join(lines)
    # A Windows console defaults to cp1252, and the unmatched-sample lines are
    # verbatim log text - one U+FFFD from a mojibake source line was enough to
    # kill the whole run with UnicodeEncodeError *after* the output file had
    # already been written. The report is diagnostics, so degrade the handful
    # of characters the console cannot draw rather than lose the run.
    _enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    print("" + report.encode(_enc, errors="replace").decode(_enc, errors="replace"))

    if args.report:
        with open(args.report, 'w', encoding='utf-8') as handle:
            handle.write(report + "\n")
        print(f"\nReport written to {args.report}")


if __name__ == '__main__':
    main()
