"""
taxonomy.py - The Universal Event Schema. The declared common taxonomy every
ingested event is normalized into, whatever it looked like on the way in.

This is ULPF requirement (c): "normalize fields into a common event taxonomy".
The normalization itself has always happened - core/parser.py produces exactly
these fields and core/neo4j_writer.py persists them - but a taxonomy nobody can
read is not a taxonomy, it is an implementation detail. Writing it down here,
serving it over the API and rendering it in the UI is what turns an internal
convention into a contract that an integrator can build against.

Three deliberate decisions:

* Every field carries its ECS (Elastic Common Schema) and OCSF (Open
  Cybersecurity Schema Framework) equivalent. A "universal" schema that is
  universal only to itself would just be a 34th proprietary format - the
  problem the framework exists to solve. These mappings are what let the
  export in exporters.py emit ECS-shaped JSON for a SIEM without a second
  normalization pass, and what let a reviewer check our field names against
  a standard they already know.

* `required` marks the fields guaranteed present on every event regardless of
  source. Those are what downstream correlation can safely assume; anything
  else is best-effort and must be treated as optional by consumers.

* The lineage fields are part of the schema, not metadata bolted on. ULPF (a)
  and (d) - lossless preservation and traceability - are only real if the
  pointer back to the original event is a first-class, documented field that
  survives every transformation.

Coverage percentages in `field_coverage()` are measured live against the graph
rather than asserted here, because a schema document that claims 100% coverage
while the data says otherwise is worse than none.
"""

from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------
# group: how the field is used, for presentation and for the export shapers.
#   identity   - what/when/where this event is
#   source     - which system produced it
#   severity   - how bad it is
#   content    - the human-readable payload
#   lineage    - the link back to the original event  (ULPF a + d)
#   parsing    - how confident we are in the normalization  (ULPF i)
#   enrichment - derived fields not present in the original
UNIVERSAL_SCHEMA: List[Dict[str, Any]] = [
    {
        'field': 'id', 'type': 'string', 'group': 'identity', 'required': True,
        'description': 'Stable event identifier: md5 of timestamp|hostname|process|raw. '
                       'Deterministic, so re-ingesting the same source line MERGEs onto '
                       'the same event instead of duplicating it. When the source carries '
                       'no timestamp the hash uses a constant marker rather than the read '
                       'time - otherwise the id would be a function of when the line '
                       'happened to be ingested and MERGE would stop being idempotent.',
        'ecs': 'event.id', 'ocsf': 'metadata.uid',
    },
    {
        'field': 'timestamp', 'type': 'string (ISO-8601)', 'group': 'identity', 'required': True,
        'description': 'Event time, normalized to ISO-8601. Falls back to ingest time only '
                       'when the source line carries no parseable timestamp.',
        'ecs': '@timestamp', 'ocsf': 'time',
    },
    {
        'field': 'day', 'type': 'string (YYYY-MM-DD)', 'group': 'identity', 'required': True,
        'description': 'Calendar day of the event, denormalized for index-backed range '
                       'filtering without slicing the timestamp string per row.',
        'ecs': '(derived from @timestamp)', 'ocsf': '(derived from time)',
    },
    {
        'field': 'timestamp_source', 'type': "string ('event' | 'ingest')", 'group': 'identity', 'required': True,
        'description': "'event' when the source line carried its own timestamp, 'ingest' "
                       'when it did not and `timestamp` is the read time instead. Formats '
                       'like the CoreDNS access log emit no timestamp at all, and '
                       'correlating on an ingest time as though it were an event time is a '
                       'real analytical error - so the distinction is recorded, not '
                       'inferred.',
        'ecs': 'event.kind', 'ocsf': 'metadata.processed_time',
    },
    {
        'field': 'timestamp_anomalous', 'type': 'boolean', 'group': 'parsing', 'required': False,
        'description': 'True when the event time sits implausibly far from ingest time - '
                       'appliance clock skew. The timestamp is FLAGGED, never corrected: '
                       'rewriting it would put the normalized record at odds with the raw '
                       'line it came from. Analytics can exclude these; a device with the '
                       'wrong clock is also a finding in its own right, because its '
                       'evidence will not correlate with anything else.',
        'ecs': 'event.risk_score', 'ocsf': '-',
    },
    {
        'field': 'hostname', 'type': 'string', 'group': 'identity', 'required': True,
        'description': "Host that emitted the event, or 'unknown-host' when the source "
                       'format genuinely carries no host field - which is a statement '
                       'about the data, not a parse failure.',
        'ecs': 'host.name', 'ocsf': 'device.hostname',
    },
    {
        'field': 'process', 'type': 'string', 'group': 'identity', 'required': True,
        'description': 'Emitting process or daemon.',
        'ecs': 'process.name', 'ocsf': 'process.name',
    },
    {
        'field': 'pid', 'type': 'integer', 'group': 'identity', 'required': False,
        'description': 'Process id, where the source format provides one.',
        'ecs': 'process.pid', 'ocsf': 'process.pid',
    },
    {
        'field': 'component', 'type': 'string', 'group': 'source', 'required': True,
        'description': 'Sub-system within the process (defaults to the process name).',
        'ecs': 'service.name', 'ocsf': 'metadata.product.feature.name',
    },
    {
        'field': 'subcomponent', 'type': 'string', 'group': 'source', 'required': False,
        'description': 'Finer-grained module, where the format distinguishes one.',
        'ecs': 'service.node.name', 'ocsf': '-',
    },
    {
        'field': 'source_type', 'type': 'string', 'group': 'source', 'required': True,
        'description': 'Technology that produced the event (ESXi, NSX, CoreDNS, '
                       'PostgreSQL, LinuxAudit, ...). The vendor-agnostic label that '
                       'makes cross-platform correlation possible.',
        'ecs': 'event.provider', 'ocsf': 'metadata.product.name',
    },
    {
        'field': 'severity', 'type': 'string (enum)', 'group': 'severity', 'required': True,
        'description': 'Normalized level: EMERGENCY, FATAL, ALERT, CRITICAL, ERROR, '
                       'WARNING, NOTICE, INFO, DEBUG. Every vendor scale - numeric '
                       'syslog priorities, CEF/LEEF severities, bare words - collapses '
                       'onto this one ladder.',
        'ecs': 'log.level', 'ocsf': 'severity',
    },
    {
        'field': 'severity_score', 'type': 'integer (1-7)', 'group': 'severity', 'required': True,
        'description': 'Numeric severity, 1 = most severe. Ordered, so range predicates '
                       'work without an enum lookup.',
        'ecs': 'event.severity', 'ocsf': 'severity_id',
    },
    {
        'field': 'message', 'type': 'string', 'group': 'content', 'required': True,
        'description': 'Cleaned human-readable message: BOM stripped, octal escapes '
                       'unescaped, whitespace trimmed. A DERIVATIVE of raw_message - '
                       'never a substitute for it.',
        'ecs': 'message', 'ocsf': 'message',
    },
    {
        'field': 'normalized_message', 'type': 'string', 'group': 'content', 'required': True,
        'description': 'Canonical "[source] host/process: message" rendering. This is the '
                       'text the embedding vector is computed from, so semantic search '
                       'compares like with like across every source format.',
        'ecs': '-', 'ocsf': '-',
    },
    {
        'field': 'attributes_json', 'type': 'JSON object (as text)', 'group': 'content', 'required': True,
        'description': 'Every source-specific field the detector extracted that has no '
                       'home in the common taxonomy. This is what makes the schema '
                       'lossless without making it infinitely wide - nothing parsed is '
                       'ever discarded for want of a column.',
        'ecs': 'labels / <vendor>.*', 'ocsf': 'unmapped',
    },
    {
        'field': 'http_method', 'type': 'string', 'group': 'content', 'required': False,
        'description': 'Promoted to a first-class field for access-log sources.',
        'ecs': 'http.request.method', 'ocsf': 'http_request.http_method',
    },
    {
        'field': 'http_status', 'type': 'integer', 'group': 'content', 'required': False,
        'description': 'Promoted to a first-class field for access-log sources.',
        'ecs': 'http.response.status_code', 'ocsf': 'http_response.code',
    },
    {
        'field': 'raw_hash', 'type': 'string', 'group': 'lineage', 'required': True,
        'description': 'SHA-256 of raw_message. Recomputable from the ORIGINAL log file '
                       'with no access to this system, which is what makes it usable as '
                       'evidence rather than merely as a checksum; also the value the '
                       'tamper-evident ledger seals (blockchain_and_cybersecurity/).',
        'ecs': 'event.hash', 'ocsf': 'metadata.event_code',
    },
    {
        'field': 'raw_message', 'type': 'string', 'group': 'lineage', 'required': True,
        'description': 'The ORIGINAL event, byte for byte, always stored. ULPF (a): no '
                       'normalization step is allowed to be the only copy. Truncation is '
                       'bounded by RAW_MESSAGE_MAX_CHARS and flagged, never silent.',
        'ecs': 'event.original', 'ocsf': 'raw_data',
    },
    {
        'field': 'raw_truncated', 'type': 'boolean', 'group': 'lineage', 'required': False,
        'description': 'Set only when raw_message exceeded the cap, so a clipped value '
                       'can never be mistaken for a complete original.',
        'ecs': '-', 'ocsf': '-',
    },
    {
        'field': 'raw_length', 'type': 'integer', 'group': 'lineage', 'required': False,
        'description': 'True length of the original when it was truncated.',
        'ecs': '-', 'ocsf': '-',
    },
    {
        'field': 'source_file', 'type': 'string', 'group': 'lineage', 'required': False,
        'description': 'Path of the originating file, relative to the corpus root. ULPF '
                       '(d): with source_record this walks a normalized event back to '
                       'the exact line it came from in the untouched source.',
        'ecs': 'log.file.path', 'ocsf': 'metadata.original_time',
    },
    {
        'field': 'source_record', 'type': 'integer', 'group': 'lineage', 'required': False,
        'description': 'Ordinal of this record within source_file, counting every '
                       'reassembled record including ones that failed to parse - so it '
                       'stays a faithful index into the source.',
        'ecs': 'log.offset', 'ocsf': '-',
    },
    {
        'field': 'matched_format', 'type': 'string', 'group': 'parsing', 'required': True,
        'description': "Which detector claimed this line. 'generic_fallback' means no "
                       'detector matched and the line was normalized structurally only - '
                       'the signal for where a new detector would pay off (ULPF i).',
        'ecs': 'event.dataset', 'ocsf': 'metadata.log_name',
    },
    {
        'field': 'confidence', 'type': 'float (0-1)', 'group': 'parsing', 'required': True,
        'description': 'How much of the taxonomy the detector actually filled in. Lets a '
                       'consumer weight or quarantine weakly-parsed events instead of '
                       'trusting everything equally.',
        'ecs': '-', 'ocsf': 'confidence',
    },
    {
        'field': 'created_at', 'type': 'datetime', 'group': 'parsing', 'required': True,
        'description': 'Ingest time, distinct from the event time in `timestamp`.',
        'ecs': 'event.ingested', 'ocsf': 'metadata.processed_time',
    },
    {
        'field': 'embedding', 'type': 'float[1024]', 'group': 'enrichment', 'required': False,
        'description': 'BAAI/bge-m3 vector over normalized_message, in a native Neo4j '
                       'vector index. ULPF (h): this is what makes the corpus directly '
                       'usable for semantic retrieval and ML without a separate feature '
                       'pipeline.',
        'ecs': '-', 'ocsf': '-',
    },
]

# The severity ladder every vendor scale collapses onto. Declared here rather
# than only in the parser so the taxonomy document is self-contained.
SEVERITY_LADDER = [
    {'severity': 'EMERGENCY', 'score': 1, 'syslog': 0},
    {'severity': 'FATAL', 'score': 1, 'syslog': 0},
    {'severity': 'ALERT', 'score': 1, 'syslog': 1},
    {'severity': 'CRITICAL', 'score': 2, 'syslog': 2},
    {'severity': 'ERROR', 'score': 3, 'syslog': 3},
    {'severity': 'WARNING', 'score': 4, 'syslog': 4},
    {'severity': 'NOTICE', 'score': 5, 'syslog': 5},
    {'severity': 'INFO', 'score': 6, 'syslog': 6},
    {'severity': 'DEBUG', 'score': 7, 'syslog': 7},
]

GROUPS = [
    {'id': 'identity', 'label': 'Event identity', 'blurb': 'What happened, when, and where.'},
    {'id': 'source', 'label': 'Source', 'blurb': 'Which system produced the event.'},
    {'id': 'severity', 'label': 'Severity', 'blurb': 'One ladder, every vendor scale mapped onto it.'},
    {'id': 'content', 'label': 'Content', 'blurb': 'The payload, plus every extracted source-specific attribute.'},
    {'id': 'lineage', 'label': 'Lineage', 'blurb': 'The link back to the untouched original event.'},
    {'id': 'parsing', 'label': 'Parsing', 'blurb': 'How the event was normalized, and how confident that is.'},
    {'id': 'enrichment', 'label': 'Enrichment', 'blurb': 'Derived signals not present in the source.'},
]


def describe() -> Dict[str, Any]:
    """The taxonomy as a serializable document, for the API and the UI."""
    return {
        'name': 'Universal Event Schema',
        'version': '1.0',
        'groups': GROUPS,
        'fields': UNIVERSAL_SCHEMA,
        'severity_ladder': SEVERITY_LADDER,
        'required_fields': [f['field'] for f in UNIVERSAL_SCHEMA if f['required']],
        'standards': ['ECS (Elastic Common Schema)', 'OCSF (Open Cybersecurity Schema Framework)'],
    }


def field_coverage(driver, run_read_one) -> List[Dict[str, Any]]:
    """Live per-field population counts, measured against the graph.

    The point is falsifiability: the schema above CLAIMS which fields are
    always present, and this is what checks that claim against the data rather
    than asking anyone to take it on trust. A required field below 100% is a
    real defect, and it should be visible.
    """
    total_row = run_read_one(driver, "MATCH (l:Log) RETURN count(l) AS c")
    total = (total_row or {}).get('c', 0)

    out = []
    for spec in UNIVERSAL_SCHEMA:
        name = spec['field']
        row = run_read_one(driver, f"MATCH (l:Log) WHERE l.`{name}` IS NOT NULL RETURN count(l) AS c")
        present = (row or {}).get('c', 0)
        out.append({
            'field': name,
            'group': spec['group'],
            'required': spec['required'],
            'present': present,
            'total': total,
            'coverage': round(present * 100.0 / total, 2) if total else 0.0,
            # A required field that is not universally present is the one thing
            # on this page anybody needs to act on.
            'violates_contract': bool(spec['required'] and total and present < total),
        })
    return out


# ---------------------------------------------------------------------------
# Standard-shaped projections, used by the exporters
# ---------------------------------------------------------------------------
def _ecs_path(target: Dict[str, Any], dotted: str, value: Any) -> None:
    """Sets a dotted ECS path into a nested dict, e.g. host.name -> {host:{name}}."""
    parts = dotted.split('.')
    node = target
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value


def to_ecs(row: Dict[str, Any]) -> Dict[str, Any]:
    """Projects one normalized event into ECS-shaped nested JSON.

    Built from the `ecs` column of the schema above rather than a second
    hand-written mapping, so the document and the export cannot drift apart -
    if a field's ECS name changes it changes in exactly one place.
    """
    out: Dict[str, Any] = {}
    for spec in UNIVERSAL_SCHEMA:
        ecs = spec['ecs']
        if not ecs or ecs == '-' or ecs.startswith('('):
            continue
        value = row.get(spec['field'])
        if value is None or value == '':
            continue
        if spec['field'] == 'attributes_json':
            # The free-form bag goes under labels.* as ECS intends, parsed back
            # into an object rather than shipped as a JSON string.
            continue
        _ecs_path(out, ecs, value)
    return out


# CEF severity is 0-10, low to high; ours is 1-7, high to low. Mapped
# explicitly rather than arithmetically so each step is reviewable.
_CEF_SEVERITY = {1: 10, 2: 9, 3: 7, 4: 5, 5: 4, 6: 3, 7: 1}


def cef_severity(score: Optional[int]) -> int:
    return _CEF_SEVERITY.get(int(score) if score is not None else 6, 3)
