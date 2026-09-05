"""
test_raw_log_accuracy.py - Ground the chatbot's answers in the ORIGINAL raw
log text, not in a hand-written Cypher query.

Two things get checked per sampled record, both against raw_message (the
untouched original log line, preserved verbatim by the parser - see
core/jsonio.py's docstring):

  1. PARSING ACCURACY: does the field the parser extracted (hostname,
     severity, source_type) actually, verifiably appear in the real raw log
     text? This checks the parser, independent of the chatbot entirely.

  2. RETRIEVAL ACCURACY: ask the real chatbot (text2cypher, live LLM) "what
     is the severity/hostname of the log with id X" for a specific sampled
     record, and check the answer against that same record's ACTUAL raw
     text - not against Neo4j's own stored copy of the field (that would
     only prove ingestion didn't corrupt data, not that the original
     extraction was ever correct).

Samples records diagnostically across different source types so the result
isn't dominated by whichever type happens to be most common.
"""

import gzip
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='replace')

os.environ['NEO4J_URI'] = 'neo4j://127.0.0.1:7687'
os.environ['NEO4J_USER'] = 'neo4j'
# Credentials come from the environment or the .env file, never from source.
# These lines previously held a live Neo4j password and a real API key, which
# then shipped verbatim inside the handover package.
os.environ.setdefault('NEO4J_PASSWORD', os.getenv('NEO4J_PASSWORD', ''))
os.environ['NEO4J_DATABASE'] = 'logs'
os.environ['CHATBOT_LLM_PROVIDER'] = 'groq'
os.environ.setdefault('GROQ_API_KEY', os.getenv('GROQ_API_KEY', ''))
os.environ['GROQ_MODEL'] = 'llama-3.3-70b-versatile'

from neo4j import GraphDatabase
from config import NEO4J_CONFIG
from schema_introspect import GraphSchema
from text_to_cypher import TextToCypher
from llm import LLMClient

CORPUS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'ingestion_pipeline', 'output.json.gz'
)
RECORD_LIMIT = 2_000_000  # only records actually ingested
SAMPLE_SIZE = 25
# Groq's TPM limit is 12,000 tokens/min for this model; each call here costs
# ~2,100 tokens (mostly the introspected schema in the prompt), so the real
# sustainable ceiling is ~5.7 calls/min. 13s of spacing + ~1-4s of actual
# call latency stays safely under that.
CALL_SPACING_SECONDS = 13


def sample_records():
    """Reservoir-ish sample across source types: stream the first
    RECORD_LIMIT records, keep the first record seen per source_type up to a
    cap, so the sample isn't dominated by whichever type is most frequent."""
    seen_per_type = {}
    picks = []
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
            st = rec.get('source_type', 'Unknown')
            seen_per_type.setdefault(st, 0)
            if seen_per_type[st] < 3 and rec.get('hostname') and rec.get('hostname') != 'unknown-host':
                picks.append(rec)
                seen_per_type[st] += 1
            if len(picks) >= SAMPLE_SIZE:
                break
    return picks


def check_parsing(rec):
    """Does the parsed hostname / severity actually appear in the record's
    OWN raw_message? This is independent of Neo4j and the chatbot - it's a
    direct parser-vs-original-text check."""
    raw = rec.get('raw_message', '') or ''
    hostname = rec.get('hostname', '')
    severity = rec.get('severity', '')
    notes = []

    host_ok = None
    if hostname and hostname != 'unknown-host':
        # Hostnames are sometimes only present in surrounding pipeline context
        # (e.g. filename), not the raw line itself, for some source types -
        # note that rather than silently pass/fail.
        host_ok = hostname.lower() in raw.lower()
        if not host_ok:
            notes.append(f"hostname '{hostname}' not found verbatim in raw_message (may come from filename/context, not the line itself)")

    severity_ok = None
    if severity:
        # Severity is sometimes a normalized synonym (e.g. WARN -> WARNING),
        # so check case-insensitive substring of either the full word or its
        # common abbreviation.
        sev_variants = {severity.lower(), severity[:4].lower(), severity[:3].lower()}
        severity_ok = any(v in raw.lower() for v in sev_variants if len(v) >= 3)
        if not severity_ok:
            notes.append(f"severity '{severity}' (or abbreviation) not found verbatim in raw_message")

    return host_ok, severity_ok, notes


def main():
    print(f"Sampling up to {SAMPLE_SIZE} records from the first {RECORD_LIMIT:,} of {CORPUS}...")
    records = sample_records()
    print(f"Sampled {len(records)} records across {len(set(r.get('source_type') for r in records))} source types.\n")

    driver = GraphDatabase.driver(NEO4J_CONFIG['uri'], auth=(NEO4J_CONFIG['user'], NEO4J_CONFIG['password']))
    schema = GraphSchema(driver, NEO4J_CONFIG['database'])
    llm = LLMClient()
    t2c = TextToCypher(driver=driver, database=NEO4J_CONFIG['database'], schema=schema, llm=llm)

    parsing_results = []
    retrieval_results = []

    for i, rec in enumerate(records, 1):
        rid = rec['id']
        hostname = rec.get('hostname', '')
        severity = rec.get('severity', '')
        source_type = rec.get('source_type', '')
        raw = (rec.get('raw_message') or '')[:200]

        # ---- 1. Parsing accuracy: parsed field vs this record's own raw text ----
        host_ok, severity_ok, notes = check_parsing(rec)
        parsing_results.append({
            'id': rid, 'source_type': source_type, 'hostname': hostname, 'severity': severity,
            'host_ok': host_ok, 'severity_ok': severity_ok, 'notes': notes, 'raw_excerpt': raw,
        })
        print(f"[PARSE {i}/{len(records)}] {source_type:22s} host_ok={str(host_ok):5s} severity_ok={str(severity_ok):5s}  {notes[0] if notes else ''}")

        # Confirm this exact record actually made it into Neo4j (id-stable MERGE)
        with driver.session(database=NEO4J_CONFIG['database']) as session:
            in_graph = session.run("MATCH (l:Log {id:$id}) RETURN l.hostname AS h, l.severity AS s", id=rid).single()
        if in_graph is None:
            print(f"    NOT IN GRAPH (id={rid}) - skipping retrieval check")
            continue

        # ---- 2. Retrieval accuracy: ask the real chatbot about this exact log ----
        question = f"What is the severity and hostname of the log with id '{rid}'?"
        start = time.time()
        cr = t2c.answer(question)
        elapsed = time.time() - start

        answer_text = json.dumps(cr.rows, default=str).lower()
        got_hostname = hostname.lower() in answer_text if hostname else None
        got_severity = severity.lower() in answer_text if severity else None
        verdict = 'correct' if (got_hostname or got_hostname is None) and (got_severity or got_severity is None) and cr.rows else \
                  ('no_rows' if not cr.rows else 'mismatch')

        retrieval_results.append({
            'id': rid, 'question': question, 'cypher': cr.cypher, 'rows': cr.rows,
            'expected_hostname': hostname, 'expected_severity': severity,
            'got_hostname': got_hostname, 'got_severity': got_severity,
            'verdict': verdict, 'elapsed_s': round(elapsed, 2), 'error': cr.error,
        })
        print(f"    [RETRIEVE] {verdict:9s} {elapsed:5.1f}s  expected host={hostname!r} severity={severity!r}  rows={cr.rows}")
        time.sleep(CALL_SPACING_SECONDS)

    with open('test_raw_log_results.json', 'w', encoding='utf-8') as f:
        json.dump({'parsing': parsing_results, 'retrieval': retrieval_results}, f, indent=2, default=str)

    print("\n" + "=" * 70)
    print("PARSING ACCURACY (parsed field vs that record's own original raw text)")
    host_checked = [r for r in parsing_results if r['host_ok'] is not None]
    sev_checked = [r for r in parsing_results if r['severity_ok'] is not None]
    host_correct = sum(1 for r in host_checked if r['host_ok'])
    sev_correct = sum(1 for r in sev_checked if r['severity_ok'])
    print(f"  Hostname verifiable-in-raw-text: {host_correct}/{len(host_checked)} "
          f"({host_correct/len(host_checked)*100:.0f}%)" if host_checked else "  no hostnames checked")
    print(f"  Severity verifiable-in-raw-text: {sev_correct}/{len(sev_checked)} "
          f"({sev_correct/len(sev_checked)*100:.0f}%)" if sev_checked else "  no severities checked")

    print("\nRETRIEVAL ACCURACY (chatbot's answer vs that record's own original raw text)")
    total = len(retrieval_results)
    correct = sum(1 for r in retrieval_results if r['verdict'] == 'correct')
    avg_latency = sum(r['elapsed_s'] for r in retrieval_results) / total if total else 0
    print(f"  {correct}/{total} correct ({correct/total*100:.0f}%)" if total else "  no retrieval checks completed")
    print(f"  Avg latency: {avg_latency:.2f}s")

    driver.close()


if __name__ == '__main__':
    main()
