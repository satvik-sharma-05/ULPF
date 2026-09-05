"""Coverage test against the source categories and formats in the problem
statement, using LA/sample_logs.{txt,xml,csv}.

Separate from test_custom_formats.py, which checks that individual detectors
extract the right FIELDS. This one asks a coarser question the field tests
cannot: is there a named detector at all for every kind of source we claim to
handle - network devices, servers, operating systems, applications,
databases, cloud services, containers, endpoint security, IAM, IoT - across
Syslog, JSON, XML, CSV, CEF, LEEF and proprietary vendor formats.

The bar is deliberately "a NAMED detector claimed it". generic_fallback
always returns something, so a run that only counted non-empty results would
report 100% while extracting nothing. A fallback here means the category is
being stored as undifferentiated text.

Run:  python -m batch.test_source_coverage
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.parser import LogParser                  # noqa: E402
from core.structured_readers import iter_records   # noqa: E402

#  LA/ - four levels up from ingestion_pipeline/batch/.
_HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.abspath(os.path.join(_HERE, '..', '..', '..', '..', '..'))

# One label per line of sample_logs.txt, in order.
CATEGORIES = [
    'Network / firewall', 'Network / IDS', 'Network / switch', 'Network / WAF',
    'Network / VPN', 'Network / firewall', 'Network / firewall',
    'Network / NGFW', 'Network / firewall', 'Network / proxy',
    'Server / OS auth', 'Server / OS privilege', 'Server / OS kernel',
    'Server / hypervisor', 'Server / network virtualisation',
    'Database', 'Database',
    'Application', 'Application', 'Application / JVM',
    'Cloud / AWS', 'Cloud / Azure', 'Cloud / GCP',
    'Container / k8s audit', 'Container / kubelet', 'Container / CoreDNS',
    'Container / service mesh',
    'Endpoint / EDR', 'Endpoint / EDR',
    'IAM / SSO', 'IAM / SSO', 'IAM / MFA',
    'IoT / industrial gateway', 'IoT / MQTT broker', 'IoT / power',
    'Other / print server', 'Other / monitoring', 'Other / VDI',
]


class _Skip(Exception):
    pass



#  Hosts, and the sites they belong to, that appear nowhere in this codebase.
#  The point of these is that a name from an estate we have never seen must
#  still resolve, because the fallback works positionally rather than by
#  recognising anyone's naming.
FALLBACK_CASES = [
    # (record, expected hostname, expected process)
    ('Nov 14 22:04:19 fw-perimeter-3.acme-industrial.example '
     'vendord[8821]: unrecognised proprietary payload 0x91',
     'fw-perimeter-3.acme-industrial.example', 'vendord'),
    # No domain at all, and a name this code has never been taught.
    ('Nov 14 22:05:00 zx9-edge-02 weird-agent: state transition A -> B',
     'zx9-edge-02', 'weird-agent'),
    # The record states its own host; that beats any positional guess.
    ('some entirely unknown format hostname=core-sw-11.contoso.example '
     'action=drop', 'core-sw-11.contoso.example', None),
    # A syslog PRI in front must not displace the host slot.
    ('<134>Nov 14 22:06:11 iot-mesh-07 collectord[12]: reading=41.2',
     'iot-mesh-07', 'collectord'),
]


def _check_fallback(failures):
    """The fallback must be vendor-agnostic, and must refuse to guess on
    binary. Both were real defects: it used to search only for this one
    customer's domains, and once made positional it began inventing hosts
    like "E.mu" out of the high-byte noise in rotated archives."""
    from core.parsers.generic_fallback import detect_generic_fallback

    checks = 0
    for record, want_host, want_proc in FALLBACK_CASES:
        r = detect_generic_fallback(record)
        checks += 1
        if r.get('hostname') != want_host:
            failures.append('fallback host %r, expected %r (%s)'
                            % (r.get('hostname'), want_host, record[:48]))
        if want_proc is not None:
            checks += 1
            if r.get('process') != want_proc:
                failures.append('fallback process %r, expected %r (%s)'
                                % (r.get('process'), want_proc, record[:48]))

    # Binary read as text: no invented host, no invented severity, and the
    # record still comes back rather than being dropped (requirement a).
    binary = ''.join(chr(c) for c in (0x80, 0x9d, 0xff, 0x1b, 0x93)) + 'E.mu' \
        + ''.join(chr(c) for c in (0x8f, 0xd2, 0x11, 0xa7, 0xfe, 0x03))
    r = detect_generic_fallback(binary)
    checks += 1
    if r.get('hostname') is not None:
        failures.append('fallback invented hostname %r from binary'
                        % r.get('hostname'))
    checks += 1
    if not r.get('attributes', {}).get('binary_content'):
        failures.append('binary record was not flagged binary_content')
    checks += 1
    if r.get('message') is None:
        failures.append('binary record lost its message entirely')
    return checks


#  Format detection, both directions. The bug these lock down: sniff_format()
#  consulted the extension first and returned unconditionally, so a CSV named
#  anything.log was parsed as syslog - header row and all - and every record
#  fell to the fallback. The opposite mistake is just as available, since
#  csv.Sniffer() will claim comma-heavy syslog as CSV, so both directions are
#  asserted here rather than only the one that broke.
_CSV_TEXT = ('timestamp,hostname,source_type,severity,message\n'
             '2026-09-03T09:49:00Z,fw-dmz-01,Firewall,WARNING,Connection denied\n'
             '2026-09-03T09:49:15Z,fw-dmz-01,Firewall,INFO,Session established\n')
_SYSLOG_TEXT = (
    '<166>Sep  3 09:16:11 asa-edge-01 %ASA-6-302013: Built inbound TCP, outside, ok\n'
    '<134>Sep  3 09:19:30 sw-core-02 %LINK-3-UPDOWN: Interface Gi0/24, changed, down\n'
    '<134>Sep  3 09:20:01 vpn-gw-01 openvpn[1123]: user01 TLS, ok, yes\n')
_XML_TEXT = ('<?xml version="1.0"?>\n<Events><Event><System>'
             '<Computer>SRV-1</Computer></System></Event></Events>\n')

SNIFF_CASES = [
    # (label, filename, text, expected format)
    ('CSV pasted under a .log name', 'pasted.log', _CSV_TEXT, 'csv'),
    ('CSV exported as export.log', 'export.log', _CSV_TEXT, 'csv'),
    ('CSV named .csv', 'a.csv', _CSV_TEXT, 'csv'),
    ('XML pasted under a .log name', 'pasted.log', _XML_TEXT, 'xml'),
    # Must NOT be mistaken for CSV despite the commas.
    ('comma-heavy syslog', 'pasted.log', _SYSLOG_TEXT, 'text'),
    # A PRI is not an XML tag.
    ('RFC 5424 PRI', 'pasted.log',
     '<134>1 2026-09-03T09:28:00Z nsx-mgr-03 NSX 3320 - message\n', 'text'),
    ('NDJSON', 'pasted.log', '{"a":1}\n{"a":2}\n', 'json'),
]


def _check_sniff(failures):
    from core.structured_readers import sniff_format
    checks = 0
    for label, filename, text, want in SNIFF_CASES:
        checks += 1
        got = sniff_format(filename, text[:2048])
        if got != want:
            failures.append('sniff %s: got %r, expected %r' % (label, got, want))
    return checks


#  Requirement (a) as an assertion rather than a claim.
#
#  parse() used to store the DETECTION form of the record - stripped, so a
#  regex would not trip on a leading space - as raw_message. A record ending
#  "request content = " came back without its trailing space, and losslessness
#  was quietly false for every record with surrounding whitespace (5 of 305 in
#  one real export). Trailing whitespace in a log distinguishes an empty field
#  from an absent one, and it is what a forensic hash of the original file
#  disagrees with.
#
#  The second half matters as much: the record id must NOT move as a result.
#  It is derived from the stripped form on purpose, so ids already in the
#  graph stay valid and two records differing only in trailing space remain
#  one event.
VERBATIM_CASES = [
    'Sep  3 09:25:10 web-srv-07 sshd[4412]: request content = ',
    '   <134>Sep  3 09:20:01 vpn-gw-01 openvpn[1123]: TLS ok',
    'Sep  3 09:25:44 web-srv-07 sudo: trailing tab\t',
    '{"eventTime":"2026-09-03T09:33:00Z","eventSource":"s3.amazonaws.com",'
    '"eventName":"DeleteBucket","errorCode":"AccessDenied"}  ',
    'plain line with no timestamp at all   ',
]


def _check_verbatim(failures):
    """raw_message must equal the input byte for byte, and the id must be
    computed from the stripped form so it does not move."""
    import hashlib
    from core.parser import LogParser

    parser = LogParser()
    checks = 0
    for record in VERBATIM_CASES:
        result = parser.parse(record)
        checks += 1
        if result.get('raw_message') != record:
            failures.append('raw_message altered: %r became %r'
                            % (record, result.get('raw_message')))
        checks += 1
        stripped = record.rstrip('\n').rstrip('\r').strip()
        ts = (result.get('timestamp')
              if result.get('timestamp_source') == 'event' else None)
        seed = '%s|%s|%s|%s' % (ts or 'no-event-timestamp', result['hostname'],
                                result['process'], stripped)
        expected = 'log-' + hashlib.md5(seed.encode('utf-8')).hexdigest()[:16]
        if result['id'] != expected:
            failures.append('id for %r is %s, expected %s (ids must be '
                            'derived from the stripped record)'
                            % (record[:40], result['id'], expected))
    return checks


#  CEF/LEEF timestamp normalization and LEEF delimiter inference.
#
#  Two defects, both silent, both in the formats security appliances actually
#  export in:
#
#   1. `rt=Sep 03 2026 09:23:22` is the format the CEF Implementation Standard
#      specifies, and no entry in _TS_FORMATS matched it. normalize_timestamp()
#      returns its input untouched when nothing matches, so the event kept a
#      non-ISO timestamp, `day` came out null, and every CEF record silently
#      left every date-range query and trend chart.
#
#   2. LEEF extension fields were split on the spec default (tab), so a
#      pipe-delimited record - what Check Point and several QRadar
#      integrations send without declaring a delimiter - yielded ONE field, in
#      which the first key swallowed the rest of the line. It looked fine
#      because the four header fields are in the same bag, so a record with
#      zero recovered attributes still reported five.
CEF_LEEF_CASES = [
    # (label, line, expected ISO prefix, minimum attribute count)
    ('CEF rt= spec format',
     'CEF:0|Palo Alto Networks|PAN-OS|10.2|threat|url|5|'
     'rt=Sep 03 2026 09:23:22 src=10.20.30.40 dst=93.184.216.34 act=deny',
     '2026-09-03T09:23:22', 8),
    ('LEEF pipe-delimited, undeclared',
     'LEEF:2.0|Check Point|Firewall|R81|Drop|devTime=1788412522000|'
     'src=10.20.30.42|dst=172.217.16.14|dstPort=80|proto=TCP|action=Drop|sev=6',
     '2026-', 11),
    ('LEEF tab-delimited, spec 1.0',
     'LEEF:1.0|Lancope|StealthWatch|1.0|41|devTime=Sep 03 2026 09:23:22\t'
     'src=10.0.0.1\tdst=10.0.0.2\tsev=5',
     '2026-09-03T09:23:22', 8),
]


def _check_cef_leef(failures):
    from core.parser import LogParser

    parser = LogParser()
    checks = 0
    for label, line, want_ts, min_attrs in CEF_LEEF_CASES:
        result = parser.parse(line)
        checks += 1
        ts = str(result.get('timestamp') or '')
        if not ts.startswith(want_ts):
            failures.append('%s: timestamp %r does not start with %r'
                            % (label, ts, want_ts))
        checks += 1
        if result.get('day') is None:
            failures.append('%s: day is null, so the event drops out of every '
                            'date-range query' % label)
        checks += 1
        n = len(result.get('attributes') or {})
        if n < min_attrs:
            failures.append('%s: %d attributes, expected at least %d'
                            % (label, n, min_attrs))
    return checks


#  The OCSF emitter, which exists so "OCSF-mapped" stops being a lookup table
#  nothing consumes. Only the three implemented classes are asserted; the
#  point of the last case is that an event with no auth or network evidence
#  lands in the catch-all rather than being forced into a class.
OCSF_CASES = [
    # (label, line, expected class_uid, expected status_id or None)
    ('Okta lockout', '{"published":"2026-09-03T09:41:30.000Z",'
     '"eventType":"user.account.lock","displayMessage":"Max sign in attempts '
     'exceeded","outcome":{"result":"DENY","reason":"LOCKED_OUT"},'
     '"actor":{"alternateId":"alice@corp.example"},'
     '"client":{"ipAddress":"203.0.113.61"}}', 3002, 2),
    ('sshd failed password',
     'Sep  3 09:25:10 web-srv-07 sshd[4412]: Failed password for invalid user '
     'admin from 203.0.113.101 port 52344 ssh2', 3002, 2),
    ('PAN-OS deny',
     'CEF:0|Palo Alto Networks|PAN-OS|10.2|threat|url|5|rt=Sep 03 2026 '
     '09:23:22 src=10.20.30.40 dst=93.184.216.34 spt=51514 dpt=443 act=deny',
     4001, 2),
    ('JVM GC, neither auth nor network',
     '[2026-09-03T09:32:10.442+0000][gc] GC(881) Pause Young 512M->128M',
     1008, None),
]


def _check_ocsf(failures):
    import json as _json
    import os as _os
    import sys as _sys

    analytics = _os.path.abspath(_os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)),
        '..', '..', 'analytics_pipeline'))
    if analytics not in _sys.path:
        _sys.path.insert(0, analytics)
    try:
        import ocsf
    except ImportError:
        # analytics_pipeline is a separate deployable; when it is not present
        # beside this one there is nothing to assert, and failing here would
        # be wrong.
        return 0

    from core.parser import LogParser
    parser = LogParser()
    checks = 0
    for label, line, want_class, want_status in OCSF_CASES:
        row = parser.parse(line)
        row['attributes_json'] = _json.dumps(row['attributes'])
        doc = ocsf.to_ocsf(row)
        checks += 1
        if doc['class_uid'] != want_class:
            failures.append('ocsf %s: class_uid %d, expected %d'
                            % (label, doc['class_uid'], want_class))
        checks += 1
        # type_uid is defined by the spec as class_uid * 100 + activity_id.
        if doc['type_uid'] != doc['class_uid'] * 100 + doc['activity_id']:
            failures.append('ocsf %s: type_uid %d violates '
                            'class_uid*100+activity_id' % (label, doc['type_uid']))
        if want_status is not None:
            checks += 1
            if doc.get('status_id') != want_status:
                failures.append('ocsf %s: status_id %r, expected %r'
                                % (label, doc.get('status_id'), want_status))
        checks += 1
        if doc.get('raw_data') != line:
            failures.append('ocsf %s: raw_data is not the original event' % label)
    return checks


#  Which detector must claim which line. Ordering in registry.py is the most
#  fragile thing in the parser - a broad new format placed too early takes
#  lines from a specific one, and nothing fails, the data just gets quietly
#  worse. logfmt did exactly this to Fortinet before these cases existed.
ATTRIBUTION_CASES = [
    ('FortiGate traffic',
     'date=2026-09-03 time=09:22:03 devname="FGT-EDGE-01" type="traffic" '
     'level="warning" srcip=10.20.30.41 dstip=8.8.8.8 action="blocked" '
     'msg="Denied by firewall policy"',
     'fortinet_kv'),
    ('containerd logfmt',
     'time="2026-06-12T04:06:00.759812460Z" level=info msg="starting signal '
     'loop" namespace=moby path=/run/containerd',
     'logfmt'),
    ('Palo Alto CEF',
     'CEF:0|Palo Alto Networks|PAN-OS|10.2|threat|url|5|rt=Sep 03 2026 '
     '09:23:22 src=10.20.30.40 act=deny',
     'cef'),
    ('Check Point LEEF',
     'LEEF:2.0|Check Point|Firewall|R81|Drop|devTime=1788412522000|'
     'src=10.20.30.42|dstPort=80|action=Drop|sev=6',
     'leef'),
    ('Cisco ASA with PRI',
     '<166>Sep  3 09:16:11 asa-edge-01 %ASA-6-302013: Built inbound TCP '
     'connection 129472',
     'pri_syslog'),
    ('Linux sshd',
     'Sep  3 09:25:10 web-srv-07 sshd[4412]: Failed password for invalid '
     'user admin from 203.0.113.101 port 52344 ssh2',
     'rfc3164_wrapper'),
    ('PostgreSQL with %u@%d',
     '2026-09-03 09:30:02.910 UTC [4415] app@billing FATAL:  password '
     'authentication failed for user "app"',
     'postgres_native_log'),
    ('AWS CloudTrail',
     '{"eventTime":"2026-09-03T09:33:00Z","eventSource":"s3.amazonaws.com",'
     '"eventName":"DeleteBucket","errorCode":"AccessDenied"}',
     'aws_cloudtrail'),
    ('Okta system log',
     '{"published":"2026-09-03T09:41:30.000Z","eventType":"user.account.lock",'
     '"displayMessage":"Max sign in attempts exceeded",'
     '"actor":{"alternateId":"a@b.example"}}',
     'okta_system_log'),
    ('Envoy sidecar',
     '[2026-09-03T09:38:44.201Z] "POST /api/checkout HTTP/1.1" 503 UF 0 91 12 '
     '- "10.244.0.9" "curl/8.4.0" "b1f2-4d" "api.internal" "10.244.2.31:8080"',
     'envoy_upstream_access'),
]


def _check_attribution(failures):
    from core.parser import LogParser

    parser = LogParser()
    checks = 0
    for label, line, expected in ATTRIBUTION_CASES:
        checks += 1
        got = parser.parse(line).get('matched_format')
        if got != expected:
            failures.append('attribution %s: claimed by %r, expected %r - '
                            'check detector ordering in registry.py'
                            % (label, got, expected))
    return checks

def _samples(name):
    path = os.path.join(SAMPLES, name)
    if not os.path.exists(path):
        # The handover ships no log files of any kind, synthetic ones
        # included, so this test skips rather than fails there. It is
        # meant to be run in the development tree.
        raise _Skip(path)
    return open(path, encoding='utf-8').read()


def main():
    # The fallback cases are self-contained, so they run everywhere - including
    # the handover, which ships no log files of any kind and therefore skips
    # everything below.
    failures = []
    checks = (_check_fallback(failures) + _check_sniff(failures)
              + _check_verbatim(failures) + _check_cef_leef(failures)
              + _check_ocsf(failures) + _check_attribution(failures))
    if failures:
        print('FAILED (fallback / format detection):')
        for f in failures:
            print('  ' + f)
        return 1
    print('%d checks passed (vendor-agnostic host, binary guard, format '
          'detection, verbatim raw, CEF/LEEF timestamps, OCSF classes, '
          'detector attribution)' % checks)
    try:
        return _run()
    except _Skip as missing:
        print('SKIPPED: sample-log checks, samples not present (%s)' % missing)
        return 0


def _run():
    parser = LogParser()
    failures = []
    checks = 0

    lines = [ln for ln in _samples('sample_logs.txt').splitlines() if ln.strip()]
    if len(lines) != len(CATEGORIES):
        raise SystemExit('sample_logs.txt has %d lines, CATEGORIES has %d - '
                         'they must stay in step' % (len(lines), len(CATEGORIES)))

    covered = {}
    for line, category in zip(lines, CATEGORIES):
        result = parser.parse(line)
        fmt = result.get('matched_format')
        checks += 1
        if fmt == 'generic_fallback':
            failures.append('%s: no named detector for %s' % (category, line[:60]))
        else:
            covered.setdefault(category, set()).add(fmt)

        # A claimed record that carries no message is not actually parsed.
        checks += 1
        if not str(result.get('message') or '').strip():
            failures.append('%s: %s produced an empty message' % (category, fmt))

    # XML and CSV are record-per-element/row, so they arrive through
    # structured_readers rather than the line detectors.
    for name, hint, expect_host in (('sample_logs.xml', 'xml', 'SRV-DC-02.corp.example'),
                                    ('sample_logs.csv', 'csv', 'fw-dmz-01')):
        records = list(iter_records(_samples(name), name, hint))
        checks += 1
        if not records:
            failures.append('%s: reader produced no records' % name)
            continue
        first = parser.parse(records[0])
        checks += 1
        if first.get('hostname') != expect_host:
            failures.append('%s: hostname %r, expected %r'
                            % (name, first.get('hostname'), expect_host))
        # Regression guard for the Windows Event XML <Data Name=..> shape,
        # whose values used to be dropped on the floor.
        checks += 1
        blob = json.dumps(first)
        if name.endswith('.xml') and 'administrator' not in blob:
            failures.append('sample_logs.xml: <Data Name=..> values were lost')
        checks += 1
        if str(first.get('message', '')).lstrip().startswith('{'):
            failures.append('%s: message is a raw JSON blob' % name)

    print('%d checks across %d source categories'
          % (checks, len(set(CATEGORIES))))
    if failures:
        print('\nFAILED:')
        for f in failures:
            print('  ' + f)
        return 1
    print('All passed. Named-detector coverage: %d/%d lines.'
          % (len(lines), len(lines)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
