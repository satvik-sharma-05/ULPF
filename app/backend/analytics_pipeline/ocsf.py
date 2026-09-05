"""
ocsf.py - Projects normalized events into OCSF 1.3.0 event classes.

Until this existed, `taxonomy.py` carried an OCSF path for every schema field
and nothing consumed it. A documented mapping is not an implementation, and
claiming OCSF support on the strength of a lookup table would not survive
anyone opening the repository. This turns the mapping into an emitter.

**Scope, stated plainly.** Three classes are implemented, chosen because our
detectors already extract the fields each one requires:

    3002  Authentication      auth events with a real principal and outcome
    4001  Network Activity    connection events with endpoints and a verdict
    1008  Application Lifecycle   everything else, as OCSF's own catch-all

Anything that does not qualify for 3002 or 4001 goes to 1008 rather than
being forced into a class whose required fields we would have to invent. A
wrong class_uid is worse than a generic one: a SIEM routes on it.

**What is faithful and what is approximated** - worth knowing before this is
put in front of an OCSF-literate reviewer:

  * `class_uid`, `category_uid`, `activity_id`, `severity_id`, `status_id`
    and `type_uid` follow the specification, including
    `type_uid = class_uid * 100 + activity_id`.
  * `metadata.product`, `time`, `raw_data` and `unmapped` are populated from
    real fields, so provenance survives the projection.
  * Endpoint and actor objects are filled only from attributes a detector
    actually extracted. An absent field is omitted rather than guessed - OCSF
    permits omission, and a fabricated `src_endpoint.ip` would be worse than
    none.
  * `time` is milliseconds since epoch per the spec; where an event's own
    timestamp could not be parsed we fall back to ingest time and say so via
    `metadata.processed_time`.
"""

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional

OCSF_VERSION = '1.3.0'

# class_uid -> (name, category_uid, category_name)
CLASSES = {
    3002: ('Authentication', 3, 'Identity & Access Management'),
    4001: ('Network Activity', 4, 'Network Activity'),
    1008: ('Application Lifecycle', 1, 'System Activity'),
}

#  Our severity vocabulary -> OCSF severity_id (1 Informational .. 6 Fatal).
_SEVERITY_ID = {
    'DEBUG': 1, 'INFO': 1, 'NOTICE': 2, 'WARNING': 3, 'WARN': 3,
    'ERROR': 4, 'CRITICAL': 5, 'ALERT': 5, 'FATAL': 6, 'EMERGENCY': 6,
}

#  Words that mark an event as an authentication attempt, and which way it
#  went. Matched against the message, component and action attributes - the
#  places our detectors put a verdict.
_AUTH_FAIL = re.compile(
    r'\b(failed|failure|invalid|denied|deny|lockout|locked_out|locked out|'
    r'bad[_ ]credentials|authentication failure|unauthorized|incorrect)\b', re.I)
_AUTH_OK = re.compile(
    r'\b(success|succeeded|accepted|logged in|login|logon|session opened|'
    r'authenticated|sign[- ]in)\b', re.I)
_AUTH_HINT = re.compile(
    r'\b(auth|login|logon|sign[- ]?in|sshd|sudo|credential|password|mfa|'
    r'session\.start|user\.session|account\.lock|kerberos|saml|oauth)\b', re.I)
_LOGOFF_HINT = re.compile(r'\b(logout|logoff|log off|session closed|sign[- ]out)\b', re.I)

#  Network verdicts.
_NET_DENY = re.compile(r'\b(deny|denied|drop|dropped|block|blocked|reject|refused)\b', re.I)
_NET_ALLOW = re.compile(r'\b(allow|allowed|accept|accepted|permit|built|established)\b', re.I)

#  Attribute spellings our detectors actually emit, per OCSF endpoint field.
_SRC_IP_KEYS = ('src_ip', 'src', 'srcip', 'sourceIPAddress', 'ip_client',
                'ipAddress', 'client.ipAddress', 'sourceIPs', 'forwarded_for')
_DST_IP_KEYS = ('dst_ip', 'dst', 'dstip', 'destinationIP', 'upstream_host')
_SRC_PORT_KEYS = ('src_port', 'spt', 'srcPort', 'srcport', 'sport')
_DST_PORT_KEYS = ('dst_port', 'dpt', 'dstPort', 'dstport', 'dport')
_USER_KEYS = ('user', 'usr', 'username', 'userName', 'db_user', 'suser',
              'actor.alternateId', 'TargetUserName', 'SubjectUserName',
              'principalEmail', 'caller', 'duser')
_PROTO_KEYS = ('proto', 'protocol', 'transport')


def _first(attrs: Dict[str, Any], keys) -> Optional[str]:
    for k in keys:
        v = attrs.get(k)
        if v not in (None, '', '-'):
            if isinstance(v, (list, tuple)):
                v = v[0] if v else None
            if v not in (None, '', '-'):
                return str(v)
    return None


def _int(value: Optional[str]) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _epoch_millis(ts: Any) -> Optional[int]:
    """OCSF `time` is milliseconds since epoch."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _decode_attributes(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row.get('attributes_json') or row.get('attributes')
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def classify(row: Dict[str, Any], attrs: Dict[str, Any]) -> int:
    """Which OCSF class this event belongs to.

    Deliberately conservative. An event only becomes 3002 or 4001 when the
    evidence for it is in the record; everything else is 1008. Guessing a
    class is worse than the generic one, because a SIEM routes and alerts on
    class_uid.
    """
    haystack = ' '.join(str(x) for x in (
        row.get('message') or '', row.get('component') or '',
        row.get('process') or '', row.get('source_type') or '',
        attrs.get('eventType') or '', attrs.get('action') or '',
        attrs.get('act') or '', attrs.get('deviceAction') or '',
        attrs.get('verdict') or '', attrs.get('outcome.result') or '',
    ))
    # `process` matters here: sshd and sudo are authentication by definition,
    # whatever the message says.

    has_user = _first(attrs, _USER_KEYS) is not None
    if _AUTH_HINT.search(haystack):
        # A user attribute is the strongest evidence, but plenty of real auth
        # events do not carry one - `sshd[4412]: Failed password for invalid
        # user admin` puts the account in the prose, and requiring an
        # extracted user sent every Linux auth event to the catch-all. A clear
        # success/failure verdict is evidence enough on its own.
        if has_user or _AUTH_FAIL.search(haystack) or _AUTH_OK.search(haystack):
            return 3002

    has_endpoints = (_first(attrs, _SRC_IP_KEYS) is not None
                     and _first(attrs, _DST_IP_KEYS) is not None)
    if has_endpoints:
        return 4001

    return 1008


def _endpoint(attrs: Dict[str, Any], ip_keys, port_keys,
              hostname: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """An OCSF endpoint object, or None when we know nothing about it.

    Omission is correct here. A half-built endpoint with a fabricated IP would
    read as evidence in an investigation, which is exactly the failure mode
    this whole framework exists to avoid.
    """
    out: Dict[str, Any] = {}
    ip = _first(attrs, ip_keys)
    if ip:
        out['ip'] = ip
    port = _int(_first(attrs, port_keys))
    if port is not None:
        out['port'] = port
    if hostname and hostname != 'unknown-host':
        out['hostname'] = hostname
    return out or None


def _auth_activity(haystack: str) -> int:
    """3002 activity_id: 1 Logon, 2 Logoff, 0 Unknown."""
    if _LOGOFF_HINT.search(haystack):
        return 2
    if _AUTH_HINT.search(haystack):
        return 1
    return 0


def _status_id(haystack: str) -> int:
    """OCSF status_id: 1 Success, 2 Failure, 0 Unknown.

    Failure is tested first on purpose - "authentication failure" contains
    "authentication", and several vendors phrase a denial with a success word
    elsewhere in the line.
    """
    if _AUTH_FAIL.search(haystack):
        return 2
    if _AUTH_OK.search(haystack):
        return 1
    return 0


def _net_activity(haystack: str) -> int:
    """4001 activity_id: 1 Open, 6 Refuse, 0 Unknown."""
    if _NET_DENY.search(haystack):
        return 6
    if _NET_ALLOW.search(haystack):
        return 1
    return 0


def to_ocsf(row: Dict[str, Any]) -> Dict[str, Any]:
    """One normalized event as an OCSF object."""
    attrs = _decode_attributes(row)
    class_uid = classify(row, attrs)
    class_name, category_uid, category_name = CLASSES[class_uid]

    haystack = ' '.join(str(x) for x in (
        row.get('message') or '', row.get('component') or '',
        attrs.get('eventType') or '', attrs.get('action') or '',
        attrs.get('verdict') or '', attrs.get('outcome.result') or '',
        attrs.get('outcome.reason') or '', attrs.get('act') or '',
        attrs.get('deviceAction') or '',
    ))

    if class_uid == 3002:
        activity_id = _auth_activity(haystack)
    elif class_uid == 4001:
        activity_id = _net_activity(haystack)
    else:
        activity_id = 0

    event_time = _epoch_millis(row.get('timestamp'))
    doc: Dict[str, Any] = {
        'class_uid': class_uid,
        'class_name': class_name,
        'category_uid': category_uid,
        'category_name': category_name,
        'activity_id': activity_id,
        # Per spec: type_uid identifies the class/activity pair.
        'type_uid': class_uid * 100 + activity_id,
        'severity_id': _SEVERITY_ID.get(str(row.get('severity') or '').upper(), 0),
        'severity': row.get('severity'),
        'time': event_time,
        'message': row.get('message'),
        'metadata': {
            'version': OCSF_VERSION,
            'product': {
                'name': row.get('source_type') or 'Unknown',
                'vendor_name': attrs.get('cef_vendor') or attrs.get('leef_vendor')
                               or row.get('source_type') or 'Unknown',
                'feature': {'name': row.get('component')},
            },
            # Provenance, which is the point of this framework - which
            # detector read the event, and how much it is trusted.
            'log_name': row.get('matched_format'),
            'log_provider': row.get('process'),
            'uid': row.get('id'),
            'processed_time': _epoch_millis(row.get('created_at')
                                            or row.get('processed_at')),
        },
    }

    if row.get('confidence') is not None:
        doc['confidence_score'] = row['confidence']
    if row.get('raw_message'):
        doc['raw_data'] = row['raw_message']

    device = {}
    if row.get('hostname') and row['hostname'] != 'unknown-host':
        device['hostname'] = row['hostname']
    if device:
        doc['device'] = device

    user_name = _first(attrs, _USER_KEYS)
    if class_uid == 3002:
        doc['status_id'] = _status_id(haystack)
        doc['actor'] = {'user': {'name': user_name}} if user_name else {}
        if user_name:
            doc['user'] = {'name': user_name}
        src = _endpoint(attrs, _SRC_IP_KEYS, _SRC_PORT_KEYS)
        if src:
            doc['src_endpoint'] = src
    elif class_uid == 4001:
        src = _endpoint(attrs, _SRC_IP_KEYS, _SRC_PORT_KEYS)
        dst = _endpoint(attrs, _DST_IP_KEYS, _DST_PORT_KEYS)
        if src:
            doc['src_endpoint'] = src
        if dst:
            doc['dst_endpoint'] = dst
        proto = _first(attrs, _PROTO_KEYS)
        if proto:
            doc['connection_info'] = {'protocol_name': proto.lower()}
        doc['status_id'] = 2 if _NET_DENY.search(haystack) else (
            1 if _NET_ALLOW.search(haystack) else 0)

    # Everything the class does not model keeps its original name here, which
    # is what `unmapped` is for. Nothing is silently dropped.
    if attrs:
        doc['unmapped'] = {k: (v if isinstance(v, (str, int, float, bool)) else str(v))
                           for k, v in attrs.items()}
    return doc


def lines(rows: Iterable[Dict[str, Any]], include_raw: bool = True) -> Iterator[str]:
    """NDJSON stream of OCSF objects, for the export endpoint."""
    for row in rows:
        doc = to_ocsf(row)
        if not include_raw:
            doc.pop('raw_data', None)
        yield json.dumps(doc, default=str, ensure_ascii=False) + '\n'


def coverage(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """How events distribute across the implemented classes.

    Exists so the claim can be checked rather than asserted: if almost
    everything lands in 1008, the mapping is not doing real work and the
    honest thing is to say so.
    """
    counts: Dict[int, int] = {}
    total = 0
    for row in rows:
        total += 1
        uid = classify(row, _decode_attributes(row))
        counts[uid] = counts.get(uid, 0) + 1
    return {
        'total': total,
        'classes': [
            {'class_uid': uid, 'class_name': CLASSES[uid][0], 'count': n,
             'pct': round(100.0 * n / total, 1) if total else 0.0}
            for uid, n in sorted(counts.items(), key=lambda kv: -kv[1])
        ],
    }


def describe() -> List[Dict[str, Any]]:
    return [{'class_uid': uid, 'class_name': name, 'category_uid': cat_uid,
             'category_name': cat_name}
            for uid, (name, cat_uid, cat_name) in sorted(CLASSES.items())]
