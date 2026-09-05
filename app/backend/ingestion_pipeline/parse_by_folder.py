"""
parse_by_folder.py - Parse the REAL raw log corpus (tar_files/logs/, 92
folders x ~100 files, 33GB), preserving the true folder identity that
batch/parse_logs.py itself discards (it only ever keeps
os.path.basename(file_path) as source_file - see its parse_one_file()).

Adds BOTH source_file (basename, same as before) and source_folder (the
actual containing folder name) to every record, then writes one JSON export
per folder - matching the "same as the logs structure" request: one export
per original folder, containing that folder's ~100 files' worth of records.

File-level parallelism (one file = one unit of work sent to a worker),
matching parse_logs.py's own design - far less IPC overhead than per-record,
since a whole file's ~10K-100K records cross the process boundary as a
single message instead of one message per record.

Usage:
    python parse_by_folder.py --path ../tar_files/logs --out parsed_by_folder --workers 10
"""

import argparse
import os
import sys
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.jsonio import StreamingJSONArrayWriter
from core.multiline import reassemble
from core.parser import LogParser

_PARSER = None


def _get_parser() -> LogParser:
    global _PARSER
    if _PARSER is None:
        _PARSER = LogParser()
    return _PARSER


def find_files(root: str) -> List[Tuple[str, str, str]]:
    """(folder_name, file_path, basename) for every *.txt file under root,
    at any depth - folder_name is the immediate parent directory's name."""
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        folder_name = os.path.basename(dirpath)
        for filename in filenames:
            if filename.endswith('.txt'):
                out.append((folder_name, os.path.join(dirpath, filename), filename))
    return sorted(out)


def parse_one_file(args: Tuple[str, str, str]):
    folder_name, file_path, basename = args
    parser = _get_parser()
    records: List[Dict[str, Any]] = []
    entity_counts: Counter = Counter()
    error_count = 0
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as handle:
            for raw_record in reassemble(handle):
                try:
                    parsed = parser.parse(raw_record)
                except Exception:
                    error_count += 1
                    continue
                parsed['source_file'] = basename
                parsed['source_folder'] = folder_name
                for entity in parsed.get('entities') or []:
                    entity_counts[entity.get('type', '?')] += 1
                records.append(parsed)
    except Exception as e:
        print(f"  [warn] could not read {file_path}: {e}", file=sys.stderr)
        error_count += 1
    return (folder_name, records, entity_counts, error_count)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--path', default='../tar_files/logs')
    ap.add_argument('--out', default='parsed_by_folder')
    ap.add_argument('--workers', type=int, default=os.cpu_count())
    args = ap.parse_args()

    files = find_files(args.path)
    folders = sorted({f[0] for f in files})
    print(f"Found {len(files):,} file(s) across {len(folders):,} folder(s) under {args.path}")
    print(f"Parsing with {args.workers} worker process(es) -> {args.out}/<folder_name>.json.gz\n")

    os.makedirs(args.out, exist_ok=True)

    writers: Dict[str, StreamingJSONArrayWriter] = {}
    total_records = 0
    total_entities = 0
    total_errors = 0
    files_done = 0
    start = time.time()

    max_inflight = max(2, args.workers * 2)
    work_iter = iter(files)

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(parse_one_file, item) for item in
                   __import__('itertools').islice(work_iter, max_inflight)}
        while futures:
            done, futures = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                folder_name, records, entity_counts, errors = future.result()
                if folder_name not in writers:
                    path = os.path.join(args.out, f"{folder_name}.json.gz")
                    w = StreamingJSONArrayWriter(path)
                    w.__enter__()
                    writers[folder_name] = w
                for rec in records:
                    writers[folder_name].write(rec)
                total_records += len(records)
                total_entities += sum(entity_counts.values())
                total_errors += errors
                files_done += 1

                if files_done % 50 == 0 or files_done == len(files):
                    elapsed = time.time() - start
                    rate = total_records / elapsed if elapsed else 0
                    eta = (elapsed / files_done) * (len(files) - files_done) if files_done else 0
                    print(f"  {files_done:,}/{len(files):,} files | {total_records:,} records | "
                          f"{rate:,.0f} rec/s | {total_entities:,} entities | "
                          f"ETA {eta/60:.1f}min | {len(writers)} folders open", flush=True)

            for item in __import__('itertools').islice(work_iter, len(done)):
                futures.add(pool.submit(parse_one_file, item))

    for w in writers.values():
        w.__exit__(None, None, None)

    elapsed = time.time() - start
    print(f"\nDONE: {total_records:,} records across {len(writers)} folder exports under {args.out}/ "
          f"in {elapsed:.0f}s ({total_records/elapsed:,.0f} rec/s)")
    print(f"Entities extracted: {total_entities:,} ({total_entities/total_records:.2f} per log)"
          if total_records else "")
    print(f"Read/parse errors: {total_errors:,}")


if __name__ == '__main__':
    main()
