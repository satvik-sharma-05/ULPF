"""
parsers/windows_security.py - Windows Event Log entries forwarded by the
VMware Log Insight Windows agent (liagent) - Security auditing is the most
common channel here, but System-log sources (Service Control Manager,
Windows Error Reporting, LsaSrv, ...) use the exact same envelope. The
header line is a single structured record; its body (Subject:/Object:/
Process Information: blocks, when present) follows as separate physical
lines with no timestamp of their own - multiline.reassemble() joins them
back into one record before this detector ever sees them.
"""

import re
from typing import Any, Dict, Optional

from .base import ISO_TS, first_line, parse_kv_bag, rest_of, strip_bom, normalize_timestamp

_RE_WINDOWS_EVENT = re.compile(
    r'^(?P<ts>' + ISO_TS + r')\s+(?P<host>\S+)\s+(?P<channel>[\w\-]+)\s+-\s+'
    r'(?P<event_id>\d+)\s+\[liagent@(?P<agent>\d+)\s+(?P<kv>[^\]]*)\]\s*(?P<msg>.*)$'
)
_RE_WINSEC_KV_LINE = re.compile(r'^\t+([A-Za-z][\w /()\\-]*?)\s*:\t*\s*(.*)$')
_RE_WINSEC_SECTION = re.compile(r'^([A-Z][\w \-/]*)\s*:\s*$')


def detect_windows_security_event(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_WINDOWS_EVENT.match(first_line(record))
    if not m:
        return None
    kv = parse_kv_bag(m.group('kv'))
    sections: Dict[str, Dict[str, str]] = {}
    current_section = 'General'
    for line in rest_of(record).split('\n'):
        if not line.strip():
            continue
        sm = _RE_WINSEC_SECTION.match(line.strip())
        if sm and not line.startswith('\t'):
            current_section = sm.group(1).strip()
            sections.setdefault(current_section, {})
            continue
        kvm = _RE_WINSEC_KV_LINE.match(line)
        if kvm:
            sections.setdefault(current_section, {})[kvm.group(1).strip()] = kvm.group(2).strip()

    channel = m.group('channel')
    attributes = {
        'channel': channel,
        'event_id': m.group('event_id'),
        'eventrecordid': kv.get('eventrecordid'),
        'eventsourcename': kv.get('eventsourcename'),
        'keywords': kv.get('keywords'),
        'opcode': kv.get('opcode'),
        'task': kv.get('task'),
        'userid': kv.get('userid'),
    }
    if sections:
        attributes['sections'] = sections
    subject = sections.get('Subject') or sections.get('Subject ') or sections.get('Creator Subject', {})
    account = subject.get('Account Name') if isinstance(subject, dict) else None

    is_security = channel == 'Microsoft-Windows-Security-Auditing'
    severity = 'NOTICE' if 'success' in (attributes['keywords'] or '').lower() else 'WARNING'
    if not is_security:
        severity = 'ERROR' if 'error' in channel.lower() else ('WARNING' if not sections else severity)

    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'hostname': m.group('host'),
        'source_type': 'WindowsSecurityAudit' if is_security else 'WindowsEventLog',
        'process': channel,
        'component': attributes['task'] or channel,
        'severity': severity,
        'message': strip_bom(m.group('msg')).strip(),
        'attributes': attributes,
        'entities': [e for e in (account, kv.get('userid'), m.group('host')) if e],
    }
