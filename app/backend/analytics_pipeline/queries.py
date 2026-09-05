"""
queries.py - Every Cypher aggregation the dashboard needs, one function each.

All read-only (MATCH/RETURN, never CREATE/SET/DELETE/MERGE) - this pipeline
never writes to the graph the ingestion pipeline built.

Cost note: an aggregation has to touch every row it aggregates over - there's
no way around that for "count logs per day" on the full corpus. What keeps
this usable at the 117M-log scale documented in DEPLOYMENT.md is (a) every
filter here runs on an indexed property (`timestamp`, `severity_score`,
`day`, `source_type`, `hostname` all have indexes from
core/graph_schema.py's INDEXES), and (b) time-bucketed endpoints take an
explicit day/limit so a caller can't accidentally trigger a full-corpus scan
from a single dashboard panel.
"""

from typing import Any, Dict, List, Optional

from config import (ALERT_BAND_MAX_SCORE, ALERT_LEVELS, ALERT_MAX_SCORE,
                    ERROR_MAX_SCORE, level_for_score)

from filters import EMPTY, LogFilter
from neo4j_client import run_read, run_read_one, run_read_stream

# CASE expression reused across queries: buckets a log's severity_score into
# "alert" (EMERGENCY/FATAL/ALERT/CRITICAL) vs "error" (adds ERROR) vs neither.
_ALERT_CASE = f"CASE WHEN l.severity_score <= {ALERT_MAX_SCORE} THEN 1 ELSE 0 END"
_ERROR_CASE = f"CASE WHEN l.severity_score <= {ERROR_MAX_SCORE} THEN 1 ELSE 0 END"


def overview(driver, flt: LogFilter = EMPTY) -> Dict[str, Any]:
    """Headline counters for the top of the dashboard.

    Every counter here honours the same filter, so the tiles can never
    disagree with the charts below them - a filtered "total logs" that sat
    next to a filtered chart drawn from a different population would be worse
    than no filter at all.

    Host/user/source counts are counted THROUGH the logs when a filter is
    active (how many hosts appear in this slice) rather than as raw node
    counts (how many hosts exist at all), because under a filter the second
    number answers a question nobody asked.
    """
    w = flt.clause()
    fp = flt.params()

    # No WHERE at all when nothing is filtered: `MATCH (l:Log) RETURN count(l)`
    # is answered from Neo4j's label count store without touching a single
    # node, and adding a trivially-true predicate silently downgrades it to a
    # full label scan. This runs on every dashboard poll, so the difference is
    # not academic.
    total = (
        run_read_one(driver, f"MATCH (l:Log) WHERE true{w} RETURN count(l) AS c", **fp)
        if flt.active else
        run_read_one(driver, "MATCH (l:Log) RETURN count(l) AS c")
    )

    if flt.active:
        hosts = run_read_one(driver, f"MATCH (l:Log) WHERE l.hostname IS NOT NULL{w} RETURN count(DISTINCT l.hostname) AS c", **fp)
        sources = run_read_one(driver, f"MATCH (l:Log) WHERE l.source_type IS NOT NULL{w} RETURN count(DISTINCT l.source_type) AS c", **fp)
        users = run_read_one(driver, f"MATCH (l:Log) WHERE true{w} WITH l MATCH (u:User)-[]-(l) RETURN count(DISTINCT u) AS c", **fp)
    else:
        hosts = run_read_one(driver, "MATCH (h:Host) RETURN count(h) AS c")
        users = run_read_one(driver, "MATCH (u:User) RETURN count(u) AS c")
        sources = run_read_one(driver, "MATCH (s:SourceType) RETURN count(s) AS c")

    alerts = run_read_one(
        driver, f"MATCH (l:Log) WHERE l.severity_score <= {ALERT_MAX_SCORE}{w} RETURN count(l) AS c", **fp
    )
    errors = run_read_one(
        driver, f"MATCH (l:Log) WHERE l.severity_score <= {ERROR_MAX_SCORE}{w} RETURN count(l) AS c", **fp
    )
    # Index-backed ORDER BY + LIMIT 1 on the range index over Log.timestamp -
    # cheap even at full corpus scale, unlike a min()/max() aggregate scan.
    earliest = run_read_one(
        driver, f"MATCH (l:Log) WHERE l.timestamp IS NOT NULL{w} RETURN l.timestamp AS ts ORDER BY ts ASC LIMIT 1", **fp
    )
    latest = run_read_one(
        driver, f"MATCH (l:Log) WHERE l.timestamp IS NOT NULL{w} RETURN l.timestamp AS ts ORDER BY ts DESC LIMIT 1", **fp
    )

    # Per-level counts in ONE query rather than one round trip per level -
    # this runs on every dashboard poll.
    level_rows = run_read(driver, f"""
        MATCH (l:Log)
        WHERE l.severity_score <= {ALERT_BAND_MAX_SCORE}{w}
        RETURN l.severity_score AS score, count(l) AS c
    """, **fp)
    by_score = {r['score']: r['c'] for r in level_rows}
    by_level = {
        lvl['id']: sum(c for s, c in by_score.items() if lvl['min_score'] <= s <= lvl['max_score'])
        for lvl in ALERT_LEVELS
    }

    return {
        'total_logs': (total or {}).get('c', 0),
        'total_hosts': (hosts or {}).get('c', 0),
        'total_users': (users or {}).get('c', 0),
        'total_sources': (sources or {}).get('c', 0),
        # "Alerts" now spans the whole actionable band (CRITICAL..WARNING)
        # rather than only CRITICAL-and-worse. A corpus with zero CRITICAL
        # records previously showed "0 alerts", which reads as "nothing is
        # wrong" when there are in fact hundreds of errors and warnings.
        'total_alerts': sum(by_level.values()),
        'alerts_by_level': by_level,
        'total_critical': by_level.get('critical', 0),
        'total_errors': (errors or {}).get('c', 0),
        'total_warnings': by_level.get('warning', 0),
        'earliest_timestamp': (earliest or {}).get('ts'),
        'latest_timestamp': (latest or {}).get('ts'),
        'filtered': flt.active,
    }


def timeseries_by_day(driver, days: Optional[int] = None, flt: LogFilter = EMPTY) -> List[Dict[str, Any]]:
    """Log volume + severity breakdown, one row per calendar day.

    `days` limits to the most recent N calendar days present in the graph
    (not wall-clock "today", since this is a batch-ingested historical corpus)
    - omit it to return every day, capped by ANALYTICS_TIMESERIES_MAX_DAYS in
    the caller.
    """
    query = f"""
        MATCH (l:Log)
        WHERE l.day IS NOT NULL{flt.clause()}
        WITH l.day AS bucket,
             count(l) AS total,
             sum({_ALERT_CASE}) AS alerts,
             sum({_ERROR_CASE}) AS errors
        RETURN bucket, total, alerts, errors
        ORDER BY bucket DESC
        {'LIMIT $days' if days else ''}
    """
    fp = flt.params()
    rows = run_read(driver, query, days=days, **fp) if days else run_read(driver, query, **fp)
    return list(reversed(rows))  # chronological order for charting


def timeseries_within_day(driver, day: str, granularity: str = 'hour') -> List[Dict[str, Any]]:
    """Sub-day volume for one calendar day, bucketed by hour or minute -
    what "logs per second" style panels are built from (divide the bucket
    width in seconds to get an average rate for that bucket)."""
    # ISO-8601 prefix length: 'YYYY-MM-DDTHH' = 13 chars, '...THH:MM' = 16.
    prefix_len = 16 if granularity == 'minute' else 13
    query = f"""
        MATCH (l:Log)
        WHERE l.day = $day AND l.timestamp IS NOT NULL
        WITH substring(l.timestamp, 0, {prefix_len}) AS bucket,
             count(l) AS total,
             sum({_ALERT_CASE}) AS alerts,
             sum({_ERROR_CASE}) AS errors
        RETURN bucket, total, alerts, errors
        ORDER BY bucket ASC
    """
    return run_read(driver, query, day=day)


def severity_distribution(driver, flt: LogFilter = EMPTY) -> List[Dict[str, Any]]:
    query = f"""
        MATCH (l:Log)
        WHERE l.severity IS NOT NULL{flt.clause()}
        RETURN l.severity AS severity, l.severity_score AS score, count(l) AS count
        ORDER BY score
    """
    return run_read(driver, query, **flt.params())


def by_source_type(driver, flt: LogFilter = EMPTY) -> List[Dict[str, Any]]:
    query = f"""
        MATCH (l:Log)
        WHERE l.source_type IS NOT NULL{flt.clause()}
        WITH l.source_type AS source_type, count(l) AS total, sum({_ERROR_CASE}) AS errors
        RETURN source_type, total, errors
        ORDER BY total DESC
    """
    return run_read(driver, query, **flt.params())


def top_hosts(driver, limit: int = 10, metric: str = 'total', flt: LogFilter = EMPTY) -> List[Dict[str, Any]]:
    order_col = 'errors' if metric == 'errors' else 'total'
    query = f"""
        MATCH (l:Log)
        WHERE l.hostname IS NOT NULL{flt.clause()}
        WITH l.hostname AS host, count(l) AS total, sum({_ERROR_CASE}) AS errors
        RETURN host, total, errors
        ORDER BY {order_col} DESC
        LIMIT $limit
    """
    return run_read(driver, query, limit=limit, **flt.params())


def top_processes(driver, limit: int = 10, flt: LogFilter = EMPTY) -> List[Dict[str, Any]]:
    query = f"""
        MATCH (l:Log)
        WHERE l.process IS NOT NULL AND l.process <> ''{flt.clause()}
        WITH l.process AS process, count(l) AS total, sum({_ERROR_CASE}) AS errors
        RETURN process, total, errors
        ORDER BY total DESC
        LIMIT $limit
    """
    return run_read(driver, query, limit=limit, **flt.params())


def top_error_components(driver, limit: int = 10, flt: LogFilter = EMPTY) -> List[Dict[str, Any]]:
    """Components ranked by error volume specifically (not total volume) -
    "which source shows more errors" is an error ranking, not a traffic one."""
    query = f"""
        MATCH (l:Log)
        WHERE l.component IS NOT NULL AND l.component <> '' AND l.severity_score <= {ERROR_MAX_SCORE}{flt.clause()}
        WITH l.component AS component, count(l) AS errors
        RETURN component, errors
        ORDER BY errors DESC
        LIMIT $limit
    """
    return run_read(driver, query, limit=limit, **flt.params())


# Fields returned for a log ROW (a table line). Deliberately excludes
# raw_message/attributes_json/embedding: a table of 200 rows carrying every
# raw line would be megabytes of payload for text nobody reads until they
# click. log_detail() below returns those for the one row actually opened.
_ROW_FIELDS = """
    l.id AS id, l.timestamp AS timestamp, l.timestamp_source AS timestamp_source,
    l.day AS day, l.hostname AS hostname,
    l.source_type AS source_type, l.process AS process, l.component AS component,
    l.severity AS severity, l.severity_score AS severity_score,
    l.message AS message, l.matched_format AS matched_format,
    l.confidence AS confidence, l.pid AS pid
"""


def list_logs(driver, limit: int = 100, max_severity_score: Optional[int] = None,
              severity: Optional[str] = None, hostname: Optional[str] = None,
              source_type: Optional[str] = None, day: Optional[str] = None,
              search: Optional[str] = None, before_timestamp: Optional[str] = None,
              after_timestamp: Optional[str] = None, alert_level: Optional[str] = None,
              order: str = 'desc') -> List[Dict[str, Any]]:
    """Newest-first (or oldest-first) log rows, with optional filters.

    `before_timestamp`/`after_timestamp` are keyset cursors rather than
    SKIP/OFFSET: the live tail polls repeatedly and "everything newer than
    what I already have" has to stay cheap no matter how deep the table is,
    which SKIP does not - it re-walks every skipped row each call.
    """
    where = ["l.timestamp IS NOT NULL"]
    params: Dict[str, Any] = {'limit': limit}

    if alert_level:
        band = next((l for l in ALERT_LEVELS if l['id'] == alert_level), None)
        if band:
            where.append("l.severity_score >= $lvl_min AND l.severity_score <= $lvl_max")
            params['lvl_min'] = band['min_score']
            params['lvl_max'] = band['max_score']
    if max_severity_score is not None:
        where.append("l.severity_score <= $max_score")
        params['max_score'] = max_severity_score
    if severity:
        where.append("l.severity = $severity")
        params['severity'] = severity
    if hostname:
        where.append("l.hostname = $hostname")
        params['hostname'] = hostname
    if source_type:
        where.append("l.source_type = $source_type")
        params['source_type'] = source_type
    if day:
        where.append("l.day = $day")
        params['day'] = day
    if before_timestamp:
        where.append("l.timestamp < $before_ts")
        params['before_ts'] = before_timestamp
    if after_timestamp:
        where.append("l.timestamp > $after_ts")
        params['after_ts'] = after_timestamp
    if search:
        # CONTAINS, not the fulltext index: this powers a type-ahead filter
        # box where the term is a fragment ("naa.600", "vmhba") rather than a
        # word, and a fulltext index tokenizes those away entirely.
        where.append("toLower(l.message) CONTAINS toLower($search)")
        params['search'] = search

    direction = 'ASC' if order == 'asc' else 'DESC'
    query = f"""
        MATCH (l:Log)
        WHERE {' AND '.join(where)}
        RETURN {_ROW_FIELDS}
        ORDER BY l.timestamp {direction}
        LIMIT $limit
    """
    rows = run_read(driver, query, **params)
    # Banding is derived once here, server-side, so the table, the filters and
    # the counters can never disagree about what counts as which level.
    for row in rows:
        row['alert_level'] = level_for_score(row.get('severity_score'))
    return rows


def log_detail(driver, log_id: str) -> Optional[Dict[str, Any]]:
    """Everything about ONE log, including the verbatim raw line, the
    format-specific attribute bag, and every entity it references with the
    relationship that connects them - the payload behind clicking a row."""
    query = """
        MATCH (l:Log {id: $log_id})
        OPTIONAL MATCH (l)-[r]->(e)
        WHERE NOT e:Severity AND NOT e:SourceType AND NOT e:Day
              AND NOT e:Process AND NOT e:Component
        WITH l, collect(DISTINCT {
            rel: type(r),
            label: labels(e)[0],
            value: coalesce(e.name, e.id, e.address, e.path, e.moref, e.value,
                            e.code, e.key, e.sid, e.number, e.date, e.level)
        }) AS entities
        RETURN l.id AS id, l.timestamp AS timestamp, l.day AS day,
               l.hostname AS hostname, l.source_type AS source_type,
               l.process AS process, l.component AS component,
               l.subcomponent AS subcomponent, l.pid AS pid,
               l.severity AS severity, l.severity_score AS severity_score,
               l.message AS message, l.normalized_message AS normalized_message,
               l.raw_message AS raw_message, l.raw_truncated AS raw_truncated,
               l.raw_length AS raw_length,
               l.attributes_json AS attributes_json,
               l.confidence AS confidence, l.matched_format AS matched_format,
               l.http_method AS http_method, l.http_status AS http_status,
               (l.embedding IS NOT NULL) AS has_embedding,
               entities
    """
    rows = run_read(driver, query, log_id=log_id)
    if not rows:
        return None
    detail = rows[0]
    # The OPTIONAL MATCH yields one all-null entry when a log references
    # nothing; strip those so the UI shows "no entities" rather than a row of
    # blanks.
    detail['entities'] = [e for e in (detail.get('entities') or []) if e.get('value')]
    return detail


def distinct_values(driver, field: str, limit: int = 200) -> List[str]:
    """Distinct values of one indexed Log property, for the filter dropdowns.

    `field` is whitelisted rather than interpolated freely - it lands in the
    query text (Cypher can't parameterize a property name), so accepting an
    arbitrary string here would be an injection point.
    """
    allowed = {'hostname', 'source_type', 'severity', 'day', 'process', 'matched_format'}
    if field not in allowed:
        return []
    query = f"""
        MATCH (l:Log)
        WHERE l.{field} IS NOT NULL
        RETURN DISTINCT l.{field} AS value
        ORDER BY value
        LIMIT $limit
    """
    return [r['value'] for r in run_read(driver, query, limit=limit)]


def hourly_heatmap(driver, days: int = 14, flt: LogFilter = EMPTY) -> List[Dict[str, Any]]:
    """Log volume per (day, hour-of-day) - the classic observability heatmap.

    Answers "when do things go wrong" in a way a daily total cannot: a nightly
    batch window or a 09:00 login storm is a column in this grid and invisible
    in a per-day line.
    """
    query = f"""
        MATCH (l:Log)
        WHERE l.day IS NOT NULL AND l.timestamp IS NOT NULL{flt.clause()}
        WITH l.day AS day,
             toInteger(substring(l.timestamp, 11, 2)) AS hour,
             count(l) AS total,
             sum({_ERROR_CASE}) AS errors
        RETURN day, hour, total, errors
        ORDER BY day DESC, hour ASC
        LIMIT {days * 24}
    """
    return run_read(driver, query, **flt.params())


def error_rate_by_day(driver, days: int = 30, flt: LogFilter = EMPTY) -> List[Dict[str, Any]]:
    """Error RATE per day, not error count.

    A day with 10x the traffic and 10x the errors is not getting worse, but a
    volume chart makes it look that way. Rate separates "busier" from "sicker".
    """
    query = f"""
        MATCH (l:Log)
        WHERE l.day IS NOT NULL{flt.clause()}
        WITH l.day AS bucket, count(l) AS total, sum({_ERROR_CASE}) AS errors
        RETURN bucket, total, errors,
               CASE WHEN total = 0 THEN 0.0
                    ELSE toFloat(errors) * 100.0 / total END AS error_rate
        ORDER BY bucket DESC
        LIMIT $days
    """
    rows = run_read(driver, query, days=days, **flt.params())
    return list(reversed(rows))


# Entity labels worth surfacing, with the property holding their display value.
# Restricted to a whitelist because the label goes into the query text (Cypher
# cannot parameterize a label) - see distinct_values() for the same reasoning.
_ENTITY_LABELS = [
    ('User', 'name'), ('IPAddress', 'address'), ('Device', 'id'),
    ('VM', 'moref'), ('Datastore', 'name'), ('Operation', 'id'),
    ('Pod', 'name'), ('ErrorCode', 'code'), ('File', 'path'),
    ('SystemdUnit', 'name'), ('Executable', 'path'), ('Task', 'id'),
]


def graph_composition(driver) -> List[Dict[str, Any]]:
    """Node count per label - what the knowledge graph is actually made of.

    The dashboard otherwise only ever shows :Log properties, which undersells
    the thing that makes this a graph rather than a log table.
    """
    rows = run_read(driver, """
        CALL db.labels() YIELD label
        CALL (label) {
            MATCH (n) WHERE label IN labels(n)
            RETURN count(n) AS c
        }
        RETURN label, c AS count
        ORDER BY count DESC
    """)
    if rows:
        return rows
    # `CALL (var) { }` is Neo4j 5.23+. Older servers need the legacy form, and
    # some managed instances disallow it entirely - fall back to one count per
    # label rather than showing an empty panel.
    labels = [r['label'] for r in run_read(driver, "CALL db.labels() YIELD label RETURN label")]
    out = []
    for label in labels:
        row = run_read_one(driver, f"MATCH (n:`{label}`) RETURN count(n) AS c")
        if row:
            out.append({'label': label, 'count': row['c']})
    return sorted(out, key=lambda r: -r['count'])


def top_entities(driver, limit_per_type: int = 5) -> Dict[str, List[Dict[str, Any]]]:
    """The busiest extracted entities per type, ranked by how many logs
    reference them - the "who/what shows up most" view of the entity layer."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for label, key in _ENTITY_LABELS:
        rows = run_read(driver, f"""
            MATCH (l:Log)-[]->(e:`{label}`)
            WHERE e.`{key}` IS NOT NULL
            WITH e.`{key}` AS value, count(l) AS mentions
            RETURN value, mentions
            ORDER BY mentions DESC
            LIMIT $limit
        """, limit=limit_per_type)
        if rows:
            out[label] = rows
    return out


def top_messages(driver, limit: int = 10, errors_only: bool = True, flt: LogFilter = EMPTY) -> List[Dict[str, Any]]:
    """The most repeated log messages - noise ranking.

    Grouped on the first 120 characters rather than the whole message: almost
    every repeated log line differs only in a trailing id or duration, so
    grouping on the full text would return every line exactly once and say
    nothing.
    """
    where = f"WHERE l.message IS NOT NULL AND l.severity_score <= {ERROR_MAX_SCORE}" \
        if errors_only else "WHERE l.message IS NOT NULL"
    where += flt.clause()
    query = f"""
        MATCH (l:Log)
        {where}
        WITH substring(l.message, 0, 120) AS pattern,
             count(l) AS occurrences,
             collect(DISTINCT l.hostname)[..4] AS hosts,
             head(collect(l.severity)) AS severity
        RETURN pattern, occurrences, hosts, severity
        ORDER BY occurrences DESC
        LIMIT $limit
    """
    return run_read(driver, query, limit=limit, **flt.params())


def severity_by_source(driver, limit: int = 8, flt: LogFilter = EMPTY) -> List[Dict[str, Any]]:
    """Severity split per source type - which platform is noisy vs which is
    actually failing."""
    query = f"""
        MATCH (l:Log)
        WHERE l.source_type IS NOT NULL{flt.clause()}
        WITH l.source_type AS source_type,
             count(l) AS total,
             sum(CASE WHEN l.severity_score <= {ALERT_MAX_SCORE} THEN 1 ELSE 0 END) AS critical,
             sum(CASE WHEN l.severity_score = 3 THEN 1 ELSE 0 END) AS errors,
             sum(CASE WHEN l.severity_score = 4 THEN 1 ELSE 0 END) AS warnings
        RETURN source_type, total, critical, errors, warnings
        ORDER BY total DESC
        LIMIT $limit
    """
    return run_read(driver, query, limit=limit, **flt.params())


def parse_coverage(driver, limit: int = 15, flt: LogFilter = EMPTY) -> List[Dict[str, Any]]:
    """How much of the corpus each detector (`matched_format`) actually
    covers - the same "generic_fallback share" signal parse_logs.py reports
    at ingest time, but queryable live against whatever's in the graph now."""
    query = f"""
        MATCH (l:Log)
        WHERE l.matched_format IS NOT NULL{flt.clause()}
        WITH l.matched_format AS format, count(l) AS total
        RETURN format, total
        ORDER BY total DESC
        LIMIT $limit
    """
    return run_read(driver, query, limit=limit, **flt.params())


# Every field of the Universal Event Schema, for export. Deliberately NOT
# _ROW_FIELDS: a table row omits raw_message and attributes_json because a
# 200-row page does not need them, whereas an export that dropped them would
# not be lossless - which is the entire point of the format.
_EXPORT_FIELDS = """
    l.id AS id, l.timestamp AS timestamp, l.timestamp_source AS timestamp_source,
    l.day AS day, l.hostname AS hostname,
    l.process AS process, l.pid AS pid, l.component AS component,
    l.subcomponent AS subcomponent, l.source_type AS source_type,
    l.severity AS severity, l.severity_score AS severity_score,
    l.message AS message, l.normalized_message AS normalized_message,
    l.attributes_json AS attributes_json,
    l.http_method AS http_method, l.http_status AS http_status,
    l.raw_message AS raw_message, l.raw_truncated AS raw_truncated,
    l.raw_length AS raw_length,
    l.source_file AS source_file, l.source_record AS source_record,
    l.matched_format AS matched_format, l.confidence AS confidence,
    toString(l.created_at) AS created_at
"""


def stream_export(driver, flt: LogFilter = EMPTY, limit: Optional[int] = None):
    """Streams normalized events for a SIEM / Data Lake export.

    Ordered by timestamp so an export is reproducible run to run, and so an
    interrupted transfer can be resumed from the last event received instead
    of restarted from the beginning.

    Returns a generator - the rows are never all in memory at once. See
    run_read_stream() for why that matters here and not elsewhere.
    """
    query = f"""
        MATCH (l:Log)
        WHERE l.timestamp IS NOT NULL{flt.clause()}
        RETURN {_EXPORT_FIELDS}
        ORDER BY l.timestamp ASC, l.id ASC
        {'LIMIT $export_limit' if limit else ''}
    """
    params = dict(flt.params())
    if limit:
        params['export_limit'] = limit
    return run_read_stream(driver, query, **params)
