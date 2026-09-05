"""
parsers/base.py - Shared helpers used by every format detector.

Nothing platform-specific lives here: timestamp normalization, loose
key=value bag parsing, severity vocabulary, and the small string-cleanup
helpers (BOM stripping, syslog octal-escape decoding) that more than one
detector needs.
"""

import re
from datetime import datetime, timezone
from typing import Dict, Optional

ISO_TS = r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?'

_KV_PATTERN = re.compile(r"""(?P<key>[\w][\w\-.]*)=(?:'(?P<sq>[^']*)'|"(?P<dq>[^"]*)"|(?P<bare>[^\s,'"\]]+))""")
_OCTAL_ESCAPE = re.compile(r'#0([0-3][0-7])')

SEVERITY_ALIASES = {
    'EMERGENCY': 'EMERGENCY', 'FATAL': 'FATAL', 'ALERT': 'ALERT',
    'CRITICAL': 'CRITICAL', 'CRIT': 'CRITICAL', 'MAJOR': 'CRITICAL',
    'ERROR': 'ERROR', 'ERR': 'ERROR', 'E': 'ERROR',
    'WARNING': 'WARNING', 'WARN': 'WARNING', 'W': 'WARNING',
    'NOTICE': 'NOTICE',
    'INFO': 'INFO', 'INFORMATION': 'INFO', 'I': 'INFO', 'NORMAL': 'INFO', 'SUCCESS': 'INFO',
    'DEBUG': 'DEBUG', 'TRACE': 'DEBUG', 'F': 'FATAL',
}

SEVERITY_SCORE = {
    'EMERGENCY': 1, 'FATAL': 1, 'ALERT': 1,
    'CRITICAL': 2,
    'ERROR': 3,
    'WARNING': 4,
    'NOTICE': 5,
    'INFO': 6,
    'DEBUG': 7,
}

_SEVERITY_HINT_RE = re.compile(
    r'\b(FATAL|CRITICAL|ERROR|FAILED|FAILURE|FAULT|EXCEPTION|TIMED OUT|TIMEOUT|WARNING|WARN)\b',
    re.IGNORECASE,
)

_TS_FORMATS = [
    '%Y-%m-%dT%H:%M:%S.%fZ',
    '%Y-%m-%dT%H:%M:%SZ',
    '%Y-%m-%dT%H:%M:%S.%f%z',
    '%Y-%m-%dT%H:%M:%S%z',
    '%Y-%m-%d %H:%M:%S.%f',
    '%Y-%m-%d %H:%M:%S',
    # T separator with no timezone. Absent until now, which meant the
    # composite normalisation above produced a string nothing could
    # parse - the conversion worked and the result was still discarded.
    '%Y-%m-%dT%H:%M:%S',
    '%Y-%m-%dT%H:%M:%S.%f',
    '%d/%b/%Y:%H:%M:%S %z',
    # CEF `rt=` / LEEF `devTime=`, per the ArcSight CEF Implementation
    # Standard. Their absence was a real defect rather than an omission:
    # normalize_timestamp() returns the input untouched when nothing matches,
    # so every CEF event kept a non-ISO timestamp, `day` resolved to null and
    # the record silently left every date-range query and trend chart. CEF is
    # what most security appliances export, so this was the worst format to
    # get wrong.
    '%b %d %Y %H:%M:%S',            # Sep 03 2026 09:23:22
    '%b %d %Y %H:%M:%S.%f',         # Sep 03 2026 09:23:22.123
    '%b %d %Y %H:%M:%S %z',
    '%b %d %Y %H:%M:%S.%f %z',
    # Vendor date-first variants seen in appliance exports.
    '%d/%m/%Y %H:%M:%S',
    '%Y/%m/%d %H:%M:%S',
    '%m/%d/%Y %H:%M:%S',
    '%m/%d/%Y %I:%M:%S %p',
]


def normalize_severity(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return SEVERITY_ALIASES.get(raw.strip().upper())


def guess_severity_from_text(text: str) -> Optional[str]:
    """Best-effort severity when a format has no explicit level field."""
    m = _SEVERITY_HINT_RE.search(text)
    if not m:
        return None
    token = m.group(1).upper()
    if token in ('FAILED', 'FAILURE', 'FAULT', 'EXCEPTION', 'ERROR'):
        return 'ERROR'
    if token in ('TIMED OUT', 'TIMEOUT'):
        return 'WARNING'
    return SEVERITY_ALIASES.get(token)


def unescape_syslog_octal(text: str) -> str:
    """Some appliance forwarders flatten embedded newlines/tabs in a single
    syslog line as literal '#012'/'#011' (octal escapes for \\n / \\t) instead
    of emitting real multi-line output. Decode them back so the message reads
    the way the application actually logged it."""
    if '#0' not in text:
        return text
    return _OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), text)


def strip_bom(text: str) -> str:
    return text.replace('﻿', '').replace('\x00', '')


def parse_kv_bag(s: str) -> Dict[str, str]:
    """Parses a loose 'key=value key="value" key='value'' bag into a dict -
    NSX's [nsx@6876 ...], Aria's [host='...' thread='...' ...], ESXi's
    [Originator@6876 sub=... opID=...], and Linux auditd's msg='...' body."""
    out = {}
    for m in _KV_PATTERN.finditer(s):
        quoted = m.group('sq') if m.group('sq') is not None else m.group('dq')
        if quoted is not None:
            # Explicitly delimited: whatever is inside the quotes is the value.
            out[m.group('key')] = quoted
            continue
        out[m.group('key')] = _trim_bare_value(m.group('bare'))
    return out


#  Punctuation that ends a sentence rather than a value.
_TRAILING_PUNCT = ';:,.'


def _trim_bare_value(val: str) -> str:
    """Strips prose punctuation an unquoted value ran into.

    "by (uid=33)" was yielding uid='33)': the paren closes the prose, not the
    value. That matters beyond tidiness, because the value becomes a graph
    property and an entity - '33)' and '33' are two different nodes.

    A close paren is only removed when the value contains no open paren, so a
    value carrying a balanced pair keeps it intact.
    """
    if not val:
        return val
    while val:
        last = val[-1]
        if last in _TRAILING_PUNCT:
            val = val[:-1]
        elif last == ')' and '(' not in val:
            val = val[:-1]
        elif last == ']' and '[' not in val:
            val = val[:-1]
        else:
            break
    return val


#  Appliance date separators. `2026|09|04|10:14:02` is a real format and
#  strptime has no directive for it, so the separators are normalised to
#  the ISO spelling before parsing rather than adding eight more entries
#  to _TS_FORMATS for every punctuation choice a vendor might make.
_COMPOSITE_TS = re.compile(
    r'^(\d{4})[-/|.](\d{2})[-/|.](\d{2})[T |](\d{2}):(\d{2}):(\d{2})')


def normalize_timestamp(raw_ts: str, assume_year: Optional[int] = None) -> str:
    """Best-effort normalization to an ISO-8601 UTC string. Falls back to
    returning the original string untouched if nothing matches, so a display
    value is always preserved even when it can't be machine-normalized."""
    if not raw_ts:
        return raw_ts
    ts = raw_ts.strip()
    cm = _COMPOSITE_TS.match(ts)
    if cm:
        ts = '%s-%s-%sT%s:%s:%s' % cm.groups() + ts[cm.end():]
    ts_dot = re.sub(r'(\d{2}:\d{2}:\d{2}),(\d+)', r'\1.\2', ts)
    for fmt in _TS_FORMATS:
        try:
            dt = datetime.strptime(ts_dot, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    # RFC3164 / glog: no year in the source line at all
    for fmt in ('%b %d %H:%M:%S', '%m%d %H:%M:%S.%f'):
        try:
            dt = datetime.strptime(ts_dot, fmt)
            year = assume_year or datetime.now(timezone.utc).year
            dt = dt.replace(year=year, tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue
    return raw_ts


def first_line(record: str) -> str:
    return record.split('\n', 1)[0]


def rest_of(record: str) -> str:
    parts = record.split('\n', 1)
    return parts[1] if len(parts) > 1 else ''
