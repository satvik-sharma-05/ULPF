"""
tools.py - The retrieval tools an LLM planner can call.

Each tool answers one distinct question shape (see planner.py's prompt for the
full rationale, and README.md for the worked examples this maps to):

  vector_search      semantic search over log text - "why", "explain", vague
  fulltext_search    exact keyword search over log text - specific error codes
  graph_neighbors    what's structurally connected to a named entity -
                     dependency/impact analysis ("what depends on this datastore")
  path_traversal     how two named entities are connected - "did X affect Y"
  temporal_window    logs in a time range, in order - timelines, "what changed"
  text2cypher        wraps the existing TextToCypher - counts/filters/rankings

Every tool returns a ToolResult: the raw evidence rows (JSON-safe once passed
through api.py's jsonify), a one-line human note for the plan trace, and a
pre-formatted text block ready to drop into the final answer's prompt - so
planner.py never needs per-tool-type formatting logic.

Two correctness constraints shape graph_neighbors and path_traversal
specifically, both driven by this graph's actual shape (~117M :Log nodes):

  - They traverse ONLY the low-cardinality *structural* relationships
    (_STRUCTURAL_RELS below) - never the six relationship types that touch
    :Log directly (EMITTED, EMITTED_BY, FROM_COMPONENT, HAS_SEVERITY,
    FROM_SOURCE, ON_DAY). Host-[:EMITTED]->Log alone is ~117M edges; a
    shortestPath or open-ended neighbor expansion that could route through
    Log would explore that fan-out before hitting any hop limit. Log content
    retrieval is what vector_search/fulltext_search/temporal_window are for -
    a clean separation, not a missing feature.
  - Hop counts are Python ints clamped before being interpolated into the
    Cypher text (variable-length pattern bounds like `*..N` can't be bound
    Cypher parameters - Neo4j requires them literal in the query). Safe here
    specifically because the value is clamped to a small range, never raw
    text from a question or the LLM.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, NamedTuple, Optional

from entity_resolver import EntityResolver

logger = logging.getLogger(__name__)

# Every relationship type in the schema that does NOT touch :Log directly -
# see the module docstring for why that exclusion matters. Derived from
# ingestion's graph_schema.py CORE_RELATIONSHIPS/DERIVED_RELATIONSHIPS by
# removing the six Log-adjacent ones (not imported live: chatbot_pipeline is
# deliberately standalone, same reasoning as embeddings.py's duplication).
_STRUCTURAL_RELS = (
    "RUNS_ON|PART_OF|IS_TYPE|ACCESSED|MEMBER_OF|BELONGS_TO|IDENTIFIES|"
    "ATTACHED_TO|MOUNTED_ON|HOSTED_ON|INTERFACE_OF|ASSIGNED_TO|INSTALLED_ON|"
    "MANAGED_ON|ENFORCED_ON|EXECUTED_ON|RAN_ON|SEEN_ON|IN_NAMESPACE|"
    "OF_SERVICE|RUNS_IN|STORED_IN"
)

_LOG_FIELDS = (
    "l.id AS id, l.timestamp AS timestamp, l.hostname AS hostname, "
    "l.source_type AS source_type, l.process AS process, l.severity AS severity, "
    "l.severity_score AS severity_score, l.message AS message"
)


@dataclass
class ToolContext:
    driver: Any
    database: Optional[str]
    resolver: EntityResolver
    embedder: Any = None
    vector_index: str = 'log_embedding_index'
    fulltext_index: str = 'log_message_fulltext'


class ToolResult(NamedTuple):
    tool: str
    ok: bool
    note: str                       # one-line summary for the plan trace
    evidence: List[Dict[str, Any]]  # raw rows, for the API response
    context_text: str               # pre-formatted block for the final-answer prompt


def _run(ctx: ToolContext, query: str, **params) -> List[Dict[str, Any]]:
    if ctx.driver is None:
        return []
    with ctx.driver.session(database=ctx.database) as session:
        return [dict(record) for record in session.run(query, **params)]


def _no_driver(tool: str) -> ToolResult:
    """Every tool checks this explicitly rather than relying on `_run`'s
    silent `[]` when there's no connection: without this, fulltext_search and
    temporal_window would report `ok=True, "0 results"` for a database that
    was never actually reached - indistinguishable from a genuine empty
    search result, which is a meaningfully different and more actionable
    situation than a real "not found"."""
    return ToolResult(tool, False, "no database connection", [], "")


def _format_logs(rows: List[Dict[str, Any]], heading: str) -> str:
    if not rows:
        return f"{heading}: (none found)"
    lines = [heading + ":"]
    for row in rows:
        lines.append(
            f"  [{row.get('timestamp')}] {row.get('severity')} "
            f"{row.get('hostname')}/{row.get('process')}: {row.get('message')}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# vector_search / fulltext_search - with metadata pre-filtering, which the
# GraphRAG manual-mode path (graph_rag.py) doesn't have. Neo4j's vector index
# doesn't support arbitrary predicate pushdown into the ANN search itself
# (portably, across the 5.11+ versions this project targets), so this
# over-fetches candidates (k * 5) from the index, applies exact filters, then
# trims to k - the standard workaround for "filtered vector search".
# ---------------------------------------------------------------------------
def vector_search(ctx: ToolContext, query: str, top_k: int = 10, host: Optional[str] = None,
                  day_from: Optional[str] = None, day_to: Optional[str] = None,
                  max_severity_score: Optional[int] = None) -> ToolResult:
    if ctx.driver is None:
        return _no_driver('vector_search')
    top_k = max(1, min(int(top_k or 10), 50))
    embedder = ctx.embedder
    if embedder is None:
        # Same process-wide singleton graph_rag.py's GraphRAG uses, so the
        # model is loaded at most once regardless of which caller asks first.
        from embeddings import get_shared_embedder
        embedder = get_shared_embedder()
    if embedder is None:
        return ToolResult('vector_search', False, "embedding model unavailable", [], "")

    vector = embedder.embed(query)
    if not vector:
        return ToolResult('vector_search', False, "could not embed the query", [], "")

    cypher = f"""
    CALL db.index.vector.queryNodes('{ctx.vector_index}', $overfetch, $embedding)
    YIELD node AS l, score
    WHERE ($host IS NULL OR l.hostname = $host)
      AND ($day_from IS NULL OR l.day >= $day_from)
      AND ($day_to IS NULL OR l.day <= $day_to)
      AND ($max_sev IS NULL OR l.severity_score <= $max_sev)
    RETURN {_LOG_FIELDS}, score
    ORDER BY score DESC
    LIMIT $top_k
    """
    try:
        rows = _run(ctx, cypher, overfetch=top_k * 5, embedding=vector, host=host,
                    day_from=day_from, day_to=day_to, max_sev=max_severity_score, top_k=top_k)
    except Exception as e:
        return ToolResult('vector_search', False, f"vector search failed: {e}", [], "")

    note = f"{len(rows)} log(s) semantically similar to {query!r}"
    return ToolResult('vector_search', True, note, rows, _format_logs(rows, "Semantically similar logs"))


def fulltext_search(ctx: ToolContext, query: str, top_k: int = 10, host: Optional[str] = None,
                    day_from: Optional[str] = None, day_to: Optional[str] = None) -> ToolResult:
    if ctx.driver is None:
        return _no_driver('fulltext_search')
    top_k = max(1, min(int(top_k or 10), 50))
    terms = ' OR '.join(w for w in query.split() if len(w) > 2) or query
    cypher = f"""
    CALL db.index.fulltext.queryNodes('{ctx.fulltext_index}', $terms, {{limit: $overfetch}})
    YIELD node AS l, score
    WHERE ($host IS NULL OR l.hostname = $host)
      AND ($day_from IS NULL OR l.day >= $day_from)
      AND ($day_to IS NULL OR l.day <= $day_to)
    RETURN {_LOG_FIELDS}, score
    ORDER BY score DESC
    LIMIT $top_k
    """
    try:
        rows = _run(ctx, cypher, terms=terms, overfetch=top_k * 5, host=host,
                    day_from=day_from, day_to=day_to, top_k=top_k)
    except Exception as e:
        return ToolResult('fulltext_search', False, f"fulltext search failed: {e}", [], "")

    note = f"{len(rows)} log(s) containing {query!r}"
    return ToolResult('fulltext_search', True, note, rows, _format_logs(rows, "Logs matching the search terms"))


# ---------------------------------------------------------------------------
# graph_neighbors / path_traversal - structural-only, see module docstring.
# ---------------------------------------------------------------------------
def graph_neighbors(ctx: ToolContext, entity: str, hops: int = 1, limit: int = 25) -> ToolResult:
    if ctx.driver is None:
        return _no_driver('graph_neighbors')
    hops = max(1, min(int(hops or 1), 3))
    limit = max(1, min(int(limit or 25), 100))

    resolved = ctx.resolver.resolve(entity)
    if resolved is None:
        return ToolResult('graph_neighbors', False,
                          f"could not find any node matching {entity!r}", [], "")

    cypher = f"""
    MATCH (start:`{resolved.label}` {{`{resolved.key}`: $value}})
    MATCH path = (start)-[:{_STRUCTURAL_RELS}*1..{hops}]-(connected)
    WHERE connected <> start
    WITH DISTINCT connected, min(length(path)) AS hop_distance
    RETURN labels(connected)[0] AS label,
           coalesce(connected.name, connected.id, connected.moref, connected.address,
                    connected.path, connected.sid, connected.key, connected.url,
                    connected.code, connected.value, toString(connected.number)) AS value,
           hop_distance
    ORDER BY hop_distance ASC
    LIMIT $limit
    """
    try:
        rows = _run(ctx, cypher, value=resolved.value, limit=limit)
    except Exception as e:
        return ToolResult('graph_neighbors', False, f"graph traversal failed: {e}", [], "")

    note = f"{len(rows)} node(s) within {hops} hop(s) of {resolved.label}:{resolved.value}"
    if not rows:
        text = f"Nothing found within {hops} hop(s) of {resolved.label} {resolved.value!r}."
    else:
        lines = [f"Connected to {resolved.label} {resolved.value!r}:"]
        for row in rows:
            lines.append(f"  ({row['hop_distance']} hop{'s' if row['hop_distance'] != 1 else ''}) "
                         f"{row['label']}: {row['value']}")
        text = "\n".join(lines)
    return ToolResult('graph_neighbors', True, note, rows, text)


def path_traversal(ctx: ToolContext, entity_a: str, entity_b: str, max_hops: int = 4) -> ToolResult:
    if ctx.driver is None:
        return _no_driver('path_traversal')
    max_hops = max(1, min(int(max_hops or 4), 6))

    a = ctx.resolver.resolve(entity_a)
    b = ctx.resolver.resolve(entity_b)
    if a is None or b is None:
        missing = entity_a if a is None else entity_b
        return ToolResult('path_traversal', False,
                          f"could not find any node matching {missing!r}", [], "")

    cypher = f"""
    MATCH (a:`{a.label}` {{`{a.key}`: $value_a}}), (b:`{b.label}` {{`{b.key}`: $value_b}})
    MATCH p = shortestPath((a)-[:{_STRUCTURAL_RELS}*..{max_hops}]-(b))
    RETURN [n IN nodes(p) | {{label: labels(n)[0],
             value: coalesce(n.name, n.id, n.moref, n.address, n.path, n.sid, n.key, n.url,
                             n.code, n.value, toString(n.number))}}]
           AS path_nodes,
           [r IN relationships(p) | type(r)] AS path_rels,
           length(p) AS hops
    LIMIT 1
    """
    try:
        rows = _run(ctx, cypher, value_a=a.value, value_b=b.value)
    except Exception as e:
        return ToolResult('path_traversal', False, f"path search failed: {e}", [], "")

    if not rows:
        note = f"no structural path within {max_hops} hops between {a.label}:{a.value} and {b.label}:{b.value}"
        return ToolResult('path_traversal', True, note, [],
                          f"No connection found between {a.value!r} and {b.value!r} "
                          f"within {max_hops} hops.")

    row = rows[0]
    nodes, rels = row['path_nodes'], row['path_rels']
    chain = nodes[0]['label'] + ':' + str(nodes[0]['value'])
    for rel, node in zip(rels, nodes[1:]):
        chain += f" -[{rel}]-> {node['label']}:{node['value']}"
    note = f"path found, {row['hops']} hop(s): {chain}"
    return ToolResult('path_traversal', True, note, rows, f"Connection found:\n  {chain}")


# ---------------------------------------------------------------------------
# temporal_window - relies on Log.timestamp being an indexed, lexicographically
# sortable ISO-8601 string (confirmed by the ingestion parser's normalization).
# Bare dates ("2026-06-15") are widened to a full-day range rather than
# compared as-is: a bare-date `end` bound would otherwise string-compare as
# LESS than any timestamp that day (e.g. "2026-06-15" < "2026-06-15T10:00:00"),
# excluding the entire day it was meant to include.
# ---------------------------------------------------------------------------
def _widen_bound(value: Optional[str], is_end: bool) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    if 'T' in value:
        return value
    return value + ('T23:59:59.999999' if is_end else 'T00:00:00')


def temporal_window(ctx: ToolContext, start: str, end: str, host: Optional[str] = None,
                    max_severity_score: Optional[int] = None, limit: int = 100) -> ToolResult:
    if ctx.driver is None:
        return _no_driver('temporal_window')
    limit = max(1, min(int(limit or 100), 500))
    start_bound = _widen_bound(start, is_end=False)
    end_bound = _widen_bound(end, is_end=True)
    if not start_bound or not end_bound:
        return ToolResult('temporal_window', False, "start and end are both required", [], "")

    cypher = f"""
    MATCH (l:Log)
    WHERE l.timestamp >= $start AND l.timestamp <= $end
      AND ($host IS NULL OR l.hostname = $host)
      AND ($max_sev IS NULL OR l.severity_score <= $max_sev)
    RETURN {_LOG_FIELDS}
    ORDER BY l.timestamp ASC
    LIMIT $limit
    """
    try:
        rows = _run(ctx, cypher, start=start_bound, end=end_bound, host=host,
                    max_sev=max_severity_score, limit=limit)
    except Exception as e:
        return ToolResult('temporal_window', False, f"temporal query failed: {e}", [], "")

    note = f"{len(rows)} log(s) between {start_bound} and {end_bound}"
    return ToolResult('temporal_window', True, note, rows,
                      _format_logs(rows, f"Logs from {start_bound} to {end_bound}, in order"))


# ---------------------------------------------------------------------------
# parent_child_retrieval - expands ONE already-known log (typically an id
# returned by an earlier vector_search/fulltext_search/pattern_match call, in
# a previous planning round) into every other log sharing its correlation id
# (Operation/Trace/Task). This is the closest thing this graph has to "show me
# the rest of this incident" - there is no separate Incident node (see the
# README's "What this doesn't do (yet)" section), but a shared opID/trace/task
# is exactly what a real operation's logs actually have in common.
#
# Safe by construction despite touching :Log: it starts from ONE specific,
# already-resolved log (not an open-ended entity that could be a hub), so the
# fan-out is bounded by how many logs actually share that one correlation id -
# capped further by `limit`.
# ---------------------------------------------------------------------------
def parent_child_retrieval(ctx: ToolContext, log_id: str, limit: int = 50) -> ToolResult:
    if ctx.driver is None:
        return _no_driver('parent_child_retrieval')
    limit = max(1, min(int(limit or 50), 200))

    seed_check = _run(ctx, "MATCH (l:Log {id: $log_id}) RETURN l.id AS id LIMIT 1", log_id=log_id)
    if not seed_check:
        return ToolResult('parent_child_retrieval', False, f"no log found with id {log_id!r}", [], "")

    cypher = f"""
    MATCH (seed:Log {{id: $log_id}})
    OPTIONAL MATCH (seed)-[:PART_OF_OPERATION]->(op:Operation)
    OPTIONAL MATCH (seed)-[:HAS_TRACE]->(tr:Trace)
    OPTIONAL MATCH (seed)-[:ABOUT_TASK]->(task:Task)
    WITH seed, [x IN [op, tr, task] WHERE x IS NOT NULL] AS correlators
    UNWIND correlators AS correlator
    MATCH (correlator)<-[]-(related:Log)
    WHERE related <> seed
    RETURN DISTINCT {_LOG_FIELDS.replace('l.', 'related.')}
    ORDER BY related.timestamp ASC
    LIMIT $limit
    """
    try:
        rows = _run(ctx, cypher, log_id=log_id, limit=limit)
    except Exception as e:
        return ToolResult('parent_child_retrieval', False, f"expansion failed: {e}", [], "")

    if not rows:
        note = f"log {log_id} has no operation/trace/task id shared with any other log"
        return ToolResult('parent_child_retrieval', True, note, [],
                          f"Log {log_id} isn't correlated with any other log via a shared "
                          f"operation, trace, or task id.")

    note = f"{len(rows)} log(s) sharing an operation/trace/task with {log_id}"
    text = _format_logs(rows, f"Logs correlated with {log_id} (same operation/trace/task)")
    return ToolResult('parent_child_retrieval', True, note, rows, text)


# ---------------------------------------------------------------------------
# query_template - deterministic, hand-written Cypher for the handful of
# question shapes that show up constantly (top hosts, counts by severity, host
# inventory). Faster and more reliable than asking an LLM to reinvent the same
# query every time via text2cypher, at the cost of only covering exactly these
# shapes. Add more templates here if another question shape turns out to be
# just as common - not a reason to avoid text2cypher for everything else.
# ---------------------------------------------------------------------------
_QUERY_TEMPLATES: Dict[str, Dict[str, str]] = {
    'top_hosts_by_severity': {
        'cypher': """
            MATCH (h:Host)-[:EMITTED]->(l:Log)
            WHERE ($max_sev IS NULL OR l.severity_score <= $max_sev)
              AND ($day_from IS NULL OR l.day >= $day_from)
              AND ($day_to IS NULL OR l.day <= $day_to)
            RETURN h.name AS host, count(l) AS log_count
            ORDER BY log_count DESC
            LIMIT $limit
        """,
        'description': 'Hosts ranked by log volume, optionally filtered to a severity/day range.',
    },
    'count_by_severity': {
        'cypher': """
            MATCH (l:Log)-[:HAS_SEVERITY]->(s:Severity)
            WHERE ($host IS NULL OR l.hostname = $host)
              AND ($day_from IS NULL OR l.day >= $day_from)
              AND ($day_to IS NULL OR l.day <= $day_to)
            RETURN s.level AS severity, s.score AS score, count(l) AS log_count
            ORDER BY s.score ASC
        """,
        'description': 'Log counts grouped by severity level, optionally scoped to one host/day range.',
    },
    'host_inventory': {
        'cypher': """
            MATCH (h:Host)
            RETURN h.name AS host, h.first_seen AS first_seen, h.last_seen AS last_seen
            ORDER BY h.name
            LIMIT $limit
        """,
        'description': 'All known hosts in the graph.',
    },
}


def query_template(ctx: ToolContext, template: str, host: Optional[str] = None,
                   day_from: Optional[str] = None, day_to: Optional[str] = None,
                   max_severity_score: Optional[int] = None, limit: int = 25) -> ToolResult:
    if ctx.driver is None:
        return _no_driver('query_template')
    spec = _QUERY_TEMPLATES.get(template)
    if spec is None:
        return ToolResult('query_template', False,
                          f"unknown template {template!r} - available: {sorted(_QUERY_TEMPLATES)}", [], "")
    limit = max(1, min(int(limit or 25), 200))

    try:
        rows = _run(ctx, spec['cypher'], host=host, day_from=day_from, day_to=day_to,
                    max_sev=max_severity_score, limit=limit)
    except Exception as e:
        return ToolResult('query_template', False, f"template query failed: {e}", [], "")

    lines = [f"Template: {template} ({spec['description']})", "Results:"]
    for row in rows[:40]:
        lines.append("  " + " | ".join(f"{k}={v}" for k, v in row.items()))
    note = f"{len(rows)} row(s) from template {template!r}"
    return ToolResult('query_template', True, note, rows, "\n".join(lines))


# ---------------------------------------------------------------------------
# pattern_match - a small set of well-known vSphere/VMware failure signatures,
# looked up by name rather than making the planner reconstruct the right
# search terms from scratch every time.
#
# Honesty note: this is a best-effort STARTING set of generic vSphere
# terminology, not confirmed to appear in this specific corpus's exact wording
# the way core/parsers/entities.py's patterns were validated against the real
# 33GB corpus. Extend _KNOWN_PATTERNS once real examples are seen - the terms
# below are Lucene query syntax (wildcards/AND/OR are deliberate, not escaped,
# since these are fixed strings this module wrote, not raw user input passed
# through - the same trust boundary reasoning as everywhere else user text is
# NOT interpolated directly into a query).
# ---------------------------------------------------------------------------
_KNOWN_PATTERNS: Dict[str, Dict[str, str]] = {
    'ha_restart': {
        'terms': 'HA AND restart OR "vSphere HA" OR "restarted the VM"',
        'description': 'vSphere HA restarting a VM after a host failure.',
    },
    'apd': {
        'terms': 'APD OR "All Paths Down" OR PDL OR "Permanent Device Loss"',
        'description': 'All-Paths-Down / Permanent-Device-Loss storage connectivity failure.',
    },
    'host_isolation': {
        'terms': 'isolat* OR "network isolation" OR "host is isolated"',
        'description': 'A host losing network connectivity to the point vSphere HA declares it isolated.',
    },
    'network_partition': {
        'terms': 'partition* OR "network partition" OR "split brain"',
        'description': 'A network partition separating hosts or cluster members from each other.',
    },
    'snapshot_failure': {
        'terms': 'snapshot AND (failed OR error OR "consolidation needed")',
        'description': 'A VM snapshot operation failing or needing consolidation.',
    },
}


def pattern_match(ctx: ToolContext, pattern: str, host: Optional[str] = None,
                  day_from: Optional[str] = None, day_to: Optional[str] = None,
                  top_k: int = 15) -> ToolResult:
    if ctx.driver is None:
        return _no_driver('pattern_match')
    spec = _KNOWN_PATTERNS.get(pattern)
    if spec is None:
        return ToolResult('pattern_match', False,
                          f"unknown pattern {pattern!r} - available: {sorted(_KNOWN_PATTERNS)}", [], "")
    top_k = max(1, min(int(top_k or 15), 50))

    cypher = f"""
    CALL db.index.fulltext.queryNodes('{ctx.fulltext_index}', $terms, {{limit: $overfetch}})
    YIELD node AS l, score
    WHERE ($host IS NULL OR l.hostname = $host)
      AND ($day_from IS NULL OR l.day >= $day_from)
      AND ($day_to IS NULL OR l.day <= $day_to)
    RETURN {_LOG_FIELDS}, score
    ORDER BY score DESC
    LIMIT $top_k
    """
    try:
        rows = _run(ctx, cypher, terms=spec['terms'], overfetch=top_k * 5, host=host,
                    day_from=day_from, day_to=day_to, top_k=top_k)
    except Exception as e:
        return ToolResult('pattern_match', False, f"pattern search failed: {e}", [], "")

    note = f"{len(rows)} log(s) matching the {pattern!r} pattern ({spec['description']})"
    text = _format_logs(rows, f"Logs matching known pattern {pattern!r}: {spec['description']}")
    return ToolResult('pattern_match', True, note, rows, text)


# ---------------------------------------------------------------------------
# text2cypher - thin wrapper so it fits the same ToolResult contract as
# everything else. The actual generation/guard/execution logic stays in
# text_to_cypher.py; this just adapts its CypherResult into a ToolResult.
# ---------------------------------------------------------------------------
def make_text2cypher_tool(text_to_cypher):
    def _tool(ctx: ToolContext, question: str) -> ToolResult:
        result = text_to_cypher.answer(question)
        if result.error or not result.rows:
            return ToolResult('text2cypher', False,
                              result.error or "query returned no rows", [], "")
        lines = [f"Cypher query run:\n  {result.cypher}", "Results:"]
        for row in result.rows[:40]:
            lines.append("  " + " | ".join(f"{k}={v}" for k, v in row.items()))
        if len(result.rows) > 40:
            lines.append(f"  ... and {len(result.rows) - 40} more rows")
        note = f"{len(result.rows)} row(s) from a generated Cypher query"
        return ToolResult('text2cypher', True, note, result.rows, "\n".join(lines))
    return _tool


# ---------------------------------------------------------------------------
# Registry: name -> (callable, description-for-the-planner-prompt, param spec)
# planner.py builds its prompt from TOOL_SPECS so the two can never drift.
# ---------------------------------------------------------------------------
TOOL_SPECS: Dict[str, Dict[str, Any]] = {
    'vector_search': {
        'fn': vector_search,
        'description': 'Semantic search over log message text. Use for vague, descriptive, or '
                       '"why/explain" questions where the exact wording in the logs is unknown.',
        'params': 'query (string, required), host (string, optional), day_from (YYYY-MM-DD, optional), '
                  'day_to (YYYY-MM-DD, optional), max_severity_score (int 1-7, optional)',
    },
    'fulltext_search': {
        'fn': fulltext_search,
        'description': 'Exact keyword search over log message text. Use for specific error codes, '
                       'identifiers, or exact phrases the question quotes directly.',
        'params': 'query (string, required), host (string, optional), day_from (YYYY-MM-DD, optional), '
                  'day_to (YYYY-MM-DD, optional)',
    },
    'graph_neighbors': {
        'fn': graph_neighbors,
        'description': 'What is structurally connected to a named entity (a host, VM, user, device, '
                       'datastore, process, ...). Use for dependency/impact/blast-radius questions '
                       '("what depends on X", "what runs on X", "what is attached to X").',
        'params': 'entity (string, required - the name/id as it appears in the question), '
                  'hops (int 1-3, default 1)',
    },
    'path_traversal': {
        'fn': path_traversal,
        'description': 'How two named entities are structurally connected. Use for "how are X and Y '
                       'related", "did X affect Y", "is X connected to Y".',
        'params': 'entity_a (string, required), entity_b (string, required), max_hops (int 1-6, default 4)',
    },
    'temporal_window': {
        'fn': temporal_window,
        'description': 'Logs in a specific time range, in chronological order. Use for "what happened '
                       'between T1 and T2", timelines, "what changed before/after X".',
        'params': 'start (ISO timestamp or YYYY-MM-DD, required), end (ISO timestamp or YYYY-MM-DD, '
                  'required), host (string, optional), max_severity_score (int 1-7, optional)',
    },
    'parent_child_retrieval': {
        'fn': parent_child_retrieval,
        'description': 'Expand ONE already-known log (an id returned by an earlier vector_search, '
                       'fulltext_search, or pattern_match call in this same conversation) into every '
                       'other log sharing its operation/trace/task id - the closest equivalent this '
                       'graph has to "show me the rest of this incident". Only useful in a later round, '
                       'after another tool has already surfaced a specific log id.',
        'params': 'log_id (string, required - an id field from a previous tool result), limit (int, default 50)',
    },
    'query_template': {
        'fn': query_template,
        'description': f'Deterministic, pre-written Cypher for common statistics questions. '
                       f'Available templates: {", ".join(sorted(_QUERY_TEMPLATES))}. Prefer this over '
                       f'text2cypher whenever one of these templates already covers the question.',
        'params': 'template (string, required - one of the available template names), host (optional), '
                  'day_from/day_to (YYYY-MM-DD, optional), max_severity_score (int 1-7, optional), '
                  'limit (int, default 25)',
    },
    'pattern_match': {
        'fn': pattern_match,
        'description': f'Search for a known vSphere/VMware failure signature by name (curated search '
                       f'terms, more reliable than freeform vector/fulltext search for these specific '
                       f'shapes). Available patterns: {", ".join(sorted(_KNOWN_PATTERNS))}.',
        'params': 'pattern (string, required - one of the available pattern names), host (optional), '
                  'day_from/day_to (YYYY-MM-DD, optional), top_k (int, default 15)',
    },
    'text2cypher': {
        'fn': None,  # bound at runtime via make_text2cypher_tool - needs a live TextToCypher instance
        'description': 'Translate a structural, counting, filtering, or ranking question directly into '
                       'a graph query. Use for "how many", "which host has the most", "list all X" when '
                       'no query_template already covers it.',
        'params': 'question (string, required - pass the user\'s question, or a rephrased version of it)',
    },
}
