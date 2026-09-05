"""
multiline.py - Reassembles physical log lines into logical records.

Most sources in this pipeline emit one record per physical line. A few
don't: a Windows Security Event's body (Subject:/Object:/... blocks) and a
Java stack trace both continue across several physical lines with no
timestamp of their own on the continuation lines. The rule that tells them
apart from the start of a new record is the same one log shippers use for
multi-line messages (Filebeat's multiline.pattern, rsyslog's "last line
wins"): a line starts a new record only if it looks like a record header
(matches one of the known signatures below); anything else is a continuation
of whichever record is currently open.

Every format with a detector in parsers/ needs a signature here, and the two
lists have to be kept in step. A detector without a matching record-start
pattern works only on files that contain nothing else: on a mixed feed its
records are silently welded onto whatever preceded them. That is what
happened to CEF, LEEF, PRI-prefixed syslog, the JSON cloud/EDR/IAM formats
and Fortinet key=value, all of which parsed correctly in isolation and
disappeared in combination.
"""

import re
from typing import Iterable, Iterator

_RECORD_START_PATTERNS = [
    # ISO 8601 / RFC 3339, optional comma-millis, optional Z or +HHMM offset
    re.compile(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}([.,]\d+)?(Z|[+-]\d{2}:?\d{2})?\b'),
    # Bracketed ISO timestamp (JVM unified GC logging): [2026-...+0000]
    re.compile(r'^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([.,]\d+)?[+-]\d{4}\]'),
    # RFC 3164 syslog: "Jun 12 04:00:08 host ..."
    re.compile(r'^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\S+\s'),
    # glog (kubelet/component logs): E0612 04:00:08.653331 ...
    re.compile(r'^[EWIF]\d{4}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\d+\s'),
    # Epoch seconds.millis lead-in (Squid access log)
    re.compile(r'^\d{9,10}\.\d+\s+\d+\s'),
    # CoreDNS: self-contained, no leading timestamp of its own
    re.compile(r'^\[INFO\]\s+\d{1,3}(?:\.\d{1,3}){3}:\d+\s+-\s+\d+\s+"'),
    # Bare client-ip access log (k8s probe / ingress)
    re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}\s+-\s+-\s+\[\d{1,2}/[A-Za-z]{3}/\d{4}'),
    # Apache/CASA combined log with the bracketed timestamp leading the line
    re.compile(r'^\[\d{1,2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2}\s[+-]\d{4}\]'),
    # PostgreSQL bracketed local time
    re.compile(r'^\[\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}\]\s+\[\w+\]'),
    # Syslog with a leading PRI - "<134>Sep  3 09:20:01 host ...". Every
    # network appliance that speaks syslog over the wire sends this, and
    # without it a whole firewall feed collapsed into one record.
    re.compile(r'^<\d{1,3}>'),
    # CEF and LEEF: self-identifying headers, one record per line by
    # specification.
    re.compile(r'^(?:CEF|LEEF):\d'),
    # A JSON object at column 0 - one event per line (NDJSON) or the opening
    # brace of a pretty-printed one, whose inner lines are indented and so
    # still read as continuations.
    re.compile(r'^\{'),
    # Fortinet and the other key=value appliance formats, which lead with
    # their own date/time keys rather than a bare timestamp.
    re.compile(r'^(?:date|time|devname|devid|itime)=\S'),
    # Bracketed ISO timestamp ending in Z - Envoy's access log. The JVM GC
    # pattern above looks similar but requires a +HHMM offset, so this shape
    # was falling through it.
    re.compile(r'^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.,]\d+)?Z\]'),
    # ctime, as used by a number of appliance and Windows-side agents:
    # "Wed Sep  3 09:16:11 2026".
    re.compile(r'^[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+'
               r'\d{2}:\d{2}:\d{2}\s+\d{4}\b'),
]


def _is_record_start(line: str) -> bool:
    stripped = line.lstrip('﻿')
    return any(p.match(stripped) for p in _RECORD_START_PATTERNS)


def reassemble(lines: Iterable[str]) -> Iterator[str]:
    """Groups physical lines into logical records; each yielded record is a
    single string that may itself contain embedded newlines. Blank lines are
    dropped rather than treated as boundaries, since real multi-line records
    (a Windows Security Event's body, in particular) contain blank lines
    internally - the next matched header line is what actually closes a
    record, not whitespace."""
    buffer = []
    for raw_line in lines:
        line = raw_line.rstrip('\n').rstrip('\r')
        if not line.strip():
            continue
        if _is_record_start(line):
            if buffer:
                yield '\n'.join(buffer)
            buffer = [line]
        elif buffer:
            buffer.append(line)
        else:
            # Stream/file opens mid-record (rare, e.g. a truncated export) -
            # start a synthetic record instead of silently dropping the line.
            buffer = [line]
    if buffer:
        yield '\n'.join(buffer)


def reassemble_text(text: str) -> Iterator[str]:
    """Convenience wrapper for an already-loaded string (e.g. a whole file
    read into memory) rather than a line iterator."""
    return reassemble(text.splitlines())
