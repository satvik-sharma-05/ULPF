"""
compare_parsed_vs_original.py - Full-corpus verification: for every parsed
folder, independently re-read its ORIGINAL raw .txt files and check the
parsed output against them. Two layers, both real:

  1. COMPLETENESS: reassemble each original file into logical records (same
     multiline-joining logic the parser itself used) and compare that count
     - and the actual text content - against the parsed output's raw_message
     values for that file. Catches anything dropped, duplicated, or
     corrupted between "real file" and "parsed JSON", independent of
     whether individual field extraction is correct.

  2. FIELD ACCURACY: for each parsed record, check hostname/severity/
     process/IP-entities against that record's OWN raw_message text -
     same three-way severity grading (confirmed/inferred/contradicted) as
     test_parsing_accuracy.py, since many formats never spell severity out
     literally (that's not a defect, see that file's docstring).

One unit of work = one FOLDER (its parsed .json.gz is read exactly once,
grouped by source_file in memory, then every original .txt file in that
folder is checked against its pre-grouped slice) - not one unit per file,
which would re-read/re-decode the same folder's output up to ~100x over.

Usage:
    python compare_parsed_vs_original.py --raw ../tar_files/logs --parsed parsed_by_folder --workers 10
"""

import argparse
import gzip
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.multiline import reassemble

_SEVERITY_WORDS = ['EMERGENCY', 'FATAL', 'ALERT', 'CRITICAL', 'ERROR', 'WARNING', 'WARN', 'NOTICE', 'INFO', 'DEBUG']
_IP_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')


def check_field_accuracy(rec: Dict[str, Any]) -> Dict[str, str]:
    raw = rec.get('raw_message', '') or ''
    raw_upper = raw.upper()
    out = {}

    hostname = rec.get('hostname', '')
    if hostname and hostname != 'unknown-host':
        out['hostname'] = 'confirmed' if hostname.lower() in raw.lower() else 'mismatch'

    severity = (rec.get('severity') or '').upper()
    words_present = [w for w in _SEVERITY_WORDS if re.search(rf'\b{w}\b', raw_upper)]
    if severity and severity in words_present:
        out['severity'] = 'confirmed'
    elif not words_present:
        out['severity'] = 'inferred'
    elif severity and severity not in words_present:
        out['severity'] = 'contradicted'

    process = rec.get('process', '')
    if process:
        out['process'] = 'confirmed' if process.lower() in raw.lower() else 'mismatch'

    extracted_ips = {e['value'] for e in (rec.get('entities') or []) if e.get('type') == 'ip'}
    raw_ips = set(_IP_RE.findall(raw))
    if extracted_ips or raw_ips:
        out['ip_missed'] = 'yes' if (raw_ips - extracted_ips) else 'no'
        out['ip_hallucinated'] = 'yes' if (extracted_ips - raw_ips) else 'no'

    return out


def _digest(text: str) -> bytes:
    """16-byte blake2b digest instead of keeping the full raw string around -
    a folder can hold 1M+ records, and storing every raw_message twice (once
    per side of the comparison) is what OOM'd the first version of this
    script under 10 parallel workers. Collision probability at this scale is
    astronomically below the actual risk of a bug elsewhere in the pipeline,
    so this trades a theoretical near-zero risk for a large, real memory win."""
    import hashlib
    return hashlib.blake2b(text.encode('utf-8', errors='ignore'), digest_size=16).digest()


def compare_one_folder(args: Tuple[str, str, List[str]]):
    """(folder_name, raw_folder_path, parsed_json_path) -> list of per-file stats."""
    folder_name, raw_folder_path, parsed_path = args
    field_stats = Counter()

    # --- Read the folder's parsed output exactly ONCE, group by source_file.
    # Only the digest of raw_message is kept per record, not the record
    # itself - field-accuracy stats are tallied immediately, so nothing about
    # the record needs to survive past this loop iteration. ---
    by_file: Dict[str, Counter] = defaultdict(Counter)
    try:
        with gzip.open(parsed_path, 'rt', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line in ('[', ']'):
                    continue
                if line.endswith(','):
                    line = line[:-1]
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                by_file[rec.get('source_file', '')][_digest((rec.get('raw_message') or '').strip())] += 1
                for field, verdict in check_field_accuracy(rec).items():
                    field_stats[f'{field}:{verdict}'] += 1
    except Exception as e:
        return [{'folder': folder_name, 'file': None, 'error': f'could not read parsed output: {e}'}]

    # --- Compare each original file against its pre-grouped digest counts ---
    file_results = []
    for filename in sorted(os.listdir(raw_folder_path)):
        if not filename.endswith('.txt'):
            continue
        orig_path = os.path.join(raw_folder_path, filename)
        try:
            original_texts: Counter = Counter()
            original_count = 0
            with open(orig_path, 'r', encoding='utf-8', errors='ignore') as handle:
                for r in reassemble(handle):
                    original_texts[_digest(r.strip())] += 1
                    original_count += 1
        except Exception as e:
            file_results.append({'folder': folder_name, 'file': filename, 'error': f'could not read original: {e}'})
            continue

        parsed_texts = by_file.get(filename, Counter())
        parsed_count = sum(parsed_texts.values())

        missing = original_texts - parsed_texts
        extra = parsed_texts - original_texts

        file_results.append({
            'folder': folder_name, 'file': filename,
            'original_count': original_count, 'parsed_count': parsed_count,
            'count_match': 1 if original_count == parsed_count else 0,
            'missing_records': sum(missing.values()), 'extra_records': sum(extra.values()),
            'content_exact_match': 1 if not missing and not extra else 0,
        })

    return file_results, dict(field_stats)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--raw', default='../tar_files/logs')
    ap.add_argument('--parsed', default='parsed_by_folder')
    ap.add_argument('--workers', type=int, default=os.cpu_count())
    ap.add_argument('--out', default='comparison_results.json')
    args = ap.parse_args()

    work = []
    for dirpath, _dirnames, filenames in os.walk(args.raw):
        folder_name = os.path.basename(dirpath)
        parsed_path = os.path.join(args.parsed, f'{folder_name}.json.gz')
        if os.path.isfile(parsed_path) and any(fn.endswith('.txt') for fn in filenames):
            work.append((folder_name, dirpath, parsed_path))

    print(f"Comparing {len(work):,} folder(s) (each read once, ~100 files/folder checked internally), "
          f"{args.workers} workers\n")

    all_file_results = []
    field_totals = Counter()
    folders_done = 0
    start = time.time()
    max_inflight = max(2, args.workers * 2)
    work_iter = iter(work)

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(compare_one_folder, item) for item in
                   __import__('itertools').islice(work_iter, max_inflight)}
        while futures:
            done, futures = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                result = future.result()
                if isinstance(result, list):  # error case
                    all_file_results.extend(result)
                else:
                    file_results, field_stats = result
                    all_file_results.extend(file_results)
                    for k, v in field_stats.items():
                        field_totals[k] += v
                folders_done += 1
                elapsed = time.time() - start
                eta = (elapsed / folders_done) * (len(work) - folders_done) if folders_done else 0
                print(f"  {folders_done:,}/{len(work):,} folders compared | "
                      f"{len(all_file_results):,} files so far | ETA {eta/60:.1f}min", flush=True)
            for item in __import__('itertools').islice(work_iter, len(done)):
                futures.add(pool.submit(compare_one_folder, item))

    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump({'files': all_file_results, 'field_totals': dict(field_totals)}, f, indent=2, default=str)

    total_files = len(all_file_results)
    exact_content_files = sum(r.get('content_exact_match', 0) for r in all_file_results)
    count_match_files = sum(r.get('count_match', 0) for r in all_file_results)
    total_missing = sum(r.get('missing_records', 0) for r in all_file_results)
    total_extra = sum(r.get('extra_records', 0) for r in all_file_results)
    total_original = sum(r.get('original_count', 0) for r in all_file_results)
    total_parsed = sum(r.get('parsed_count', 0) for r in all_file_results)

    print("\n" + "=" * 74)
    print(f"COMPLETENESS ({total_files:,} files compared)")
    if total_files:
        print(f"  Files with exact record count match : {count_match_files:,}/{total_files:,} "
              f"({count_match_files/total_files*100:.2f}%)")
        print(f"  Files with exact content match       : {exact_content_files:,}/{total_files:,} "
              f"({exact_content_files/total_files*100:.2f}%)")
    print(f"  Total original records: {total_original:,}   Total parsed records: {total_parsed:,}")
    print(f"  Missing (in original, not in parsed) : {total_missing:,}")
    print(f"  Extra (in parsed, not in original)   : {total_extra:,}")

    print(f"\nFIELD ACCURACY (aggregated across all parsed records)")
    for key in sorted(field_totals):
        print(f"  {key:30s} {field_totals[key]:>12,}")


if __name__ == '__main__':
    main()
