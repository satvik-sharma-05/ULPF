"""
parsers/vrops_casa_horizon.py - vRealize Operations bridge/analytics logs
and the CASA appliance web layer shared by vROps/Horizon admin UIs
(ajp-nio thread logs, the custom "In/Out [reqid] ..." request tracer, and
the bracket-first combined access log CASA's embedded Tomcat emits).
"""

import re
from typing import Any, Dict, Optional

from .base import ISO_TS, first_line, normalize_severity, normalize_timestamp, parse_kv_bag

_RE_VROPS_BRIDGE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[,.]\d+)(?P<off>[+-]\d{4})\s+'
    r'(?P<level>\w+)\s*\[(?P<thread>[^\]]*)\]\s+(?P<logger>[\w.$]+)\s*-\s*(?P<msg>.*)$'
)


def detect_vrops_bridge(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_VROPS_BRIDGE.match(first_line(record))
    if not m:
        return None
    return {
        'timestamp': normalize_timestamp(m.group('ts') + m.group('off')),
        'source_type': 'vROps',
        'process': 'vrops-bridge',
        'component': m.group('logger'),
        'severity': normalize_severity(m.group('level')),
        'message': m.group('msg').strip(),
        'attributes': {'thread': m.group('thread')},
    }


_RE_VROPS_PROFILING = re.compile(
    r'^\s*(?P<callee>[\w.$]+(?:\([^)]*\))?)\s*-\s*(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+'
    r'(?P<time>\d{2}:\d{2}:\d{2}):(?P<ms>\d+)\s*-\s*(?P<dur>\d+)\s*ms\.?$'
)


def detect_vrops_profiling(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_VROPS_PROFILING.match(first_line(record))
    if not m:
        return None
    ts_raw = f"{m.group('mon')} {m.group('day')} {m.group('time')}.{m.group('ms')}"
    return {
        'timestamp': normalize_timestamp(ts_raw),
        'source_type': 'vROps',
        'process': 'vrops-analytics',
        'component': m.group('callee'),
        'severity': 'DEBUG',
        'message': f"{m.group('callee')} took {m.group('dur')}ms",
        'attributes': {'duration_ms': int(m.group('dur'))},
    }


#  vROps/CASA's shared appliance web layer: a bare thread name (ajp-nio
#  request threads, background pool threads, SSL handshake threads, ...),
#  an optional short request-id tag, an optional PID parenthetical some
#  collector plugins add, and an optional logger:line - the only constant
#  is the thread bracket immediately after LEVEL and a trailing "- msg".
_RE_VROPS_CASA_THREADED = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[,.]\d+)(?P<off>[+-]\d{4})\s+'
    r'(?P<level>\w+)\s*\[(?P<thread>[^\]]*)\]\s*(?:\[(?P<reqtag>[^\]]*)\]\s*)?(?:\(\d+\)\s*)?'
    # logger/line are optional (some services log an empty logger field), but
    # the "- msg" separator itself is always present - keeping it outside the
    # optional group so an absent logger never leaves a stray leading dash in msg.
    r'(?:(?P<logger>[\w.$]+)(?::(?P<line>\d+))?\s*)?-\s*(?P<msg>.*)$'
)
_RE_CASA_INOUT = re.compile(
    r'^(?P<direction>In|Out)\s*\*?\s*\[(?P<reqid>\d+)\]\s*(?:\[(?P<tag>[^\]]*)\]\s*)?'
    r'(?:(?P<client>\S+)\s+)?(?P<method>GET|POST|PUT|DELETE|PATCH|HEAD)\s+(?P<url>\S+)'
    r'(?:\s*:\s*(?P<outcome>.*))?$'
)


def detect_casa_ajp(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_VROPS_CASA_THREADED.match(first_line(record))
    if not m:
        return None
    msg = m.group('msg').strip()
    attributes = {'thread': m.group('thread').strip()}
    io = _RE_CASA_INOUT.match(msg)
    if io:
        attributes.update({
            'direction': io.group('direction'), 'client': io.group('client'),
            'http_method': io.group('method'), 'http_path': io.group('url'),
            'outcome': io.group('outcome'),
        })
    return {
        'timestamp': normalize_timestamp(m.group('ts') + m.group('off')),
        'source_type': 'CASA',
        'process': 'casa',
        'component': m.group('logger') or 'ajp',
        'severity': normalize_severity(m.group('level')),
        'message': msg,
        'attributes': attributes,
    }


#  Horizon Connection Server's own audit trail. Same structured-data
#  envelope as NSX manager syslog and Windows Security auditing (a VMware
#  Log Insight agent convention: APP-NAME token token [SD-ID@n k="v" ...] msg).
_RE_HORIZON_VIEW_AUDIT = re.compile(
    r'^(?P<ts>' + ISO_TS + r')\s+(?P<host>\S+)\s+View\s+-\s+(?P<id>\d+)\s+'
    r'\[View@\d+\s+(?P<kv>[^\]]*)\]\s*(?P<msg>.*)$'
)


def detect_horizon_view_audit(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_HORIZON_VIEW_AUDIT.match(first_line(record))
    if not m:
        return None
    kv = parse_kv_bag(m.group('kv'))
    view_severity = (kv.get('Severity') or '').upper()
    if 'FAILURE' in view_severity or 'ERROR' in view_severity:
        severity = 'ERROR'
    elif 'WARN' in view_severity:
        severity = 'WARNING'
    elif view_severity:
        severity = 'INFO'
    else:
        severity = None
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'hostname': m.group('host'),
        'source_type': 'Horizon',
        'process': 'View',
        'component': kv.get('Module', 'Broker'),
        'severity': severity,
        'message': m.group('msg').strip() or kv.get('EventType', ''),
        'attributes': kv,
        'entities': [v for k, v in kv.items() if k in ('UserSID', 'Source') and v],
    }


_RE_APACHE_BRACKET_FIRST = re.compile(
    r'^\[(?P<ts>\d{1,2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2}\s[+-]\d{4})\]\s+'
    r'(?P<client>\S+)\s+\S+\s+\S+\s+"(?P<method>\S+)\s+(?P<path>\S+)\s+[^"]*"\s+'
    r'(?P<status>\d+)\s+(?P<bytes>\d+|-)\s+(?P<duration>\d+)$'
)


def detect_apache_combined_bracket_first(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_APACHE_BRACKET_FIRST.match(first_line(record))
    if not m:
        return None
    status = int(m.group('status'))
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'source_type': 'CASA',
        'process': 'casa-admin',
        'component': 'http-access',
        'severity': 'ERROR' if status >= 500 else ('WARNING' if status >= 400 else 'INFO'),
        'message': f"{m.group('method')} {m.group('path')} {status}",
        'attributes': {
            'client_ip': m.group('client'), 'http_method': m.group('method'),
            'http_path': m.group('path'), 'http_status': status,
            'bytes': m.group('bytes'), 'duration_ms': m.group('duration'),
        },
    }


#  vROps's user session audit trail: comma-separated "Key : Value" pairs
#  (note the " : " separator, unlike every other detector's key=value bag)
#  following a bare "<ts,ms+off> - " envelope with no LEVEL/thread at all.
_RE_VROPS_SESSION_AUDIT = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[,.]\d+)(?P<off>[+-]\d{4})\s*-\s*'
    r'(?P<body>UserId\s*:.*)$'
)
_RE_COLON_KV_PAIR = re.compile(r'([A-Za-z][\w ]*?)\s*:\s*([^,]*)')


def detect_vrops_session_audit(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_VROPS_SESSION_AUDIT.match(first_line(record))
    if not m:
        return None
    attributes = {k.strip(): v.strip() for k, v in _RE_COLON_KV_PAIR.findall(m.group('body'))}
    return {
        'timestamp': normalize_timestamp(m.group('ts') + m.group('off')),
        'source_type': 'vROps',
        'process': 'vrops-session',
        'component': 'session-audit',
        'severity': 'INFO',
        'message': f"Session activity for {attributes.get('UserName', attributes.get('UserId', 'unknown user'))}",
        'attributes': attributes,
        'entities': [v for k, v in attributes.items() if k in ('UserId', 'UserName', 'Session') and v],
    }


#  vcops-watchdog's Python logging.Formatter output: "<ts, no ms> LEVEL -
#  <user> - <script> - <function> - msg" - a dash-chain rather than a
#  key=value bag, and no sub-second precision on the timestamp.
_RE_VCOPS_WATCHDOG = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s+(?P<level>\w+)\s+-\s+(?P<user>\S+)\s+-\s+'
    r'(?P<script>[\w\-]+)\s+-\s+(?P<func>\w+)\s+-\s+(?P<msg>.*)$'
)


def detect_vcops_watchdog(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_VCOPS_WATCHDOG.match(first_line(record))
    if not m:
        return None
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'source_type': 'vROps',
        'process': m.group('script'),
        'component': m.group('func'),
        'severity': normalize_severity(m.group('level')),
        'message': m.group('msg').strip(),
        'attributes': {'user': m.group('user')},
    }
