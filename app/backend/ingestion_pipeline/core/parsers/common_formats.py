"""
parsers/common_formats.py - Widely deployed formats found by testing against
vendors the repository had never seen.

Every detector here exists because a fixture-based test said the parser was
fine and an adversarial test said otherwise. The existing suite parsed
`sample_logs.txt`, which was written for this parser - so it proved the two
agreed, not that the parser handled the world. Re-testing with twenty-eight
vendors that appear nowhere in the fixtures (Juniper, SonicWall, Sophos, Zeek,
Cloudflare, Zscaler, SentinelOne, SailPoint, nginx, HAProxy, Redis, MongoDB,
MySQL, Tomcat, Veeam, BACnet, Zigbee...) dropped the figure from 97% to 75%.

These close the misses. In rough order of how much traffic they represent:

  rfc5424        the standard the problem statement names, previously PARSED
                 WRONG - pri_syslog read the version digit as the hostname
  nginx/apache   the Combined Log Format, on more web servers than anything else
  mysql_error    the default MySQL/MariaDB server log
  redis          the default Redis log
  sophos_xg      device="..." key=value, shared by several appliance vendors
  veeam          bracketed-level application logs
  zeek           tab-separated network telemetry
"""

import re
from typing import Any, Dict, Optional

from .base import (first_line, guess_severity_from_text, normalize_severity,
                   normalize_timestamp, parse_kv_bag)

# ---------------------------------------------------------------- RFC 5424
#  <PRI>VERSION SP TIMESTAMP SP HOSTNAME SP APP-NAME SP PROCID SP MSGID
#  SP STRUCTURED-DATA [SP MSG]
#
#  A nil value is "-" in every position, which is why each field allows it.
#  This MUST be registered before pri_syslog: that detector's optional RFC
#  3164 timestamp simply does not match a 5424 line, so its `host` group
#  swallowed the version digit and every 5424 event arrived with hostname "1".
_RE_RFC5424 = re.compile(
    r'^<(?P<pri>\d{1,3})>(?P<version>\d{1,2})\s+'
    r'(?P<ts>-|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\s+'
    r'(?P<host>-|\S{1,255})\s+'
    r'(?P<app>-|\S{1,48})\s+'
    r'(?P<procid>-|\S{1,128})\s+'
    r'(?P<msgid>-|\S{1,32})\s+'
    r'(?P<rest>.*)$'
)
#  STRUCTURED-DATA: one or more [id key="value" ...] elements, or a bare "-".
_RE_SD_ELEMENT = re.compile(r'\[(?P<sd_id>[^\s\]]+)(?P<params>(?:\s+[\w.\-]+="(?:[^"\\]|\\.)*")*)\]')

_SEVERITY_BY_CODE = {
    0: 'EMERGENCY', 1: 'ALERT', 2: 'CRITICAL', 3: 'ERROR',
    4: 'WARNING', 5: 'NOTICE', 6: 'INFO', 7: 'DEBUG',
}


def _nil(value: Optional[str]) -> Optional[str]:
    return None if value in (None, '-') else value


def detect_rfc5424(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_RFC5424.match(first_line(record))
    if not m:
        return None
    pri = int(m.group('pri'))
    if pri > 191:                       # 23*8+7 is the highest legal priority
        return None

    rest = (m.group('rest') or '').strip()
    attributes: Dict[str, Any] = {
        'syslog_facility': pri // 8,
        'syslog_severity': pri % 8,
        'syslog_version': m.group('version'),
    }

    # Structured data carries the interesting fields in 5424 - dropping it
    # would leave the message text and nothing else.
    message = rest
    if rest.startswith('['):
        consumed = 0
        for sd in _RE_SD_ELEMENT.finditer(rest):
            if sd.start() != consumed:
                break
            consumed = sd.end()
            attributes['sd_id'] = sd.group('sd_id')
            attributes.update(parse_kv_bag(sd.group('params') or ''))
        message = rest[consumed:].strip()
    elif rest.startswith('- '):
        message = rest[2:].strip()

    procid = _nil(m.group('procid'))
    pid = int(procid) if procid and procid.isdigit() else None

    return {
        'timestamp': normalize_timestamp(m.group('ts')) if _nil(m.group('ts')) else None,
        'hostname': _nil(m.group('host')),
        'source_type': 'Syslog',
        'process': _nil(m.group('app')) or 'syslog',
        'pid': pid,
        'component': _nil(m.group('msgid')) or _nil(m.group('app')) or 'syslog',
        'severity': _SEVERITY_BY_CODE.get(pri % 8, 'INFO'),
        'message': message or rest,
        'attributes': attributes,
    }


# ------------------------------------------------- nginx / Apache combined
#  host - user [dd/Mon/yyyy:HH:MM:SS +0000] "METHOD path proto" status bytes
#  "referer" "user-agent" [request_time]
#
#  The most deployed access-log format there is. Without it an nginx line fell
#  to the fallback, which then took the REFERER's hostname as the event's host
#  - a wrong host is worse than none, because it correlates.
_RE_COMBINED = re.compile(
    r'^(?P<client>\S+)\s+(?P<ident>\S+)\s+(?P<user>\S+)\s+'
    r'\[(?P<ts>\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2}\s[+-]\d{4})\]\s+'
    r'"(?P<method>[A-Z]+)\s+(?P<path>\S*)\s*(?P<proto>[^"]*)"\s+'
    r'(?P<status>\d{3})\s+(?P<bytes>\d+|-)'
    r'(?:\s+"(?P<referer>[^"]*)"\s+"(?P<agent>[^"]*)")?'
    r'(?:\s+(?P<duration>[\d.]+))?\s*$'
)


def detect_web_access(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_COMBINED.match(first_line(record))
    if not m:
        return None
    status = int(m.group('status'))
    g = m.groupdict()
    attrs = {k: v for k, v in (
        ('client_ip', g['client']), ('http_method', g['method']),
        ('http_path', g['path']), ('http_status', status),
        ('bytes', g['bytes']), ('referer', g['referer']),
        ('user_agent', g['agent']), ('duration_s', g['duration']),
        ('user', g['user']),
    ) if v not in (None, '', '-')}
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        # The CLIENT, not the referer's host. The fallback used to pick the
        # referer URL out of the line, which put an unrelated third-party
        # hostname on the event.
        'hostname': None,
        'source_type': 'WebAccess',
        'process': 'httpd',
        'component': 'access',
        'severity': 'ERROR' if status >= 500 else ('WARNING' if status >= 400 else 'INFO'),
        'message': '%s %s %d' % (m.group('method'), m.group('path'), status),
        'attributes': attrs,
    }


# ------------------------------------------------------------ MySQL / MariaDB
#  2026-09-05T11:10:02.334512Z 41 [Warning] [MY-010055] [Server] message
_RE_MYSQL = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+(?P<thread>\d+)\s+'
    r'\[(?P<level>\w+)\]\s*(?:\[(?P<code>[\w\-]+)\]\s*)?(?:\[(?P<subsystem>\w+)\]\s*)?'
    r'(?P<msg>.*)$'
)


def detect_mysql_error(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_MYSQL.match(first_line(record))
    if not m:
        return None
    attrs = {k: v for k, v in (('thread_id', m.group('thread')),
                               ('error_code', m.group('code'))) if v}
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'source_type': 'MySQL',
        'process': 'mysqld',
        'component': m.group('subsystem') or 'server',
        'severity': normalize_severity(m.group('level')) or 'INFO',
        'message': m.group('msg').strip(),
        'attributes': attrs,
    }


# ------------------------------------------------------------------- Redis
#  pid:role dd Mon yyyy HH:MM:SS.mmm LEVELCHAR message
#  role is one of X/C/S/M (sentinel, child, slave, master); the level is a
#  single character, which is why this needs its own detector rather than a
#  generic level match.
_RE_REDIS = re.compile(
    r'^(?P<pid>\d+):(?P<role>[XCSM])\s+'
    r'(?P<ts>\d{2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2}:\d{2}\.\d+)\s+'
    r'(?P<level>[.\-*#])\s+(?P<msg>.*)$'
)
_REDIS_LEVEL = {'.': 'DEBUG', '-': 'INFO', '*': 'NOTICE', '#': 'WARNING'}
_REDIS_ROLE = {'X': 'sentinel', 'C': 'child', 'S': 'replica', 'M': 'master'}


def detect_redis(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_REDIS.match(first_line(record))
    if not m:
        return None
    msg = m.group('msg').strip()
    level = _REDIS_LEVEL.get(m.group('level'), 'INFO')
    # Redis marks warnings with '#' but writes genuine errors through the same
    # channel, so the text still decides when it says something stronger.
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'source_type': 'Redis',
        'process': 'redis-server',
        'pid': int(m.group('pid')),
        'component': _REDIS_ROLE.get(m.group('role'), 'server'),
        'severity': guess_severity_from_text(msg) or level,
        'message': msg,
        'attributes': {'redis_role': _REDIS_ROLE.get(m.group('role'), m.group('role'))},
    }


# --------------------------------------------------------------- Sophos XG
#  device="SFW" date=... time=... log_type="Firewall" status="Deny" ...
#  A key=value bag that names itself with `device=`. Distinct from Fortinet
#  (devname=) and from logfmt (which requires a msg= key), so it needs its own
#  detector rather than falling through to either.
_SOPHOS_MARKERS = ('device=', 'log_type=', 'log_component=')


def detect_sophos_kv(record: str) -> Optional[Dict[str, Any]]:
    line = first_line(record)
    if sum(marker in line for marker in _SOPHOS_MARKERS) < 2:
        return None
    kv = parse_kv_bag(line)
    if not kv:
        return None
    date, time_ = kv.get('date'), kv.get('time')
    ts = normalize_timestamp('%s %s' % (date, time_)) if date and time_ else None
    status = (kv.get('status') or '').lower()
    severity = ('WARNING' if status in ('deny', 'drop', 'denied')
                else normalize_severity(kv.get('severity') or kv.get('priority') or '')
                or guess_severity_from_text(line) or 'INFO')
    return {
        'timestamp': ts,
        'hostname': kv.get('device_name') or kv.get('device'),
        'source_type': kv.get('device') or 'Appliance',
        'process': kv.get('log_component') or kv.get('log_type') or 'appliance',
        'component': kv.get('log_subtype') or kv.get('log_type') or 'firewall',
        'severity': severity,
        # Parenthesised: `'%s %s' % (a, b).strip()` binds .strip() to the
        # TUPLE, not to the formatted string, and raises AttributeError -
        # which the per-detector try/except swallowed, so the detector simply
        # never fired and the line fell to the fallback silently.
        'message': (kv.get('message') or kv.get('msg')
                    or ('%s %s' % (kv.get('log_component', ''),
                                   kv.get('status', ''))).strip()
                    or line),
        'attributes': kv,
    }


# ------------------------------------------------------- bracketed-level app
#  2026-09-05 11:17:02 [Warning] Job "Nightly-SQL" finished with warnings
#  Veeam and a long tail of Windows-side applications write this shape.
_RE_BRACKET_LEVEL = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+'
    r'\[(?P<level>Trace|Debug|Info|Information|Notice|Warn|Warning|Error|Fatal|Critical)\]\s+'
    r'(?P<msg>.*)$', re.IGNORECASE)


def detect_bracket_level_app(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_BRACKET_LEVEL.match(first_line(record))
    if not m:
        return None
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'source_type': 'Application',
        'process': 'application',
        'component': 'application',
        'severity': normalize_severity(m.group('level')) or 'INFO',
        'message': m.group('msg').strip(),
        'attributes': {},
    }


# -------------------------------------------------------------- Zeek conn.log
#  Tab-separated, no header on the wire: ts uid orig_h orig_p resp_h resp_p
#  proto service duration orig_bytes resp_bytes conn_state ...
#
#  Positional and therefore fragile, so it is anchored hard: an epoch with
#  fractional seconds, a 17-character Zeek UID, then two IP/port pairs. That
#  combination does not occur in the other formats here.
_RE_ZEEK_CONN = re.compile(
    r'^(?P<ts>\d{9,10}\.\d+)\s+(?P<uid>[A-Za-z0-9]{15,20})\s+'
    r'(?P<orig_h>\d{1,3}(?:\.\d{1,3}){3})\s+(?P<orig_p>\d{1,5})\s+'
    r'(?P<resp_h>\d{1,3}(?:\.\d{1,3}){3})\s+(?P<resp_p>\d{1,5})\s+'
    r'(?P<proto>tcp|udp|icmp)\s+(?P<service>\S+)\s+'
    r'(?P<duration>[\d.\-]+)\s+(?P<orig_bytes>[\d\-]+)\s+(?P<resp_bytes>[\d\-]+)\s+'
    r'(?P<conn_state>[A-Z0-9]{2,4})\b(?P<rest>.*)$'
)
#  Zeek connection states worth flagging: rejected, and attempted-but-unanswered.
_ZEEK_BAD_STATE = {'REJ', 'RSTO', 'RSTR', 'RSTOS0', 'RSTRH', 'S0', 'SH', 'SHR'}


def detect_zeek_conn(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_ZEEK_CONN.match(first_line(record))
    if not m:
        return None
    g = m.groupdict()
    state = g['conn_state']
    attrs = {k: v for k, v in (
        ('uid', g['uid']), ('src_ip', g['orig_h']), ('src_port', g['orig_p']),
        ('dst_ip', g['resp_h']), ('dst_port', g['resp_p']),
        ('proto', g['proto']), ('service', g['service']),
        ('duration_s', g['duration']), ('orig_bytes', g['orig_bytes']),
        ('resp_bytes', g['resp_bytes']), ('conn_state', state),
    ) if v not in (None, '', '-')}
    return {
        'timestamp': normalize_timestamp(g['ts']),
        'source_type': 'Zeek',
        'process': 'zeek',
        'component': 'conn',
        'severity': 'WARNING' if state in _ZEEK_BAD_STATE else 'INFO',
        'message': '%s %s:%s -> %s:%s %s' % (g['service'], g['orig_h'], g['orig_p'],
                                             g['resp_h'], g['resp_p'], state),
        'attributes': attrs,
    }
