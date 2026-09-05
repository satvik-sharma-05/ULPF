"""
parsers/vmacore.py - VMware's "vmacore" appliance logging format.

Every VMware Java/C++ appliance daemon that isn't ESXi hostd shares one inner
shape:

    <iso-ts+offset> <level> <proc>[<pid>] [<SDID>@<n> sub=... opID=...] <msg>

Site Recovery Manager (`vmware-dr`) is the big producer of it in this corpus,
and it arrives double-wrapped - an RFC3164 syslog envelope from the forwarder,
then the real record inside:

    Jun 15 04:14:59 MDVMWSRMAPP01 vmware-dr: 2026-06-15T09:44:59.992+05:30 \
        warning vmware-dr[1628447] [SRM@6876 sub=DatastoreGroupManager \
        opID=4e9e0d7b] Skipping device 'naa.6001...' in error state on host 'host-74'

Before this detector existed those lines fell through to the generic RFC3164
handler, which kept `process=vmware-dr` but threw away the severity, the
`sub=` component and the `opID=` correlation id - and those three fields are
exactly what makes an SRM log worth storing. The outer envelope's timestamp is
deliberately ignored in favour of the inner one: the wrapper is stamped in the
forwarder's timezone with no year, while the inner value carries both.
"""

import re
from typing import Any, Dict, Optional

from .base import first_line, guess_severity_from_text, normalize_severity, normalize_timestamp, parse_kv_bag

# The structured-data id names the product: SRM@6876, Originator@6876, ...
_INNER = (
    r'(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2}))\s+'
    r'(?P<level>verbose|trivia|info|warning|error|panic|debug|notice|critical)\s+'
    r'(?P<proc>[\w\-.]+)\[(?P<pid>\d+)\]\s+'
    r'\[(?P<sdid>\w+)@\d+\s+(?P<kv>[^\]]*)\]\s*(?P<msg>.*)$'
)

# Wrapped in the forwarder's RFC3164 envelope (the common case).
_RE_VMACORE_WRAPPED = re.compile(
    r'^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+(?P<host>\S+)\s+(?P<app>[\w\-.]+):\s+' + _INNER
)
# Same record delivered without the envelope.
_RE_VMACORE_BARE = re.compile(r'^' + _INNER)

# Which product each structured-data id belongs to, so source_type is the
# actual product rather than a catch-all "VMware".
_SDID_SOURCE_TYPE = {
    'SRM': 'SRM',
    'Originator': 'ESXi',
    'Vpxd': 'vCenter',
    'Hms': 'vSphereReplication',
}


def detect_vmacore_appliance(record: str) -> Optional[Dict[str, Any]]:
    line = first_line(record)
    match = _RE_VMACORE_WRAPPED.match(line)
    hostname = None
    if match:
        hostname = match.group('host')
    else:
        match = _RE_VMACORE_BARE.match(line)
    if not match:
        return None

    kv = parse_kv_bag(match.group('kv'))
    message = match.group('msg').strip()
    sdid = match.group('sdid')

    # `sub=DatastoreGroupManager.DeviceFetcherPropertyQueue` is a dotted
    # component path; keep the whole thing as the component and the leading
    # segment as the subcomponent so both granularities are queryable.
    sub = kv.get('sub')
    subcomponent = sub.split('.', 1)[1] if sub and '.' in sub else None

    return {
        'timestamp': normalize_timestamp(match.group('ts')),
        'hostname': hostname,
        'source_type': _SDID_SOURCE_TYPE.get(sdid, 'VMwareAppliance'),
        'process': match.group('proc'),
        'pid': int(match.group('pid')),
        'component': sub or match.group('proc'),
        'subcomponent': subcomponent,
        'severity': normalize_severity(match.group('level')) or guess_severity_from_text(message),
        'message': message,
        'attributes': dict(kv, sdid=sdid),
    }
