"""Detector for the Voltrix Edge appliance, completed from a synthesized draft.

The draft came from `python -m batch.synth_parser --file novel_source.log
--name voltrix_edge --only-unmatched` in 662ms. What the machine got right was
the SHAPE: the pipe layout, the field boundaries, which segments are ints and
which are words, and that two of them are IPv4. What it could not know is what
any of them MEAN - it emitted hostname=None with a TODO and named the fields
number..number9 and field..field7.

Everything below this line is the human half: assigning meaning.
"""

import re
from typing import Any, Dict, Optional

from .base import first_line, normalize_severity

# Draft regex, with the six separate date/time ints regrouped into one
# timestamp capture and every field given the name the vendor's own docs use.
_RE_VOLTRIX_EDGE = re.compile(
    r'^(?P<y>\d{4})\|(?P<mo>\d{2})\|(?P<d>\d{2})\|'
    r'(?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})\|'
    r'(?P<host>[A-Za-z][\w.\-]*)\|(?P<subsystem>[A-Za-z_][\w.\-]*)\|'
    r'(?P<event_id>\d+)\|(?P<action>[A-Za-z_][\w.\-]*)\|'
    r'sess=(?P<session>[\w\-]+)\|usr=(?P<user>[\w.\-]+)\|'
    r'src=(?P<src_ip>[\d.\-]+)\|dst=(?P<dst_ip>[\d.\-]+)\|'
    r'proto=(?P<proto>[\w\-]+)\|pol=(?P<policy>[\w.\-]+)\|'
    r'bytes=(?P<bytes>\d+)\|verdict=(?P<verdict>[\w.\-]+)$'
)

# The appliance's own action vocabulary, mapped onto the common severity scale.
_ACTION_SEVERITY = {
    'DENY': 'WARNING', 'FAIL': 'ERROR', 'ALLOW': 'INFO',
    'WARN': 'WARNING', 'CRIT': 'CRITICAL',
}


def detect_voltrix_edge(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_VOLTRIX_EDGE.match(first_line(record))
    if not m:
        return None
    g = m.groupdict()
    attrs = {k: v for k, v in (
        ('event_id', g['event_id']), ('session', g['session']),
        ('user', g['user']), ('src_ip', g['src_ip']), ('dst_ip', g['dst_ip']),
        ('proto', g['proto']), ('policy', g['policy']),
        ('bytes', g['bytes']), ('verdict', g['verdict']),
        ('action', g['action']),
    ) if v not in (None, '', '-')}
    return {
        'timestamp': '%s-%s-%sT%s:%s:%s+00:00' % (g['y'], g['mo'], g['d'],
                                                  g['h'], g['mi'], g['s']),
        'hostname': g['host'],
        'source_type': 'VoltrixEdge',
        'process': 'voltrix-edge',
        'component': g['subsystem'],
        'severity': (_ACTION_SEVERITY.get(g['action'])
                     or normalize_severity(g['action']) or 'INFO'),
        'message': '%s %s %s' % (g['subsystem'], g['action'], g['verdict']),
        'attributes': attrs,
    }
