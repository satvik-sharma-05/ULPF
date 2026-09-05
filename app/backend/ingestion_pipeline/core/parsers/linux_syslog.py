"""
parsers/linux_syslog.py - Plain Linux/appliance syslog, in the two shapes
this corpus actually uses:

  Placeholder style (ISO timestamp, RFC5424-lite): <ts> <host> <app> - - -  <msg>
  (audispd's `type=X msg=audit(epoch:seq): k=v ...` audit records, plus
  bare kernel/sudo lines that share the same "- - -" NILVALUE placeholders)

  RFC3164 wrapper: <Mon> <DD> <HH:MM:SS> <host> <app>[<pid>]: <rest>
  <rest> is then peeled again depending on <app> - Configurator and hms
  carry their own embedded timestamp+level+logger, minio-logs wraps a
  Python-repr JSON blob, the kernel wraps auditd/AppArmor records, sudo and
  systemd wrap PAM session events, and anything else (cron, ...) is just
  plain text.

The PAM/sudo/kernel peeling matters more than it looks: those lines are the
only record in this corpus of *who* ran *what* as root on the appliances, so
leaving them as opaque message text (which is what the generic fall-through
did) meant the graph had no edge for the single most security-relevant thing
in the whole dataset.
"""

import ast
import re
from typing import Any, Dict, Optional

from .base import (
    ISO_TS, first_line, guess_severity_from_text, normalize_severity,
    normalize_timestamp, unescape_syslog_octal,
)

#  RFC5424-lite NILVALUE placeholders: APP-NAME PROCID MSGID SD-DATA MSG.
#  PROCID is sometimes a real pid (systemd's own pid 1, cron's pid) and
#  sometimes '-'; MSGID and SD-DATA are '-' for every process seen here.
_RE_APPLIANCE_PLACEHOLDER = re.compile(
    r'^(?P<ts>' + ISO_TS + r')\s+(?P<host>\S+)\s+(?P<proc>[\w.\-]+)\s+(?P<procid>\d+|-)\s+-\s+-\s*(?P<msg>.*)$'
)
#  audispd quotes with single quotes (msg='op=PAM:session_open ... acct="root"')
#  while the kernel's AppArmor records use double quotes (apparmor="ALLOWED"
#  operation="open" profile="/usr/sbin/sssd"). Both forms are matched here so a
#  value never arrives still wrapped in its own quotes - `apparmor="ALLOWED"`
#  parsed as the literal string '"ALLOWED"' would silently fail every
#  comparison downstream.
_RE_AUDIT_KV = re.compile(r"""(\w+)=(?:'([^']*)'|"([^"]*)"|([^\s]+))""")


def _audit_kv_pairs(body: str):
    """Yields (key, value) from an auditd/AppArmor key=value body, picking
    whichever of the three quoting styles actually matched."""
    for key, single_quoted, double_quoted, bare in _RE_AUDIT_KV.findall(body):
        # Checked by "is it the group that participated" rather than
        # truthiness, so a legitimately empty value (msg='') is preserved
        # instead of falling through to the next alternative.
        if single_quoted:
            value = single_quoted
        elif double_quoted:
            value = double_quoted
        else:
            value = bare
        yield key, value


def detect_appliance_placeholder_syslog(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_APPLIANCE_PLACEHOLDER.match(first_line(record))
    if not m:
        return None
    proc = m.group('proc')
    msg = m.group('msg').strip()
    attributes: Dict[str, Any] = {}
    source_type = 'Syslog'
    severity = guess_severity_from_text(msg)

    if proc == 'audispd':
        source_type = 'LinuxAudit'
        am = re.match(r'^type=(?P<type>\w+)\s+msg=audit\((?P<epoch>[\d.]+):(?P<seq>\d+)\):\s*(?P<body>.*)$', msg)
        if am:
            attributes['audit_type'] = am.group('type')
            attributes['audit_epoch'] = am.group('epoch')
            attributes['audit_seq'] = am.group('seq')
            for key, value in _audit_kv_pairs(am.group('body')):
                attributes[key] = value
            severity = severity or ('WARNING' if attributes.get('res') not in (None, 'success') else 'INFO')

    procid = m.group('procid')
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'hostname': m.group('host'),
        'source_type': source_type,
        'process': proc,
        'pid': int(procid) if procid.isdigit() else None,
        'component': proc,
        'severity': severity,
        'message': msg,
        'attributes': attributes,
        'entities': [v for k, v in attributes.items() if k in ('acct', 'exe') and v],
    }


_RE_RFC3164_WRAPPER = re.compile(
    r'^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})\s+'
    # proc is sometimes wrapped in its own parens, e.g. "(systemd):", "(sd-pam):"
    r'(?P<host>\S+)\s+\(?(?P<proc>[\w.\-]+)\)?(?:\[(?P<pid>\d+)\])?:?\s?(?P<rest>.*)$'
)
_RE_CONFIGURATOR_INNER = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2},\d+)\s+(?P<level>\w+)\s+\((?P<thread>[^)]*)\)\s+'
    r'\[(?P<ctx>[^\]]*)\]\s+(?P<logger>[\w.$]+)\s*-\s*(?P<msg>.*)$'
)
_RE_WRAPPED_APP_LOG_INNER = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}\.\d+)\s+(?P<level>\w+)\s+'
    r'(?P<logger>\S+)\s+\[(?P<thread>[^\]]*)\]\s+\((?P<ctx>[^)]*)\)\s+\[(?P<tags>[^\]]*)\]\s*\|\s*(?P<msg>.*)$'
)
_RE_WRAPPED_KV_TAG = re.compile(r'(\w[\w-]*)\s*[:=]\s*([^;,\]]+)')

#  kernel: [16318586.807516] audit: type=1400 audit(1781496896.937:744688):
#          apparmor="ALLOWED" operation="open" profile="/usr/sbin/sssd" ...
#  The bracketed value is kernel uptime, not a wall-clock time, so the outer
#  syslog timestamp stays authoritative here (unlike the appliance formats
#  above, whose inner timestamp is a real one and wins).
_RE_KERNEL_AUDIT = re.compile(
    r'^\[(?P<uptime>[\d.]+)\]\s+audit:\s+type=(?P<type>\d+)\s+'
    r'audit\((?P<epoch>[\d.]+):(?P<seq>\d+)\):\s*(?P<body>.*)$'
)
_RE_KERNEL_BRACKET = re.compile(r'^\[(?P<uptime>[\d.]+)\]\s*(?P<msg>.*)$')

#  pam_unix(sudo:session): session opened for user root(uid=0) by root(uid=0)
#  pam_unix(systemd-user:session): session closed for user root
_RE_PAM_SESSION = re.compile(
    r'^pam_unix\((?P<pam_service>[\w\-]+):(?P<pam_type>\w+)\):\s+'
    r'session\s+(?P<action>opened|closed)\s+for\s+user\s+'
    r'(?P<user>[\w.\-$\\]+)(?:\(uid=(?P<uid>\d+)\))?'
    r'(?:\s+by\s+(?P<by_user>[\w.\-$\\]+)(?:\(uid=(?P<by_uid>\d+)\))?)?'
)

#  sudo:     root : PWD=/ ; USER=root ; COMMAND=/usr/bin/systemctl status
_RE_SUDO_COMMAND = re.compile(
    r'^\s*(?P<user>[\w.\-$\\]+)\s*:\s*(?P<kv>(?:\w+=[^;]*;\s*)*\w+=.*)$'
)


def _parse_kernel_audit(rest: str, host: str, ts_raw: str) -> Optional[Dict[str, Any]]:
    """Kernel-emitted auditd/AppArmor records. Same key=value body as audispd,
    so it reuses _RE_AUDIT_KV and lands the same attribute names - meaning an
    AppArmor denial and an audispd syscall record become queryable together."""
    m = _RE_KERNEL_AUDIT.match(rest)
    if not m:
        return None
    attributes: Dict[str, Any] = {
        'audit_type_id': m.group('type'),
        'audit_epoch': m.group('epoch'),
        'audit_seq': m.group('seq'),
        'kernel_uptime_s': m.group('uptime'),
    }
    for key, value in _audit_kv_pairs(m.group('body')):
        attributes[key] = value

    apparmor = (attributes.get('apparmor') or '').upper()
    if apparmor in ('DENIED', 'AUDIT'):
        severity = 'WARNING'
    elif apparmor == 'ALLOWED':
        severity = 'INFO'
    else:
        severity = guess_severity_from_text(m.group('body')) or 'INFO'

    return {
        'timestamp': normalize_timestamp(ts_raw),
        'hostname': host,
        'source_type': 'LinuxAudit',
        'process': 'kernel',
        'component': 'apparmor' if 'apparmor' in attributes else 'audit',
        'severity': severity,
        'message': m.group('body').strip(),
        'attributes': attributes,
    }


def _parse_pam_session(rest: str, host: str, proc: str, pid: Optional[str],
                       ts_raw: str) -> Optional[Dict[str, Any]]:
    """`session opened/closed for user X by Y` - the login audit trail."""
    m = _RE_PAM_SESSION.match(rest)
    if not m:
        return None
    attributes = {
        'pam_service': m.group('pam_service'),
        'pam_type': m.group('pam_type'),
        'action': m.group('action'),
        'acct': m.group('user'),
        'uid': m.group('uid'),
        'by_user': m.group('by_user'),
        'by_uid': m.group('by_uid'),
    }
    return {
        'timestamp': normalize_timestamp(ts_raw),
        'hostname': host,
        'source_type': 'LinuxAudit',
        'process': proc,
        'pid': int(pid) if pid and pid.isdigit() else None,
        'component': 'pam-session',
        'severity': 'NOTICE',
        'message': rest.strip(),
        'attributes': {k: v for k, v in attributes.items() if v},
    }


def _parse_sudo_command(rest: str, host: str, proc: str, ts_raw: str) -> Optional[Dict[str, Any]]:
    """`root : PWD=/ ; USER=root ; COMMAND=/usr/bin/foo` - privilege escalation
    with the target account and the exact command line."""
    m = _RE_SUDO_COMMAND.match(rest)
    if not m or 'COMMAND=' not in rest:
        return None
    attributes: Dict[str, Any] = {'acct': m.group('user')}
    for part in m.group('kv').split(';'):
        if '=' not in part:
            continue
        key, _, value = part.partition('=')
        key = key.strip().lower()
        value = value.strip()
        if not value:
            continue
        if key == 'command':
            attributes['exe'] = value.split(' ', 1)[0]
            attributes['command'] = value
        elif key == 'user':
            attributes['target_user'] = value
        else:
            attributes[key] = value
    return {
        'timestamp': normalize_timestamp(ts_raw),
        'hostname': host,
        'source_type': 'LinuxAudit',
        'process': proc,
        'component': 'sudo-command',
        'severity': 'NOTICE',
        'message': rest.strip(),
        'attributes': attributes,
    }


def detect_rfc3164_wrapper(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_RFC3164_WRAPPER.match(first_line(record))
    if not m:
        return None
    proc = m.group('proc')
    rest = m.group('rest')
    host = m.group('host')
    ts_raw = f"{m.group('mon')} {m.group('day')} {m.group('time')}"

    if proc == 'minio-logs' and rest.strip().startswith('{'):
        result = _parse_minio_json(rest.strip(), host, ts_raw)
        if result:
            return result

    if proc == 'kernel':
        result = _parse_kernel_audit(rest, host, ts_raw)
        if result:
            return result
        # Non-audit kernel lines still carry the uptime bracket; strip it so
        # the message reads as the kernel actually wrote it.
        km = _RE_KERNEL_BRACKET.match(rest)
        if km:
            msg = km.group('msg').strip()
            return {
                'timestamp': normalize_timestamp(ts_raw),
                'hostname': host,
                'source_type': 'LinuxKernel',
                'process': 'kernel',
                'component': 'kernel',
                'severity': guess_severity_from_text(msg),
                'message': msg,
                'attributes': {'kernel_uptime_s': km.group('uptime')},
            }

    if rest.startswith('pam_unix('):
        result = _parse_pam_session(rest, host, proc, m.group('pid'), ts_raw)
        if result:
            return result

    if proc in ('sudo', 'su'):
        result = _parse_sudo_command(rest, host, proc, ts_raw)
        if result:
            return result

    if proc == 'Configurator':
        cm = _RE_CONFIGURATOR_INNER.match(rest)
        if cm:
            return {
                'timestamp': normalize_timestamp(cm.group('ts')),
                'hostname': host,
                'source_type': 'Horizon',
                'process': 'Configurator',
                'component': cm.group('logger'),
                'severity': normalize_severity(cm.group('level')) or 'INFO',
                'message': cm.group('msg').strip(),
                'attributes': {'thread': cm.group('thread')},
            }

    wm = _RE_WRAPPED_APP_LOG_INNER.match(rest)
    if wm:
        return _parse_wrapped_app_log(wm, host, proc)

    # Generic RFC3164 fall-through (sudo, cron, systemd, etc.)
    msg = rest.strip()
    return {
        'timestamp': normalize_timestamp(ts_raw),
        'hostname': host,
        'source_type': 'Syslog',
        'process': proc,
        'pid': int(m.group('pid')) if m.group('pid') else None,
        'component': proc,
        'severity': guess_severity_from_text(msg),
        'message': msg,
        'attributes': {},
    }


def _parse_minio_json(body: str, host: str, ts_raw: str) -> Optional[Dict[str, Any]]:
    try:
        data = ast.literal_eval(body)
    except (ValueError, SyntaxError):
        return None
    if not isinstance(data, dict):
        return None
    api = data.get('api', {}) if isinstance(data.get('api'), dict) else {}
    status_code = api.get('statusCode')
    severity = 'ERROR' if isinstance(status_code, int) and status_code >= 500 else (
        'WARNING' if isinstance(status_code, int) and status_code >= 400 else 'INFO')
    return {
        'timestamp': data.get('time') or normalize_timestamp(ts_raw),
        'hostname': host,
        'source_type': 'MinIO',
        'process': 'minio',
        'component': 'S3-API',
        'severity': severity,
        'message': f"{api.get('name', 'Unknown')} {data.get('trigger', '')} "
                   f"{api.get('bucket', '')}/{api.get('object', '')} -> {api.get('status', '')}".strip(),
        'attributes': {
            'api_name': api.get('name'), 'bucket': api.get('bucket'), 'object': api.get('object'),
            'status_code': status_code, 'remotehost': data.get('remotehost'),
            'request_id': data.get('requestID'), 'user_agent': data.get('userAgent'),
            'access_key': data.get('accessKey'), 'parent_user': data.get('parentUser'),
        },
        'entities': [e for e in (data.get('remotehost'), data.get('accessKey')) if e],
    }


def _parse_wrapped_app_log(wm: re.Match, host: str, proc: str) -> Dict[str, Any]:
    msg = unescape_syslog_octal(wm.group('msg'))
    attributes = {'thread': wm.group('thread'), 'context': wm.group('ctx')}
    for k, v in _RE_WRAPPED_KV_TAG.findall(wm.group('tags')):
        attributes[k] = v.strip()
    pipe_body = msg.split('|', 1)[1].strip() if '|' not in wm.group('tags') and '|' in msg else msg
    for k, v in _RE_WRAPPED_KV_TAG.findall(pipe_body):
        if k in ('operationID', 'sessionID', 'method', 'target', 'user', 'client', 'moid'):
            attributes[k] = v.strip()
    return {
        'timestamp': normalize_timestamp(wm.group('ts')),
        'hostname': host,
        'source_type': 'HMS',
        'process': proc,
        'component': wm.group('logger'),
        'severity': normalize_severity(wm.group('level')) or guess_severity_from_text(msg),
        'message': msg,
        'attributes': attributes,
    }
