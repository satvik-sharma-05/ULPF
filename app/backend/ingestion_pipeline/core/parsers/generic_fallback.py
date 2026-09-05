"""
parsers/generic_fallback.py - Last-resort extractor.

Always returns a result (never None) so no record is ever dropped, even if it
matches none of the specific format detectors. This is the path every log
source takes on its first day, before anyone has written a detector for it,
so how much it recovers decides how useful an unknown source is on arrival -
requirement (e), plug-and-play onboarding, rests almost entirely here.

It works STRUCTURALLY rather than by recognising particular vendors. An
earlier version searched for a handful of hard-coded domain suffixes and
hostname prefixes - the naming of the one estate the development corpus came
from - which meant that on any other network the fallback returned no
hostname at all. Those values now live in .env, not here: publishing an
organisation's internal naming in source is a poor default in both senses. A framework whose last-resort path only works at one
customer is not vendor-agnostic. Site-specific naming is still honoured, but
as configuration (ULPF_SITE_DOMAINS / ULPF_HOST_PATTERN) layered on top of
rules that hold anywhere:

  * RFC 3164/5424 put the host in a fixed position - the token straight after
    the timestamp - whatever it happens to be called. That single rule covers
    most of what any syslog-speaking device emits.
  * `proc[pid]:` and `proc:` after the host are the universal syslog idiom
    for the emitting program.
  * An explicit `host=`/`hostname=`/`dvchost=` in a key=value bag beats any
    guess, so it is consulted first.
"""

import os
import re
from typing import Any, Dict, Optional

from .base import (ISO_TS, first_line, guess_severity_from_text,
                   normalize_timestamp, parse_kv_bag, strip_bom)

_GENERIC_TS_PATTERNS = [
    re.compile(r'\b' + ISO_TS + r'\b'),
    re.compile(r'\b\d{1,2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2}\b'),      # Apache
    re.compile(r'\b[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\b'),      # RFC 3164
    re.compile(r'\b[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4}\b'),  # ctime
    re.compile(r'\b\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}\b'),            # US vendor
    re.compile(r'\b\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}\b'),            # PAN-OS
]

#  A record may open with a syslog PRI, a bracketed timestamp, or nothing.
_LEADING_PRI = re.compile(r'^<\d{1,3}>\s*')
#  The host slot in RFC 3164/5424: <ts> <host> <rest>. Version-stamped 5424
#  lines put a digit between them, which is skipped.
_AFTER_TS_HOST = re.compile(r'^(?:\d\s+)?(?P<host>[A-Za-z0-9][\w.\-]{1,62})\s')
#  `proc[pid]:` / `proc:` - the emitting program, in syslog's usual place.
_PROC = re.compile(r'^(?P<proc>[\w./\-]{1,48})(?:\[(?P<pid>\d+)\])?\s*:\s*')
#  A dotted name is only a hostname if it is not a bare IP and not a filename.
_FQDN = re.compile(r'\b(?![\d.]+\b)([A-Za-z0-9][\w\-]*(?:\.[A-Za-z0-9][\w\-]*)+)\b')
_FILE_EXT = re.compile(
    r'\.(?:log|txt|json|xml|csv|conf|cfg|ini|py|sh|so|c|cpp|h|java|js|jar|'
    r'gz|zip|tar|db|pid|sock|yaml|yml|html|php|exe|dll)$', re.IGNORECASE)
#  A short unqualified name still reads as a host if it mixes letters with a
#  digit or dash (web-srv-07, fw01, esx2) - an English word does not.
_HOSTLIKE = re.compile(r'^(?=.*[\d\-])[A-Za-z][\w\-]{1,62}$')

_KV_HOST_KEYS = ('hostname', 'host', 'dvchost', 'devicehostname', 'shost',
                 'computername', 'computer', 'devname', 'device', 'node',
                 'agent_host', 'src_host', 'server')
_KV_PROC_KEYS = ('process', 'proc', 'program', 'app', 'appname', 'application',
                 'service', 'processname', 'process_name')

#  Site naming, as configuration rather than as code. Comma-separated domain
#  suffixes; a hostname ending in one of them is preferred over any guess.
_SITE_DOMAINS = tuple(
    d.strip().lower().lstrip('.')
    # Deliberately generic. The deployment's real domains belong in .env,
    # not baked into source that gets published - and a framework that
    # claims to be vendor-agnostic should not ship one customer's domain
    # as its built-in assumption.
    for d in os.getenv('ULPF_SITE_DOMAINS', 'local').split(',')
    if d.strip()
)
#  An optional site-specific regex for hosts that follow a local convention
#  but no domain suffix (an appliance naming scheme, say). Group 1, or the
#  whole match, is taken as the hostname.
_SITE_HOST_RE: Optional[re.Pattern] = None
#  No default pattern. A site with its own naming convention sets
#  ULPF_HOST_PATTERN in .env; shipping one estate's prefix as the
#  built-in would make every other deployment's fallback slightly wrong.
_raw_site_re = os.getenv('ULPF_HOST_PATTERN', '')
if _raw_site_re:
    try:
        _SITE_HOST_RE = re.compile(_raw_site_re)
    except re.error:
        _SITE_HOST_RE = None


#  Printable-ASCII ratio above which a record is treated as text. Binary
#  content read as text (a rotated .gz, a core dump, a keystore) produces
#  high-byte noise in which the FQDN pattern will happily match "W.z" or
#  "E.mu" - and a guessed host is worse than no host, because it becomes a
#  (:Host) node in the graph and pollutes every correlation that touches it.
_TEXT_RATIO = 0.85
#  Tab, LF, CR and the printable ASCII range. Expressed as code points
#  rather than as a regex character class because the escapes such a class
#  needs are exactly the kind that get mangled in transit and then fail
#  silently - a literal tab in the pattern still compiles.
_PRINTABLE_ORDS = frozenset((9, 10, 13)) | frozenset(range(32, 127))


def is_mostly_text(line: str) -> bool:
    if not line:
        return True
    sample = line[:400]
    printable = sum(1 for ch in sample if ord(ch) in _PRINTABLE_ORDS)
    return printable / len(sample) >= _TEXT_RATIO


def _looks_like_host(token: str) -> bool:
    if not token or len(token) < 4 or _FILE_EXT.search(token):
        return False
    if token.replace('.', '').isdigit():        # a bare IP or a number
        return False
    if '.' in token:
        if not _FQDN.fullmatch(token):
            return False
        labels = token.split('.')
        # A real name has a substantial first label and an alphabetic final
        # one. This is what separates web-srv-07.corp.example from the "E.mu"
        # the pattern finds inside binary noise.
        return (len(labels[0]) >= 2 and len(labels[-1]) >= 2
                and labels[-1].isalpha())
    return bool(_HOSTLIKE.match(token))


def _extract_hostname(line: str, kv: Dict[str, str]) -> Optional[str]:
    """Best available host identity, most reliable evidence first."""
    # 1. The record says so outright.
    for key in _KV_HOST_KEYS:
        for k, v in kv.items():
            if k.lower() == key and v and _looks_like_host(v):
                return v

    # 2. Site convention, where one is configured.
    if _SITE_HOST_RE is not None:
        m = _SITE_HOST_RE.search(line)
        if m:
            return m.group(1) if m.groups() else m.group(0)

    # 3. The syslog host slot: the token straight after the timestamp. This is
    #    positional, so it works for a device whose name follows no convention
    #    this code could ever be taught.
    body = _LEADING_PRI.sub('', line)
    for pat in _GENERIC_TS_PATTERNS:
        m = pat.search(body)
        if not m or m.start() > 32:   # the timestamp leads the line, or it is
            continue                  # a timestamp from inside the message
        after = body[m.end():].lstrip()
        hm = _AFTER_TS_HOST.match(after)
        if hm and _looks_like_host(hm.group('host')):
            return hm.group('host')
        break

    # 4. A fully-qualified name anywhere in the line, preferring a configured
    #    site domain over an arbitrary one (an FQDN in the message body is
    #    often the object of the event rather than its origin, so an
    #    unqualified guess is not made here).
    candidates = [c for c in _FQDN.findall(body) if _looks_like_host(c)]
    for cand in candidates:
        if cand.lower().endswith(_SITE_DOMAINS):
            return cand
    return candidates[0] if candidates else None


def _extract_process(line: str, kv: Dict[str, str], hostname: Optional[str]):
    """The emitting program and its pid, from syslog's `proc[pid]:` slot."""
    for key in _KV_PROC_KEYS:
        for k, v in kv.items():
            if k.lower() == key and v:
                return str(v)[:64], None

    body = _LEADING_PRI.sub('', line)
    if hostname:
        idx = body.find(hostname)
        if idx != -1:
            body = body[idx + len(hostname):].lstrip()
    m = _PROC.match(body)
    if not m:
        return None, None
    proc = m.group('proc')
    # A timestamp fragment or a bare number is not a program name.
    if proc.isdigit() or not re.search(r'[A-Za-z]', proc):
        return None, None
    pid = int(m.group('pid')) if m.group('pid') else None
    return proc, pid


def detect_generic_fallback(record: str) -> Dict[str, Any]:
    text = strip_bom(record)
    line = first_line(text)

    timestamp = None
    for pat in _GENERIC_TS_PATTERNS:
        m = pat.search(line)
        if m:
            timestamp = normalize_timestamp(m.group(0))
            break

    kv = parse_kv_bag(line)
    textual = is_mostly_text(line)
    if textual:
        hostname = _extract_hostname(line, kv)
        process, pid = _extract_process(line, kv, hostname)
    else:
        # Flagged rather than dropped: requirement (a) is that nothing is
        # lost, so the raw bytes are still preserved and the record still
        # lands in the schema - it is simply not pretended to be parsed.
        hostname = process = pid = None
        kv = {}

    result: Dict[str, Any] = {
        'timestamp': timestamp,
        'hostname': hostname,
        'source_type': 'Unknown',
        'process': process,
        'component': None,
        'severity': guess_severity_from_text(text),
        'message': line.strip(),
        'attributes': kv,
    }
    if pid is not None:
        result['pid'] = pid
    if not textual:
        result['attributes'] = {'binary_content': True}
        result['severity'] = None
    return result
