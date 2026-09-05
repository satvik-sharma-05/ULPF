"""
test_50_questions.py - 50-question text2cypher accuracy/speed benchmark.

Runs each question through the REAL text2cypher pipeline (TextToCypher.generate
+ .run, same code api.py/chat.py use - schema introspected live, read-only
guard enforced, same LLM client), then grades the result against a
hand-written ground-truth Cypher query for the same question.

Grading (automated, value-based so column naming/order differences between the
generated and ground-truth query don't cause false negatives):
  - Both single-row/single-value (a scalar count) -> exact numeric match.
  - Otherwise -> each row's values (not column names) turned into a frozenset;
    compare the multiset of those against ground truth's. Full match = correct,
    partial overlap = partial, no overlap = incorrect.
  - Cypher refused (write clause) or execution error -> incorrect, noted why.

Not a substitute for human judgment on the hardest, most open-ended
questions - see the "note" field for anything the auto-grader flagged as
worth a manual look.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['NEO4J_URI'] = 'neo4j://127.0.0.1:7687'
os.environ['NEO4J_USER'] = 'neo4j'
# Credentials come from the environment or the .env file, never from source.
# These lines previously held a live Neo4j password and a real API key, which
# then shipped verbatim inside the handover package.
os.environ.setdefault('NEO4J_PASSWORD', os.getenv('NEO4J_PASSWORD', ''))
os.environ['NEO4J_DATABASE'] = 'logs'
os.environ['CHATBOT_LLM_PROVIDER'] = 'openrouter'
os.environ.setdefault('OPEN_ROUTER_API_KEY', os.getenv('OPEN_ROUTER_API_KEY', ''))
os.environ['OPENROUTER_MODEL'] = 'openai/gpt-oss-20b:free'

from neo4j import GraphDatabase
from config import NEO4J_CONFIG
from schema_introspect import GraphSchema
from text_to_cypher import TextToCypher
from llm import LLMClient

QUESTIONS = [
    # ---- TIER 1: trivial single counts/filters ----
    (1, "How many Log nodes are in the graph?",
        "MATCH (l:Log) RETURN count(l) AS count"),
    (1, "How many Host nodes are there?",
        "MATCH (h:Host) RETURN count(h) AS count"),
    (1, "How many logs have severity ERROR?",
        "MATCH (l:Log) WHERE l.severity = 'ERROR' RETURN count(l) AS count"),
    (1, "How many distinct source types are there?",
        "MATCH (s:SourceType) RETURN count(DISTINCT s) AS count"),
    (1, "How many User nodes exist?",
        "MATCH (u:User) RETURN count(u) AS count"),
    (1, "How many logs have severity_score less than or equal to 3?",
        "MATCH (l:Log) WHERE l.severity_score <= 3 RETURN count(l) AS count"),
    (1, "How many Process nodes are there?",
        "MATCH (p:Process) RETURN count(p) AS count"),
    (1, "How many distinct days are represented in the logs?",
        "MATCH (d:Day) RETURN count(DISTINCT d) AS count"),
    (1, "How many IPAddress nodes are in the graph?",
        "MATCH (ip:IPAddress) RETURN count(ip) AS count"),
    (1, "How many logs came from source type 'ESXi'?",
        "MATCH (l:Log)-[:FROM_SOURCE]->(s:SourceType {name:'ESXi'}) RETURN count(l) AS count"),

    # ---- TIER 2: simple aggregation / top-N ----
    (2, "Which host emitted the most logs?",
        "MATCH (h:Host)-[:EMITTED]->(l:Log) RETURN h.name AS host, count(l) AS logs ORDER BY logs DESC LIMIT 1"),
    (2, "What are the top 5 hosts by number of logs emitted?",
        "MATCH (h:Host)-[:EMITTED]->(l:Log) RETURN h.name AS host, count(l) AS logs ORDER BY logs DESC LIMIT 5"),
    (2, "Which source type has the most logs?",
        "MATCH (l:Log)-[:FROM_SOURCE]->(s:SourceType) RETURN s.name AS source, count(l) AS logs ORDER BY logs DESC LIMIT 1"),
    (2, "How many logs does each severity level have?",
        "MATCH (l:Log) RETURN l.severity AS severity, count(l) AS logs ORDER BY logs DESC"),
    (2, "Which process emitted the most logs?",
        "MATCH (l:Log)-[:EMITTED_BY]->(p:Process) RETURN p.name AS process, count(l) AS logs ORDER BY logs DESC LIMIT 1"),
    (2, "What are the 3 least common source types?",
        "MATCH (l:Log)-[:FROM_SOURCE]->(s:SourceType) RETURN s.name AS source, count(l) AS logs ORDER BY logs ASC LIMIT 3"),
    (2, "Which day had the most logs?",
        "MATCH (l:Log)-[:ON_DAY]->(d:Day) RETURN d.date AS day, count(l) AS logs ORDER BY logs DESC LIMIT 1"),
    (2, "Which 5 components have the most logs?",
        "MATCH (l:Log)-[:FROM_COMPONENT]->(c:Component) RETURN c.name AS component, count(l) AS logs ORDER BY logs DESC LIMIT 5"),
    (2, "Which 5 hosts have emitted the fewest logs?",
        "MATCH (h:Host)-[:EMITTED]->(l:Log) RETURN h.name AS host, count(l) AS logs ORDER BY logs ASC LIMIT 5"),
    (2, "What is the average number of logs per host?",
        "MATCH (h:Host)-[:EMITTED]->(l:Log) WITH h, count(l) AS c RETURN avg(c) AS count"),

    # ---- TIER 3: multi-hop / filtered joins ----
    (3, "Which hosts produced ERROR severity logs?",
        "MATCH (h:Host)-[:EMITTED]->(l:Log) WHERE l.severity = 'ERROR' RETURN DISTINCT h.name AS host"),
    (3, "How many ERROR logs did each host produce? Show the top 10.",
        "MATCH (h:Host)-[:EMITTED]->(l:Log) WHERE l.severity='ERROR' RETURN h.name AS host, count(l) AS errors ORDER BY errors DESC LIMIT 10"),
    (3, "Which users accessed more than one host?",
        "MATCH (u:User)-[:ACCESSED]->(h:Host) WITH u, collect(DISTINCT h.name) AS hosts WHERE size(hosts) > 1 RETURN u.name AS user, hosts"),
    (3, "Which processes ran on more than 2 distinct hosts?",
        "MATCH (p:Process)-[:RAN_ON]->(h:Host) WITH p, collect(DISTINCT h.name) AS hosts WHERE size(hosts) > 2 RETURN p.name AS process, hosts"),
    (3, "Which hosts have logs from more than 3 different source types?",
        "MATCH (h:Host)-[:EMITTED]->(l:Log)-[:FROM_SOURCE]->(s:SourceType) WITH h, count(DISTINCT s) AS num_sources WHERE num_sources > 3 RETURN h.name AS host, num_sources ORDER BY num_sources DESC"),
    (3, "How many distinct hosts does each source type appear on?",
        "MATCH (h:Host)-[:EMITTED]->(l:Log)-[:FROM_SOURCE]->(s:SourceType) RETURN s.name AS source, count(DISTINCT h) AS hosts ORDER BY hosts DESC"),
    (3, "Which 10 components are associated with the most ERROR severity logs?",
        "MATCH (l:Log)-[:FROM_COMPONENT]->(c:Component) WHERE l.severity='ERROR' RETURN c.name AS component, count(l) AS errors ORDER BY errors DESC LIMIT 10"),
    (3, "Which processes are associated with the JVM_GC source type?",
        "MATCH (l:Log)-[:FROM_SOURCE]->(s:SourceType {name:'JVM_GC'}) MATCH (l)-[:EMITTED_BY]->(p:Process) RETURN DISTINCT p.name AS process"),
    (3, "Which hosts have logs with both ERROR and WARNING severity?",
        "MATCH (h:Host)-[:EMITTED]->(l:Log) WHERE l.severity IN ['ERROR','WARNING'] WITH h, collect(DISTINCT l.severity) AS sevs WHERE size(sevs) = 2 RETURN h.name AS host"),
    (3, "For the top 10 hosts, how many unique processes emitted logs on each?",
        "MATCH (h:Host)-[:EMITTED]->(l:Log)-[:EMITTED_BY]->(p:Process) RETURN h.name AS host, count(DISTINCT p) AS processes ORDER BY processes DESC LIMIT 10"),

    # ---- TIER 4: temporal, ratios, multi-condition ----
    (4, "What is the busiest day (most logs), and how many logs did it have?",
        "MATCH (l:Log)-[:ON_DAY]->(d:Day) RETURN d.date AS day, count(l) AS logs ORDER BY logs DESC LIMIT 1"),
    (4, "Which source type has the highest proportion of ERROR severity logs (min 10 logs)?",
        "MATCH (l:Log)-[:FROM_SOURCE]->(s:SourceType) WITH s, count(l) AS total, count(CASE WHEN l.severity='ERROR' THEN 1 END) AS errors WHERE total >= 10 RETURN s.name AS source, errors, total, toFloat(errors)/total AS error_rate ORDER BY error_rate DESC LIMIT 5"),
    (4, "Which hosts emitted logs on more than 2 different days?",
        "MATCH (h:Host)-[:EMITTED]->(l:Log)-[:ON_DAY]->(d:Day) WITH h, count(DISTINCT d) AS days WHERE days > 2 RETURN h.name AS host, days ORDER BY days DESC"),
    (4, "What is the distribution of logs by severity_score?",
        "MATCH (l:Log) RETURN l.severity_score AS score, count(l) AS logs ORDER BY score"),
    (4, "Which host has the highest ratio of ERROR to total logs, minimum 10 logs on that host?",
        "MATCH (h:Host)-[:EMITTED]->(l:Log) WITH h, count(l) AS total, count(CASE WHEN l.severity='ERROR' THEN 1 END) AS errors WHERE total >= 10 RETURN h.name AS host, errors, total, toFloat(errors)/total AS rate ORDER BY rate DESC LIMIT 5"),
    (4, "How many logs reference a File (via TOUCHED_FILE)?",
        "MATCH (l:Log)-[:TOUCHED_FILE]->(:File) RETURN count(DISTINCT l) AS count"),
    (4, "Which 5 users are associated with the most logs?",
        "MATCH (l:Log)-[:PERFORMED_BY]->(u:User) RETURN u.name AS user, count(l) AS logs ORDER BY logs DESC LIMIT 5"),
    (4, "How many distinct Operations are associated with ERROR logs?",
        "MATCH (l:Log)-[:PART_OF_OPERATION]->(o:Operation) WHERE l.severity='ERROR' RETURN count(DISTINCT o) AS count"),
    (4, "For each severity level, what are the earliest and latest log timestamps?",
        "MATCH (l:Log) RETURN l.severity AS severity, min(l.timestamp) AS earliest, max(l.timestamp) AS latest ORDER BY severity"),
    (4, "How many logs were emitted by each of the top 10 (host, process) pairs?",
        "MATCH (h:Host)-[:EMITTED]->(l:Log)-[:EMITTED_BY]->(p:Process) RETURN h.name AS host, p.name AS process, count(l) AS logs ORDER BY logs DESC LIMIT 10"),

    # ---- TIER 5: hardest - multi-hop chains, nested aggregation ----
    (5, "Find (user, host) pairs where the user accessed the host and that host also emitted an ERROR log.",
        "MATCH (u:User)-[:ACCESSED]->(h:Host) MATCH (h)-[:EMITTED]->(l:Log) WHERE l.severity='ERROR' RETURN DISTINCT u.name AS user, h.name AS host LIMIT 25"),
    (5, "Which pairs of hosts share a common user who accessed both?",
        "MATCH (u:User)-[:ACCESSED]->(h1:Host), (u)-[:ACCESSED]->(h2:Host) WHERE h1 <> h2 AND id(h1) < id(h2) RETURN u.name AS user, h1.name AS host1, h2.name AS host2 LIMIT 25"),
    (5, "For each source type, what is the average severity_score of its logs?",
        "MATCH (l:Log)-[:FROM_SOURCE]->(s:SourceType) RETURN s.name AS source, avg(l.severity_score) AS avg_severity ORDER BY avg_severity"),
    (5, "Find pairs of hosts that share at least one common process that ran on both.",
        "MATCH (p:Process)-[:RAN_ON]->(h1:Host), (p)-[:RAN_ON]->(h2:Host) WHERE h1<>h2 AND id(h1)<id(h2) RETURN p.name AS process, h1.name AS host1, h2.name AS host2 LIMIT 25"),
    (5, "For ERROR and WARNING severities only, how many logs of each does every source type have?",
        "MATCH (l:Log)-[:FROM_SOURCE]->(s:SourceType) WHERE l.severity IN ['ERROR','WARNING'] RETURN s.name AS source, l.severity AS severity, count(l) AS logs ORDER BY source, severity"),
    (5, "Which 5 components appear in logs across the most distinct hosts?",
        "MATCH (h:Host)-[:EMITTED]->(l:Log)-[:FROM_COMPONENT]->(c:Component) RETURN c.name AS component, count(DISTINCT h) AS hosts ORDER BY hosts DESC LIMIT 5"),
    (5, "Find the top 5 (host, process) pairs by log count, along with what fraction of that host's total logs they represent.",
        "MATCH (h:Host)-[:EMITTED]->(l:Log)-[:EMITTED_BY]->(p:Process) WITH h, p, count(l) AS pc MATCH (h)-[:EMITTED]->(l2:Log) WITH h, p, pc, count(l2) AS total RETURN h.name AS host, p.name AS process, pc AS logs, total, toFloat(pc)/total AS pct ORDER BY logs DESC LIMIT 5"),
    (5, "List the number of ERROR logs per day, ordered by date.",
        "MATCH (l:Log)-[:ON_DAY]->(d:Day) WHERE l.severity='ERROR' RETURN d.date AS day, count(l) AS errors ORDER BY day"),
    (5, "Which (host, process, source type) combination has the single highest log count?",
        "MATCH (h:Host)-[:EMITTED]->(l:Log)-[:EMITTED_BY]->(p:Process) MATCH (l)-[:FROM_SOURCE]->(s:SourceType) RETURN h.name AS host, p.name AS process, s.name AS source, count(l) AS logs ORDER BY logs DESC LIMIT 1"),
    (5, "For hosts with more than 100 logs, what fraction of their logs are ERROR or CRITICAL severity? Rank descending, top 10.",
        "MATCH (h:Host)-[:EMITTED]->(l:Log) WITH h, count(l) AS total, count(CASE WHEN l.severity IN ['ERROR','CRITICAL'] THEN 1 END) AS bad WHERE total > 100 RETURN h.name AS host, bad, total, toFloat(bad)/total AS bad_rate ORDER BY bad_rate DESC LIMIT 10"),
]


def _hashable(v):
    """Neo4j rows can contain lists (collect(...)) or whole nodes - neither is
    hashable as-is, so frozenset(row.values()) blows up on exactly the
    questions (list aggregation, whole-node returns) this benchmark most
    wants to exercise. Recursively coerce into something hashable instead."""
    if isinstance(v, list):
        return tuple(_hashable(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, _hashable(x)) for k, x in v.items()))
    if isinstance(v, float):
        return round(v, 3)
    if hasattr(v, 'items'):  # neo4j Node/Relationship - property-map-like
        try:
            return tuple(sorted((k, _hashable(x)) for k, x in dict(v).items()))
        except Exception:
            return str(v)
    return v


def rows_to_valueset(rows):
    """Order/column-name-independent representation: each row -> frozenset of
    its (hashable-coerced) values."""
    return [frozenset(_hashable(v) for v in row.values()) for row in rows]


def grade(generated_rows, expected_rows, error):
    if error:
        return 'incorrect', error
    if not expected_rows and not generated_rows:
        return 'correct', 'both empty'
    if len(expected_rows) == 1 and len(expected_rows[0]) == 1 and \
       len(generated_rows) == 1 and len(generated_rows[0]) == 1:
        gv = list(generated_rows[0].values())[0]
        ev = list(expected_rows[0].values())[0]
        if isinstance(gv, (int, float)) and isinstance(ev, (int, float)):
            return ('correct', f'{gv} == {ev}') if abs(gv - ev) < 0.01 else ('incorrect', f'{gv} != {ev}')
    gen_set = rows_to_valueset(generated_rows)
    exp_set = rows_to_valueset(expected_rows)
    gen_multiset = {}
    for fs in gen_set:
        gen_multiset[fs] = gen_multiset.get(fs, 0) + 1
    matched = 0
    for fs in exp_set:
        if gen_multiset.get(fs, 0) > 0:
            matched += 1
            gen_multiset[fs] -= 1
    if not exp_set:
        return ('partial', f'ground truth empty, generated {len(generated_rows)} rows') if generated_rows else ('correct', 'both empty')
    frac = matched / len(exp_set)
    if frac >= 0.99:
        return 'correct', f'{matched}/{len(exp_set)} rows matched'
    if frac > 0:
        return 'partial', f'{matched}/{len(exp_set)} rows matched'
    return 'incorrect', f'0/{len(exp_set)} rows matched (got {len(generated_rows)} rows)'


def main():
    driver = GraphDatabase.driver(NEO4J_CONFIG['uri'], auth=(NEO4J_CONFIG['user'], NEO4J_CONFIG['password']))
    schema = GraphSchema(driver, NEO4J_CONFIG['database'])
    llm = LLMClient()
    t2c = TextToCypher(driver=driver, database=NEO4J_CONFIG['database'], schema=schema, llm=llm)

    results = []
    for i, (tier, question, gt_cypher) in enumerate(QUESTIONS, 1):
        start = time.time()
        try:
            cr = t2c.answer(question)
            error = cr.error
            generated_cypher = cr.cypher
            generated_rows = cr.rows
        except Exception as e:
            error = f"harness exception: {e}"
            generated_cypher = None
            generated_rows = []
        elapsed = time.time() - start

        try:
            with driver.session(database=NEO4J_CONFIG['database']) as session:
                expected_rows = [dict(r) for r in session.run(gt_cypher)]
        except Exception as e:
            expected_rows = []
            print(f"WARNING: ground truth query failed for Q{i}: {e}")

        verdict, note = grade(generated_rows, expected_rows, error)
        results.append({
            'n': i, 'tier': tier, 'question': question,
            'generated_cypher': generated_cypher, 'error': error,
            'generated_row_count': len(generated_rows), 'expected_row_count': len(expected_rows),
            'verdict': verdict, 'note': note, 'elapsed_s': round(elapsed, 2),
        })
        print(f"[{i}/50] tier={tier} {verdict:9s} {elapsed:5.1f}s  {note[:60]:60s}  {question[:70]}")
        time.sleep(1.5)  # be gentle with the free-tier rate limit

    with open('test_50_results.json', 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, default=str)

    total = len(results)
    correct = sum(1 for r in results if r['verdict'] == 'correct')
    partial = sum(1 for r in results if r['verdict'] == 'partial')
    incorrect = sum(1 for r in results if r['verdict'] == 'incorrect')
    avg_latency = sum(r['elapsed_s'] for r in results) / total

    print("\n" + "=" * 70)
    print(f"TOTAL: {total}  Correct: {correct} ({correct/total*100:.0f}%)  "
          f"Partial: {partial} ({partial/total*100:.0f}%)  Incorrect: {incorrect} ({incorrect/total*100:.0f}%)")
    print(f"Avg latency: {avg_latency:.2f}s")
    for tier in range(1, 6):
        tier_results = [r for r in results if r['tier'] == tier]
        tc = sum(1 for r in tier_results if r['verdict'] == 'correct')
        tp = sum(1 for r in tier_results if r['verdict'] == 'partial')
        tavg = sum(r['elapsed_s'] for r in tier_results) / len(tier_results)
        print(f"  Tier {tier}: {tc}/10 correct, {tp}/10 partial, avg {tavg:.1f}s")

    driver.close()


if __name__ == '__main__':
    main()
