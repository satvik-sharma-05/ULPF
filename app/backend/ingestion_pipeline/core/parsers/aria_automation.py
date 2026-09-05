"""
parsers/aria_automation.py - VMware Aria Automation microservices: the
Spring Boot services (event-broker, abx-service, catalog-service-app,
project-service, migration-service, vco, tango-blueprint, ...), their
embedded reactor-netty access-log lines, the `vracli` CLI tool, and the
appliance-level `[LEVEL][ts][host] msg` bracket log.
"""

import re
from typing import Any, Dict, Optional

from .base import first_line, normalize_severity, normalize_timestamp, parse_kv_bag

#  Only the envelope (timestamp, level, service name, key='value' bag) is
#  fixed; what follows the bag varies by service:
#    "logger.method:110 - msg"     (event-broker, abx-service, vco, ...)
#    "- 10.244.2.1 - - [...] ..."  (catalog/user-profile/cgs-service access
#                                   logs: an empty logger field, so the line
#                                   goes straight from the bag to "- msg")
#    "GET /health HTTP/1.1 200 ..." (terraform-service: no logger, no dash
#                                    separator at all)
#  so the tail is captured whole and picked apart in Python rather than one
#  regex trying to cover all three shapes at once (that risked the access
#  log's IP address being swallowed as a bogus "logger" name).
_RE_ARIA_SPRINGBOOT = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+"
    r"(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|TRACE)\s+(?P<service>[\w-]+)\s+\[(?P<kv>host=.*?)\]\s+(?P<tail>.*)$"
)
_RE_LOGGER_DASH_MSG = re.compile(r"^(?:\{\}\s+)?(?P<logger>[\w.$]+)(?::(?P<line>\d+))?\s*-\s*(?P<msg>.*)$")
_RE_REACTOR_ACCESS = re.compile(
    r'^-?\s*(?P<ip>\S+)\s+-\s+-\s+\[(?P<ts>[^\]]+)\]\s+"(?P<method>\S+)\s+(?P<path>\S+)\s+[^"]*"\s+'
    r'(?P<status>\d+)\s+(?P<respsize>\S+)\s+(?:(?P<upstreamsize>\d+)\s+)?(?P<duration>\d+)\s*ms$'
)
#  Python services (idem-service-worker, ...) tag the tail with their own
#  "[file.py:line - function()]" locator instead of a Java "logger:line -".
_RE_PYTHON_LOGGER_TAG = re.compile(
    r'^\[(?P<file>[\w.]+):(?P<line>\d+)\s*-\s*(?P<func>\w+)\(\)\]\s*(?P<msg>.*)$'
)


def detect_aria_automation_springboot(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_ARIA_SPRINGBOOT.match(first_line(record))
    if not m:
        return None
    kv = parse_kv_bag(m.group('kv'))
    tail = m.group('tail').strip()
    attributes = {k: v for k, v in kv.items() if v}
    base = {
        'timestamp': normalize_timestamp(m.group('ts')),
        'hostname': kv.get('host'),
        'source_type': 'AriaAutomation',
        'process': m.group('service'),
        'severity': normalize_severity(m.group('level')),
    }

    access = _RE_REACTOR_ACCESS.match(tail)
    if access:
        status = int(access.group('status'))
        attributes.update({
            'client_ip': access.group('ip'), 'http_method': access.group('method'),
            'http_path': access.group('path'), 'http_status': status,
            'duration_ms': access.group('duration'),
        })
        base.update({
            'component': 'http-access',
            'severity': 'ERROR' if status >= 500 else ('WARNING' if status >= 400 else 'INFO'),
            'message': f"{access.group('method')} {access.group('path')} {status}",
            'attributes': attributes,
            'entities': [access.group('ip')],
        })
        return base

    py_tag = _RE_PYTHON_LOGGER_TAG.match(tail)
    if py_tag:
        attributes['source_line'] = f"{py_tag.group('file')}:{py_tag.group('line')}"
        base.update({
            'component': py_tag.group('func'),
            'message': py_tag.group('msg').strip(),
            'attributes': attributes,
            'entities': [v for k, v in kv.items() if k in ('user', 'trace') and v],
        })
        return base

    logger_match = _RE_LOGGER_DASH_MSG.match(tail)
    if logger_match and not logger_match.group('logger').replace('.', '').isdigit():
        base.update({
            'component': kv.get('component') or logger_match.group('logger'),
            'message': logger_match.group('msg').strip(),
            'attributes': attributes,
            'entities': [v for k, v in kv.items() if k in ('user', 'trace') and v],
        })
        return base

    # No logger token at all (terraform-service's bare "GET /path ..." lines)
    base.update({
        'component': kv.get('component') or 'general',
        'message': tail,
        'attributes': attributes,
        'entities': [v for k, v in kv.items() if k in ('user', 'trace') and v],
    })
    return base


_RE_VRACLI = re.compile(r'^\[vracli\]\s+\[(?P<level>\w+)\]\s*(?P<msg>.*)$')


def detect_vracli(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_VRACLI.match(first_line(record))
    if not m:
        return None
    return {
        'source_type': 'AriaAutomation',
        'process': 'vracli',
        'component': 'vracli',
        'severity': normalize_severity(m.group('level')) or 'DEBUG',
        'message': m.group('msg').strip(),
        'attributes': {},
    }


#  The appliance's own Python maintenance scripts (log cleanup, replica
#  count checks, the applicator scheduler, ...) log as "<ts,ms> [<logger>
#  ]?[LEVEL] msg" - no [vracli] tag, no host bracket, logger name optional.
_RE_APPLIANCE_PY_SCRIPT_LOG = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2},\d+)\s+(?:(?P<logger>[\w.]+)\s+)?'
    r'\[(?P<level>\w+)\]\s*(?P<msg>.*)$'
)


def detect_appliance_py_script_log(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_APPLIANCE_PY_SCRIPT_LOG.match(first_line(record))
    if not m:
        return None
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'source_type': 'AriaAutomation',
        'process': 'appliance-script',
        'component': m.group('logger') or 'general',
        'severity': normalize_severity(m.group('level')) or 'INFO',
        'message': m.group('msg').strip(),
        'attributes': {},
    }


_RE_ARIA_APPLIANCE_BRACKET = re.compile(
    r'^\[(?P<level>\w+)\]\[(?P<ts>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})\]\[(?P<host>[^\]]+)\]\s*(?P<msg>.*)$'
)


#  Some services (migration-service, ...) log their own Spring Boot actuator
#  health-check requests in a plain "<ts> GMT METHOD path proto status
#  key='value' ..." line instead of the bracketed envelope above.
_RE_SPRING_ACTUATOR_ACCESS = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})\s+GMT\s+'
    r'(?P<method>\S+)\s+(?P<path>\S+)\s+\S+\s+(?P<status>\d+)\s+(?P<kv>.*)$'
)


def detect_spring_actuator_access(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_SPRING_ACTUATOR_ACCESS.match(first_line(record))
    if not m:
        return None
    kv = parse_kv_bag(m.group('kv'))
    status = int(m.group('status'))
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'source_type': 'AriaAutomation',
        'process': 'actuator',
        'component': 'http-access',
        'severity': 'ERROR' if status >= 500 else ('WARNING' if status >= 400 else 'INFO'),
        'message': f"{m.group('method')} {m.group('path')} {status}",
        'attributes': {
            'http_method': m.group('method'), 'http_path': m.group('path'), 'http_status': status,
            'thread': kv.get('thread'), 'duration_us': kv.get('duration'), 'bytes': kv.get('bytes'),
        },
    }


def detect_aria_appliance_bracket(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_ARIA_APPLIANCE_BRACKET.match(first_line(record))
    if not m:
        return None
    return {
        'timestamp': normalize_timestamp(m.group('ts')),
        'hostname': m.group('host'),
        'source_type': 'AriaAutomation',
        'process': 'appliance',
        'component': 'appliance-config',
        'severity': normalize_severity(m.group('level')) or 'INFO',
        'message': m.group('msg').strip(),
        'attributes': {},
    }


#  Aria/vRealize Lifecycle Manager's classic Spring Boot logback layout
#  (%d %-5level %host --- [%thread] %-40logger [%method] : %msg), padding
#  and all - a different house style from the event-broker-style services
#  in aria_automation_springboot above.
_RE_VRLCM_CLASSIC_LOGBACK = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+(?P<level>\w+)\s+(?P<host>\S+)\s+---\s+'
    r'\[(?P<thread>[^\]]*)\]\s+(?P<logger>\S+)\s+\[(?P<method>[^\]]*)\]\s*:\s*(?P<msg>.*)$'
)
#  vRLCM's *other* house style: no host field, pid appended to "vrlcm"
#  itself, and a bare "--" separator instead of "--- ... :".
_RE_VRLCM_PID_STYLE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+(?P<level>\w+)\s+vrlcm\[(?P<pid>\d+)\]\s+'
    r'\[(?P<thread>[^\]]*)\]\s+\[(?P<logger>[^\]]*)\]\s+--\s*(?P<msg>.*)$'
)


def detect_vrlcm_classic_logback(record: str) -> Optional[Dict[str, Any]]:
    line = first_line(record)
    m = _RE_VRLCM_CLASSIC_LOGBACK.match(line)
    if m:
        return {
            'timestamp': normalize_timestamp(m.group('ts')),
            'hostname': m.group('host'),
            'source_type': 'AriaAutomation',
            'process': 'vrlcm',
            'component': m.group('logger'),
            'severity': normalize_severity(m.group('level')),
            'message': m.group('msg').strip(),
            'attributes': {'thread': m.group('thread'), 'method': m.group('method')},
        }
    m = _RE_VRLCM_PID_STYLE.match(line)
    if m:
        return {
            'timestamp': normalize_timestamp(m.group('ts')),
            'source_type': 'AriaAutomation',
            'process': 'vrlcm',
            'pid': int(m.group('pid')),
            'component': m.group('logger'),
            'severity': normalize_severity(m.group('level')),
            'message': m.group('msg').strip(),
            'attributes': {'thread': m.group('thread')},
        }
    return None


#  RabbitMQ's own log (the "ebs pub/sub" broker backing event-broker's
#  message queues): "<ts.microsec+offset> [level] <erlang-pid> msg".
_RE_RABBITMQ_LOG = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}\.\d+)(?P<off>[+-]\d{2}:\d{2})\s+'
    r'\[(?P<level>\w+)\]\s+(?P<epid><[\d.]+>)\s+(?P<msg>.*)$'
)


def detect_rabbitmq_log(record: str) -> Optional[Dict[str, Any]]:
    m = _RE_RABBITMQ_LOG.match(first_line(record))
    if not m:
        return None
    return {
        'timestamp': normalize_timestamp(m.group('ts') + m.group('off')),
        'source_type': 'AriaAutomation',
        'process': 'rabbitmq',
        'component': 'ebs-pub-sub',
        'severity': normalize_severity(m.group('level')) or 'INFO',
        'message': m.group('msg').strip(),
        'attributes': {'erlang_pid': m.group('epid')},
    }
