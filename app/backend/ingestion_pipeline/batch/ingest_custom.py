"""
batch/ingest_custom.py - Ingest ONE user-supplied log file of any supported
format straight into the graph. This is what "Custom logs" mode runs.

    python -m batch.ingest_custom --file /path/to/whatever.csv
    python -m batch.ingest_custom --file vendor.log --format text --no-embed
    python -m batch.ingest_custom --file events.xml --wipe

Supported: Syslog / plain text, JSON, JSON Lines, XML, CSV/TSV, CEF, LEEF,
and proprietary vendor text shapes (which fall through to the same detector
chain and generic fallback the bundled corpus uses). Format is sniffed from
the filename and content unless --format says otherwise.

The whole point is that this is NOT a second pipeline: the file is peeled
into records by core/structured_readers.py, then every record goes through
the exact same LogParser -> entity extraction -> EmbeddingGenerator ->
Neo4jWriter path the bundled corpus does. Same graph shape, same embeddings,
so the chatbot and analytics work identically on uploaded logs.

Embeddings are ON by default here (unlike batch/ingest_to_neo4j.py, where
--embed is opt-in because the full 117M-record corpus makes them a
multi-week job). An uploaded file is small enough that embedding it inline
costs seconds-to-minutes, and without them the chatbot silently degrades to
full-text search on exactly the data the user just added - the wrong default
for this mode. Use --no-embed to skip them anyway.
"""

import argparse
import os
import sys
import time
from collections import Counter
from typing import Any, Callable, Dict, Iterator, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from batch.ingest_to_neo4j import add_neo4j_args, apply_neo4j_overrides, batched, connect_or_exit

# Guards a single upload. A pasted multi-GB file would otherwise be read into
# memory whole by read_text() below; the CLI can raise it deliberately.
DEFAULT_MAX_BYTES = 200 * 1024 * 1024


def read_text(path: str, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    size = os.path.getsize(path)
    if size > max_bytes:
        raise ValueError(
            f"{path} is {size / 1024 / 1024:.1f}MB, over the {max_bytes / 1024 / 1024:.0f}MB "
            f"limit for a single custom upload. Split it, or raise --max-mb."
        )
    # Same tolerance the batch parser uses: stray NUL bytes and bad encodings
    # in a vendor export shouldn't abort the whole file.
    with open(path, 'r', encoding='utf-8', errors='ignore') as handle:
        return handle.read()


def ingest_text(
    text: str,
    filename: str,
    writer,
    format_hint: str = 'auto',
    embed: bool = True,
    limit: Optional[int] = None,
    batch_size: int = 500,
    source_label: Optional[str] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Any]:
    """Parses and writes one file's worth of log text. Returns a summary dict.

    Importable (not just a CLI): the upload API calls this directly, so the
    HTTP path and the command line can never diverge in what they actually do
    to the graph.
    """
    from core.parser import LogParser
    from core.structured_readers import detect_format_label, iter_records

    detected = detect_format_label(filename, text[:2048], format_hint)
    parser = LogParser()

    embedder = None
    if embed:
        from core.embeddings import EmbeddingGenerator
        embedder = EmbeddingGenerator()

    format_counts: Counter = Counter()
    source_counts: Counter = Counter()
    entity_counts: Counter = Counter()
    parse_errors = 0
    total_parsed = 0
    total_written = 0
    start = time.time()

    def record_stream() -> Iterator[Dict[str, Any]]:
        nonlocal parse_errors, total_parsed
        for ordinal, raw in enumerate(iter_records(text, filename, format_hint), start=1):
            if limit and total_parsed >= limit:
                return
            try:
                parsed = parser.parse(raw)
            except Exception:
                # parse() is contractually non-raising; count rather than
                # lose a record silently if that ever stops being true.
                parse_errors += 1
                continue
            # Tags every log with the upload it came from, so a user can later
            # ask "what did that file contain" - and so a bad upload can be
            # identified in the graph rather than being anonymous.
            parsed['source_file'] = source_label or os.path.basename(filename)
            parsed['source_record'] = ordinal
            total_parsed += 1
            format_counts[parsed.get('matched_format', 'generic_fallback')] += 1
            source_counts[parsed.get('source_type', 'Unknown')] += 1
            for entity in parsed.get('entities') or []:
                entity_counts[entity.get('type', '?')] += 1
            yield parsed

    for batch in batched(record_stream(), batch_size):
        if embedder:
            texts = [r.get('normalized_message') or r.get('message') or '' for r in batch]
            for record, vector in zip(batch, embedder.generate_batch(texts)):
                record['embedding'] = vector
        total_written += writer.write_batch(batch)
        if progress:
            progress(total_parsed, total_written)

    elapsed = time.time() - start
    return {
        'filename': os.path.basename(filename),
        'detected_format': detected,
        'records_parsed': total_parsed,
        'records_written': total_written,
        'parse_errors': parse_errors,
        'embeddings': bool(embedder),
        # Truthful about *which* embeddings: core/embeddings.py falls back to a
        # deterministic hash vector when the model can't load, and those are
        # syntactically valid but semantically meaningless - vector search
        # would return plausible nonsense. Surfacing it here means an upload
        # that silently lost semantic search says so.
        'embedding_model_loaded': bool(embedder and embedder.is_loaded),
        'elapsed_seconds': round(elapsed, 2),
        'by_format': dict(format_counts.most_common(10)),
        'by_source_type': dict(source_counts.most_common(10)),
        'entities_by_type': dict(entity_counts.most_common(15)),
        'total_entities': sum(entity_counts.values()),
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('--file', required=True, help="The log file to ingest")
    ap.add_argument('--format', default='auto',
                    choices=('auto', 'text', 'json', 'csv', 'xml', 'cef', 'leef'),
                    help="Override format detection. Default: auto (sniffed)")
    ap.add_argument('--no-embed', action='store_true',
                    help="Skip embeddings (vector search won't work on this data)")
    ap.add_argument('--limit', type=int, default=None, help="Stop after this many records")
    ap.add_argument('--batch-size', type=int, default=500, help="Records per transaction")
    ap.add_argument('--max-mb', type=int, default=DEFAULT_MAX_BYTES // (1024 * 1024),
                    help=f"Maximum file size to accept. Default: {DEFAULT_MAX_BYTES // (1024*1024)}MB")
    ap.add_argument('--wipe', action='store_true',
                    help="DESTRUCTIVE - delete every node in the database first")
    add_neo4j_args(ap)
    args = ap.parse_args()

    apply_neo4j_overrides(args)

    if not os.path.isfile(args.file):
        print(f"No such file: {args.file}")
        sys.exit(1)

    try:
        text = read_text(args.file, max_bytes=args.max_mb * 1024 * 1024)
    except ValueError as e:
        print(str(e))
        sys.exit(1)

    writer, neo4j_config = connect_or_exit()

    if args.wipe:
        from batch.ingest_to_neo4j import wipe_database
        wipe_database(writer, neo4j_config)

    embed = not args.no_embed
    if embed:
        print("Loading embedding model (BAAI/bge-m3)...")

    print(f"\nIngesting {args.file} ...\n")

    def show(parsed: int, written: int) -> None:
        print(f"  {parsed:,} parsed | {written:,} written", flush=True)

    summary = ingest_text(
        text=text, filename=args.file, writer=writer, format_hint=args.format,
        embed=embed, limit=args.limit, batch_size=args.batch_size, progress=show,
    )

    print("\n" + "=" * 60)
    print("CUSTOM INGEST SUMMARY")
    print("=" * 60)
    print(f"File            : {summary['filename']}")
    print(f"Detected format : {summary['detected_format']}")
    print(f"Records parsed  : {summary['records_parsed']:,}")
    print(f"Records written : {summary['records_written']:,}")
    print(f"Parse errors    : {summary['parse_errors']:,}")
    print(f"Elapsed         : {summary['elapsed_seconds']}s")
    print(f"Entities        : {summary['total_entities']:,}")
    if summary['embeddings'] and not summary['embedding_model_loaded']:
        print("\nWARNING: the embedding model did not load, so the vectors written are the\n"
              "deterministic hash fallback - they are NOT semantically meaningful and\n"
              "vector search over this data will return nonsense. Install\n"
              "sentence-transformers + torch and re-run to fix.")
    if summary['by_format']:
        print("\nBy matched_format:")
        for name, count in summary['by_format'].items():
            print(f"  {name:<30} {count:>8,}")
    if summary['entities_by_type']:
        print("\nEntities by type:")
        for name, count in summary['entities_by_type'].items():
            print(f"  {name:<30} {count:>8,}")

    from batch.ingest_to_neo4j import report_graph
    report_graph(writer, neo4j_config)
    writer.close()


if __name__ == '__main__':
    main()
