"""
parsers/cloud.py - Public cloud audit and platform logs.

AWS CloudTrail, Azure Activity/Sign-in, GCP Cloud Audit, and the JSON shape
Kubernetes' own audit log uses. The Background names cloud services explicitly
and these three providers are essentially all of that market.

All four are JSON, which makes the detection cheap and reliable: each provider
stamps its records with a field nobody else uses - `eventSource` ending in
`.amazonaws.com`, an Azure `operationName` beside a `resourceId` starting
`/SUBSCRIPTIONS/`, a GCP `protoPayload` with a `@type` - so a record can be
attributed without guessing.

What matters here is the same thing that matters everywhere else in this
framework: the identity, the resource and the outcome have to land in the
schema rather than staying buried in nested JSON. A CloudTrail record whose
`errorCode` never becomes a severity is a failed API call that looks like an
informational one, and that is exactly the event a SOC is looking for.
"""

import json
import re
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


def _flatten(obj: Any, prefix: str = '', out: Optional[Dict[str, str]] = None,
             depth: int = 0) -> Dict[str, str]:
    """Nested JSON -> flat attributes. Bounded depth and value length: a single
    CloudTrail record can carry a whole IAM policy document, and storing it as
    one attribute helps nobody."""
    out = {} if out is None else out
    if depth > 3:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(v, f'{prefix}{k}.' if prefix else f'{k}.', out, depth + 1)
    elif isinstance(obj, list):
        if obj and all(not isinstance(x, (dict, list)) for x in obj):
            out[prefix.rstrip('.')] = ', '.join(str(x) for x in obj)[:_MAX_ATTR_LEN]
    elif obj is not None:
        key = prefix.rstrip('.')
        if key:
            out[key] = str(obj)[:_MAX_ATTR_LEN]
    return out


def detect_aws_cloudtrail(record: str) -> Optional[Dict[str, Any]]:
    """AWS CloudTrail management and data events."""
    obj = _load(record)
    if not obj:
        return None
    source = obj.get('eventSource') or ''
    if not (isinstance(source, str) and source.endswith('.amazonaws.com')
            and obj.get('eventName')):
        return None

    identity = obj.get('userIdentity') or {}
    principal = (identity.get('userName') or identity.get('arn')
                 or identity.get('principalId') or 'unknown')

    # A failed API call is the finding. Without this every AccessDenied reads
    # as INFO and disappears into the volume.
    error = obj.get('errorCode')
    severity = 'ERROR' if error else 'INFO'
    if error and re.search(r'AccessDenied|Unauthorized|Forbidden', str(error), re.I):
        severity = 'WARNING'

    attributes = _flatten({k: v for k, v in obj.items()
                           if k not in ('requestParameters', 'responseElements')})
    return {
        'timestamp': normalize_timestamp(obj.get('eventTime') or ''),
        # The account is the closest thing CloudTrail has to a host: the API
        # call did not happen on a machine.
        'hostname': obj.get('recipientAccountId') or 'aws-account',
        'source_type': 'AWS-CloudTrail',
        'process': source.replace('.amazonaws.com', ''),
        'component': obj.get('eventName'),
        'severity': severity,
        'severity_score': SEVERITY_SCORE.get(severity, 6),
        'message': (f"{obj.get('eventName')} on {source} by {principal}"
                    + (f" failed: {error}" if error else '')),
        'attributes': attributes,
        'matched_format': 'aws_cloudtrail',
    }


def detect_azure_activity(record: str) -> Optional[Dict[str, Any]]:
    """Azure Activity, Sign-in and Diagnostic records."""
    obj = _load(record)
    if not obj:
        return None
    resource = str(obj.get('resourceId') or obj.get('ResourceId') or '')
    operation = obj.get('operationName') or obj.get('OperationName')
    if not (operation and resource.upper().startswith('/SUBSCRIPTIONS/')):
        return None

    level = str(obj.get('level') or obj.get('Level') or '').upper()
    result = str(obj.get('resultType') or obj.get('ResultType') or '')
    severity = level if level in SEVERITY_SCORE else None
    if not severity:
        severity = 'ERROR' if result and result.lower() not in ('success', '0') else 'INFO'

    identity = obj.get('identity') or obj.get('Identity') or {}
    caller = (identity.get('claims', {}).get('name')
              if isinstance(identity, dict) else None) or obj.get('caller') or 'unknown'

    # /SUBSCRIPTIONS/<id>/RESOURCEGROUPS/<rg>/PROVIDERS/<ns>/<type>/<name>
    parts = [p for p in resource.split('/') if p]
    return {
        'timestamp': normalize_timestamp(str(obj.get('time') or obj.get('TimeGenerated') or '')),
        'hostname': parts[-1] if parts else 'azure-resource',
        'source_type': 'Azure',
        'process': obj.get('category') or obj.get('Category') or 'Activity',
        'component': str(operation),
        'severity': severity,
        'severity_score': SEVERITY_SCORE.get(severity, 6),
        'message': f"{operation} by {caller}" + (f" -> {result}" if result else ''),
        'attributes': _flatten({k: v for k, v in obj.items() if k != 'properties'}),
        'matched_format': 'azure_activity',
    }


def detect_gcp_audit(record: str) -> Optional[Dict[str, Any]]:
    """GCP Cloud Audit logs."""
    obj = _load(record)
    if not obj:
        return None
    payload = obj.get('protoPayload')
    if not isinstance(payload, dict) or 'serviceName' not in payload:
        return None

    status = payload.get('status') or {}
    code = status.get('code') if isinstance(status, dict) else None
    severity = str(obj.get('severity') or '').upper()
    if severity not in SEVERITY_SCORE:
        severity = 'ERROR' if code not in (None, 0) else 'INFO'

    auth = payload.get('authenticationInfo') or {}
    principal = auth.get('principalEmail', 'unknown') if isinstance(auth, dict) else 'unknown'
    resource = obj.get('resource') or {}
    labels = resource.get('labels') if isinstance(resource, dict) else {}
    host = (labels or {}).get('instance_id') or (labels or {}).get('project_id') or 'gcp-project'

    return {
        'timestamp': normalize_timestamp(str(obj.get('timestamp') or '')),
        'hostname': str(host),
        'source_type': 'GCP-Audit',
        'process': str(payload.get('serviceName')),
        'component': str(payload.get('methodName') or 'audit'),
        'severity': severity,
        'severity_score': SEVERITY_SCORE.get(severity, 6),
        'message': f"{payload.get('methodName')} on {payload.get('serviceName')} by {principal}",
        'attributes': _flatten({k: v for k, v in obj.items() if k != 'protoPayload'}),
        'matched_format': 'gcp_audit',
    }


def detect_k8s_audit(record: str) -> Optional[Dict[str, Any]]:
    """Kubernetes API server audit events."""
    obj = _load(record)
    if not obj:
        return None
    if obj.get('kind') != 'Event' or 'requestURI' not in obj:
        return None

    status = obj.get('responseStatus') or {}
    code = status.get('code') if isinstance(status, dict) else None
    severity = 'INFO'
    if isinstance(code, int):
        severity = 'ERROR' if code >= 500 else 'WARNING' if code >= 400 else 'INFO'

    user = (obj.get('user') or {}).get('username', 'unknown')
    obj_ref = obj.get('objectRef') or {}
    return {
        'timestamp': normalize_timestamp(str(obj.get('requestReceivedTimestamp')
                                             or obj.get('stageTimestamp') or '')),
        'hostname': str((obj.get('sourceIPs') or ['k8s-apiserver'])[0]),
        'source_type': 'Kubernetes-Audit',
        'process': 'kube-apiserver',
        'component': str(obj_ref.get('resource') or 'api'),
        'severity': severity,
        'severity_score': SEVERITY_SCORE.get(severity, 6),
        'message': (f"{obj.get('verb')} {obj.get('requestURI')} by {user}"
                    + (f" -> {code}" if code else '')),
        'attributes': _flatten({k: v for k, v in obj.items()
                                if k not in ('responseObject', 'requestObject', 'annotations')}),
        'matched_format': 'k8s_audit',
    }
