"""
test_parsing_accuracy.py - Parser accuracy vs the ORIGINAL raw log text,
sampled randomly (not clustered near the file start) across the ingested
2M-record range. No LLM involved - this is purely "does the parser's
extraction match reality," independent of the chatbot entirely.

Fixes the previous naive check's false-negative problem: many real log
formats (syslog, ESXi, vCenter) don't print severity as a literal word at
all - the parser infers it from event semantics, which is correct behavior,
not an error. So severity is graded three ways instead of pass/fail:
  - CONFIRMED   : raw text contains the exact severity word extracted
  - INFERRED    : raw text contains no severity keyword at all (can't verify
                  either way - not counted as right or wrong)
  - CONTRADICTED: raw text contains a *different* severity keyword than what
                  was extracted - a real, unambiguous parser error

Also checks: hostname (must appear in raw text), process name (if any), and
IP address entities (regex-extracted from raw text, compared against what
the parser stored as :IPAddress entities) - IPs are objectively checkable
regardless of log format.
"""

import gzip
import json
import os
import random
import re
import sys

CORPUS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'ingestion_pipeline', 'output.json.gz'
)
RECORD_LIMIT = 2_000_000  # only records actually ingested
SAMPLE_SIZE = 100

_SEVERITY_WORDS = ['EMERGENCY', 'FATAL', 'ALERT', 'CRITICAL', 'ERROR', 'WARNING', 'WARN', 'NOTICE', 'INFO', 'DEBUG']
_IP_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')


def sample_random_records():
    """True random sample across the full ingested range, not clustered near
    the start of the file - uses reservoir sampling so it's a single
    streaming pass regardless of SAMPLE_SIZE vs RECORD_LIMIT."""
    random.seed(42)
    reservoir = []
    n = 0
    with gzip.open(CORPUS, 'rt', encoding='utf-8') as f:
        for i, raw_line in enumerate(f):
            if i >= RECORD_LIMIT:
                break
            line = raw_line.strip()
            if not line or line in ('[', ']'):
                continue
            if line.endswith(','):
                line = line[:-1]
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or not rec.get('raw_message'):
                continue
            n += 1
            if len(reservoir) < SAMPLE_SIZE:
                reservoir.append(rec)
            else:
                j = random.randint(0, n - 1)
                if j < SAMPLE_SIZE:
                    reservoir[j] = rec
    return reservoir


def check_record(rec):
    raw = rec.get('raw_message', '') or ''
    raw_upper = raw.upper()
    result = {'id': rec['id'], 'source_type': rec.get('source_type', '')}

    # --- hostname ---
    hostname = rec.get('hostname', '')
    if hostname and hostname != 'unknown-host':
        result['hostname_check'] = 'confirmed' if hostname.lower() in raw.lower() else 'MISMATCH'
    else:
        result['hostname_check'] = 'n/a (unknown-host)'

    # --- severity: confirmed / inferred / contradicted ---
    severity = (rec.get('severity') or '').upper()
    words_present = [w for w in _SEVERITY_WORDS if re.search(rf'\b{w}\b', raw_upper)]
    if severity and severity in words_present:
        result['severity_check'] = 'confirmed'
    elif not words_present:
        result['severity_check'] = 'inferred (no severity word in raw text)'
    elif severity and severity not in words_present:
        result['severity_check'] = f'CONTRADICTED (raw text says {words_present}, parser said {severity})'
    else:
        result['severity_check'] = 'n/a'

    # --- process name ---
    process = rec.get('process', '')
    if process:
        result['process_check'] = 'confirmed' if process.lower() in raw.lower() else 'MISMATCH'
    else:
        result['process_check'] = 'n/a (no process extracted)'

    # --- IP entities: objectively checkable via regex regardless of format ---
    extracted_ips = {e['value'] for e in (rec.get('entities') or []) if e.get('type') == 'ip'}
    raw_ips = set(_IP_RE.findall(raw))
    if extracted_ips or raw_ips:
        missed = raw_ips - extracted_ips        # IPs in raw text the parser didn't extract
        hallucinated = extracted_ips - raw_ips  # IPs the parser claims exist but aren't in raw text
        result['ip_check'] = {
            'raw_ips': sorted(raw_ips), 'extracted_ips': sorted(extracted_ips),
            'missed': sorted(missed), 'hallucinated': sorted(hallucinated),
        }
    else:
        result['ip_check'] = 'n/a (no IPs either side)'

    return result


def main():
    print(f"Randomly sampling {SAMPLE_SIZE} records from the first {RECORD_LIMIT:,} of {CORPUS}...")
    records = sample_random_records()
    print(f"Sampled {len(records)} records.\n")

    results = [check_record(r) for r in records]
    with open('test_parsing_results.json', 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, default=str)

    host_checked = [r for r in results if r['hostname_check'] not in ('n/a (unknown-host)',)]
    host_ok = sum(1 for r in host_checked if r['hostname_check'] == 'confirmed')

    sev_confirmed = sum(1 for r in results if r['severity_check'] == 'confirmed')
    sev_inferred = sum(1 for r in results if r['severity_check'].startswith('inferred'))
    sev_contradicted = [r for r in results if r['severity_check'].startswith('CONTRADICTED')]

    proc_checked = [r for r in results if r['process_check'] not in ('n/a (no process extracted)',)]
    proc_ok = sum(1 for r in proc_checked if r['process_check'] == 'confirmed')

    ip_checked = [r for r in results if isinstance(r['ip_check'], dict)]
    ip_perfect = sum(1 for r in ip_checked if not r['ip_check']['missed'] and not r['ip_check']['hallucinated'])
    ip_hallucinated = [r for r in ip_checked if r['ip_check']['hallucinated']]
    ip_missed = [r for r in ip_checked if r['ip_check']['missed']]

    print("=" * 70)
    print(f"Sample size: {len(results)} records, randomly drawn from the first {RECORD_LIMIT:,} of the real corpus\n")

    print(f"HOSTNAME  : {host_ok}/{len(host_checked)} confirmed in raw text "
          f"({host_ok/len(host_checked)*100:.0f}%)" if host_checked else "HOSTNAME: none checkable")

    print(f"SEVERITY  : {sev_confirmed} confirmed (word literally in raw text), "
          f"{sev_inferred} inferred (raw text has no severity word - not verifiable either way), "
          f"{len(sev_contradicted)} CONTRADICTED (real error)")
    if sev_contradicted:
        for r in sev_contradicted:
            print(f"    id={r['id']} source={r['source_type']}: {r['severity_check']}")

    print(f"PROCESS   : {proc_ok}/{len(proc_checked)} confirmed in raw text "
          f"({proc_ok/len(proc_checked)*100:.0f}%)" if proc_checked else "PROCESS: none checkable")
    if len(proc_checked) - proc_ok:
        for r in proc_checked:
            if r['process_check'] == 'MISMATCH':
                print(f"    MISMATCH id={r['id']} source={r['source_type']}")

    print(f"IP ENTITIES: {ip_perfect}/{len(ip_checked)} exact match (no missed, no hallucinated) "
          f"({ip_perfect/len(ip_checked)*100:.0f}%)" if ip_checked else "IP ENTITIES: none checkable")
    if ip_hallucinated:
        print(f"    {len(ip_hallucinated)} record(s) with a hallucinated IP (extracted but not in raw text):")
        for r in ip_hallucinated:
            print(f"      id={r['id']}: extracted={r['ip_check']['extracted_ips']} raw={r['ip_check']['raw_ips']}")
    if ip_missed:
        print(f"    {len(ip_missed)} record(s) with a missed IP (in raw text but not extracted):")
        for r in ip_missed:
            print(f"      id={r['id']}: extracted={r['ip_check']['extracted_ips']} raw={r['ip_check']['raw_ips']}")


if __name__ == '__main__':
    main()
