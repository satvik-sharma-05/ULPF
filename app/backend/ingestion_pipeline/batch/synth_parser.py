"""Draft a detector for a log format the framework does not yet recognise.

This is the practical form of requirement (i). Point it at a file of unmatched
lines and it prints a reviewable detector module - the shape inferred from the
data instead of read off the screen by a person.

    python -m batch.synth_parser --file unknown.log --name acme_firewall
    python -m batch.synth_parser --file unknown.log --out core/parsers/acme.py

It deliberately does NOT register what it writes. A parser that appears by
itself is a parser whose behaviour nobody knows, and every normalized event in
this system is supposed to be explainable. The draft is printed, a human reads
it, decides which captured field is the hostname, and commits it.

`--only-unmatched` runs the existing detectors first and synthesises from the
lines that fell through, which is the normal case: a new source is rarely
entirely new, it is the 8% of its lines nothing handles.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.parser import LogParser                                   # noqa: E402
from core.parser_synth import group_by_shape, synthesize, to_detector_source  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--file', required=True, help='File of sample log lines')
    ap.add_argument('--name', default='generated', help='Name for the source type')
    ap.add_argument('--out', default=None, help='Write the module here instead of stdout')
    ap.add_argument('--limit', type=int, default=2000, help='Lines to read')
    ap.add_argument('--only-unmatched', action='store_true',
                    help='Synthesise only from lines no existing detector claims')
    args = ap.parse_args()

    with open(args.file, 'r', encoding='utf-8', errors='ignore') as fh:
        lines = [ln.rstrip('\n') for ln in fh][:args.limit]
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        print('No usable lines in that file.', file=sys.stderr)
        return 1

    if args.only_unmatched:
        parser = LogParser()
        before = len(lines)
        lines = [ln for ln in lines
                 if parser.parse(ln).get('matched_format') == 'generic_fallback']
        print(f"{before - len(lines)} of {before} line(s) already match a detector; "
              f"synthesising from the remaining {len(lines)}.\n", file=sys.stderr)
        if not lines:
            print('Nothing unmatched - no new detector needed.', file=sys.stderr)
            return 0

    shapes = group_by_shape(lines)
    print(f"{len(shapes)} distinct line shape(s) in {len(lines)} line(s):", file=sys.stderr)
    for shape, members in shapes[:5]:
        print(f"  {len(members):5} lines  {members[0][:96]}", file=sys.stderr)
    print(file=sys.stderr)

    draft = synthesize(lines)
    if not draft:
        print('Could not infer a pattern - too few lines sharing a shape.', file=sys.stderr)
        return 1

    print(f"Largest shape: {draft['support']} line(s), covering "
          f"{draft['coverage']}% of the input.", file=sys.stderr)
    print(f"Fields discovered: {len(draft['fields'])}", file=sys.stderr)
    for f in draft['fields']:
        hint = f" -> {f['schema_hint']}" if f['schema_hint'] else ''
        print(f"  {f['name']:12} {f['token']:10} e.g. {str(f['examples'][0])[:40]}{hint}",
              file=sys.stderr)
    if draft['over_fit_risk']:
        print("\nWARNING: fewer than 4 sample lines. A constant in a small sample is not\n"
              "evidence of a constant in the format - feed it more lines if you can.",
              file=sys.stderr)
    for w in draft['warnings']:
        print(f"  note: {w}", file=sys.stderr)
    print(file=sys.stderr)

    source = to_detector_source(draft, args.name)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as fh:
            fh.write(source)
        print(f"Draft written to {args.out}. Review it, decide which field is the\n"
              f"hostname, then add it to core/parsers/registry.py.", file=sys.stderr)
    else:
        print(source)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
