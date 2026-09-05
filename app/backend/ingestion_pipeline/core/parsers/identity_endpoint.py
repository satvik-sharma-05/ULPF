"""
parsers/identity_endpoint.py - Endpoint security and identity providers.

EDR and IdP are the two source categories the Background names that CEF/LEEF
was covering only by accident. Both matter more than their volume suggests: an
IdP is where an account compromise first shows up, and an EDR is where the
thing that account did shows up next. Correlating the two is most of what an
investigation is.

All of these emit JSON, and each stamps its records with something no other
vendor uses - Okta's `eventType` beside a `legacyEventType`, CrowdStrike's
`ComputerName` in a `metadata`/`event` envelope, Defender's `AlertId`, Duo's
`factor` beside an `integration` - so attribution needs no guessing.

The recurring decision in here is what counts as severe. A successful login is
INFO; a *failed* one is a WARNING and a locked-out account is worse, because
those are the events an investigation starts from. Left at INFO they vanish
into the volume, which is the same failure mode as the CloudTrail `errorCode`
in cloud.py.
"""

import json
from typing import Any, Dict, Optional

from .base import SEVERITY_SCORE, first_line, normalize_timestamp

_MAX_ATTR_LEN = 300


def _load(record: str) -> Optional[Dict[str, Any]]:
    text = first_line(record).strip()
    if not text.startswith('{'):
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _flat(obj: Any, prefix: str = '', out: Optional[Dict[str, str]] = None,
          depth: int = 0) -> Dict[str, str]:
    out = {} if out is None else out
    if depth > 3:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flat(v, f'{prefix}{k}.' if prefix else f'{k}.', out, depth + 1)
    elif isinstance(obj, list):
        if obj and all(not isinstance(x, (dict, list)) for x in obj):
            out[prefix.rstrip('.')] = ', '.join(str(x) for x in obj)[:_MAX_ATTR_LEN]
    elif obj is not None and prefix.rstrip('.'):
        out[prefix.rstrip('.')] = str(obj)[:_MAX_ATTR_LEN]
    return out


def _result(severity: str, **kw) -> Dict[str, Any]:
    kw['severity'] = severity
    kw['severity_score'] = SEVERITY_SCORE.get(severity, 6)
    return kw


# ---------------------------------------------------------------------------
# Identity providers
# ---------------------------------------------------------------------------
def detect_okta_system_log(record: str) -> Optional[Dict[str, Any]]:
    """Okta System Log events."""
    obj = _load(record)
    if not obj or 'eventType' not in obj:
        return None
    outcome = obj.get('outcome') or {}
    actor = obj.get('actor') or {}
    client = obj.get('client') or {}
    if not isinstance(outcome, dict) or 'displayMessage' not in obj:
        return None

    result = str(outcome.get('result') or '').upper()
    event_type = str(obj.get('eventType'))
    severity = 'INFO'
    if result in ('FAILURE', 'DENY'):
        severity = 'WARNING'
    if 'lockout' in event_type.lower() or result == 'DENY':
        severity = 'ERROR'

    ip = ((client.get('ipAddress') if isinstance(client, dict) else None) or 'unknown')
    return _result(
        severity,
        timestamp=normalize_timestamp(str(obj.get('published') or '')),
        # An IdP event happens to an identity, not on a host; the client IP is
        # the closest thing to a location and is what correlates with the rest.
        hostname=str(ip),
        source_type='Okta',
        process='okta-system-log',
        component=event_type,
        message=f"{obj.get('displayMessage')} — {actor.get('alternateId') or actor.get('displayName') or 'unknown'}"
                + (f" ({outcome.get('reason')})" if outcome.get('reason') else ''),
        attributes=_flat({k: v for k, v in obj.items() if k not in ('debugContext', 'target')}),
        matched_format='okta_system_log',
    )


def detect_duo_auth(record: str) -> Optional[Dict[str, Any]]:
    """Duo authentication log."""
    obj = _load(record)
    if not obj or 'factor' not in obj or 'integration' not in obj:
        return None
    result = str(obj.get('result') or '').lower()
    reason = str(obj.get('reason') or '')
    severity = 'INFO' if result == 'success' else 'WARNING'
    if 'fraud' in reason.lower() or 'denied' in result:
        severity = 'ERROR'
    return _result(
        severity,
        timestamp=normalize_timestamp(str(obj.get('isotimestamp') or obj.get('timestamp') or '')),
        hostname=str(obj.get('ip') or obj.get('access_device', {}).get('ip') or 'duo'),
        source_type='Duo',
        process='duo-auth',
        component=str(obj.get('integration')),
        message=f"{obj.get('username')} {result} via {obj.get('factor')}"
                + (f": {reason}" if reason else ''),
        attributes=_flat(obj),
        matched_format='duo_auth',
    )


# ---------------------------------------------------------------------------
# Endpoint detection and response
# ---------------------------------------------------------------------------
_CS_SEVERITY = {1: 'INFO', 2: 'NOTICE', 3: 'WARNING', 4: 'ERROR', 5: 'CRITICAL'}


def detect_crowdstrike(record: str) -> Optional[Dict[str, Any]]:
    """CrowdStrike Falcon streaming detections."""
    obj = _load(record)
    if not obj:
        return None
    meta = obj.get('metadata') or {}
    event = obj.get('event') or {}
    if not (isinstance(event, dict) and isinstance(meta, dict)
            and meta.get('eventType') and 'ComputerName' in event):
        return None

    # Falcon grades 1-5; anything it calls a detection is at least a warning,
    # because a detection is by definition something it wants a human to see.
    sev = event.get('SeverityName') or _CS_SEVERITY.get(event.get('Severity'), 'WARNING')
    sev = str(sev).upper()
    if sev not in SEVERITY_SCORE:
        sev = 'WARNING'

    return _result(
        sev,
        timestamp=normalize_timestamp(str(meta.get('eventCreationTime')
                                          or event.get('ProcessStartTime') or '')),
        hostname=str(event.get('ComputerName')),
        source_type='CrowdStrike',
        process=str(event.get('FileName') or 'falcon'),
        component=str(meta.get('eventType')),
        message=(f"{event.get('DetectName') or meta.get('eventType')}"
                 f" on {event.get('ComputerName')}"
                 + (f": {event.get('DetectDescription')}" if event.get('DetectDescription') else '')),
        attributes=_flat(obj),
        matched_format='crowdstrike_falcon',
    )


def detect_defender_alert(record: str) -> Optional[Dict[str, Any]]:
    """Microsoft Defender / 365 Defender alerts."""
    obj = _load(record)
    if not obj:
        return None
    if not (obj.get('AlertId') or obj.get('alertId')) or not (
            obj.get('Severity') or obj.get('severity')):
        return None

    raw_sev = str(obj.get('Severity') or obj.get('severity') or '').upper()
    severity = {'INFORMATIONAL': 'INFO', 'LOW': 'NOTICE',
                'MEDIUM': 'WARNING', 'HIGH': 'ERROR'}.get(raw_sev, raw_sev)
    if severity not in SEVERITY_SCORE:
        severity = 'WARNING'

    host = (obj.get('DeviceName') or obj.get('ComputerDnsName')
            or obj.get('MachineId') or 'defender-device')
    title = obj.get('Title') or obj.get('AlertDisplayName') or 'Defender alert'
    return _result(
        severity,
        timestamp=normalize_timestamp(str(obj.get('AlertTime') or obj.get('TimeGenerated') or '')),
        hostname=str(host),
        source_type='MicrosoftDefender',
        process='defender',
        component=str(obj.get('Category') or 'alert'),
        message=f"{title} on {host}",
        attributes=_flat(obj),
        matched_format='defender_alert',
    )
