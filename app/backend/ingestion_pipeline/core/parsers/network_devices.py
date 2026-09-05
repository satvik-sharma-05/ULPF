"""
parsers/network_devices.py - Perimeter network appliances.

Firewalls, IDS/IPS, VPN concentrators, WAFs, routers and switches: the devices
the Current Scope names explicitly. Two detectors cover almost all of them,
because almost all of them speak one of two dialects.

**Priority-prefixed RFC 3164.** A network appliance emits
`<166>Sep  2 09:16:11 asa-edge-01 %ASA-6-302013: ...`. The existing syslog
detector anchors on the month and so never matched any of it - Cisco ASA,
Cisco IOS, Snort, F5, OpenVPN and pfSense all fell through to
generic_fallback and, worse, lost the hostname that was sitting right there in
the line. On a perimeter estate the hostname IS the device, so losing it makes
the event nearly useless for correlation.

**Fortinet-style key=value.** FortiGate, and several others, emit a flat bag of
`key=value` pairs with no syslog envelope at all.

The `<PRI>` number is worth decoding rather than skipping: severity = PRI % 8
is what the device itself declared, which beats inferring severity from the
words in the message. That is the difference between an ASA connection-teardown
being correctly INFO and being guessed as an error because it contains the word
"reset".
"""

import re
from typing import Any, Dict, Optional

from .base import (SEVERITY_SCORE, first_line, guess_severity_from_text,
                   normalize_timestamp, parse_kv_bag)

# RFC 3164 severity, straight from the priority value.
_PRI_SEVERITY = {
    0: 'EMERGENCY', 1: 'ALERT', 2: 'CRITICAL', 3: 'ERROR',
    4: 'WARNING', 5: 'NOTICE', 6: 'INFO', 7: 'DEBUG',
}

_PRI_FACILITY = {
    0: 'kern', 1: 'user', 2: 'mail', 3: 'daemon', 4: 'auth', 5: 'syslog',
    6: 'lpr', 7: 'news', 8: 'uucp', 9: 'cron', 10: 'authpriv', 11: 'ftp',
    16: 'local0', 17: 'local1', 18: 'local2', 19: 'local3',
    20: 'local4', 21: 'local5', 22: 'local6', 23: 'local7',
}

# <PRI>Mon DD HH:MM:SS host tag[pid]: message
_RE_PRI_SYSLOG = re.compile(
    r'^<(?P<pri>\d{1,3})>\s*'
    r'(?:(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})\s+)?'
    r'(?P<host>[\w.\-]+)\s+'
    r'(?P<tag>[^\s:\[]{1,48})(?:\[(?P<pid>\d+)\])?:?\s*'
    r'(?P<rest>.*)$'
)

# Cisco-style mnemonic: %ASA-6-302013, %LINK-3-UPDOWN
_RE_CISCO_MNEMONIC = re.compile(r'^%(?P<facility>[A-Z0-9_]+)-(?P<sev>\d)-(?P<mnemonic>[A-Z0-9_]+):\s*(?P<msg>.*)$')

# Vendors that identify themselves in a key=value bag rather than a syslog
# envelope. `devname` is FortiGate; the others are close relatives.
_RE_FORTINET = re.compile(r'\bdevname="?[\w.\-]+"?', re.IGNORECASE)
_RE_DATE_TIME_KV = re.compile(r'^date=\d{4}-\d{2}-\d{2}\s+time=\d{2}:\d{2}:\d{2}\b', re.IGNORECASE)

# Which product a tag belongs to, so source_type is the technology rather than
# the process name. Anything unmatched stays NetworkDevice, which is still a
# useful bucket.
_TAG_PRODUCTS = (
    (re.compile(r'^snort', re.I), 'Snort'),
    (re.compile(r'^suricata', re.I), 'Suricata'),
    (re.compile(r'^openvpn', re.I), 'OpenVPN'),
    (re.compile(r'^filterlog', re.I), 'pfSense'),
    (re.compile(r'^ASM|^bigip', re.I), 'F5-BIGIP'),
    (re.compile(r'^charon|^ipsec|^strongswan', re.I), 'IPsec'),
    (re.compile(r'^sshd', re.I), 'SSH'),
    (re.compile(r'^haproxy', re.I), 'HAProxy'),
    (re.compile(r'^nginx', re.I), 'NGINX'),
)


def _decode_pri(pri: int) -> Dict[str, Any]:
    return {
        'severity': _PRI_SEVERITY.get(pri % 8, 'INFO'),
        'facility': _PRI_FACILITY.get(pri // 8, str(pri // 8)),
    }


def detect_pri_syslog(record: str) -> Optional[Dict[str, Any]]:
    """Priority-prefixed RFC 3164, as emitted by most network appliances."""
    line = first_line(record)
    m = _RE_PRI_SYSLOG.match(line)
    if not m:
        return None

    pri = int(m.group('pri'))
    if pri > 191:                      # 23*8+7 is the highest legal priority
        return None
    decoded = _decode_pri(pri)

    host = m.group('host')
    tag = m.group('tag')
    rest = (m.group('rest') or '').strip()
    severity = decoded['severity']
    product = 'NetworkDevice'
    attributes: Dict[str, Any] = {'syslog_facility': decoded['facility'],
                                  'syslog_priority': pri}

    # A Cisco mnemonic carries its own severity digit, which is more specific
    # than the envelope's - "%ASA-6-..." is informational even on a facility
    # that would otherwise suggest otherwise.
    cisco = _RE_CISCO_MNEMONIC.match(rest)
    if cisco:
        product = 'Cisco'
        tag = cisco.group('facility')
        attributes['cisco_mnemonic'] = cisco.group('mnemonic')
        severity = _PRI_SEVERITY.get(int(cisco.group('sev')), severity)
        rest = cisco.group('msg').strip()
    else:
        for pattern, name in _TAG_PRODUCTS:
            if pattern.match(tag):
                product = name
                break

    # Any key=value pairs in the body are real fields - src, dst, action,
    # support_id - and belong in the attributes bag rather than only in prose.
    kv = parse_kv_bag(rest)
    if kv:
        attributes.update(kv)

    ts = None
    if m.group('mon'):
        ts = normalize_timestamp(f"{m.group('mon')} {m.group('day')} {m.group('time')}")

    return {
        'timestamp': ts,
        'hostname': host,
        'source_type': product,
        'process': tag,
        'component': tag,
        'pid': m.group('pid'),
        'severity': severity,
        'severity_score': SEVERITY_SCORE.get(severity, 6),
        'message': rest or line,
        'attributes': attributes,
        'matched_format': 'pri_syslog',
    }


def detect_fortinet_kv(record: str) -> Optional[Dict[str, Any]]:
    """FortiGate and relatives: a flat key=value bag, no syslog envelope."""
    line = first_line(record)
    if not (_RE_FORTINET.search(line) or _RE_DATE_TIME_KV.match(line)):
        return None

    kv = parse_kv_bag(line)
    if not kv:
        return None

    host = kv.get('devname') or kv.get('hostname') or kv.get('devid')
    date, time = kv.get('date'), kv.get('time')
    ts = normalize_timestamp(f"{date} {time}") if date and time else None

    raw_level = (kv.get('level') or kv.get('severity') or '').upper()
    severity = raw_level if raw_level in SEVERITY_SCORE else None
    if not severity:
        # A blocked or denied packet is the finding on a firewall; without this
        # every drop reads as INFO and the interesting events disappear.
        action = (kv.get('action') or '').lower()
        severity = ('WARNING' if action in ('blocked', 'deny', 'denied', 'drop', 'dropped')
                    else guess_severity_from_text(line) or 'INFO')

    message = kv.get('msg') or kv.get('logdesc') or line
    return {
        'timestamp': ts,
        'hostname': host,
        'source_type': 'Fortinet' if kv.get('devname') or kv.get('devid') else 'NetworkDevice',
        'process': kv.get('type') or 'firewall',
        'component': kv.get('subtype') or kv.get('type') or 'firewall',
        'severity': severity,
        'severity_score': SEVERITY_SCORE.get(severity, 6),
        'message': message,
        'attributes': kv,
        'matched_format': 'fortinet_kv',
    }
