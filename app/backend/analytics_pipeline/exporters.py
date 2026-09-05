"""
exporters.py - Streaming exports of normalized events, for SIEM and Data Lake
ingestion.

This is ULPF requirement (g). The graph is the analytics store; it is not
where a SOC's existing Splunk/QRadar/Elastic deployment or a Parquet-backed
lake wants to read from. Four wire formats cover the realistic destinations:

    ndjson  one normalized event per line, the full Universal Event Schema.
            The lake format - newline-delimited JSON loads directly into
            Spark, DuckDB, BigQuery and every object-store pipeline.
    ecs     the same events projected into Elastic Common Schema shape, so an
            Elastic/OpenSearch cluster ingests them with no mapping work.
    cef     ArcSight Common Event Format, the lingua franca most SIEMs accept
            over syslog when nothing else is agreed.
    csv     flat columns, for a spreadsheet or a quick load into a relational
            staging table.

Two properties matter more than the format list:

* Everything STREAMS. A generator yields one line at a time and FastAPI sends
  it as it is produced, so exporting ten million events costs a constant
  amount of memory. Building a list and serialising it at the end is how an
  export endpoint takes a service down.

* Every export carries the lineage fields. An event that reaches a SIEM
  without `raw_message`, `source_file` and `source_record` has been stripped
  of exactly the thing that makes it admissible for a forensic or compliance
  question - so the export is lossless by default, and dropping the raw
  original is an explicit opt-out (`include_raw=false`) for the case where
  bandwidth genuinely matters more.
"""

import csv
import io
import json
from typing import Any, Dict, Iterable, Iterator, List, Optional

from taxonomy import UNIVERSAL_SCHEMA, cef_severity, to_ecs

FORMATS = ('ndjson', 'ecs', 'ocsf', 'cef', 'csv')

#  Adding a format means touching FORMATS, _WRITERS, MEDIA_TYPES, filename()
#  and describe_formats(). Missing one is silent until a request hits it -
#  adding OCSF, the media-type entry was forgotten and every OCSF export
#  returned a 500 while the emitter itself was correct. A guard at import
#  time turns that class of mistake into a startup failure instead.
MEDIA_TYPES = {
    'ndjson': 'application/x-ndjson',
    'ecs': 'application/x-ndjson',
    'ocsf': 'application/x-ndjson',
    'cef': 'text/plain; charset=utf-8',
    'csv': 'text/csv; charset=utf-8',
}

# Flat column order for CSV. attributes_json and embedding are excluded: a
# nested bag and a 1024-float vector do not belong in a flat table, and
# including the vector would make the file ~20x larger for no analytical gain
# in a spreadsheet.
CSV_COLUMNS = [
    f['field'] for f in UNIVERSAL_SCHEMA
    if f['field'] not in ('embedding',)
]


def _decode_attributes(row: Dict[str, Any]) -> Dict[str, Any]:
    """attributes_json is stored as text (Neo4j properties cannot nest). Parsed
    back into an object for the JSON formats so consumers do not have to
    double-decode; left alone if it is not valid JSON rather than raising and
    killing an export part-way through."""
    raw = row.get('attributes_json')
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _base_record(row: Dict[str, Any], include_raw: bool) -> Dict[str, Any]:
    out = {k: v for k, v in row.items() if k != 'embedding'}
    out['attributes'] = _decode_attributes(row)
    out.pop('attributes_json', None)
    if not include_raw:
        out.pop('raw_message', None)
    return out


def _ndjson_lines(rows: Iterable[Dict[str, Any]], include_raw: bool) -> Iterator[str]:
    for row in rows:
        yield json.dumps(_base_record(row, include_raw), default=str, ensure_ascii=False) + '\n'


def _ecs_lines(rows: Iterable[Dict[str, Any]], include_raw: bool) -> Iterator[str]:
    for row in rows:
        doc = to_ecs(row)
        attrs = _decode_attributes(row)
        if attrs:
            # ECS puts unmapped vendor fields under labels.*, values as strings.
            doc['labels'] = {k: str(v) for k, v in attrs.items()}
        if include_raw and row.get('raw_message'):
            doc.setdefault('event', {})['original'] = row['raw_message']
        elif not include_raw:
            doc.get('event', {}).pop('original', None)
        # Lineage has no single agreed ECS home; log.file.path and log.offset
        # are the closest and to_ecs already placed them.
        doc['ecs'] = {'version': '8.11.0'}
        yield json.dumps(doc, default=str, ensure_ascii=False) + '\n'


def _ocsf_lines(rows: Iterable[Dict[str, Any]], include_raw: bool) -> Iterator[str]:
    """OCSF 1.3.0 NDJSON. See ocsf.py for which classes are implemented and,
    just as importantly, which are deliberately not."""
    import ocsf
    yield from ocsf.lines(rows, include_raw)


def _cef_escape(value: Any, header: bool) -> str:
    """CEF escaping. Header fields escape | and \\; extension VALUES escape =
    and \\ instead. Getting this wrong silently corrupts every downstream
    parse, so the two cases are handled separately rather than approximated."""
    s = str(value if value is not None else '')
    s = s.replace('\\', '\\\\')
    s = s.replace('\n', ' ').replace('\r', ' ')
    return s.replace('|', '\\|') if header else s.replace('=', '\\=')


def _cef_lines(rows: Iterable[Dict[str, Any]], include_raw: bool) -> Iterator[str]:
    for row in rows:
        header = '|'.join([
            'CEF:0',
            'ULPF',                                            # device vendor
            'Universal Log Pre-processing Framework',          # device product
            '1.0',                                             # device version
            _cef_escape(row.get('matched_format') or 'generic_fallback', True),
            _cef_escape((row.get('message') or '')[:120], True),
            str(cef_severity(row.get('severity_score'))),
        ])
        ext = {
            'rt': row.get('timestamp'),
            'dvchost': row.get('hostname'),
            'deviceProcessName': row.get('process'),
            'cat': row.get('source_type'),
            'msg': row.get('message'),
            'cs1Label': 'severity', 'cs1': row.get('severity'),
            'cs2Label': 'component', 'cs2': row.get('component'),
            'cs3Label': 'sourceFile', 'cs3': row.get('source_file'),
            'cn1Label': 'sourceRecord', 'cn1': row.get('source_record'),
            'externalId': row.get('id'),
        }
        if row.get('http_method'):
            ext['requestMethod'] = row['http_method']
        if row.get('http_status') is not None:
            ext['cn2Label'], ext['cn2'] = 'httpStatus', row['http_status']
        if include_raw and row.get('raw_message'):
            ext['cs4Label'], ext['cs4'] = 'rawEvent', row['raw_message']
        body = ' '.join(f"{k}={_cef_escape(v, False)}" for k, v in ext.items() if v not in (None, ''))
        yield f"{header}|{body}\n"


def _csv_lines(rows: Iterable[Dict[str, Any]], include_raw: bool) -> Iterator[str]:
    columns = [c for c in CSV_COLUMNS if include_raw or c != 'raw_message']
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction='ignore', lineterminator='\n')
    writer.writeheader()
    yield buf.getvalue()
    for row in rows:
        buf.seek(0)
        buf.truncate(0)
        flat = {c: row.get(c) for c in columns}
        writer.writerow(flat)
        yield buf.getvalue()


_WRITERS = {
    'ndjson': _ndjson_lines,
    'ecs': _ecs_lines,
    'ocsf': _ocsf_lines,
    'cef': _cef_lines,
    'csv': _csv_lines,
}


#  Fail at import rather than on the first request for a half-registered
#  format. Cheap, and it catches exactly the mistake described above.
_missing = [f for f in FORMATS if f not in _WRITERS or f not in MEDIA_TYPES]
if _missing:
    raise RuntimeError(
        'export formats registered in FORMATS but missing a writer or media '
        'type: %s' % ', '.join(_missing))


def stream(rows: Iterable[Dict[str, Any]], fmt: str, include_raw: bool = True) -> Iterator[str]:
    """Yields the export one line at a time. See the module note on streaming."""
    writer = _WRITERS.get(fmt)
    if writer is None:
        raise ValueError(f"Unknown export format {fmt!r}. Valid: {sorted(FORMATS)}")
    return writer(rows, include_raw)


def filename(fmt: str, filtered: bool) -> str:
    ext = {'ndjson': 'ndjson', 'ecs': 'ndjson', 'ocsf': 'ndjson',
           'cef': 'cef', 'csv': 'csv'}[fmt]
    scope = 'filtered' if filtered else 'all'
    return f"ulpf-events-{scope}.{ext}"


def describe_formats() -> List[Dict[str, str]]:
    """For the UI, so the export options explain themselves."""
    return [
        {'id': 'ndjson', 'label': 'NDJSON',
         'blurb': 'One event per line, full Universal Event Schema. The Data Lake format — '
                  'loads directly into Spark, DuckDB, BigQuery or any object-store pipeline.'},
        {'id': 'ecs', 'label': 'ECS JSON',
         'blurb': 'Projected into Elastic Common Schema. Ingests into Elasticsearch or '
                  'OpenSearch with no field mapping work.'},
        {'id': 'ocsf', 'label': 'OCSF JSON',
         'blurb': 'Open Cybersecurity Schema Framework 1.3.0, NDJSON. Authentication (3002), '
                  'Network Activity (4001) and Application Lifecycle (1008) are implemented; '
                  'events that do not clearly belong to the first two go to 1008 rather than '
                  'being forced into a class.'},
        {'id': 'cef', 'label': 'CEF',
         'blurb': 'ArcSight Common Event Format — the format most SIEMs accept over syslog '
                  'when nothing else is agreed.'},
        {'id': 'csv', 'label': 'CSV',
         'blurb': 'Flat columns for a spreadsheet or a relational staging table.'},
    ]
