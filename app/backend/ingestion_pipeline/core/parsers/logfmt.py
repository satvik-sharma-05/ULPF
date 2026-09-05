"""
parsers/logfmt.py - logfmt, the key=value line format.

    time="2026-06-12T04:06:00.759Z" level=info msg="starting signal loop" namespace=moby

Used by containerd, Docker, Grafana, Loki, Prometheus tooling and most Go
services - so on any host running containers it is one of the highest-volume
formats present, and it was the single largest real format in this corpus's
unmatched residue: the top four unmatched shapes were all logfmt, differing
only in which plugin name appeared in the message.

Found by pointing `batch/synth_parser` at the records the 53 hand-written
detectors did not claim. The synthesizer identified the shape and inferred
`timestamp` and `message` correctly, but fitted a regex to one variant at 46%
coverage - because logfmt's whole point is that the KEY SET varies per line.
That is a case where the right answer is a parser for the grammar rather than
a pattern for one shape, which is exactly the judgement the tool leaves to a
person.

Ordered near the end of the registry: `k=v` is a broad shape, so every
product-specific detector keeps first refusal on its own lines.
"""

import re
from typing import Any, Dict, Optional

from .base import (first_line, guess_severity_from_text, normalize_severity,
                   normalize_timestamp)

#  logfmt: bare or double-quoted values, backslash escapes inside quotes.
#  Anchored per-pair rather than splitting on whitespace, because a quoted
#  value legitimately contains spaces - splitting first is the usual way this
#  format gets parsed wrongly.
_PAIR = re.compile(r'([A-Za-z_][\w.\-]*)=(?:"((?:[^"\\]|\\.)*)"|(\S*))')

#  A line is logfmt only if it is essentially ALL key=value. A syslog message
#  that happens to contain one `k=v` is not logfmt, and claiming it would take
#  those lines away from the detector that should have them.
_MIN_PAIRS = 3
_MIN_COVERAGE = 0.75

#  The conventional key names. logfmt has no spec, but these three are near
#  universal across the Go ecosystem.
_TIME_KEYS = ('time', 'ts', 'timestamp', 'at')
_LEVEL_KEYS = ('level', 'lvl', 'severity')
_MSG_KEYS = ('msg', 'message', 'event')


def _unescape(value: str) -> str:
    """Undo the backslash escaping logfmt applies inside quoted values."""
    return re.sub(r'\\(.)', r'\1', value)


def detect_logfmt(record: str) -> Optional[Dict[str, Any]]:
    line = first_line(record)
    if '=' not in line:
        return None

    pairs = {}
    matched_span = 0
    for m in _PAIR.finditer(line):
        key = m.group(1)
        value = _unescape(m.group(2)) if m.group(2) is not None else (m.group(3) or '')
        pairs[key.lower()] = value
        matched_span += m.end() - m.start()

    if len(pairs) < _MIN_PAIRS:
        return None
    # The pairs must account for most of the line. Without this the detector
    # would claim any verbose syslog message containing a few `k=v` fragments.
    if matched_span / max(len(line.strip()), 1) < _MIN_COVERAGE:
        return None
    # logfmt without a message key is usually something else that happens to
    # be key=value - Fortinet, for one, which has its own detector.
    if not any(k in pairs for k in _MSG_KEYS):
        return None

    ts = next((pairs[k] for k in _TIME_KEYS if pairs.get(k)), None)
    level = next((pairs[k] for k in _LEVEL_KEYS if pairs.get(k)), None)
    message = next((pairs[k] for k in _MSG_KEYS if pairs.get(k)), '')

    #  component: the emitting subsystem, under whichever key this producer
    #  chose. containerd uses `namespace`, Grafana uses `logger`.
    component = next((pairs[k] for k in
                      ('component', 'logger', 'namespace', 'module', 'caller',
                       'subsystem') if pairs.get(k)), None)

    consumed = set(_TIME_KEYS) | set(_LEVEL_KEYS) | set(_MSG_KEYS)
    attributes = {k: v for k, v in pairs.items()
                  if k not in consumed and v not in ('', None)}

    return {
        'timestamp': normalize_timestamp(ts) if ts else None,
        'source_type': 'logfmt',
        'process': pairs.get('component') or pairs.get('namespace') or 'logfmt',
        'component': component or 'logfmt',
        'severity': (normalize_severity(level) if level
                     else guess_severity_from_text(message)),
        'message': message,
        'attributes': attributes,
    }
