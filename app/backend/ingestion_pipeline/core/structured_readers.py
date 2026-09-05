"""
core/structured_readers.py - Turns a whole uploaded file (CSV, XML, JSON, or
plain text/Syslog/CEF/LEEF) into the stream of "record strings" LogParser
already knows how to consume.

Why this exists rather than teaching LogParser about file formats: parser.py
is deliberately "one raw string in, one normalized log out" - it has no
concept of a file. CSV and XML are record-*per-row*/*per-element*, not
record-per-line, so something has to peel a file into individual records
before parser.parse() ever sees them. The peeling strategy here is uniform:
flatten each CSV row / XML element into a plain dict and JSON-encode it, so
it lands on parser.py's existing "a record string that starts with `{` is a
pre-structured event" path (`_parse_json_structure`) - no new parsing logic
duplicated, just a new way to arrive at a dict.

Plain text/Syslog/CEF/LEEF need no flattening - core.multiline.reassemble()
already turns a stream of lines into logical records, and the CEF/LEEF/syslog
detectors in core/parsers/ take it from there.
"""

import csv
import io
import json
import logging
import re
from typing import Any, Dict, Iterator, Optional
from xml.etree import ElementTree

from .multiline import _is_record_start, reassemble

logger = logging.getLogger(__name__)

#  RFC 5424 opens with a priority in angle brackets, so a leading "<" is
#  not by itself evidence of XML.
_RE_SYSLOG_PRI = re.compile(r'^<[0-9]{1,3}>')

SUPPORTED_FORMATS = ('auto', 'text', 'json', 'csv', 'xml', 'cef', 'leef')

#  Extensions that make a real claim about STRUCTURE. These are trusted
#  over the content sniff: someone who named a file .csv meant it.
_STRUCTURED_EXT = {
    '.csv': 'csv', '.tsv': 'csv',
    '.xml': 'xml',
    '.json': 'json', '.jsonl': 'json', '.ndjson': 'json',
}
#  Extensions that say "this is a log" and nothing whatsoever about its
#  structure. A CSV export named export.log is still a CSV, and treating
#  the extension as authoritative here fed it to the syslog path - header
#  row and all - which is what this split exists to prevent.
_LOG_EXT = ('.log', '.txt', '.syslog', '.cef', '.leef')

#  The flat mapping is kept for anything that wants the whole picture; the
#  sniffer no longer uses it as one lookup, because the halves differ in
#  how much they should be believed.
_EXT_FORMAT = dict(_STRUCTURED_EXT, **{e: 'text' for e in _LOG_EXT})


def sniff_format(filename: str, sample_text: str) -> str:
    """Detects the format from the content, using the extension only where it
    actually claims a structure.

    Order matters, and used not to: the extension was consulted first and
    returned unconditionally, so `.log` - which means nothing more specific
    than "a log" - overrode the file's own contents and sent CSV and XML down
    the syslog path.
    """
    lowered = filename.lower()
    for ext, fmt in _STRUCTURED_EXT.items():
        if lowered.endswith(ext):
            return fmt

    stripped = sample_text.lstrip()
    if _looks_like_xml(stripped):
        return 'xml'
    if stripped.startswith('{') or stripped.startswith('['):
        return 'json'
    if 'CEF:' in stripped[:200] or 'LEEF:' in stripped[:200]:
        return 'text'  # the CEF/LEEF line detectors handle it from here
    if _looks_like_csv(stripped):
        return 'csv'
    return 'text'


def _looks_like_xml(stripped: str) -> bool:
    """A leading '<' is not enough: RFC 5424 syslog opens with a PRI."""
    if not stripped.startswith('<'):
        return False
    if stripped.startswith('<?xml'):
        return True
    return not _RE_SYSLOG_PRI.match(stripped)


#  Delimiters worth trying, in the order they are worth trying them.
_CSV_DELIMS = (',', '\t', ';', '|')
_CSV_MIN_COLUMNS = 3
_CSV_MIN_ROWS = 2


def _looks_like_csv(stripped: str) -> bool:
    """Strict on purpose.

    csv.Sniffer() will claim almost any syslog as CSV, because firewall and
    IDS lines are full of commas - and misreading a syslog feed as CSV is the
    worse failure of the two, since it destroys every record rather than
    missing one file. So this asks for a genuinely tabular shape: the same
    field count, at least three columns, across several consecutive lines,
    and a first line that is not itself a log record header.

    The deliberate gap: a HEADERLESS csv whose first column is a timestamp
    reads as a log record and is treated as text. That is the safe direction
    to fail in, and selecting CSV explicitly overrides it.
    """
    lines = [ln for ln in stripped.splitlines()[:6] if ln.strip()]
    if len(lines) < _CSV_MIN_ROWS + 1:      # a header, plus rows to confirm it
        return False
    if _is_record_start(lines[0]):
        return False
    for delim in _CSV_DELIMS:
        try:
            rows = [r for r in csv.reader(lines, delimiter=delim) if r]
        except csv.Error:
            continue
        widths = [len(r) for r in rows]
        if len(widths) < _CSV_MIN_ROWS + 1:
            continue
        if widths[0] >= _CSV_MIN_COLUMNS and len(set(widths)) == 1:
            return True
    return False


def _iter_csv(text: str) -> Iterator[str]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample)
    except csv.Error:
        dialect = csv.excel  # plain comma-separated
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    for row in reader:
        # DictReader puts unmatched extra columns under the None key - fold
        # them into a visible field instead of silently discarding data or
        # crashing json.dumps on a None key.
        extra = row.pop(None, None)
        if extra:
            row['_extra_columns'] = extra
        yield json.dumps({k: v for k, v in row.items() if k is not None}, default=str)


def _flatten_xml_element(el: ElementTree.Element) -> Dict[str, Any]:
    """One level of flattening: the element's own attributes, plus each
    child's tag -> text (or, if a child repeats, a list of its texts). Deep
    enough for the common vendor shapes (Windows Event XML, CEF-over-XML
    wrappers) without pulling in a full XML-to-dict library."""
    out: Dict[str, Any] = dict(el.attrib)
    if el.text and el.text.strip():
        out['_text'] = el.text.strip()
    children: Dict[str, Any] = {}
    for child in el:
        tag = child.tag.split('}')[-1]  # drop an XML namespace prefix
        text = (child.text or '').strip()
        attrib = dict(child.attrib)

        # <Data Name="TargetUserName">administrator</Data> and friends:
        # the element is a name/value pair wearing a generic tag, so key
        # it by the name it declares rather than by that tag. Without
        # this the value is dropped and every such field is lost.
        key_attr = next((a for a in ('Name', 'name', 'key', 'Key')
                         if a in attrib), None)
        if key_attr and text and len(attrib) == 1:
            children[attrib[key_attr]] = text
            continue

        value: Any
        if attrib:
            # Keep the text too - it used to be discarded outright.
            value = dict(attrib)
            if text:
                value['_text'] = text
        else:
            value = text
        if not value and len(child):
            value = _flatten_xml_element(child)
        if tag in children:
            if not isinstance(children[tag], list):
                children[tag] = [children[tag]]
            children[tag].append(value)
        else:
            children[tag] = value
    out.update(children)
    return out


def _iter_xml(text: str) -> Iterator[str]:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as e:
        logger.warning(f"XML upload did not parse as a whole document: {e}")
        return
    children = list(root)
    if not children:
        # A single self-contained event with no repeating wrapper.
        yield json.dumps(_flatten_xml_element(root), default=str)
        return
    # The common shape is <Events><Event>...</Event><Event>...</Event></Events>
    # - one record per direct child of the root, regardless of tag name (a
    # file mixing element types under one root still yields one record each).
    for child in children:
        yield json.dumps(_flatten_xml_element(child), default=str)


def _iter_json(text: str) -> Iterator[str]:
    stripped = text.strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        # Not a single JSON document - assume JSON Lines and pass each
        # non-blank line straight through; parser.parse() validates it itself.
        for line in stripped.splitlines():
            line = line.strip()
            if line:
                yield line
        return

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield json.dumps(item, default=str)
    elif isinstance(data, dict):
        # A single event, or an export wrapper like {"logs": [...]}.
        records = data.get('logs') or data.get('records') or data.get('events')
        if isinstance(records, list):
            for item in records:
                if isinstance(item, dict):
                    yield json.dumps(item, default=str)
        else:
            yield json.dumps(data, default=str)


def iter_records(file_text: str, filename: str = '', format_hint: str = 'auto') -> Iterator[str]:
    """The single entry point: yields one record string per logical event,
    ready for `core.parser.LogParser().parse()`.

    `format_hint` is whatever the caller (a CLI flag, or the upload API's
    form field) was given; 'auto' sniffs from the filename/content.
    """
    fmt = format_hint if format_hint and format_hint != 'auto' else sniff_format(filename, file_text[:2048])

    if fmt == 'csv':
        yield from _iter_csv(file_text)
    elif fmt == 'xml':
        yield from _iter_xml(file_text)
    elif fmt == 'json':
        yield from _iter_json(file_text)
    else:
        # text / cef / leef / syslog / anything else: one physical-line-group
        # per record: core.multiline.reassemble() handles continuation lines,
        # and core/parsers/ (including the cef_leef.py detectors) takes it
        # from there - same path the batch/realtime pipelines already use.
        yield from reassemble(file_text.splitlines())


def detect_format_label(filename: str, file_text: str, format_hint: str = 'auto') -> str:
    """Human-readable version of sniff_format(), for surfacing "detected as
    CSV" in an upload UI before the user commits to ingesting it."""
    fmt = format_hint if format_hint and format_hint != 'auto' else sniff_format(filename, file_text[:2048])
    return {
        'csv': 'CSV', 'xml': 'XML', 'json': 'JSON / JSON Lines', 'text': 'Syslog / CEF / LEEF / plain text',
    }.get(fmt, fmt)
