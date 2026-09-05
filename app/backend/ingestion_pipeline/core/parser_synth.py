"""
core/parser_synth.py - Writes a detector for a format nobody has seen before.

This is requirement (i), "reduced parser development effort", taken literally:
instead of a human reading sample lines and writing a regex, the framework
reads them and writes one.

**How it works.** Given lines that share a shape, tokenize each into a sequence
of typed slots - timestamp, ipv4, number, quoted string, uuid, level, word,
punctuation - and align the sequences. Slots that hold the same literal on
every line are structure; slots that vary are fields. The regex is then the
literals joined by capture groups for the variable slots, and each group is
named by what it looks like and where it sits.

**Why alignment rather than an LLM.** The inference is deterministic, runs in
milliseconds, needs no model on an air-gapped VM, and - most importantly - the
output is a plain regex a human can read, correct and commit. A generated
parser nobody can audit is worse than no generated parser. An LLM can still
improve the field NAMES afterwards (see `name_fields`), which is the part it is
actually good at; it never invents the pattern.

**What it does not do.** It cannot invent semantics. It will tell you slot 4 is
an IP address that varies; whether that is a source or a destination is a
judgement it leaves to the person reading the draft. The output is a *draft
detector*, printed for review, not something silently registered at runtime -
a parser that appears by itself is a parser nobody knows the behaviour of.
"""

import re
from collections import Counter

from .parser_semantics import (CORE_FIELDS, draft_confidence,
                               infer_field)
from typing import Any, Dict, List, Optional, Tuple

# Ordered most specific first: a timestamp must win over the numbers inside it.
TOKEN_TYPES: List[Tuple[str, str]] = [
    ('iso_ts', r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?'),
    ('bsd_ts', r'[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}'),
    # A date split by a separator the tokenizer would otherwise treat as
    # punctuation: 2026|09|04|10:14:02, 2026/09/04 10:14:02. Must come
    # before `int`, or the year is consumed as a bare number and the
    # whole timestamp disappears into six meaningless integers.
    ('composite_ts',
     r'\d{4}[-/|.]\d{2}[-/|.]\d{2}[T |]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?'),
    ('epoch_ms', r'\b\d{13}\b'),
    ('uuid', r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'),
    ('ipv4_port', r'\d{1,3}(?:\.\d{1,3}){3}:\d{1,5}'),
    ('ipv4', r'\d{1,3}(?:\.\d{1,3}){3}'),
    ('mac', r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}'),
    ('priority', r'<\d{1,3}>'),
    ('quoted', r'"[^"]*"'),
    ('bracketed', r'\[[^\]]*\]'),
    ('level', r'\b(?:EMERG|EMERGENCY|ALERT|CRIT|CRITICAL|FATAL|ERROR|ERR|WARNING|WARN|'
               r'NOTICE|INFO|DEBUG|TRACE)\b'),
    ('hexnum', r'\b0x[0-9a-fA-F]+\b'),
    ('float', r'\b\d+\.\d+\b'),
    ('int', r'\b\d+\b'),
    ('word', r'[A-Za-z_][\w.\-]*'),
    ('space', r'\s+'),
    ('punct', r'[^\w\s]'),
]

_MASTER = re.compile('|'.join(f'(?P<{name}>{pat})' for name, pat in TOKEN_TYPES))

# Token types that are always a field rather than structure - a timestamp is
# never boilerplate even if every sample happens to share one.
# 'bracketed' and 'level' belong here for the same reason as the rest: a pid in
# [2814] and a severity word are fields even when a two-line sample happens to
# share them. Omitting 'bracketed' hard-coded the pid into the pattern, so the
# draft matched one process and nothing else.
ALWAYS_FIELD = {'iso_ts', 'bsd_ts', 'epoch_ms', 'uuid', 'ipv4', 'ipv4_port',
                'mac', 'quoted', 'bracketed', 'level', 'hexnum', 'float',
                'int', 'priority'}

# A word that carries a digit or a hyphen is almost always an instance
# identifier - a hostname, a node name, a backend server. Treated as a field
# even when every sample line shares it.
#
# This is the difference between a usable draft and an unusable one. Trained on
# two lines from the same host, the first version baked `lb-edge-01` and
# `haproxy[2814]` into the pattern as literals, producing a detector that
# matched exactly one machine and silently ignored the rest of the estate. A
# constant in a small sample is not evidence of a constant in the format.
_IDENTIFIER_LIKE = re.compile(r'^(?=.*[\d\-])[\w.\-]{2,}$')


def _looks_like_identifier(value: str) -> bool:
    return bool(_IDENTIFIER_LIKE.match(value)) and not value.isdigit()

# What each token type maps to in the Universal Event Schema, when it can be
# inferred from shape alone.
SCHEMA_HINT = {
    'iso_ts': 'timestamp', 'bsd_ts': 'timestamp', 'epoch_ms': 'timestamp',
    'level': 'severity', 'ipv4': 'attributes.ip', 'ipv4_port': 'attributes.endpoint',
    'mac': 'attributes.mac', 'uuid': 'attributes.uuid', 'priority': 'severity',
}


def tokenize(line: str) -> List[Tuple[str, str]]:
    """Line -> [(token_type, literal), ...]."""
    out = []
    for m in _MASTER.finditer(line):
        kind = m.lastgroup
        out.append((kind, m.group()))
    return out


def _shape(tokens: List[Tuple[str, str]]) -> Tuple[str, ...]:
    return tuple(k for k, _ in tokens)


def group_by_shape(lines: List[str]) -> List[Tuple[Tuple[str, ...], List[str]]]:
    """Buckets lines by token shape, largest bucket first.

    A log file is usually several formats interleaved, so synthesising one
    pattern for the whole file produces a pattern that matches none of it. Each
    shape gets its own draft.
    """
    buckets: Dict[Tuple[str, ...], List[str]] = {}
    for line in lines:
        line = line.rstrip('\n')
        if not line.strip():
            continue
        buckets.setdefault(_shape(tokenize(line)), []).append(line)
    return sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True)



def _rename_group(fragment: str, new_name: str) -> str:
    """Renames the named group in an emitted regex fragment.

    The synthesizer names captures after their token type (`ip2`, `field7`)
    because that is all shape analysis knows. Once the semantics pass has
    identified the field, the group is renamed so the regex documents itself -
    `(?P<src_ip>...)` needs no accompanying prose, and the generated detector
    can assign it to the schema directly.
    """
    return re.sub(r'\(\?P<[^>]+>', '(?P<%s>' % new_name, fragment, count=1)


def _field_name(kind: str, index: int, used: Counter) -> str:
    base = {
        'iso_ts': 'timestamp', 'bsd_ts': 'timestamp', 'epoch_ms': 'timestamp',
        'ipv4': 'ip', 'ipv4_port': 'endpoint', 'mac': 'mac', 'uuid': 'uuid',
        'level': 'severity', 'priority': 'priority', 'quoted': 'text',
        'bracketed': 'context', 'hexnum': 'hex', 'float': 'value',
        'int': 'number', 'word': 'field',
    }.get(kind, 'field')
    used[base] += 1
    return base if used[base] == 1 else f'{base}{used[base]}'


def synthesize(lines: List[str], min_support: int = 2) -> Optional[Dict[str, Any]]:
    """Infers a pattern from lines that share a shape.

    `min_support` guards against generalising from a single example: with one
    line every token looks constant, and the resulting regex matches exactly
    that line and nothing else.
    """
    groups = group_by_shape(lines)
    if not groups:
        return None
    shape, members = groups[0]
    if len(members) < min_support:
        return None

    columns = [tokenize(m) for m in members]
    width = len(shape)
    parts: List[str] = []
    fields: List[Dict[str, Any]] = []
    warnings: List[str] = []
    used: Counter = Counter()
    # A schema field can only be claimed once - two slots both inferred as
    # `hostname` would produce a duplicate regex group name, which is a
    # compile error rather than a subtle bug, but the second one is also
    # probably wrong.
    used_semantic: set = set()

    for i in range(width):
        kind = shape[i]
        values = {col[i][1] for col in columns}
        only = next(iter(values))
        constant = len(values) == 1 and kind not in ALWAYS_FIELD

        # A word that looks like an identifier is a field even if this sample
        # never varies it - see _IDENTIFIER_LIKE.
        if constant and kind == 'word' and _looks_like_identifier(only):
            constant = False
            warnings.append(
                f"slot {i} ({only!r}) was constant across the sample but looks like an "
                f"identifier, so it was captured rather than hard-coded")

        if constant:
            parts.append(re.escape(only))
            continue

        if kind == 'space':
            parts.append(r'\s+')
            continue

        name = _field_name(kind, i, used)
        pattern = dict(TOKEN_TYPES)[kind]
        if kind == 'quoted':
            parts.append(f'"(?P<{name}>[^"]*)"')
        elif kind == 'bracketed':
            parts.append(rf'\[(?P<{name}>[^\]]*)\]')
        else:
            parts.append(f'(?P<{name}>{pattern})')

        # columns[0][i][1] is what this slot holds in the line being
        # searched. Passing sorted(values)[0] instead meant the key
        # lookup searched for a value from a different line and missed
        # every key= pair.
        inference = infer_field(members[0], shape, i, kind,
                                sorted(values), columns[0][i][1])
        # The inferred meaning renames the capture: `src_ip` in a regex is
        # self-documenting where `ip2` needs the draft's prose to decode.
        claimed = inference.get('field') or inference.get('attribute_name')
        if claimed and claimed.isidentifier() and claimed not in used_semantic:
            name = claimed
            used_semantic.add(name)
            parts[-1] = _rename_group(parts[-1], name)
        fields.append({
            'name': name,
            'token': kind,
            'schema_hint': SCHEMA_HINT.get(kind),
            'examples': sorted(values)[:3],
            'distinct_in_sample': len(values),
            'inferred_field': inference.get('field'),
            'evidence': inference.get('evidence'),
            'confidence': inference.get('confidence'),
        })

    regex = '^' + ''.join(parts) + '$'
    try:
        compiled = re.compile(regex)
    except re.error as e:
        return None

    matched = sum(1 for m in members if compiled.match(m))
    total_matched = sum(1 for line in lines if line.strip() and compiled.match(line.rstrip('\n')))

    return {
        'regex': regex,
        'fields': fields,
        'confidence': draft_confidence(
            [{'field': f.get('inferred_field')} for f in fields]),
        'shape': list(shape),
        'sample_lines': members[:3],
        'support': len(members),
        'lines_seen': len([x for x in lines if x.strip()]),
        # Coverage of the WHOLE input, not just the bucket - a pattern covering
        # 12% of a file is a real result and the number people need to see.
        'coverage': round(total_matched / max(1, len([x for x in lines if x.strip()])) * 100, 1),
        'self_match': matched == len(members),
        # Surfaced, not hidden: these are the decisions most likely to be wrong,
        # and the reviewer is the one who can settle them.
        'warnings': warnings,
        'over_fit_risk': len(members) < 4,
        'other_shapes': [{'count': len(v), 'example': v[0][:110]} for _, v in groups[1:4]],
    }


def to_detector_source(draft: Dict[str, Any], name: str = 'generated') -> str:
    """Renders the draft as a detector module that runs as written.

    Every field carries the evidence that named it, as a comment. A generated
    parser is still a parser whose behaviour someone has to be able to
    explain, and this project stores provenance on every event precisely so
    that nothing in the pipeline is unexplainable - the parser included.
    """
    ident = re.sub(r'\W+', '_', name).strip('_').lower() or 'generated'
    fields = draft['fields']
    by_field = {}
    for f in fields:
        target = f.get('inferred_field')
        if target and target not in by_field:
            by_field[target] = f

    def group(target):
        f = by_field.get(target)
        return f['name'] if f else None

    lines = []

    ts = group('timestamp')
    if ts:
        lines.append("        # %s" % by_field['timestamp'].get('evidence'))
        lines.append("        'timestamp': normalize_timestamp(m.group('%s'))," % ts)

    host = group('hostname')
    if host:
        lines.append("        # %s" % by_field['hostname'].get('evidence'))
        lines.append("        'hostname': m.group('%s')," % host)
    else:
        lines.append("        # No slot carried positional, key or type evidence of a")
        lines.append("        # hostname. Left unset rather than guessed - a wrong host")
        lines.append("        # is worse than none, because it correlates.")
        lines.append("        'hostname': None,")

    proc = group('process')
    if proc:
        lines.append("        # %s" % by_field['process'].get('evidence'))
        lines.append("        'process': m.group('%s')," % proc)
    else:
        lines.append("        'process': '%s'," % ident)

    sev = group('severity')
    if sev:
        lines.append("        # %s" % by_field['severity'].get('evidence'))
        lines.append("        'severity': normalize_severity(m.group('%s'))"
                     " or guess_severity_from_text(record)," % sev)
    else:
        lines.append("        # No severity token in this format; fall back to")
        lines.append("        # keyword evidence in the line itself.")
        lines.append("        'severity': guess_severity_from_text(record),")

    comp = group('component')
    if comp:
        lines.append("        'component': m.group('%s')," % comp)
    else:
        lines.append("        'component': '%s'," % ident)

    msg = group('message')
    if msg:
        lines.append("        'message': m.group('%s')," % msg)
    else:
        lines.append("        # No dedicated message field; the line is the event.")
        lines.append("        'message': first_line(record),")

    lines.append("        'source_type': '%s'," % name)

    # Everything else becomes an attribute under the name the FORMAT used
    # where it gave one, so the bag reads like the vendor's own documentation.
    consumed = {v['name'] for v in by_field.values()
                if v.get('inferred_field') in
                ('timestamp', 'hostname', 'process', 'severity', 'component',
                 'message')}
    attrs = [f['name'] for f in fields if f['name'] not in consumed]
    if attrs:
        pairs = ', '.join("'%s': m.group('%s')" % (a, a) for a in attrs)
        lines.append("        'attributes': {k: v for k, v in {%s}.items()" % pairs)
        lines.append("                       if v not in (None, '', '-')},")

    lines.append("        'matched_format': '%s'," % ident)
    body = '\n'.join(lines)

    evidence_table = '\n'.join(
        '    %-14s %-14s %s' % (f['name'], f['token'],
                                f.get('evidence') or 'no evidence')
        for f in fields)

    confidence = draft.get('confidence', 0.0)
    verdict = ('This draft resolved the core schema fields and is ready to '
               'register.' if confidence >= 0.6 else
               'LOW CONFIDENCE - read this before registering it. The shape is '
               'inferred correctly; the meanings below are not all resolved.')

    return f'''"""Detector for `{name}`, synthesized from {draft['support']} sample line(s).

Covers {draft['coverage']}% of the sample input. Inference confidence: {confidence}.
{verdict}

Field evidence - how each capture was named:
{evidence_table}

Sample line:
    {draft['sample_lines'][0][:140] if draft['sample_lines'] else ''}
"""

import re
from typing import Any, Dict, Optional

from .base import first_line, guess_severity_from_text, normalize_severity, normalize_timestamp

_RE_{ident.upper()} = re.compile(
    r"""{draft['regex']}"""
)


def detect_{ident}(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_{ident.upper()}.match(first_line(record))
    if not m:
        return None
    return {{
{body}
    }}
'''
