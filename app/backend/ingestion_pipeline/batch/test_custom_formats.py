"""
batch/test_custom_formats.py - Coverage for the formats "Custom logs" mode
accepts: Syslog, JSON, JSONL, XML, CSV, CEF, LEEF, and an unknown vendor
shape that must still survive via the generic fallback.

    python -m batch.test_custom_formats

Deliberately dependency-free (no pytest) and Neo4j-free, matching the rest of
this pipeline's "the parser is pure standard library" property - so it runs
on a bare Python install, including on the airgapped VM.

What it actually asserts is the contract "Custom logs" mode depends on: for
every format, the record count is right and the fields the graph is built
from (hostname / severity / timestamp / entities) are genuinely extracted -
not merely that parse() returned without raising, which it always does.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.parser import LogParser
from core.structured_readers import detect_format_label, iter_records

_FAILURES = []
_CHECKS = 0


def check(label, condition, detail=''):
    global _CHECKS
    _CHECKS += 1
    if not condition:
        _FAILURES.append(f"{label}{f' - {detail}' if detail else ''}")


def parse_all(text, filename, fmt='auto'):
    parser = LogParser()
    return [parser.parse(record) for record in iter_records(text, filename, fmt)]


def test_syslog():
    text = (
        "Jun 12 04:14:59 esx01 hostd[2098]: Failed to open /vmfs/volumes/datastore1\n"
        "Jun 12 04:15:00 esx01 sudo:     root : PWD=/ ; USER=root ; COMMAND=/usr/bin/systemctl status\n"
    )
    logs = parse_all(text, 'messages.log')
    check('syslog: record count', len(logs) == 2, f"got {len(logs)}")
    check('syslog: hostname', all(l['hostname'] == 'esx01' for l in logs))
    check('syslog: sudo command detected',
          any(l['source_type'] == 'LinuxAudit' for l in logs),
          [l['source_type'] for l in logs])
    check('syslog: sudo extracts the account',
          any(any(e['type'] == 'user' for e in l['entities']) for l in logs))


def test_json_and_jsonl():
    single = '{"logs":[{"time":"2026-06-12T04:14:59Z","host":"web01","level":"WARNING","service":"nginx","msg":"upstream timed out 10.0.0.5"}]}'
    logs = parse_all(single, 'export.json')
    check('json: record count', len(logs) == 1, f"got {len(logs)}")
    check('json: hostname', logs and logs[0]['hostname'] == 'web01')
    check('json: severity', logs and logs[0]['severity'] == 'WARNING')
    check('json: ip entity',
          logs and any(e['type'] == 'ip' and e['value'] == '10.0.0.5' for e in logs[0]['entities']))

    jsonl = ('{"timestamp":"2026-06-12T05:00:00Z","hostname":"k8s-1","severity":"ERROR","message":"crashloop"}\n'
             '{"timestamp":"2026-06-12T05:00:01Z","hostname":"k8s-2","severity":"INFO","message":"ok"}')
    logs = parse_all(jsonl, 'events.jsonl')
    check('jsonl: record count', len(logs) == 2, f"got {len(logs)}")
    check('jsonl: severities', [l['severity'] for l in logs] == ['ERROR', 'INFO'])


def test_csv():
    text = ("timestamp,host,severity,process,message\n"
            "2026-06-12T04:14:59Z,esx01,ERROR,hostd,Failed to open datastore for vm-1203\n"
            "2026-06-12T04:15:00Z,esx02,INFO,vpxa,Task completed\n")
    logs = parse_all(text, 'export.csv')
    check('csv: record count', len(logs) == 2, f"got {len(logs)}")
    check('csv: hostnames', [l['hostname'] for l in logs] == ['esx01', 'esx02'])
    check('csv: severity + score', logs and logs[0]['severity'] == 'ERROR' and logs[0]['severity_score'] == 3)
    check('csv: process', logs and logs[0]['process'] == 'hostd')
    check('csv: vm entity',
          logs and any(e['type'] == 'vm' and e['value'] == 'vm-1203' for e in logs[0]['entities']))


def test_csv_non_severity_column():
    """A 'priority' column holding a syslog facility number must NOT become
    the severity - it would put '13' into a (:Severity) node and break every
    severity_score filter downstream."""
    text = "time,host,priority,msg\n2026-06-12T04:14:59Z,web01,13,connection refused\n"
    logs = parse_all(text, 'weird.csv')
    check('csv: numeric priority is not used as severity',
          logs and logs[0]['severity'] in ('INFO', 'ERROR', 'WARNING'),
          logs and logs[0]['severity'])
    check('csv: unrecognized priority kept as an attribute',
          logs and 'priority' in logs[0]['attributes'])


def test_xml():
    text = ('<Events>'
            '<Event><TimeCreated>2026-06-12T04:14:59Z</TimeCreated><Computer>dc01</Computer>'
            '<Level>ERROR</Level><Provider>Security-Auditing</Provider>'
            '<Message>Logon failure for admin from 10.1.2.3</Message></Event>'
            '<Event><TimeCreated>2026-06-12T04:15:10Z</TimeCreated><Computer>dc02</Computer>'
            '<Level>INFO</Level><Provider>Service-Control</Provider>'
            '<Message>Service started</Message></Event>'
            '</Events>')
    logs = parse_all(text, 'events.xml')
    check('xml: record count', len(logs) == 2, f"got {len(logs)}")
    check('xml: hostnames', [l['hostname'] for l in logs] == ['dc01', 'dc02'])
    check('xml: severity', logs and logs[0]['severity'] == 'ERROR')
    check('xml: process from Provider', logs and logs[0]['process'] == 'Security-Auditing')
    check('xml: ip entity',
          logs and any(e['type'] == 'ip' and e['value'] == '10.1.2.3' for e in logs[0]['entities']))


def test_cef():
    text = ('Jun 12 04:14:59 fw01 CEF:0|Palo Alto|PAN-OS|10.2|threat-1234|Malware detected|8|'
            'src=10.1.2.3 dst=10.4.5.6 spt=443 suser=jdoe dvchost=fw01.corp msg=Blocked outbound C2')
    logs = parse_all(text, 'firewall.log')
    check('cef: record count', len(logs) == 1, f"got {len(logs)}")
    log = logs[0] if logs else {}
    check('cef: matched the cef detector', log.get('matched_format') == 'cef', log.get('matched_format'))
    check('cef: source_type', log.get('source_type') == 'CEF')
    check('cef: severity 8 -> ERROR', log.get('severity') == 'ERROR', log.get('severity'))
    check('cef: hostname from dvchost', log.get('hostname') == 'fw01.corp', log.get('hostname'))
    entities = {(e['type'], e['value']) for e in log.get('entities', [])}
    check('cef: src ip entity', ('ip', '10.1.2.3') in entities, entities)
    check('cef: dst ip entity', ('ip', '10.4.5.6') in entities, entities)
    check('cef: suser entity', ('user', 'jdoe') in entities, entities)


def test_leef():
    text = ('LEEF:2.0|IBM|QRadar|1.0|4624|^|'
            'src=192.168.1.10^dst=192.168.1.20^usrName=admin^sev=9^identHostName=dc01')
    logs = parse_all(text, 'qradar.log')
    log = logs[0] if logs else {}
    check('leef: matched the leef detector', log.get('matched_format') == 'leef', log.get('matched_format'))
    check('leef: source_type', log.get('source_type') == 'LEEF')
    check('leef: severity 9 -> CRITICAL', log.get('severity') == 'CRITICAL', log.get('severity'))
    check('leef: hostname from identHostName', log.get('hostname') == 'dc01', log.get('hostname'))
    entities = {(e['type'], e['value']) for e in log.get('entities', [])}
    check('leef: usrName entity', ('user', 'admin') in entities, entities)

    # LEEF 1.0 has no delimiter field and defaults to tab.
    v1 = 'LEEF:1.0|Lancope|StealthWatch|1.0|alert-99|\tsrc=10.0.0.1\tsev=2\tidentHostName=sw01'
    logs = parse_all(v1, 'sw.log')
    log = logs[0] if logs else {}
    check('leef 1.0: matched', log.get('matched_format') == 'leef', log.get('matched_format'))
    check('leef 1.0: hostname', log.get('hostname') == 'sw01', log.get('hostname'))
    check('leef 1.0: severity 2 -> INFO', log.get('severity') == 'INFO', log.get('severity'))


def test_proprietary_vendor_shape():
    """An unknown vendor format has no detector, so it lands on the generic
    fallback - which must still produce a usable record rather than dropping
    it. That guarantee is what makes "any proprietary format" a real claim."""
    text = ('##ACME-FW## 2026-06-12 04:14:59 | sev=CRITICAL | node=acme-edge-7 | '
            'evt=TUNNEL_DOWN | peer=10.9.8.7 | detail=IPSec SA expired\n')
    logs = parse_all(text, 'acme.log')
    check('proprietary: record count', len(logs) == 1, f"got {len(logs)}")
    log = logs[0] if logs else {}
    check('proprietary: nothing was dropped', bool(log.get('id')))
    check('proprietary: message preserved', 'TUNNEL_DOWN' in (log.get('message') or ''))
    check('proprietary: severity found in text',
          log.get('severity') == 'CRITICAL', log.get('severity'))
    check('proprietary: peer ip still extracted',
          any(e['type'] == 'ip' and e['value'] == '10.9.8.7' for e in log.get('entities', [])))


def test_format_detection_labels():
    cases = [
        ('a.csv', 'h1,h2\n1,2\n', 'CSV'),
        ('a.xml', '<Events><Event/></Events>', 'XML'),
        ('a.json', '{"a":1}', 'JSON / JSON Lines'),
        ('a.log', 'Jun 12 04:14:59 host proc: hello', 'Syslog / CEF / LEEF / plain text'),
    ]
    for filename, text, expected in cases:
        actual = detect_format_label(filename, text)
        check(f'sniff {filename}', actual == expected, f"expected {expected}, got {actual}")

    # A file whose extension lies about its contents: the extension wins for
    # .csv/.xml/.json (an explicit signal), but an unknown extension falls
    # through to content sniffing.
    check('sniff unknown ext with json body',
          detect_format_label('data.dat', '{"a":1}') == 'JSON / JSON Lines')
    check('sniff unknown ext with xml body',
          detect_format_label('data.dat', '<Events><Event/></Events>') == 'XML')


def test_ids_are_stable():
    """Re-ingesting the same file must MERGE onto the same (:Log), not
    duplicate it - which depends entirely on the id being deterministic."""
    text = "timestamp,host,severity,message\n2026-06-12T04:14:59Z,esx01,ERROR,disk fail\n"
    first = parse_all(text, 'x.csv')
    second = parse_all(text, 'x.csv')
    check('stable ids across runs',
          [l['id'] for l in first] == [l['id'] for l in second])


def main():
    tests = [
        test_syslog, test_json_and_jsonl, test_csv, test_csv_non_severity_column,
        test_xml, test_cef, test_leef, test_proprietary_vendor_shape,
        test_format_detection_labels, test_ids_are_stable,
    ]
    for test in tests:
        try:
            test()
        except Exception as e:
            _FAILURES.append(f"{test.__name__} raised {type(e).__name__}: {e}")

    print(f"{_CHECKS} checks across {len(tests)} format tests")
    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILED:")
        for failure in _FAILURES:
            print(f"  - {failure}")
        sys.exit(1)
    print("All passed.")


if __name__ == '__main__':
    main()
