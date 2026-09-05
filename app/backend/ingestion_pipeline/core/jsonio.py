"""
jsonio.py - Streaming reader/writer for the parsed-log JSON file.

The corpus this pipeline targets is ~33GB across ~9,700 files, so output.json
holds tens of millions of records. `json.dump(all_records)` would need the
whole list in memory, and `json.load()` would need it again on the way back
in - neither is survivable at that size.

The compromise here: write a *real* JSON array (so the file is valid JSON and
any standard tool can read it) but put exactly one record per line:

    [
    {"id": "log-...", ...},
    {"id": "log-...", ...}
    ]

That layout is what makes it streamable - a reader can handle one line at a
time and never materialize more than a single record, while `json.load()` still
works unchanged on files small enough to fit in memory. Writing `.jsonl` is
also supported for downstream tools that prefer it.

Writing to a path ending in `.gz` transparently gzips the stream. That is not a
nicety at this scale: the full corpus is ~134M records, roughly 161GB of plain
JSON - more than a typical disk has free. Log records are extremely repetitive,
so gzip takes that to about a tenth. Readers detect `.gz` automatically, so
nothing downstream has to care which form it got.
"""

import gzip
import json
import os
from typing import Any, Dict, Iterator, Optional, TextIO

# Compression level 6 (gzip's default). Compression runs in the process that
# collects worker output, so it is in the throughput path - but here disk space
# is the binding constraint, not wall-clock time, so the better ratio wins.
# Level 9 costs several times the CPU for a couple of percent more on data this
# repetitive, which is not a trade worth making.
_GZIP_LEVEL = 6


def _open_text(path: str, mode: str) -> TextIO:
    """Opens a text stream, transparently gzipped when the path ends in .gz."""
    if path.endswith('.gz'):
        return gzip.open(path, mode, encoding='utf-8', compresslevel=_GZIP_LEVEL) \
            if 'w' in mode else gzip.open(path, mode, encoding='utf-8')
    return open(path, mode, encoding='utf-8', newline='\n') if 'w' in mode \
        else open(path, mode, encoding='utf-8')


class StreamingJSONArrayWriter:
    """Writes a JSON array incrementally, one record per line.

    Used as a context manager so the closing bracket is always written - an
    interrupted run otherwise leaves a file that no JSON parser will touch.
    """

    def __init__(self, path: str, jsonl: bool = False, ensure_ascii: bool = False):
        self.path = path
        # `.jsonl.gz` is still JSON Lines - strip the compression suffix before
        # deciding the format, or a gzipped jsonl file would be written as an
        # array instead.
        base = path[:-3] if path.endswith('.gz') else path
        self.jsonl = jsonl or base.endswith('.jsonl')
        self.ensure_ascii = ensure_ascii
        self.count = 0
        self._handle: Optional[TextIO] = None

    def __enter__(self) -> 'StreamingJSONArrayWriter':
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._handle = _open_text(self.path, 'wt')
        if not self.jsonl:
            self._handle.write('[\n')
        return self

    def write(self, record: Dict[str, Any]) -> None:
        if self._handle is None:
            raise RuntimeError("writer used outside its context manager")
        if not self.jsonl and self.count:
            self._handle.write(',\n')
        json.dump(record, self._handle, ensure_ascii=self.ensure_ascii, default=str)
        if self.jsonl:
            self._handle.write('\n')
        self.count += 1

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is None:
            return
        try:
            if not self.jsonl:
                self._handle.write('\n]\n' if self.count else ']\n')
        finally:
            self._handle.close()
            self._handle = None


def stream_read_records(path: str) -> Iterator[Dict[str, Any]]:
    """Yields records from a file written by StreamingJSONArrayWriter (or any
    JSON-lines file), without loading the whole thing.

    Falls back to a normal whole-file parse if the line-oriented read doesn't
    work - that covers a hand-written or pretty-printed output.json that didn't
    come from the writer above.
    """
    with _open_text(path, 'rt') as handle:
        first_meaningful_line_seen = False
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line in ('[', ']'):
                continue
            if line.endswith(','):
                line = line[:-1]
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if first_meaningful_line_seen:
                    # Mid-file garbage: skip the line rather than aborting a
                    # multi-hour ingest over one bad record.
                    continue
                break  # not line-oriented at all - retry as a whole document
            first_meaningful_line_seen = True
            if isinstance(record, dict):
                yield record
        else:
            return

    with _open_text(path, 'rt') as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        data = data.get('logs', data.get('records', []))
    for record in data or []:
        if isinstance(record, dict):
            yield record


def count_records(path: str) -> int:
    """Cheap record count for progress reporting."""
    return sum(1 for _ in stream_read_records(path))
