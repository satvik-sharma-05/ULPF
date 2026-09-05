"""
parsers/cef_leef.py - ArcSight Common Event Format (CEF) and IBM LEEF, the
two vendor-neutral SIEM export formats most security appliances (firewalls,
IDS/IPS, EDR, proxies) can emit instead of - or alongside - their native log
shape. Both are pipe-delimited headers followed by a key=value extension
bag, so one shared escaping/extension-parsing helper covers both.

  CEF:Version|Vendor|Product|Version|SignatureID|Name|Severity|key=val key=val
  LEEF:Version|Vendor|Product|Version|EventID|[Delimiter|]key=val<delim>key=val

Both formats are commonly forwarded over syslog, so the marker ('CEF:' /
'LEEF:') is found anywhere on the line rather than anchored to column 0 - a
line can start with an RFC3164 syslog header before it.
"""

import re
from typing import Any, Dict, List, Optional

from .base import guess_severity_from_text, normalize_timestamp

_SEVERITY_WORDS = {
    'very-high': 'CRITICAL', 'very high': 'CRITICAL',
    'high': 'ERROR', 'medium': 'WARNING', 'low': 'INFO',
}


def _split_unescaped(s: str, sep: str, maxsplit: int = -1) -> List[str]:
    """Splits on `sep`, honoring a preceding backslash as an escape (CEF/LEEF
    both escape '|', '\\\\' and '=' this way inside header/extension fields)."""
    parts: List[str] = []
    current: List[str] = []
    i, n, count = 0, len(s), 0
    while i < n:
        ch = s[i]
        if ch == '\\' and i + 1 < n:
            current.append(s[i + 1])
            i += 2
            continue
        if ch == sep and (maxsplit < 0 or count < maxsplit):
            parts.append(''.join(current))
            current = []
            count += 1
            i += 1
            continue
        current.append(ch)
        i += 1
    parts.append(''.join(current))
    return parts


def _map_numeric_or_word_severity(raw: Optional[str]) -> Optional[str]:
    """CEF/LEEF severity is 0-10 numeric (most producers) or occasionally a
    word (Low/Medium/High/Very-High, ArcSight's own convention)."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        n = int(float(raw))
        if n >= 9:
            return 'CRITICAL'
        if n >= 7:
            return 'ERROR'
        if n >= 4:
            return 'WARNING'
        return 'INFO'
    except ValueError:
        return _SEVERITY_WORDS.get(raw.lower())


def _normalize_event_timestamp(raw: Optional[str]) -> Optional[str]:
    """CEF's `rt` and LEEF's `devTime` are frequently epoch millis/seconds
    rather than a formatted date - normalize_timestamp() only handles the
    latter, so epoch is tried first."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        try:
            from datetime import datetime, timezone
            n = int(raw)
            # 13 digits ~ milliseconds since epoch (until year ~2286); 10 ~ seconds.
            seconds = n / 1000.0 if len(raw) >= 13 else float(n)
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    return normalize_timestamp(raw)


_KV_TOKEN = re.compile(r'(\w[\w.]*)=((?:(?!\s\w[\w.]*=).)*)')


def _parse_space_delimited_extension(text: str) -> Dict[str, str]:
    """CEF's extension has no quoting: a value runs until the next ` key=`
    token, which is exactly what a lookahead-bounded regex captures."""
    out: Dict[str, str] = {}
    for key, value in _KV_TOKEN.findall(text):
        value = value.strip().replace('\\=', '=').replace('\\\\', '\\')
        if value:
            out[key] = value
    return out


def detect_cef(record: str) -> Optional[Dict[str, Any]]:
    line = record.split('\n', 1)[0]
    idx = line.find('CEF:')
    if idx == -1:
        return None
    prefix = line[:idx].strip()
    parts = _split_unescaped(line[idx:], '|', maxsplit=7)
    if len(parts) < 7:
        return None
    version_field, vendor, product, version, sig_id, name, severity_raw = parts[:7]
    extension_str = parts[7] if len(parts) > 7 else ''
    if not re.match(r'^CEF:\d+$', version_field.strip()):
        return None

    ext = _parse_space_delimited_extension(extension_str)
    severity = _map_numeric_or_word_severity(severity_raw) or guess_severity_from_text(extension_str)
    hostname = (ext.get('dvchost') or ext.get('shost') or ext.get('dvc')
                or (prefix.split()[-1] if prefix else None))
    timestamp = _normalize_event_timestamp(ext.get('rt') or ext.get('start'))
    msg = ext.get('msg')

    return {
        'timestamp': timestamp,
        'hostname': hostname,
        'source_type': 'CEF',
        'process': product or vendor or 'CEF',
        'component': name or sig_id,
        'severity': severity,
        'message': f"{name} ({vendor}/{product})" + (f": {msg}" if msg else ''),
        'attributes': {
            'cef_vendor': vendor, 'cef_product': product, 'cef_version': version,
            'cef_signature_id': sig_id, 'cef_name': name, 'cef_severity': severity_raw,
            **ext,
        },
    }


_LEEF_DELIM_SPEC = re.compile(r'^(x[0-9A-Fa-f]{2}|[^=\s]{1,3})\|(.*)$', re.DOTALL)



#  Delimiters vendors actually use for LEEF extension fields. Tab is the
#  specified default; pipe is what Check Point and several QRadar integrations
#  send without declaring it; caret and semicolon turn up in appliance
#  exports.
_LEEF_DELIM_CANDIDATES = ('\t', '|', '^', ';')


def _leef_pairs(extension_str: str, delimiter: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for token in extension_str.split(delimiter):
        if '=' in token:
            k, _, v = token.partition('=')
            k, v = k.strip(), v.strip()
            if v:
                out[k] = v
    return out


def _infer_leef_delimiter(extension_str: str, default: str) -> str:
    """Pick the delimiter that actually yields the most key=value pairs.

    Only reached when the header did not declare one. Splitting on the spec
    default regardless meant a pipe-delimited record produced a single token
    in which the first key swallowed every other field.
    """
    best, best_n = default, len(_leef_pairs(extension_str, default))
    for candidate in _LEEF_DELIM_CANDIDATES:
        if candidate == default:
            continue
        n = len(_leef_pairs(extension_str, candidate))
        if n > best_n:
            best, best_n = candidate, n
    return best


def detect_leef(record: str) -> Optional[Dict[str, Any]]:
    line = record.split('\n', 1)[0]
    idx = line.find('LEEF:')
    if idx == -1:
        return None
    prefix = line[:idx].strip()
    parts = _split_unescaped(line[idx:], '|', maxsplit=5)
    if len(parts) < 6:
        return None
    version_field, vendor, product, version, event_id, rest = parts[:6]
    if not re.match(r'^LEEF:[\d.]+$', version_field.strip()):
        return None

    # LEEF 2.0 adds an explicit delimiter field before the extension; LEEF 1.0
    # goes straight from EventID to a tab-delimited extension.
    delimiter = '\t'
    declared_delimiter = False
    extension_str = rest
    dm = _LEEF_DELIM_SPEC.match(rest)
    if dm and '=' not in dm.group(1):
        declared_delimiter = True
        delim_spec, extension_str = dm.groups()
        if delim_spec.lower().startswith('x') and len(delim_spec) == 3:
            try:
                delimiter = chr(int(delim_spec[1:], 16))
            except ValueError:
                delimiter = delim_spec
        else:
            delimiter = delim_spec

    if not declared_delimiter:
        delimiter = _infer_leef_delimiter(extension_str, delimiter)
    ext = _leef_pairs(extension_str, delimiter)
    # LEEF keys are camelCase by convention (identHostName, devTime) - a
    # lowercase lookup index lets this function find them regardless of the
    # exact casing a given vendor used, while `ext` itself keeps the original
    # casing for the attributes bag.
    ext_lower = {k.lower(): v for k, v in ext.items()}

    severity_raw = ext_lower.get('sev') or ext_lower.get('severity')
    severity = _map_numeric_or_word_severity(severity_raw) or guess_severity_from_text(extension_str)
    hostname = ext_lower.get('identhostname') or ext_lower.get('src') or (prefix.split()[-1] if prefix else None)
    timestamp = _normalize_event_timestamp(ext_lower.get('devtime'))

    return {
        'timestamp': timestamp,
        'hostname': hostname,
        'source_type': 'LEEF',
        'process': product or vendor or 'LEEF',
        'component': event_id,
        'severity': severity,
        'message': f"{event_id} ({vendor}/{product})",
        'attributes': {
            'leef_vendor': vendor, 'leef_product': product, 'leef_version': version,
            'leef_event_id': event_id, 'leef_severity': severity_raw,
            **ext,
        },
    }
