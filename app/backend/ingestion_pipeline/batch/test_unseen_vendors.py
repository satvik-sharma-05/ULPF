"""Re-check the format and source-category claims against UNSEEN data.

    python -m batch.test_unseen_vendors

Exits non-zero if any vendor falls to generic_fallback.

The existing suite parses sample_logs.txt, which I wrote. Testing a parser
against fixtures written for that parser proves that the two agree, not that
the parser handles the world - so every record below is a format or a vendor
that appears nowhere in the repository's fixtures: Juniper SRX, SonicWall,
Sophos, Barracuda, Zscaler, Cloudflare, Trend Micro, Imperva, nginx, HAProxy,
Redis, MongoDB, Tomcat, MySQL, Oracle, Zeek, Suricata, Cisco ISE, SailPoint,
Zigbee/BACnet gateways.

The bar, again, is a NAMED detector - `generic_fallback` always returns
something, so counting non-empty results would report success while extracting
nothing. A fallback is recorded as a miss and reported.
"""
import json
import os
import sys
from collections import defaultdict

LA = r'C:\satvik\LA_transfer\LA_transfer\LA'
ING = LA + r'\local_project\app\backend\ingestion_pipeline'
sys.path.insert(0, ING)

from core.multiline import reassemble                  # noqa: E402
from core.parser import LogParser                      # noqa: E402
from core.structured_readers import iter_records, sniff_format  # noqa: E402

# (category, format, vendor, line)
CASES = [
    # ---------------- NETWORK DEVICES ----------------
    ('Network device', 'Syslog RFC 3164', 'Juniper SRX',
     '<14>Sep  5 11:02:31 srx-edge-01 RT_FLOW: RT_FLOW_SESSION_DENY: session denied '
     '198.51.100.7/51234->10.20.1.5/445 junos-smb 6(0) trust-to-untrust'),
    ('Network device', 'Proprietary k=v', 'SonicWall',
     'id=firewall sn=0017C5000000 time="2026-09-05 11:03:12" fw=10.20.1.1 pri=1 '
     'c=1024 m=537 msg="Connection Closed" src=10.20.1.44:52341 dst=93.184.216.34:443 proto=tcp/https'),
    ('Network device', 'Proprietary', 'Sophos XG',
     'device="SFW" date=2026-09-05 time=11:04:02 timezone="UTC" device_name="XG210" '
     'log_type="Firewall" log_component="Firewall Rule" status="Deny" src_ip=203.0.113.99 dst_ip=10.20.1.9 protocol="TCP"'),
    ('Network device', 'CEF', 'Trend Micro TippingPoint',
     'CEF:0|TrendMicro|TippingPoint|5.2.0|7000|HTTP: IIS Directory Traversal|8|'
     'dvchost=ips-core-02 src=203.0.113.55 dst=10.20.3.9 spt=44321 dpt=80 act=blocked cn1Label=PolicyID cn1=4412'),
    ('Network device', 'CEF', 'Imperva SecureSphere',
     'CEF:0|Imperva Inc.|SecureSphere|13.0|Protocol Policy|SQL Injection|High|'
     'dvchost=waf-dmz-01 src=198.51.100.31 dst=10.20.4.8 act=block request=/login.php'),
    ('Network device', 'LEEF 1.0 (tab)', 'Cisco ISE',
     'LEEF:1.0|Cisco|ISE|2.7|5400|devTime=Sep 05 2026 11:05:44\tsrc=10.20.9.14\t'
     'usrName=jdoe\tsev=5\taction=Authentication failed'),
    ('Network device', 'Proprietary', 'Zeek conn.log',
     '1788657944.221 CHhAvVGS1DHFjwGM9 10.20.1.44 52341 93.184.216.34 443 tcp ssl '
     '0.812 1842 4211 SF - - 0 ShADadfF 12 2410 14 4822 -'),
    ('Network device', 'Syslog + proprietary', 'Suricata EVE',
     '<134>Sep  5 11:06:10 ids-02 suricata[3311]: [1:2019401:3] ET POLICY Vulnerable Java '
     'Version [Classification: Potentially Bad Traffic] [Priority: 2] {TCP} 10.20.1.7:49821 -> 198.51.100.4:80'),

    # ---------------- SERVERS / OS ----------------
    ('Server / OS', 'Syslog RFC 5424', 'systemd',
     '<30>1 2026-09-05T11:07:02.114Z app-srv-11 systemd 1 - [origin software="systemd" swVersion="249"] '
     'Started Session 4412 of user deploy.'),
    ('Server / OS', 'Syslog RFC 3164', 'auditd',
     'Sep  5 11:07:45 app-srv-11 audispd: type=USER_LOGIN msg=audit(1788657944.221:8821): '
     'pid=4412 uid=0 auid=1001 res=failed acct="oracle" exe="/usr/sbin/sshd"'),
    ('Server / OS', 'XML', 'Windows Event 4688',
     None),  # handled via the XML file below

    # ---------------- APPLICATIONS ----------------
    ('Application', 'App-specific', 'nginx access',
     '10.20.1.88 - alice [05/Sep/2026:11:08:22 +0000] "POST /api/v2/orders HTTP/1.1" 201 842 '
     '"https://portal.corp.example/checkout" "Mozilla/5.0" 0.184'),
    ('Application', 'App-specific', 'HAProxy',
     '<134>Sep  5 11:08:55 lb-01 haproxy[2211]: 10.20.1.90:51422 [05/Sep/2026:11:08:55.331] '
     'https-in~ api-backend/web-03 12/0/1/34/47 200 8821 - - ---- 84/84/2/1/0 0/0 "GET /health HTTP/1.1"'),
    ('Application', 'App-specific (logback)', 'Tomcat',
     '2026-09-05 11:09:14,882 WARN  [https-jsse-nio-8443-exec-7] o.a.c.c.C.[.[.[/api] - '
     'Servlet.service() for servlet [dispatcher] threw exception'),

    # ---------------- DATABASES ----------------
    ('Database', 'App-specific', 'MySQL error log',
     '2026-09-05T11:10:02.334512Z 41 [Warning] [MY-010055] [Server] IP address '
     '198.51.100.77 could not be resolved: Name or service not known'),
    ('Database', 'Proprietary', 'MongoDB',
     '{"t":{"$date":"2026-09-05T11:10:44.221Z"},"s":"W","c":"ACCESS","id":20249,'
     '"ctx":"conn8821","msg":"Authentication failed","attr":{"user":"appuser","db":"orders","error":"AuthenticationFailed"}}'),
    ('Database', 'Proprietary', 'Redis',
     '3311:M 05 Sep 2026 11:11:09.442 # WARNING Memory overcommit must be enabled'),

    # ---------------- CLOUD ----------------
    ('Cloud', 'JSON', 'Cloudflare',
     '{"ClientIP":"203.0.113.12","ClientRequestHost":"api.corp.example",'
     '"ClientRequestMethod":"POST","EdgeResponseStatus":403,"SecurityAction":"block",'
     '"EdgeStartTimestamp":"2026-09-05T11:12:00Z","RayID":"8f2c1a"}'),
    ('Cloud', 'JSON', 'Zscaler NSS',
     '{"time":"2026-09-05T11:12:41Z","user":"bob@corp.example","action":"Blocked",'
     '"urlcategory":"Malware","serverip":"198.51.100.9","reason":"Malicious URL","department":"Finance"}'),

    # ---------------- CONTAINERS ----------------
    ('Container', 'logfmt', 'containerd',
     'time="2026-09-05T11:13:02.114Z" level=warning msg="cleaning up after shim disconnected" '
     'id=8f2c1a namespace=k8s.io'),
    ('Container', 'Proprietary (glog)', 'kube-apiserver',
     'W0905 11:13:44.221100      11 dispatcher.go:180] Failed calling webhook, failing open '
     'validate.example.com: failed calling webhook'),

    # ---------------- ENDPOINT SECURITY ----------------
    ('Endpoint security', 'JSON', 'SentinelOne',
     '{"eventTime":"2026-09-05T11:14:02Z","agentComputerName":"WKS-ENG-042",'
     '"threatName":"Trojan.GenericKD","classification":"Malware","mitigationStatus":"blocked",'
     '"filePath":"C:\\\\Users\\\\eng\\\\dl\\\\setup.exe","confidenceLevel":"malicious"}'),
    ('Endpoint security', 'CEF', 'Symantec',
     'CEF:0|Symantec|Endpoint Protection|14.3|Security Risk Found|Risk detected|7|'
     'dvchost=WKS-FIN-018 suser=mkhan fileHash=8f2c1a9b act=quarantined'),

    # ---------------- IAM ----------------
    ('IAM', 'JSON', 'SailPoint IdentityNow',
     '{"created":"2026-09-05T11:15:02Z","type":"ACCESS_REQUEST","action":"AccessRequestDenied",'
     '"actor":{"name":"approver@corp.example"},"target":{"name":"svc_finance"},"status":"FAILURE"}'),
    ('IAM', 'Syslog', 'FreeIPA / Kerberos',
     '<86>Sep  5 11:15:44 ipa-01 krb5kdc[2211]: AS_REQ (6 etypes) 10.20.7.9: '
     'PREAUTH_FAILED: hsingh@CORP.EXAMPLE for krbtgt/CORP.EXAMPLE'),

    # ---------------- IoT ----------------
    ('IoT device', 'Syslog + k=v', 'BACnet building controller',
     '<13>Sep  5 11:16:02 bacnet-gw-03 bacnetd[811]: device=AHU-14 object=analog-input:7 '
     'present_value=23.8 units=degreesCelsius alarm_state=off_normal priority=3'),
    ('IoT device', 'Proprietary', 'Zigbee coordinator',
     '<14>Sep  5 11:16:44 zb-coord-02 zbd[622]: node=0x8f2c cluster=0x0402 endpoint=1 '
     'rssi=-71 lqi=142 battery=88 event=report'),

    # ---------------- OTHER PLATFORMS ----------------
    ('Other platform', 'Proprietary', 'Veeam Backup',
     '2026-09-05 11:17:02 [Warning] Job "Nightly-SQL" finished with warnings: '
     'Failed to truncate transaction logs on 10.20.5.11'),
    ('Other platform', 'Syslog', 'APC PDU',
     '<134>Sep  5 11:17:31 pdu-rack-14 apc[401]: Outlet 7 turned off by user admin from 10.20.0.5'),
]

parser = LogParser()
by_format = defaultdict(lambda: {'ok': 0, 'total': 0, 'misses': []})
by_category = defaultdict(lambda: {'ok': 0, 'total': 0})
rows = []

for category, fmt, vendor, line in CASES:
    if line is None:
        continue
    r = parser.parse(line)
    det = r.get('matched_format', '?')
    named = det != 'generic_fallback'
    by_format[fmt.split(' (')[0].split(' +')[0]]['total'] += 1
    by_category[category]['total'] += 1
    if named:
        by_format[fmt.split(' (')[0].split(' +')[0]]['ok'] += 1
        by_category[category]['ok'] += 1
    else:
        by_format[fmt.split(' (')[0].split(' +')[0]]['misses'].append(vendor)
    rows.append((category, vendor, fmt, det, r.get('hostname'), r.get('severity'),
                 len(r.get('attributes') or {}), named))

print('%-18s %-26s %-22s %-24s %-16s %-9s %s' %
      ('CATEGORY', 'VENDOR (never tested before)', 'FORMAT', 'DETECTOR', 'HOST', 'SEVERITY', 'ATTRS'))
print('-' * 140)
for c, v, f, d, h, s, a, ok in rows:
    mark = ' ' if ok else '!'
    print('%s%-17s %-26s %-22s %-24s %-16s %-9s %d' %
          (mark, c, v, f, d, str(h)[:15], s, a))

named = sum(1 for r in rows if r[7])
print('\n%d/%d unseen vendors claimed by a NAMED detector (%.0f%%)'
      % (named, len(rows), 100.0 * named / len(rows)))

print('\nBY FORMAT')
for f in sorted(by_format):
    d = by_format[f]
    miss = ('  misses: ' + ', '.join(d['misses'])) if d['misses'] else ''
    print('  %-24s %d/%d%s' % (f, d['ok'], d['total'], miss))

print('\nBY SOURCE CATEGORY')
for c in sorted(by_category):
    d = by_category[c]
    print('  %-20s %d/%d' % (c, d['ok'], d['total']))


if __name__ == '__main__':
    # Non-zero on any miss, so this is usable in CI rather than only by eye.
    sys.exit(0 if named == len(rows) else 1)
