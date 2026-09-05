"""
parser.py - Log parser for the vRLI/VMware SDDC + Kubernetes platform corpus.

Real logs here come from NSX, vCenter, ESXi (hostd/vpxa/fdm/vmkernel/envoy),
Site Recovery Manager, VMware Aria Automation microservices, vROps,
Horizon/CASA, MinIO, Kubernetes/CoreDNS, Windows Security auditing, JVM GC,
Linux syslog/auditd/kernel, Squid and PostgreSQL server logs - roughly thirty
different line shapes, not one. The parsers/ package holds one small detector
per family; this module dispatches a raw log string to the first matching
detector (see parsers/registry.py for the priority order), fills in whatever a
format-specific detector didn't find with the always-matching generic fallback,
and applies enrichment that's the same regardless of source: severity
normalization, a stable md5 id, typed entity extraction, a one-line
embedding-ready summary, and a confidence score that only credits fields that
were genuinely found (not defaulted).

Entities are *typed* (`{'type': 'user', 'value': 'root'}`), not bare strings -
parsers/entities.py classifies them and graph_schema.py maps each type to its
own node label and relationship. That typing is what lets the graph answer
"which devices did this operation touch" instead of merely "this log mentions
this string".

A raw string may itself be multi-line (a Windows Security Event body, a
Java stack trace) - multiline.reassemble() is what produces those before
they ever reach this module when reading from a file; parse() also accepts
an already multi-line string directly, since a Kafka message could arrive
that way too.
"""

import re
import json
import os
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .config import QWEN_CONFIG, PIPELINE_CONFIG
from core.parsers.base import parse_kv_bag
from .parsers import DETECTORS, JSON_DETECTORS, detect_generic_fallback, extract_entities
from .parsers.base import (
    SEVERITY_SCORE,
    guess_severity_from_text,
    normalize_severity,
    strip_bom,
    unescape_syslog_octal,
)

logger = logging.getLogger(__name__)

# How far an event time may sit from ingest time before it is treated as
# clock skew rather than history. Two years is generous on purpose: a
# genuine backfill of old logs should not be flagged wholesale.
CLOCK_SKEW_TOLERANCE_DAYS = int(os.getenv('CLOCK_SKEW_TOLERANCE_DAYS', '730'))

# Leading YYYY-MM-DD of a normalized timestamp - stored as its own field so the
# graph can hang every log off a (:Day) node without the writer having to parse
# timestamps itself.
_DAY_PATTERN = re.compile(r'^(\d{4}-\d{2}-\d{2})')


class LogParser:
    def __init__(self):
        self.use_qwen = PIPELINE_CONFIG['enable_qwen_fallback']

    def parse(self, log_input: Any) -> Dict[str, Any]:
        """Parses a single raw log item (str, or dict with a 'raw'/'message'
        key) into the normalized schema described in the module docstring."""
        if isinstance(log_input, dict):
            raw = log_input.get('raw', log_input.get('message', str(log_input)))
        else:
            raw = str(log_input)

        # Two forms, deliberately. `raw_original` is what gets STORED, and
        # keeps every byte the source sent, because requirement (a) is that
        # the raw event survives intact - trailing whitespace included, since
        # it distinguishes an empty field from an absent one and is exactly
        # what a forensic hash of the original file would disagree with.
        # `raw_str` is the DETECTION form: no detector should care about a
        # stray leading space. It also seeds the record id, so ids stay
        # byte-identical to those already in the graph and two records
        # differing only in trailing space remain the same event.
        raw_original = raw.rstrip('\n').rstrip('\r')
        raw_str = raw_original.strip()

        if (raw_str.startswith('{') and raw_str.endswith('}')) or (raw_str.startswith('[') and raw_str.endswith(']')):
            try:
                data = json.loads(raw_str)
                if isinstance(data, dict):
                    # Provider-specific detectors first. Without this the
                    # structural reader claimed every JSON record, and a
                    # CloudTrail AccessDenied arrived with no hostname, no
                    # severity and its identity buried in the attribute bag.
                    for name, detect_fn in JSON_DETECTORS:
                        try:
                            claimed = detect_fn(raw_str)
                        except Exception:
                            continue
                        if claimed:
                            claimed.setdefault('matched_format', name)
                            return self._enrich(claimed, raw_str, raw_original)
                    return self._enrich(self._parse_json_structure(data), raw_str, raw_original)
            except (ValueError, TypeError):
                pass

        extracted = None
        matched_format = 'generic_fallback'
        for name, detect_fn in DETECTORS:
            try:
                result = detect_fn(raw_str)
            except Exception as e:
                logger.debug(f"Detector {name} raised on a record, skipping it: {e}")
                result = None
            if result is not None:
                extracted = result
                matched_format = name
                break

        if extracted is None:
            extracted = detect_generic_fallback(raw_str)

        extracted['matched_format'] = matched_format
        return self._enrich(extracted, raw_str, raw_original)

    @staticmethod
    def _scalar_items(data: Dict[str, Any]):
        """Top-level items, then the scalars one level down.

        Yielding the top level first means a field spelled at both depths
        keeps its top-level value, since the loop below skips a name it has
        already recorded and the shallower name is the more authoritative
        one. Nested objects are still yielded intact so they land in
        `attributes` unchanged - nothing is dropped, the deeper scalars are
        merely also offered to the alias match.
        """
        yield from data.items()
        for value in data.values():
            if isinstance(value, dict):
                for k, v in value.items():
                    if not isinstance(v, (dict, list)):
                        yield k, v

    def _parse_json_structure(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Maps a pre-structured event onto the normalized schema.

        Reached from three directions, which is why the alias lists are as
        broad as they are: a JSON event delivered directly by DNIF, and -
        via core/structured_readers.py - every row of an uploaded CSV and
        every element of an uploaded XML file, both of which are flattened to
        a dict and JSON-encoded specifically to land here. Column/tag names
        in a user-supplied file follow no single convention, so the aliases
        cover the common vendor spellings (Windows Event XML's Computer and
        TimeCreated, syslog-ish host/appname, CEF-ish dvchost) rather than
        only the canonical field names this pipeline itself emits.
        """
        extracted: Dict[str, Any] = {'attributes': {}, 'matched_format': 'json_structure'}
        for k, v in self._scalar_items(data):
            k_lower = k.lower().replace('-', '_').replace(' ', '_')
            if isinstance(v, (dict, list)):
                extracted['attributes'][k] = v
                continue
            if k in extracted['attributes']:
                continue
            if k_lower in ('timestamp', 'datetime', 'time', 'date', '@timestamp',
                           'timecreated', 'eventtime', 'event_time', 'created',
                           'devtime', 'rt', '_time'):
                extracted['timestamp'] = str(v)
            elif k_lower in ('hostname', 'host', 'server', 'node', 'computer',
                             'computername', 'machine', 'dvchost', 'devicehostname',
                             'agent_host', 'src_host'):
                extracted['hostname'] = str(v)
            elif k_lower in ('source_type', 'sourcetype', 'source', 'log_source',
                             'logsource', 'vendor', 'product'):
                extracted['source_type'] = str(v)
            elif k_lower in ('process', 'app', 'application', 'service', 'appname',
                             'program', 'processname', 'process_name', 'provider'):
                extracted['process'] = str(v)
            elif k_lower in ('component', 'subsystem', 'module', 'category', 'channel'):
                extracted['component'] = str(v)
            elif k_lower in ('severity', 'sev', 'level', 'priority', 'loglevel',
                             'log_level', 'levelname', 'event_severity'):
                # Only accept a value that's actually a known severity word. A
                # user-supplied CSV/XML routinely has a "level" or "priority"
                # column holding something else entirely (a syslog facility
                # number, a vendor's own "Audit Success"), and _enrich() stores
                # an unrecognized severity verbatim - which would put values
                # like "8" or "AUDIT SUCCESS" into (:Severity) nodes and break
                # every severity_score filter downstream. Anything unrecognized
                # stays an attribute, and severity falls back to text-guessing.
                if normalize_severity(str(v)):
                    extracted['severity'] = str(v).upper()
                else:
                    extracted['attributes'][k] = v
            elif k_lower in ('message', 'msg', 'event', 'description', 'body',
                             'text', 'log', 'detail', 'eventdata', 'rendereddescription'):
                extracted['message'] = str(v)
            elif k_lower in ('pid', 'processid', 'process_id'):
                try:
                    extracted['pid'] = int(str(v).strip())
                except (TypeError, ValueError):
                    extracted['attributes'][k] = v
            else:
                extracted['attributes'][k] = v

        if not extracted.get('message'):
            # Only when it produced something. An empty compose (every
            # value nested) must leave message unset so _enrich falls
            # back to the raw record rather than storing a blank.
            composed = self._compose_message(extracted)
            if composed:
                extracted['message'] = composed
        return extracted

    @staticmethod
    def _compose_message(extracted: Dict[str, Any]) -> str:
        """A readable summary for a record that carries no message field.

        Windows Event XML is the standard case: the message is rendered
        from the EventID at display time and never appears in the event.
        Falling back to the raw JSON put an unreadable blob in the UI and,
        worse, embedded it for semantic search. A k=v line over the scalar
        attributes reads like the CEF and key=value records already do.
        """
        parts = []
        for k, v in extracted.get('attributes', {}).items():
            if isinstance(v, (dict, list)) or v in (None, ''):
                continue
            parts.append('%s=%s' % (k, v))
            if len(parts) == 12:   # enough to identify the event
                break
        return ' '.join(parts)

    def _calculate_confidence(self, data: Dict[str, Any]) -> float:
        score = 0.0
        if data.get('timestamp'):
            score += 0.25
        if data.get('hostname'):
            score += 0.2
        if data.get('source_type') and data['source_type'] != 'Unknown':
            score += 0.25
        if data.get('process'):
            score += 0.15
        if data.get('severity'):
            score += 0.15
        return min(round(score, 2), 1.0)

    def _enrich(self, data: Dict[str, Any], raw: str,
                raw_original: Optional[str] = None) -> Dict[str, Any]:
        confidence = self._calculate_confidence(data)

        if confidence < QWEN_CONFIG['confidence_threshold'] and self.use_qwen:
            qwen_data = self._parse_with_qwen(raw)
            if qwen_data:
                for k, v in qwen_data.items():
                    if v and not data.get(k):
                        data[k] = v
                confidence = self._calculate_confidence(data)

        # Whether the event time came from the LINE or from the clock. A
        # format like CoreDNS's access log carries no timestamp at all, so
        # `timestamp` is the ingest time for those - which a consumer
        # correlating events needs to know rather than guess.
        parsed_timestamp = data.get('timestamp')
        timestamp = parsed_timestamp or datetime.now(timezone.utc).isoformat()
        timestamp_source = 'event' if parsed_timestamp else 'ingest'

        # An event time wildly out of step with ingest time is almost always
        # appliance clock skew - a device that booted with an unset RTC. The
        # corpus carries 141 such events stamped 2012 from an NSX agent, and
        # they stretch every date range and flatten every trend chart drawn
        # from it.
        #
        # Flagged, never corrected. Rewriting a timestamp would break
        # requirement (a): the normalized record has to agree with the raw
        # line, and the raw line really does say 2012. Analytics can exclude
        # them; a SOC should arguably be told about them, because a box with
        # the wrong clock produces evidence that will not correlate.
        timestamp_anomalous = False
        if parsed_timestamp:
            try:
                event_dt = datetime.fromisoformat(str(parsed_timestamp).replace('Z', '+00:00'))
                if event_dt.tzinfo is None:
                    event_dt = event_dt.replace(tzinfo=timezone.utc)
                skew_days = abs((datetime.now(timezone.utc) - event_dt).days)
                timestamp_anomalous = skew_days > CLOCK_SKEW_TOLERANCE_DAYS
            except (ValueError, TypeError, OverflowError):
                # An unparseable timestamp is not evidence of skew.
                timestamp_anomalous = False
        hostname = data.get('hostname') or 'unknown-host'
        process = data.get('process') or 'unknown-process'
        component = data.get('component') or process
        source_type = data.get('source_type') or 'Unknown'
        # A detector's message can legitimately be an empty string (a bare
        # marker line with no body text) - only fall back to the raw line
        # when the key is truly absent, not just falsy/empty.
        raw_message = data['message'] if data.get('message') is not None else raw[:500]
        message = strip_bom(unescape_syslog_octal(raw_message)).strip()
        # A bare marker line ("... sut[2101482]:" with nothing after the colon)
        # leaves a detector with genuinely no body text, and an empty message
        # was allowed through. That collided with the schema contract, where
        # `message` is required: 11 events in a 4,000-record sample arrived
        # with nothing to display or search on, which is not analytics-ready
        # whatever the contract says. Falling back to the raw line is not
        # invention - for these records the line IS the whole event, and
        # raw_message still holds the original either way.
        if not message:
            message = strip_bom(unescape_syslog_octal(raw[:500])).strip()
        attributes = {k: v for k, v in (data.get('attributes') or {}).items() if v not in (None, '')}
        # A detector that matched on shape alone (esxi_generic_proc and the
        # other broad syslog forms) knows nothing about the payload and
        # returns no attributes. If the body is key=value - which is what an
        # unknown appliance most often emits - those pairs are the whole
        # analytic value of the record, so recover them rather than let a
        # generic match cost us requirement (b). Only when the detector
        # offered nothing: an explicit extraction is never second-guessed.
        if not attributes:
            body = data.get('message')
            if body:
                swept = {k: v for k, v in parse_kv_bag(str(body)).items()
                         if v not in (None, '')}
                if swept:
                    attributes = swept

        severity = normalize_severity(data.get('severity')) or data.get('severity')
        if not severity:
            severity = guess_severity_from_text(raw) or 'INFO'
        severity = severity.upper()
        severity_score = SEVERITY_SCORE.get(severity, 6)

        # The id must be stable across re-runs so re-ingesting a file MERGEs
        # onto the same (:Log) instead of duplicating it. Including the raw
        # line (not just the cleaned message) keeps two records that normalize
        # to the same text but came from different sources distinct.
        #
        # A line with NO parseable timestamp must not hash the ingest-time
        # fallback, or the id is a function of when it happened to be read:
        # every re-ingest produced a brand new node and MERGE silently
        # stopped being idempotent. 1,112 events in the sample corpus (all
        # CoreDNS and vracli, neither of which emits a timestamp) had exactly
        # this problem. A constant marker keeps those ids stable; the raw
        # line still distinguishes them from one another.
        id_time = parsed_timestamp or 'no-event-timestamp'
        id_str = f"{id_time}|{hostname}|{process}|{raw}"
        log_id = f"log-{hashlib.md5(id_str.encode('utf-8')).hexdigest()[:16]}"

        entities = extract_entities(
            hostname=hostname,
            process=process,
            message=message,
            attributes=attributes,
            detector_entities=data.get('entities'),
        )
        normalized_message = f"[{source_type}] {hostname}/{process}: {message[:200]}"
        day_match = _DAY_PATTERN.match(timestamp)

        # Bound once so the stored bytes and the hash of them can never
        # disagree - the guarantee is worthless if they can.
        stored_raw = raw if raw_original is None else raw_original
        return {
            'id': log_id,
            'timestamp': timestamp,
            'timestamp_source': timestamp_source,
            'timestamp_anomalous': timestamp_anomalous,
            'day': day_match.group(1) if day_match else None,
            'hostname': hostname,
            'source_type': source_type,
            'process': process,
            'pid': data.get('pid'),
            'component': component,
            'subcomponent': data.get('subcomponent'),
            'severity': severity,
            'severity_score': severity_score,
            'message': message,
            'normalized_message': normalized_message,
            'attributes': attributes,
            'entities': entities,
            'confidence': confidence,
            'matched_format': data.get('matched_format', 'generic_fallback'),
            # The untouched record. Not `raw`, which has been stripped for
            # matching - storing that lost trailing whitespace and made the
            # losslessness guarantee false.
            'raw_message': stored_raw,
            # SHA-256 of exactly those bytes. Carried on the event rather
            # than left to the audit tool because its whole value is that
            # someone holding the ORIGINAL log file can recompute it and
            # find the matching stored event - with no ledger, no database
            # and no need to trust this system. It is also the value the
            # tamper-evident ledger seals (blockchain_and_cybersecurity/).
            'raw_hash': hashlib.sha256(stored_raw.encode('utf-8')).hexdigest(),
            'processed_at': datetime.now(timezone.utc).isoformat(),
        }

    def _parse_with_qwen(self, raw: str) -> Optional[Dict[str, Any]]:
        """Optional LLM fallback (off by default) for whatever the regex
        detectors genuinely couldn't make sense of."""
        import requests

        ollama_url = f"{QWEN_CONFIG['ollama_host'].rstrip('/')}/api/generate"
        prompt = f"""Extract JSON fields from this system/application log line.
Return ONLY valid JSON with keys: "hostname", "source_type", "process", "component", "severity", "message".

Log: {raw}
JSON:"""
        try:
            payload = {
                "model": QWEN_CONFIG['model_name'],
                "prompt": prompt,
                "stream": False,
                "format": "json",
            }
            response = requests.post(ollama_url, json=payload, timeout=QWEN_CONFIG['timeout'])
            if response.status_code == 200:
                result_text = response.json().get('response', '')
                return json.loads(result_text)
        except Exception:
            pass
        return None
