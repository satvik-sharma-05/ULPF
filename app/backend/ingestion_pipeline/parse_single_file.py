"""
parse_single_file.py - Parse one raw log .txt file into a JSON file, using
the same LogParser + multiline reassembly the rest of the pipeline uses.

Usage:
    python parse_single_file.py path/to/export3.txt
    python parse_single_file.py path/to/export3.txt --out custom_name.json
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.parser import LogParser
from core.multiline import reassemble


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input', help="Path to the raw log .txt file")
    ap.add_argument('--out', default=None,
                    help="Output .json path (default: <input>_parsed.json next to the input file)")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        print(f"No such file: {args.input}")
        sys.exit(1)

    out_path = args.out or os.path.splitext(args.input)[0] + '_parsed.json'

    parser = LogParser()
    records = []
    with open(args.input, 'r', encoding='utf-8', errors='ignore') as f:
        for raw_record in reassemble(f):
            records.append(parser.parse(raw_record))

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(records, f, indent=2)

    print(f"Parsed {len(records):,} records from {args.input}")
    print(f"Wrote {out_path}")


if __name__ == '__main__':
    main()
