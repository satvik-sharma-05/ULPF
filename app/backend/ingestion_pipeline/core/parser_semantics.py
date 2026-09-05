"""
core/parser_semantics.py - Infers what a synthesized field MEANS, not just
what it looks like.

parser_synth.py was already good at shape: it finds the token boundaries, the
types, and which slots vary. What it emitted was a draft with
`hostname: None  # TODO: which captured field is the host?` and fields called
`number2` and `field7` - correct, and useless without a person.

That TODO was the whole gap in requirement (e). Onboarding was "plug-and-play"
only up to the point where a human had to read the draft and decide what each
capture meant.

This module closes it, using the three kinds of evidence that are actually
available without understanding the vendor:

  1. POSITION - syslog puts the host in a fixed slot after the timestamp and
     the program immediately after that, whatever they are called. RFC 3164
     and 5424 are why this works across vendors nobody has taught us about.
  2. KEY NAME - a `src=`/`srcip=`/`source_ip=` pair names its own meaning. The
     vocabulary below is drawn from the key spellings our 53 existing
     detectors already handle, so it is derived from real formats rather than
     guessed.
  3. VALUE SHAPE - a token that is an IPv4 address, a severity word, or an
     ISO timestamp declares its type regardless of what it is called.

Where the three disagree, the more specific evidence wins: an explicit key
name beats a positional guess, and a positional guess beats a bare type.

**What it will not do.** It does not decide whether an IP is the source or the
destination when nothing names it - it labels both `ip` and `ip2` and says so.
Guessing direction would put wrong evidence into an investigation, which is
the one failure this project exists to prevent. `confidence` on the draft
reports how much of the pattern was resolved by evidence rather than left
generic, so a low score is a signal to read the draft rather than register it.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

#  Key spellings, grouped by the schema field they mean. Taken from the
#  vendor formats the existing detectors already parse - Fortinet, CEF, LEEF,
#  Okta, CloudTrail, Windows - rather than invented.
KEY_VOCABULARY: Dict[str, Tuple[str, ...]] = {
    'hostname': ('hostname', 'host', 'dvchost', 'devicehostname', 'shost',
                 'computername', 'computer', 'devname', 'device', 'node',
                 'agent_host', 'src_host', 'server', 'machine', 'appliance'),
    'process': ('process', 'proc', 'program', 'app', 'appname', 'application',
                'service', 'processname', 'process_name', 'daemon', 'prog'),
    'severity': ('severity', 'sev', 'level', 'loglevel', 'log_level',
                 'levelname', 'priority', 'event_severity', 'criticality'),
    'timestamp': ('timestamp', 'time', 'date', 'datetime', 'eventtime',
                  'event_time', 'devtime', 'rt', 'occurred', 'logtime'),
    'component': ('component', 'subsystem', 'module', 'category', 'channel',
                  'facility', 'subtype', 'type', 'class'),
    'message': ('message', 'msg', 'description', 'detail', 'text', 'reason',
                'event', 'body', 'summary'),
    'user': ('user', 'usr', 'username', 'user_name', 'account', 'suser',
             'duser', 'login', 'principal', 'actor', 'uid_name'),
    'src_ip': ('src', 'srcip', 'src_ip', 'source_ip', 'sourceip', 'saddr',
               'client_ip', 'clientip', 'ip_client', 'remote_ip', 'peer'),
    'dst_ip': ('dst', 'dstip', 'dst_ip', 'dest_ip', 'destip', 'daddr',
               'target_ip', 'server_ip', 'remote_addr'),
    'src_port': ('spt', 'srcport', 'src_port', 'source_port', 'sport'),
    'dst_port': ('dpt', 'dstport', 'dst_port', 'dest_port', 'dport', 'port'),
    'action': ('action', 'act', 'deviceaction', 'verdict', 'disposition',
               'outcome', 'result', 'status'),
    'protocol': ('proto', 'protocol', 'transport', 'ipproto'),
    'session': ('sess', 'session', 'sessionid', 'session_id', 'connid',
                'conn_id', 'flowid'),
    'rule': ('rule', 'policy', 'pol', 'policyid', 'ruleid', 'rule_name',
             'signature', 'sigid'),
    'bytes': ('bytes', 'size', 'len', 'length', 'sentbyte', 'rcvdbyte'),
}

#  Reverse index, built once: key spelling -> schema field.
_KEY_TO_FIELD: Dict[str, str] = {
    spelling: field
    for field, spellings in KEY_VOCABULARY.items()
    for spelling in spellings
}

#  Token types that declare their own meaning regardless of naming.
TYPE_TO_FIELD = {
    'iso_ts': 'timestamp', 'bsd_ts': 'timestamp', 'epoch_ms': 'timestamp',
    'composite_ts': 'timestamp',
    'level': 'severity', 'ipv4': 'ip', 'ipv4_port': 'endpoint', 'mac': 'mac',
    'uuid': 'uuid',
}

#  Schema fields a detector must fill for its output to be usable. Anything
#  the inference cannot resolve to one of these stays in the attribute bag,
#  which is lossless but not normalized.
CORE_FIELDS = ('timestamp', 'hostname', 'process', 'severity', 'message')

#  A hostname-shaped token: letters with a digit or dash, or a dotted name.
#  Same rule the vendor-agnostic fallback uses, for the same reason - it is
#  what separates `web-srv-07` from an English word in the message body.
_HOSTLIKE = re.compile(r'^(?=.*[\d\-.])[A-Za-z][\w.\-]{2,62}$')
_PROCLIKE = re.compile(r'^[a-z][\w.\-]{1,40}$', re.I)
_SEVERITY_WORDS = {
    'emerg', 'emergency', 'alert', 'crit', 'critical', 'fatal', 'err',
    'error', 'warn', 'warning', 'notice', 'info', 'information',
    'informational', 'debug', 'trace', 'verbose',
}


def _key_before(line: str, value: str) -> Optional[str]:
    """The `key=` immediately preceding this value, if there is one.

    This is the strongest signal available: the format is telling us what the
    field is called. Matched against the raw line rather than the token
    stream because the `=` is punctuation the tokenizer has already split off.
    """
    idx = line.find(value)
    if idx <= 0:
        return None
    prefix = line[:idx].rstrip()
    if not prefix.endswith('='):
        return None
    m = re.search(r'([A-Za-z_][\w.\-]*)=$', prefix)
    return m.group(1).lower() if m else None


def _positional_role(shape: Tuple[str, ...], index: int) -> Optional[str]:
    """Host and program by their position in a syslog-shaped line.

    RFC 3164/5424 fix the order: timestamp, host, then `program[pid]:`. That
    ordering holds for devices from vendors this code has never seen, which is
    exactly what makes it worth using on an unknown format.
    """
    # Punctuation counts as a separator, not a field. A pipe-delimited
    # appliance format puts a `punct` token between every value, and
    # skipping only whitespace shifted every position by one - the host
    # slot was being read as the program slot.
    non_space = [i for i, k in enumerate(shape)
                 if k not in ('space', 'punct')]
    if index not in non_space:
        return None
    pos = non_space.index(index)

    ts_positions = [p for p, i in enumerate(non_space)
                    if shape[i] in ('iso_ts', 'bsd_ts', 'epoch_ms',
                                    'composite_ts')]
    if not ts_positions:
        return None
    first_ts = ts_positions[0]
    if pos == first_ts + 1:
        return 'hostname'
    if pos == first_ts + 2:
        return 'process'
    return None


def infer_field(line_sample: str, shape: Tuple[str, ...], index: int,
                kind: str, values: List[str],
                value_in_line: Optional[str] = None) -> Dict[str, Any]:
    """What this slot means, and how confident we are.

    Returns the schema field, the evidence that produced it, and a per-field
    confidence. Evidence is reported so a reviewer can disagree with a
    specific inference rather than the whole draft.
    """
    # The value as it appears in THIS line - searching for a value taken
    # from a different line finds nothing, which silently disabled the
    # strongest signal available.
    example = value_in_line if value_in_line is not None else (
        values[0] if values else '')

    # 1. The format names the field itself.
    key = _key_before(line_sample, example)
    if key:
        field = _KEY_TO_FIELD.get(key)
        if field:
            return {'field': field, 'evidence': 'key name %r' % key,
                    'confidence': 0.95}
        # Not a schema field, but the format has named it. `pol` is a
        # better attribute name than `field6` by every measure that
        # matters to whoever reads the event later.
        return {'field': None, 'evidence': 'key name %r (kept as an attribute)' % key,
                'confidence': 0.6, 'attribute_name': key}

    # 2. Position in a syslog-shaped line.
    role = _positional_role(shape, index)
    if role == 'hostname' and all(_HOSTLIKE.match(v) for v in values if v):
        return {'field': 'hostname', 'evidence': 'syslog host slot (after the '
                                                 'timestamp)', 'confidence': 0.85}
    if role == 'process' and all(_PROCLIKE.match(v) for v in values if v):
        return {'field': 'process', 'evidence': 'syslog program slot',
                'confidence': 0.8}

    # 3. The value's own type.
    if kind == 'level' or all(v.lower() in _SEVERITY_WORDS for v in values if v):
        return {'field': 'severity', 'evidence': 'value is a severity word',
                'confidence': 0.9}
    typed = TYPE_TO_FIELD.get(kind)
    if typed:
        return {'field': typed, 'evidence': 'token type %s' % kind,
                'confidence': 0.75 if typed != 'ip' else 0.4}

    return {'field': None, 'evidence': 'no positional, key or type evidence',
            'confidence': 0.2}


#  Composite timestamps: a date split across several numeric slots, which the
#  shape-only synthesizer emitted as number..number6. Recognising the run is
#  what turns six meaningless integers into one usable event time.
_DATE_RUN = re.compile(
    r'(?P<y>\d{4})(?P<s1>[-/|.])(?P<mo>\d{2})(?P=s1)(?P<d>\d{2})'
    r'(?P<s2>[T |])(?P<h>\d{2}):(?P<mi>\d{2}):(?P<sec>\d{2})')


def find_composite_timestamp(line: str) -> Optional[Dict[str, Any]]:
    """A date/time spread over separator characters the tokenizer split on.

    `2026|09|04|10:14:02` is a timestamp, but only if you look at the run
    rather than at each integer. Returns a regex fragment that captures it as
    one field plus the code to assemble it.
    """
    m = _DATE_RUN.search(line)
    if not m:
        return None
    s1, s2 = re.escape(m.group('s1')), re.escape(m.group('s2'))
    return {
        'matched': m.group(0),
        'start': m.start(),
        'end': m.end(),
        'pattern': (r'(?P<y>\d{4})' + s1 + r'(?P<mo>\d{2})' + s1 +
                    r'(?P<d>\d{2})' + s2 +
                    r'(?P<h>\d{2}):(?P<mi>\d{2}):(?P<sec>\d{2})'),
        'assemble': ("'%s-%s-%sT%s:%s:%s+00:00' % (g['y'], g['mo'], g['d'], "
                     "g['h'], g['mi'], g['sec'])"),
    }


def draft_confidence(inferences: List[Dict[str, Any]]) -> float:
    """How much of the pattern was resolved by evidence.

    Reported on the draft so a low score tells a reviewer to read it rather
    than register it. Weighted by whether the CORE fields were resolved,
    because a draft that names ten attributes but no hostname has not actually
    onboarded the source.
    """
    if not inferences:
        return 0.0
    resolved = [i for i in inferences if i.get('field')]
    core_hit = len({i['field'] for i in resolved} & set(CORE_FIELDS))
    field_score = len(resolved) / len(inferences)
    core_score = core_hit / len(CORE_FIELDS)
    return round(0.4 * field_score + 0.6 * core_score, 2)
