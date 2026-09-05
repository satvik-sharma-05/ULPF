"""
parsers/platform_services.py - Supporting platform services that show up
underneath the VMware workloads: CoreDNS, the Kubernetes ingress/probe
access log, kubelet's glog-style output, Squid's proxy access log,
PostgreSQL/repmgr, and JVM unified GC logging.
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .base import normalize_severity, normalize_timestamp, first_line

_RE_CORE_DNS = re.compile(
    r'^\[INFO\]\s+(?P<client>\S+)\s+-\s+(?P<qid>\d+)\s+"(?P<qtype>\S+)\s+IN\s+(?P<qname>\S+)\.\s+'
    r'(?P<proto>\w+)\s+(?P<size>\d+)\s+\w+\s+\d+"\s+(?P<rcode>\w+)\s+(?P<flags>\S+)\s+(?P<respsize>\d+)\s+'
    r'(?P<duration>[\d.]+)s$'
)


def detect_coredns(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_CORE_DNS.match(first_line(record))
    if not m:
        return None
    return {
        'source_type': 'CoreDNS',
        'process': 'coredns',
        'component': 'dns',
        'severity': 'WARNING' if m.group('rcode') != 'NOERROR' else 'INFO',
        'message': f"{m.group('qtype')} {m.group('qname')} -> {m.group('rcode')}",
        'attributes': {
            'client': m.group('client'), 'qname': m.group('qname'), 'qtype': m.group('qtype'),
            'rcode': m.group('rcode'), 'duration_s': m.group('duration'),
        },
        'entities': [m.group('client')],
    }


_RE_K8S_ACCESS = re.compile(
    r'^(?P<ip>\d{1,3}(?:\.\d{1,3}){3})\s+-\s+-\s+\[(?P<ts>[^\]]+)\]\s+'
    r'"(?P<method>\S+)\s+(?P<path>\S+)\s+[^"]*"\s+(?P<status>\d+)\s+(?P<bytes>\S+)'
    r'(?:\s+"(?P<referrer>[^"]*)"\s+"(?P<ua>[^"]*)")?'
)


def detect_k8s_access(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_K8S_ACCESS.match(first_line(record))
    if not m:
        return None
    status = int(m.group('status'))
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'source_type': 'Kubernetes',
        'process': 'ingress',
        'component': 'http-access',
        'severity': 'ERROR' if status >= 500 else ('WARNING' if status >= 400 else 'INFO'),
        'message': f"{m.group('method')} {m.group('path')} {status}",
        'attributes': {
            'client_ip': m.group('ip'), 'http_method': m.group('method'), 'http_path': m.group('path'),
            'http_status': status, 'bytes': m.group('bytes'), 'user_agent': m.group('ua'),
        },
        'entities': [m.group('ip')],
    }


_RE_KUBELET_GLOG = re.compile(
    r'^(?P<level>[EWIF])(?P<mmdd>\d{4})\s+(?P<time>\d{2}:\d{2}:\d{2}\.\d+)\s+(?P<pid>\d+)\s+'
    r'(?P<file>\S+\.go:\d+)\]\s*(?P<msg>.*)$'
)
_GLOG_LEVELS = {'E': 'ERROR', 'W': 'WARNING', 'I': 'INFO', 'F': 'FATAL'}


def detect_kubelet_glog(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_KUBELET_GLOG.match(first_line(record))
    if not m:
        return None
    ts_raw = f"{m.group('mmdd')} {m.group('time')}"
    return {
        'timestamp': normalize_timestamp(ts_raw),
        'source_type': 'Kubernetes',
        'process': 'kubelet',
        'pid': int(m.group('pid')),
        'component': m.group('file'),
        'severity': _GLOG_LEVELS.get(m.group('level'), 'INFO'),
        'message': m.group('msg').strip(),
        'attributes': {},
    }


_RE_SQUID = re.compile(
    r'^(?P<epoch>\d{9,10}\.\d+)\s+(?P<duration>\d+)\s+(?P<client>\S+)\s+'
    r'(?P<status>\w+)/(?P<code>\d+)\s+(?P<bytes>\d+)\s+(?P<method>\S+)\s+(?P<url>\S+)\s+'
    r'(?P<ident>\S+)\s+(?P<peer>\S+)/(?P<peerhost>\S+)\s+(?P<ctype>\S+)$'
)


def detect_squid_access(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_SQUID.match(first_line(record))
    if not m:
        return None
    code = int(m.group('code'))
    dt = datetime.fromtimestamp(float(m.group('epoch')), tz=timezone.utc)
    return {
        'timestamp': dt.isoformat(),
        'source_type': 'Squid',
        'process': 'squid',
        'component': 'proxy',
        'severity': 'ERROR' if code >= 500 else ('WARNING' if code >= 400 or code == 0 else 'INFO'),
        'message': f"{m.group('method')} {m.group('url')} {m.group('status')}/{code}",
        'attributes': {
            'client': m.group('client'), 'status': m.group('status'), 'code': code,
            'duration_ms': m.group('duration'), 'bytes': m.group('bytes'),
        },
        'entities': [m.group('client')],
    }


_RE_POSTGRES_LOG = re.compile(
    r'^\[(?P<ts>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})\]\s+\[(?P<level>\w+)\]\s*(?P<msg>.*)$'
)


def detect_postgres_log(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_POSTGRES_LOG.match(first_line(record))
    if not m:
        return None
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'source_type': 'PostgreSQL',
        'process': 'postgres',
        'component': 'repmgr',
        'severity': normalize_severity(m.group('level')) or 'DEBUG',
        'message': m.group('msg').strip(),
        'attributes': {},
    }


#  PostgreSQL's own server log (distinct from repmgr's bracketed wrapper
#  above): "<ts.mmm> UTC [<pid>] LEVEL:  msg", where a LOG/DETAIL/STATEMENT
#  trio commonly describes the same event across consecutive lines.
_RE_POSTGRES_NATIVE_LOG = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}\.\d+)\s+UTC\s+\[(?P<pid>\d+)\]\s+'
    # log_line_prefix commonly carries "%u@%d " - the connected role and
    # the database - between the pid and the level. Optional, because the
    # stock prefix omits it.
    r'(?:(?P<db_user>[^\s@]+)@(?P<db_name>\S+)\s+)?'
    r'(?P<level>[A-Z]+):\s*(?P<msg>.*)$'
)
_POSTGRES_LEVEL_SEVERITY = {
    'PANIC': 'EMERGENCY', 'FATAL': 'FATAL', 'ERROR': 'ERROR', 'WARNING': 'WARNING',
    'NOTICE': 'NOTICE', 'LOG': 'INFO', 'DETAIL': 'INFO', 'STATEMENT': 'DEBUG', 'HINT': 'INFO',
}


def detect_postgres_native_log(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_POSTGRES_NATIVE_LOG.match(first_line(record))
    if not m:
        return None
    level = m.group('level')
    return {
        'timestamp': normalize_timestamp(m.group('ts') + '+0000'),
        'source_type': 'PostgreSQL',
        'process': 'postgres',
        'pid': int(m.group('pid')),
        'component': 'server',
        'severity': _POSTGRES_LEVEL_SEVERITY.get(level, 'INFO'),
        'message': m.group('msg').strip(),
        'attributes': {
            k: v for k, v in (
                ('pg_level', level),
                ('db_user', m.group('db_user')),
                ('db_name', m.group('db_name')),
            ) if v is not None
        },
    }


_RE_JVM_GC = re.compile(
    r'^\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+[+-]\d{4})\]'
    r'(?:\[(?P<uptime>[\d.]+)s\])?(?:\[(?P<level>\w+)\])?(?:\[(?P<tags>[^\]]*)\])?\s*(?P<msg>.*)$'
)


def detect_jvm_gc(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_JVM_GC.match(first_line(record))
    if not m:
        return None
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'source_type': 'JVM_GC',
        'process': 'jvm',
        'component': (m.group('tags') or 'gc').strip(),
        'severity': normalize_severity(m.group('level')) or 'DEBUG',
        'message': m.group('msg').strip(),
        'attributes': {'uptime_s': m.group('uptime')} if m.group('uptime') else {},
    }


#  The log-forwarding agent's own internal C++ debug trace (connection
#  management, CFApiTransport, ...): "<ts.microsec> <thread-id> <level>
#  logger:line | msg" - the logger/line/msg tail is optional, some lines are
#  just a bare level marker with nothing else.
_RE_CFAPI_TRACE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}\.\d+)\s+(?P<tid>0x[0-9a-fA-F]+)\s+'
    r'<(?P<level>\w+)>\s*(?:(?P<logger>[\w.$]+):(?P<line>\d+)\s*\|\s*(?P<msg>.*))?$'
)
_CFAPI_LEVEL_SEVERITY = {'trace': 'DEBUG', 'debug': 'DEBUG', 'info': 'INFO', 'warn': 'WARNING', 'error': 'ERROR'}


def detect_cfapi_trace(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_CFAPI_TRACE.match(first_line(record))
    if not m:
        return None
    return {
        'timestamp': normalize_timestamp(m.group('ts') + '+0000'),
        'source_type': 'LogForwarder',
        'process': 'liagent',
        'component': m.group('logger') or 'core',
        'severity': _CFAPI_LEVEL_SEVERITY.get(m.group('level'), 'DEBUG'),
        'message': (m.group('msg') or '').strip(),
        'attributes': {'thread_id': m.group('tid')},
    }


#  A bare "<ts>Z LEVEL <dotted.logger.name> msg" - no brackets, no host, no
#  thread - used by small Python metrics/health collector scripts.
_RE_BARE_LEVEL_LOGGER = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+'
    r'(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\s+(?P<logger>[\w]+(?:\.[\w]+)+)\s+(?P<msg>.*)$'
)


def detect_bare_level_logger_log(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_BARE_LEVEL_LOGGER.match(first_line(record))
    if not m:
        return None
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'source_type': 'Kubernetes',
        'process': m.group('logger').split('.')[0],
        'component': m.group('logger'),
        'severity': normalize_severity(m.group('level')),
        'message': m.group('msg').strip(),
        'attributes': {},
    }


#  A small Python deployment-health watcher's own dash-chain log: "<ts,ms>
#  - service - LEVEL - msg" (reports degraded/unavailable replica counts).
_RE_DEPLOYMENT_HEALTH_LOG = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2},\d+)\s+-\s+service\s+-\s+'
    r'(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\s+-\s+(?P<msg>.*)$'
)


def detect_deployment_health_log(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_DEPLOYMENT_HEALTH_LOG.match(first_line(record))
    if not m:
        return None
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'source_type': 'Kubernetes',
        'process': 'deployment-health-watcher',
        'component': 'deployment-status',
        'severity': normalize_severity(m.group('level')),
        'message': m.group('msg').strip(),
        'attributes': {},
    }


#  The default Logback/Log4j2 layout that Spring Boot and most JVM services
#  ship with: "<iso ts> LEVEL [thread] logger - message". This is the single
#  most common APPLICATION log line in an enterprise estate, and it was
#  reaching generic_fallback - the level and the logger were sitting in plain
#  sight and being thrown away.
#
#  Deliberately registered near the bottom: the shape is broad, so every
#  vendor-specific application detector above keeps first refusal on its own
#  lines.
_RE_APP_LOGGER = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,]\d+'
    r'(?:Z|[+-]\d{2}:?\d{2})?)\s+'
    r'(?P<level>TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\s+'
    r'\[(?P<thread>[^\]]+)\]\s+'
    r'(?P<logger>[\w$]+(?:\.[\w$]+)*)\s+-\s+(?P<msg>.*)$'
)


def detect_app_logger(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_APP_LOGGER.match(first_line(record))
    if not m:
        return None
    logger_name = m.group('logger')
    return {
        'timestamp': normalize_timestamp(m.group('ts').replace(',', '.')),
        'source_type': 'Application',
        # The tail of the logger is the class that emitted the line, which is
        # what an analyst greps for; the full dotted name is kept as the
        # component so package-level grouping still works.
        'process': logger_name.split('.')[-1],
        'component': logger_name,
        'severity': normalize_severity(m.group('level')) or 'INFO',
        'message': m.group('msg').strip(),
        'attributes': {'thread': m.group('thread')},
    }


#  Upstream Envoy's default access log - what an Istio/service-mesh sidecar
#  writes, and a different format entirely from the syslog-wrapped
#  "envoy-access[pid]:" shape ESXi emits (see parsers/esxi.py). CONTAINER
#  traffic is a named source category, and a 503 with a UF response flag is
#  exactly the kind of event that should never land in generic_fallback.
_RE_ENVOY_UPSTREAM = re.compile(
    r'^\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\]\s+'
    r'"(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+(?P<proto>[\w/.]+)"\s+'
    r'(?P<status>\d{3})\s+(?P<flags>\S+)\s+(?P<rest>.*)$'
)
_RE_QUOTED = re.compile(r'"([^"]*)"')
#  Trailing quoted fields, in the order Envoy's default format emits them.
_ENVOY_QUOTED_FIELDS = ('forwarded_for', 'user_agent', 'request_id',
                        'authority', 'upstream_host')


def detect_envoy_upstream_access(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_ENVOY_UPSTREAM.match(first_line(record))
    if not m:
        return None
    status = int(m.group('status'))
    quoted = _RE_QUOTED.findall(m.group('rest'))
    attrs: Dict[str, Any] = {
        'http_method': m.group('method'),
        'http_path': m.group('path'),
        'http_status': status,
        'response_flags': m.group('flags'),
    }
    for key, value in zip(_ENVOY_QUOTED_FIELDS, quoted):
        if value and value != '-':
            attrs[key] = value
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        # :authority is the service the request was addressed to - the closest
        # thing a sidecar log has to a host identity.
        'hostname': attrs.get('authority'),
        'source_type': 'Envoy',
        'process': 'envoy',
        'component': 'access',
        'severity': 'ERROR' if status >= 500 else ('WARNING' if status >= 400 else 'INFO'),
        'message': '%s %s %d %s' % (m.group('method'), m.group('path'),
                                    status, m.group('flags')),
        'attributes': attrs,
    }
