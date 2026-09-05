"""
parsers/nsx.py - NSX Manager/Edge and NSX ESX-side agent syslog.

Two shapes:
  Manager/Edge : <ts> <host> NSX <pid> <POLICY|LOAD-BALANCER|-> [nsx@6876 k="v" ...] <msg>
  ESX agent    : <ts> <host> <agent>[<pid>]: NSX <pid> - [nsx@6876 k="v" ...] <msg>
                 (nestdb-server, nsx-exporter, nsx-sha, ...; some have no [pid])
"""

import re
from typing import Any, Dict, Optional

from .base import ISO_TS, first_line, normalize_severity, normalize_timestamp, parse_kv_bag

_RE_NSX_MANAGER = re.compile(
    r'^(?P<ts>' + ISO_TS + r')\s+(?P<host>\S+)\s+NSX\s+(?P<pid>\d+)\s+'
    # MSGID: a module token (POLICY, LOAD-BALANCER, SWITCHING, FABRIC, "-", ...)
    # - not a fixed enum, new modules keep showing up in this corpus.
    r'(?P<subtype>[\w-]+)?\s*\[nsx@\d+\s+(?P<kv>[^\]]*)\]\s*(?P<msg>.*)$'
)

#  vmsyslogd occasionally wraps the payload in its own [vmsyslogd@6876 ...]
#  bracket (e.g. when it modified/truncated an oversized message) before the
#  real [nsx@6876 ...] structured data - that wrapper is optional here.
_RE_NSX_ESX_AGENT = re.compile(
    r'^(?P<ts>' + ISO_TS + r')\s+(?P<host>\S+)\s+(?P<proc>[\w.\-]+)\[(?P<pid>\d+)\]:\s+'
    r'(?:\[vmsyslogd@\d+[^\]]*\]\s*)?NSX\s+\d+\s+-\s+\[nsx@\d+\s+(?P<kv>[^\]]*)\]\s*(?P<msg>.*)$'
)
_RE_NSX_ESX_AGENT_NOPID = re.compile(
    r'^(?P<ts>' + ISO_TS + r')\s+(?P<host>\S+)\s+(?P<proc>[\w.\-]+):\s+'
    r'(?:\[vmsyslogd@\d+[^\]]*\]\s*)?NSX\s+\d+\s+-\s+\[nsx@\d+\s+(?P<kv>[^\]]*)\]\s*(?P<msg>.*)$'
)


def detect_nsx_manager(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_NSX_MANAGER.match(first_line(record))
    if not m:
        return None
    kv = parse_kv_bag(m.group('kv'))
    host = m.group('host')
    process = 'NSX Edge' if 'EDG' in host.upper() else 'NSX Manager'
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'hostname': host,
        'source_type': 'NSX',
        'process': process,
        'pid': int(m.group('pid')),
        'component': kv.get('comp', process),
        'subcomponent': kv.get('subcomp') or kv.get('s2comp'),
        'severity': normalize_severity(kv.get('level')),
        'message': m.group('msg').strip(),
        'attributes': kv,
        'entities': [v for k, v in kv.items() if k in ('Src', 'UserName', 'username') and v],
    }


def detect_nsx_esx_agent(record: str) -> Optional[Dict[str, Any]]:
    line = first_line(record)
    m = _RE_NSX_ESX_AGENT.match(line) or _RE_NSX_ESX_AGENT_NOPID.match(line)
    if not m:
        return None
    kv = parse_kv_bag(m.group('kv'))
    gd = m.groupdict()
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'hostname': m.group('host'),
        'source_type': 'NSX',
        'process': m.group('proc'),
        'pid': int(gd['pid']) if gd.get('pid') else None,
        'component': kv.get('comp', 'nsx-esx'),
        'subcomponent': kv.get('subcomp'),
        'severity': normalize_severity(kv.get('level')),
        'message': m.group('msg').strip(),
        'attributes': kv,
    }
