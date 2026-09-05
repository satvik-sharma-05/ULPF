"""
parsers/esxi.py - ESXi hypervisor daemons: hostd/vpxa/fdm task/Originator
blocks, vmkernel, envoy-access proxy logs, vcenter-server, and the generic
catch-all `<ts> <host> <proc>[<pid>]: <msg>` shape shared by localcli,
healthd, healthdPlugins, lsud, nestdb/nsx-exporter (when NSX-side detectors
in nsx.py don't already claim them), and similar host-agent daemons.
"""

import re
from typing import Any, Dict, Optional

from .base import (
    ISO_TS,
    first_line,
    guess_severity_from_text,
    normalize_severity,
    normalize_timestamp,
    parse_kv_bag,
)

_RE_ESXI_ORIGINATOR = re.compile(
    r'^(?P<ts>' + ISO_TS + r')\s+(?P<host>\S+)\s+(?P<proc>Hostd|Vpxa|Fdm)\[(?P<pid>\d+)\]:\s+'
    r'\[Originator@\d+\s+(?P<kv>[^\]]*)\]\s*(?P<msg>.*)$'
)
#  Same [Originator@...] task-log body, but wrapped with an extra
#  "OuterProc: level innerproc[pid] " prefix and no colon after the pid
#  bracket - seen from Hostd/Vpxa/Fdm/Rhttpproxy/kmxa/healthd/hostd-probe on
#  hosts logging as "localhost.localdomain".
#
#  The outer process sometimes carries its own [pid] too, which healthdPlugins
#  always does ("healthdPlugins[80776912]: error core_services[80776912]
#  [Originator@6876 sub=Default] No PID found for service: vsanmgmtd"). Without
#  the optional bracket below those lines missed this detector, fell through to
#  the generic proc shape, and lost both the `error` level and `sub=Default`.
_RE_ESXI_ORIGINATOR_VERBOSE = re.compile(
    r'^(?P<ts>' + ISO_TS + r')\s+(?P<host>\S+)\s+(?P<proc>[\w\-]+)(?:\[\d+\])?:\s+'
    r'(?P<level>\w+)\s+[\w\-]+\[(?P<pid>\d+)\]\s+'
    r'\[Originator@\d+\s+(?P<kv>[^\]]*)\]\s*(?P<msg>.*)$'
)
#  The same fault/task dump Hostd (or Rhttpproxy) pretty-prints across many
#  physical lines, each independently re-stamped with the same timestamp by
#  the syslog forwarder ("Hostd: --> ..."), rather than joined into one
#  message - so multiline.reassemble() never merges them and each fragment
#  arrives as its own record.
_RE_ESXI_DUMP_CONTINUATION = re.compile(
    r'^(?P<ts>' + ISO_TS + r')\s+(?P<host>\S+)\s+(?P<proc>[\w\-]+):\s+-->\s*(?P<msg>.*)$'
)
#  healthdPlugins reports a plugin's own status directly, with no pid or
#  [Originator@...] block: "healthdPlugins: <plugin> LEVEL msg".
_RE_HEALTHD_PLUGIN_STATUS = re.compile(
    r'^(?P<ts>' + ISO_TS + r')\s+(?P<host>\S+)\s+healthdPlugins:\s+(?P<plugin>\S+)\s+'
    r'(?P<level>ERROR|WARNING|WARN|INFO|DEBUG)\s+(?P<msg>.*)$'
)


def detect_esxi_originator(record: str) -> Optional[Dict[str, Any]]:
    line = first_line(record)
    m = _RE_ESXI_ORIGINATOR.match(line) or _RE_ESXI_ORIGINATOR_VERBOSE.match(line)
    if not m:
        return None
    kv = parse_kv_bag(m.group('kv'))
    msg = m.group('msg').strip()
    severity = normalize_severity(m.groupdict().get('level')) or guess_severity_from_text(msg)
    if not severity and msg.endswith('Status success'):
        severity = 'INFO'
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'hostname': m.group('host'),
        'source_type': 'ESXi',
        'process': m.group('proc'),
        'pid': int(m.group('pid')),
        'component': kv.get('sub', m.group('proc')),
        'severity': severity,
        'message': msg,
        'attributes': kv,
        'entities': [v for k, v in kv.items() if k in ('user', 'sid', 'opID') and v],
    }


def detect_esxi_dump_continuation(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_ESXI_DUMP_CONTINUATION.match(first_line(record))
    if not m:
        return None
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'hostname': m.group('host'),
        'source_type': 'ESXi',
        'process': m.group('proc'),
        'component': 'fault-dump',
        'severity': 'DEBUG',
        'message': m.group('msg').strip(),
        'attributes': {},
    }


def detect_healthd_plugin_status(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_HEALTHD_PLUGIN_STATUS.match(first_line(record))
    if not m:
        return None
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'hostname': m.group('host'),
        'source_type': 'ESXi',
        'process': 'healthdPlugins',
        'component': m.group('plugin'),
        'severity': normalize_severity(m.group('level')),
        'message': m.group('msg').strip(),
        'attributes': {},
    }


_RE_VMKERNEL = re.compile(
    r'^(?P<ts>' + ISO_TS + r')\s+(?P<host>\S+)\s+(?P<proc>vmkernel|vmkwarning):\s*'
    r'(?:cpu(?P<cpu>\d+):(?P<tid>\d+)\))?\s*(?P<msg>.*)$'
)


def detect_vmkernel(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_VMKERNEL.match(first_line(record))
    if not m:
        return None
    msg = m.group('msg').strip()
    proc = m.group('proc')
    severity = guess_severity_from_text(msg) or ('WARNING' if proc == 'vmkwarning' else None)
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'hostname': m.group('host'),
        'source_type': 'ESXi',
        'process': proc,
        'component': proc,
        'severity': severity,
        'message': msg,
        'attributes': {k: v for k, v in (('cpu', m.group('cpu')), ('tid', m.group('tid'))) if v},
    }


_RE_ENVOY_ACCESS = re.compile(
    r'^(?P<ts>' + ISO_TS + r')\s+(?P<host>\S+)\s+envoy-access\[(?P<pid>\d+)\]:\s+'
    r'(?P<method>\S+)\s+(?P<path>\S+)\s+(?P<status>\d+|-)\s+(?P<rest>.*)$'
)


def detect_envoy_access(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_ENVOY_ACCESS.match(first_line(record))
    if not m:
        return None
    status = m.group('status')
    status_num = int(status) if status.isdigit() else None
    severity = 'ERROR' if status_num and status_num >= 500 else ('WARNING' if status_num and status_num >= 400 else 'INFO')
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'hostname': m.group('host'),
        'source_type': 'ESXi',
        'process': 'envoy-access',
        'pid': int(m.group('pid')),
        'component': 'envoy-access',
        'severity': severity,
        'message': f"{m.group('method')} {m.group('path')} {status}",
        'attributes': {
            'http_method': m.group('method'), 'http_path': m.group('path'),
            'http_status': status_num, 'tail': m.group('rest').strip(),
        },
    }


_RE_VCENTER_SERVER = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+(?P<host>\S+)\s+vcenter-server:\s*(?P<msg>.*)$'
)


def detect_vcenter_server(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_VCENTER_SERVER.match(first_line(record))
    if not m:
        return None
    msg = m.group('msg').strip()
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'hostname': m.group('host'),
        'source_type': 'vCenter',
        'process': 'vcenter-server',
        'component': 'vcenter-server',
        'severity': guess_severity_from_text(msg),
        'message': msg,
        'attributes': {},
    }


_RE_ESXI_GENERIC_PROC = re.compile(
    # proc is usually a bare daemon name, but a few scripts log their own
    # full path (e.g. "/vsip_heap_stats.sh[pid]: ...")
    r'^(?P<ts>' + ISO_TS + r')\s+(?P<host>\S+)\s+(?P<proc>[\w./\-]+)\[(?P<pid>\d+)\]:\s*(?P<msg>.*)$'
)
# A few daemons (opslldpvim, ...) put the pid in its own bracket after the
# colon instead of appending it to the process name: "proc: [ 123 ] msg".
_RE_ESXI_GENERIC_PROC_BRACKET_PID = re.compile(
    r'^(?P<ts>' + ISO_TS + r')\s+(?P<host>\S+)\s+(?P<proc>[\w.\-]+):\s+\[\s*(?P<pid>\d+)\s*\]\s*(?P<msg>.*)$'
)
# A leading level, bare ("INFO msg") or bracketed ("[INFO] :: [risadapter.cpp:63]
# :: msg", which is how the `sut` HPE agent writes every line).
_RE_LEADING_LEVEL = re.compile(
    r'^\[?(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL|NOTICE)\]?\b\s*(?:::\s*)?(.*)$'
)
# sut's source locator: "[risadapter.cpp:63] :: message"
_RE_CPP_SOURCE_TAG = re.compile(r'^\[(?P<file>[\w.]+:\d+)\]\s*(?:::\s*)?(?P<msg>.*)$')


def detect_esxi_generic_proc(record: str) -> Optional[Dict[str, Any]]:
    line = first_line(record)
    m = _RE_ESXI_GENERIC_PROC.match(line) or _RE_ESXI_GENERIC_PROC_BRACKET_PID.match(line)
    if not m:
        return None
    msg = m.group('msg').strip()
    proc = m.group('proc')
    severity = None
    attributes: Dict[str, Any] = {}

    lvl_m = _RE_LEADING_LEVEL.match(msg)
    if lvl_m:
        severity = normalize_severity(lvl_m.group(1))
        msg = lvl_m.group(2).strip()

    # Keep the C++ source locator as an attribute instead of leaving it glued
    # to the front of every message (it makes otherwise-identical messages look
    # distinct, which skews any grouping by message text).
    src_m = _RE_CPP_SOURCE_TAG.match(msg)
    component = proc
    if src_m:
        attributes['source_line'] = src_m.group('file')
        component = src_m.group('file').split(':', 1)[0]
        msg = src_m.group('msg').strip()

    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'hostname': m.group('host'),
        'source_type': 'ESXi',
        'process': proc,
        'pid': int(m.group('pid')),
        'component': component,
        'severity': severity or guess_severity_from_text(msg),
        'message': msg,
        'attributes': attributes,
    }


#  esxupdate's own colon-delimited convention: "esxupdate: <pid>: <module>: <LEVEL>: msg"
_RE_ESXUPDATE = re.compile(
    r'^(?P<ts>' + ISO_TS + r')\s+(?P<host>\S+)\s+esxupdate:\s+(?P<pid>\d+):\s+(?P<module>[\w.]+):\s+'
    r'(?P<level>\w+):\s*(?P<msg>.*)$'
)


def detect_esxupdate(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_ESXUPDATE.match(first_line(record))
    if not m:
        return None
    msg = m.group('msg').strip()
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'hostname': m.group('host'),
        'source_type': 'ESXi',
        'process': 'esxupdate',
        'pid': int(m.group('pid')),
        'component': m.group('module'),
        'severity': normalize_severity(m.group('level')) or guess_severity_from_text(msg),
        'message': msg,
        'attributes': {},
    }


#  Last-resort ESXi catch-all: a bare "<ts> <host> <proc>: <msg>" with no
#  pid bracket at all - nsxdavim/nsxaVim's own VM-property change-data dump
#  ("Key=Value" lines, or indented "  key = value," fragments) is the main
#  source of these. Tried only after every more specific ESXi shape above,
#  so it can't shadow anything that actually has more structure to offer.
_RE_ESXI_BARE_PROC = re.compile(
    r'^(?P<ts>' + ISO_TS + r')\s+(?P<host>\S+)\s+(?P<proc>[\w.\-]+):\s*(?P<msg>.*)$'
)


def detect_esxi_bare_proc_message(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_ESXI_BARE_PROC.match(first_line(record))
    if not m:
        return None
    msg = m.group('msg').strip()
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'hostname': m.group('host'),
        'source_type': 'ESXi',
        'process': m.group('proc'),
        'component': m.group('proc'),
        'severity': guess_severity_from_text(msg),
        'message': msg,
        'attributes': {},
    }
