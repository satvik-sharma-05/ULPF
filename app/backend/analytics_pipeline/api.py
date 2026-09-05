"""
api.py - FastAPI backend for the analytics dashboard.

    uvicorn api:app --host 0.0.0.0 --port 8010

Read-only and standalone, same contract as chatbot_pipeline/api.py: the only
dependency is a Neo4j instance the ingestion pipeline populated. This service
never imports ../ingestion_pipeline or ../chatbot_pipeline and never writes to
the graph, so it can be deployed, scaled or restarted independently of both.

    GET /api/analytics/overview        headline counters + corpus time range
    GET /api/analytics/timeseries      logs/alerts/errors per day
    GET /api/analytics/timeseries/day  logs per hour/minute within one day
    GET /api/analytics/severity        log count per severity level
    GET /api/analytics/sources         log + error count per source type
    GET /api/analytics/hosts           top hosts by volume or by errors
    GET /api/analytics/processes       top processes by volume
    GET /api/analytics/error-components  components ranked by error volume
    GET /api/analytics/parse-coverage  matched_format share of the corpus
    GET /api/analytics/insights        rule-based observations over the above
    GET /api/analytics/dashboard       everything above, bundled in one call
    GET /api/analytics/health          Neo4j reachability
"""

import json
import logging
import os
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is a convenience, not a hard requirement

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'WARNING'),
                    format='%(asctime)s [%(levelname)s] %(name)s - %(message)s')
logger = logging.getLogger("analytics.api")

from config import (ALERT_BAND_MAX_SCORE, ALERT_LEVELS, ALERT_MAX_SCORE,
                    ANALYTICS_CONFIG, API_CONFIG, ERROR_MAX_SCORE, NEO4J_CONFIG)
import anomalies
import exporters

#  Bounded so the endpoint stays cheap on a large graph; it is a
#  representative sample, not a full census, and says so in its response.
OCSF_COVERAGE_SAMPLE = 20000

#  The most recent forwarder, so /forward/sinks can report what it did.
_FORWARDER = None
import taxonomy
from filters import LogFilter
from insights import build_insights
from neo4j_client import connect, run_read_one
import queries as q

app = FastAPI(title="Log Graph Analytics API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=API_CONFIG['cors_origins'],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_driver = None
_driver_last_attempt = 0.0
_DRIVER_RETRY_SECONDS = 15  # don't retry a slow TCP connect on every request while Neo4j is down


def get_driver():
    """Built lazily so a Neo4j hiccup at import time doesn't crash server
    boot - every query function already degrades to an empty result when the
    driver is None, so the dashboard just shows zeros/"not connected" instead
    of failing to start.

    A failed connection is remembered for _DRIVER_RETRY_SECONDS instead of
    retried on every call - without this, a genuinely-down Neo4j turns every
    request into a multi-second TCP connect timeout (the dashboard polls
    several endpoints per refresh), which is worse than just serving zeros
    until the next retry window."""
    global _driver, _driver_last_attempt
    if _driver is not None:
        return _driver
    now = time.monotonic()
    if now - _driver_last_attempt < _DRIVER_RETRY_SECONDS:
        return None
    _driver_last_attempt = now
    _driver = connect()
    return _driver


@app.on_event("startup")
def _warm_start():
    get_driver()


@app.on_event("shutdown")
def _shutdown():
    if _driver is not None:
        _driver.close()


# ---------------------------------------------------------------------------
# Tiny in-process TTL cache. This is a batch-ingested historical corpus, not a
# live-updating stream (see ingestion_pipeline/README.md) - the graph doesn't
# change between two dashboard polls a few seconds apart, so re-running a
# full-corpus aggregation on every request (and every panel on /dashboard) is
# pure waste. A process-local dict is enough: this API is meant to run as one
# instance in front of one Neo4j, not horizontally scaled.
# ---------------------------------------------------------------------------
_cache: Dict[str, tuple] = {}


def cached(key: str, fn: Callable[[], Any]) -> Any:
    ttl = ANALYTICS_CONFIG['cache_ttl_seconds']
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (now, value)
    return value


_DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def build_filter(start: Optional[str], end: Optional[str], severity: Optional[str],
                 source_type: Optional[str], hostname: Optional[str],
                 search: Optional[str]) -> LogFilter:
    """Turns the query string into a LogFilter, rejecting malformed dates.

    The dates are validated here rather than trusted downstream: they end up
    in a lexicographic comparison against `l.day`, so a value like '2026-6-1'
    would compare wrong (shorter string, no zero padding) and silently return
    the wrong window instead of failing. A 400 is much better than a chart
    that is quietly about the wrong dates.
    """
    for label, value in (('start', start), ('end', end)):
        if value and not _DAY_RE.match(value):
            raise HTTPException(400, f"{label} must be YYYY-MM-DD, got {value!r}")
    if start and end and start > end:
        raise HTTPException(400, f"start ({start}) is after end ({end})")
    return LogFilter(
        start=start or None, end=end or None,
        severity=(severity or None), source_type=(source_type or None),
        hostname=(hostname or None), search=(search or None),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/api/analytics/overview")
def overview() -> Dict[str, Any]:
    return cached('overview', lambda: q.overview(get_driver()))


@app.get("/api/analytics/timeseries")
def timeseries(days: Optional[int] = Query(None, ge=1, le=ANALYTICS_CONFIG['timeseries_max_days'])) -> List[Dict[str, Any]]:
    days = days or ANALYTICS_CONFIG['timeseries_max_days']
    return cached(f'timeseries:{days}', lambda: q.timeseries_by_day(get_driver(), days=days))


@app.get("/api/analytics/timeseries/day")
def timeseries_day(
    day: str = Query(..., description="YYYY-MM-DD"),
    granularity: str = Query('hour', pattern='^(hour|minute)$'),
) -> List[Dict[str, Any]]:
    return cached(
        f'timeseries_day:{day}:{granularity}',
        lambda: q.timeseries_within_day(get_driver(), day=day, granularity=granularity),
    )


@app.get("/api/analytics/severity")
def severity() -> List[Dict[str, Any]]:
    return cached('severity', lambda: q.severity_distribution(get_driver()))


@app.get("/api/analytics/sources")
def sources() -> List[Dict[str, Any]]:
    return cached('sources', lambda: q.by_source_type(get_driver()))


@app.get("/api/analytics/hosts")
def hosts(
    limit: int = Query(ANALYTICS_CONFIG['top_n_default'], ge=1, le=50),
    metric: str = Query('total', pattern='^(total|errors)$'),
) -> List[Dict[str, Any]]:
    return cached(f'hosts:{limit}:{metric}', lambda: q.top_hosts(get_driver(), limit=limit, metric=metric))


@app.get("/api/analytics/processes")
def processes(limit: int = Query(ANALYTICS_CONFIG['top_n_default'], ge=1, le=50)) -> List[Dict[str, Any]]:
    return cached(f'processes:{limit}', lambda: q.top_processes(get_driver(), limit=limit))


@app.get("/api/analytics/error-components")
def error_components(limit: int = Query(ANALYTICS_CONFIG['top_n_default'], ge=1, le=50)) -> List[Dict[str, Any]]:
    return cached(f'error_components:{limit}', lambda: q.top_error_components(get_driver(), limit=limit))


@app.get("/api/analytics/parse-coverage")
def parse_coverage(limit: int = Query(15, ge=1, le=50)) -> List[Dict[str, Any]]:
    return cached(f'parse_coverage:{limit}', lambda: q.parse_coverage(get_driver(), limit=limit))


def _compute_insights(flt: LogFilter = LogFilter()) -> List[Dict[str, Any]]:
    # Insights are derived entirely from the aggregations above, so passing the
    # filter down is what keeps a sentence like "ESXi produces the most errors"
    # true of the window on screen rather than of the whole corpus.
    driver = get_driver()
    ov = q.overview(driver, flt)
    sev = q.severity_distribution(driver, flt)
    src = q.by_source_type(driver, flt)
    hosts_errs = q.top_hosts(driver, limit=25, metric='errors', flt=flt)
    daily = q.timeseries_by_day(driver, days=ANALYTICS_CONFIG['timeseries_max_days'], flt=flt)
    return build_insights(
        overview=ov, severity_rows=sev, source_rows=src, hosts_by_errors=hosts_errs, daily=daily,
        min_volume=ANALYTICS_CONFIG['min_volume_for_rate'],
        trend_window_days=ANALYTICS_CONFIG['insights_trend_window_days'],
        heatmap=q.hourly_heatmap(driver, days=14, flt=flt),
        top_messages=q.top_messages(driver, limit=8, flt=flt),
        severity_by_source=q.severity_by_source(driver, limit=8, flt=flt),
        entities=q.top_entities(driver, 5),
    )


@app.get("/api/analytics/insights")
def insights() -> List[Dict[str, Any]]:
    return cached('insights', _compute_insights)


@app.get("/api/analytics/dashboard")
def dashboard(
    start: Optional[str] = Query(None, description="Inclusive first day, YYYY-MM-DD"),
    end: Optional[str] = Query(None, description="Inclusive last day, YYYY-MM-DD"),
    severity: Optional[str] = Query(None),
    source_type: Optional[str] = Query(None),
    hostname: Optional[str] = Query(None),
    search: Optional[str] = Query(None, description="Case-insensitive substring of the message"),
) -> Dict[str, Any]:
    """Everything the dashboard's first paint needs, in one round trip -
    avoids a waterfall of 7+ sequential/parallel fetches on initial load.

    Every panel below receives the SAME filter. That is the point: a filtered
    headline count sitting above charts drawn from the whole corpus would be
    actively misleading, so the filter is threaded through rather than applied
    to a subset of the panels.
    """
    driver = get_driver()
    flt = build_filter(start, end, severity, source_type, hostname, search)
    # An unfiltered request produces an empty suffix, so it keeps sharing the
    # existing warm cache entries instead of splitting the cache in two.
    k = flt.cache_key()
    sfx = f":{k}" if k else ""

    return {
        'overview': cached(f'overview{sfx}', lambda: q.overview(driver, flt)),
        'timeseries': cached(
            f"timeseries:{ANALYTICS_CONFIG['timeseries_max_days']}{sfx}",
            lambda: q.timeseries_by_day(driver, days=ANALYTICS_CONFIG['timeseries_max_days'], flt=flt),
        ),
        'severity': cached(f'severity{sfx}', lambda: q.severity_distribution(driver, flt)),
        'sources': cached(f'sources{sfx}', lambda: q.by_source_type(driver, flt)),
        'top_hosts': cached(f'hosts:10:errors{sfx}', lambda: q.top_hosts(driver, limit=10, metric='errors', flt=flt)),
        'top_hosts_volume': cached(f'hosts:10:total{sfx}', lambda: q.top_hosts(driver, limit=10, metric='total', flt=flt)),
        'top_processes': cached(f'processes:10{sfx}', lambda: q.top_processes(driver, limit=10, flt=flt)),
        'error_components': cached(f'error_components:10{sfx}', lambda: q.top_error_components(driver, limit=10, flt=flt)),
        'heatmap': cached(f'heatmap:14{sfx}', lambda: q.hourly_heatmap(driver, days=14, flt=flt)),
        'error_rate': cached(f'error_rate:30{sfx}', lambda: q.error_rate_by_day(driver, days=30, flt=flt)),
        # Node counts per label describe the whole graph, not a slice of logs -
        # there is no honest way to filter "how many Host nodes exist" by a
        # date range, so this panel stays global and the UI labels it as such.
        'graph_composition': cached('graph_composition', lambda: q.graph_composition(driver)),
        'entities': cached('entities:5', lambda: q.top_entities(driver, 5)),
        'top_messages': cached(f'top_messages:8:True{sfx}', lambda: q.top_messages(driver, limit=8, flt=flt)),
        'severity_by_source': cached(f'sev_by_source:8{sfx}', lambda: q.severity_by_source(driver, limit=8, flt=flt)),
        'parse_coverage': cached(f'parse_coverage:10{sfx}', lambda: q.parse_coverage(driver, limit=10, flt=flt)),
        'insights': cached(f'insights{sfx}', lambda: _compute_insights(flt)),
        'anomalies': cached(f'anomalies{sfx}', lambda: anomalies.detect(driver, flt)),
        # Echoed so the UI can render exactly what is in force, and so a
        # screenshot of a filtered dashboard says what it is a picture of.
        'filter': flt.describe(),
        'filtered': flt.active,
    }


@app.get("/api/analytics/logs")
def logs(
    limit: int = Query(100, ge=1, le=500),
    severity: Optional[str] = None,
    max_severity_score: Optional[int] = Query(None, ge=1, le=7),
    hostname: Optional[str] = None,
    source_type: Optional[str] = None,
    day: Optional[str] = None,
    search: Optional[str] = None,
    before_timestamp: Optional[str] = None,
    after_timestamp: Optional[str] = None,
    order: str = Query('desc', pattern='^(asc|desc)$'),
) -> List[Dict[str, Any]]:
    """Log rows for the alerts table and the live tail.

    Not cached: this is the one endpoint whose whole purpose is showing what
    is in the graph *right now*, and it is already keyset-paginated, so it
    stays cheap without a cache in front of it.
    """
    return q.list_logs(
        get_driver(), limit=limit, severity=severity,
        max_severity_score=max_severity_score, hostname=hostname,
        source_type=source_type, day=day, search=search,
        before_timestamp=before_timestamp, after_timestamp=after_timestamp,
        order=order,
    )


@app.get("/api/analytics/alert-levels")
def alert_levels() -> List[Dict[str, Any]]:
    """The graded alert bands, so the UI labels and filters them from the
    same definition the queries band by - not a second copy that can drift."""
    return ALERT_LEVELS


@app.get("/api/analytics/alerts")
def alerts(
    limit: int = Query(200, ge=1, le=500),
    level: Optional[str] = Query(None, pattern='^(critical|error|warning)$'),
    hostname: Optional[str] = None,
    source_type: Optional[str] = None,
    search: Optional[str] = None,
    before_timestamp: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Everything worth alerting on, newest first, graded by level.

    With no `level`, returns the whole actionable band: CRITICAL, ERROR and
    WARNING (severity_score <= 4). NOTICE/INFO/DEBUG are excluded - they are
    normal operational chatter, and counting them as alerts would make the
    number meaningless. Pass `level` to narrow to one band for triage.
    """
    return q.list_logs(
        get_driver(), limit=limit,
        alert_level=level,
        max_severity_score=None if level else ALERT_BAND_MAX_SCORE,
        hostname=hostname, source_type=source_type, search=search,
        before_timestamp=before_timestamp, order='desc',
    )


@app.get("/api/analytics/logs/{log_id}")
def log_detail(log_id: str) -> Dict[str, Any]:
    detail = q.log_detail(get_driver(), log_id)
    if detail is None:
        raise HTTPException(404, f"No log with id {log_id!r}")
    # attributes_json is stored as a JSON *string* on the node (Neo4j
    # properties can't hold nested maps) - parse it here so the UI renders a
    # key/value table instead of a wall of escaped JSON.
    raw_attrs = detail.pop('attributes_json', None)
    parsed: Dict[str, Any] = {}
    if raw_attrs:
        try:
            loaded = json.loads(raw_attrs)
            if isinstance(loaded, dict):
                parsed = loaded
        except (ValueError, TypeError):
            parsed = {'_unparsed': raw_attrs}
    detail['attributes'] = parsed
    return detail


@app.get("/api/analytics/filters")
def filters() -> Dict[str, List[str]]:
    """Distinct values for the table's filter dropdowns."""
    driver = get_driver()
    return cached('filters', lambda: {
        'hostnames': q.distinct_values(driver, 'hostname', limit=300),
        'source_types': q.distinct_values(driver, 'source_type', limit=100),
        'severities': q.distinct_values(driver, 'severity', limit=20),
        'days': q.distinct_values(driver, 'day', limit=200),
    })


@app.get("/api/analytics/heatmap")
def heatmap(days: int = Query(14, ge=1, le=60)) -> List[Dict[str, Any]]:
    return cached(f'heatmap:{days}', lambda: q.hourly_heatmap(get_driver(), days=days))


@app.get("/api/analytics/error-rate")
def error_rate(days: int = Query(30, ge=1, le=90)) -> List[Dict[str, Any]]:
    return cached(f'error_rate:{days}', lambda: q.error_rate_by_day(get_driver(), days=days))


@app.get("/api/analytics/graph-composition")
def graph_composition() -> List[Dict[str, Any]]:
    return cached('graph_composition', lambda: q.graph_composition(get_driver()))


@app.get("/api/analytics/entities")
def entities(limit_per_type: int = Query(5, ge=1, le=20)) -> Dict[str, List[Dict[str, Any]]]:
    return cached(f'entities:{limit_per_type}', lambda: q.top_entities(get_driver(), limit_per_type))


@app.get("/api/analytics/top-messages")
def top_messages(limit: int = Query(10, ge=1, le=50), errors_only: bool = True) -> List[Dict[str, Any]]:
    return cached(f'top_messages:{limit}:{errors_only}',
                  lambda: q.top_messages(get_driver(), limit=limit, errors_only=errors_only))


@app.get("/api/analytics/severity-by-source")
def severity_by_source(limit: int = Query(8, ge=1, le=25)) -> List[Dict[str, Any]]:
    return cached(f'sev_by_source:{limit}', lambda: q.severity_by_source(get_driver(), limit=limit))


@app.get("/api/analytics/health")
def health() -> Dict[str, Any]:
    driver = get_driver()
    neo4j_ok = driver is not None
    # Name the graph this service is actually reading. Switching the app's mode
    # does NOT repoint an already-running analytics process - it reads the
    # NEO4J_URI it was started with - so without this the UI has no way to tell
    # sample data from production data, and "production mode" silently shows
    # the sample sandbox. The frontend puts this on screen; the operator can
    # then see at a glance which database the numbers came from.
    return {
        'neo4j': neo4j_ok,
        'status': 'ok' if neo4j_ok else 'degraded',
        'neo4j_uri': NEO4J_CONFIG['uri'],
        'neo4j_database': NEO4J_CONFIG['database'],
    }



# ---------------------------------------------------------------------------
# Universal Event Schema + SIEM / Data Lake export  (ULPF c, d, g)
# ---------------------------------------------------------------------------
@app.get("/api/analytics/schema")
def schema() -> Dict[str, Any]:
    """The declared common event taxonomy every source is normalized into.

    Served rather than only documented so an integrator can generate against
    it, and so the UI renders the same document the exporters are built from -
    there is no second copy to drift.
    """
    return taxonomy.describe()


@app.get("/api/analytics/schema/coverage")
def schema_coverage() -> Dict[str, Any]:
    """How well the live graph actually satisfies the schema above.

    A schema that claims a field is always present is a claim, and this is the
    check. `violates_contract` marks any REQUIRED field that is not on 100% of
    events - if one ever shows up there it is a real defect, not a display
    quirk, and it should be impossible to miss.
    """
    rows = cached('schema_coverage',
                  lambda: taxonomy.field_coverage(get_driver(), run_read_one))
    return {
        'fields': rows,
        'violations': [r for r in rows if r['violates_contract']],
    }


@app.get("/api/analytics/export/formats")
def export_formats() -> List[Dict[str, str]]:
    return exporters.describe_formats()


@app.get("/api/analytics/integrity/status")
def integrity_status() -> Dict[str, Any]:
    """Tamper-evident ledger state: sealed or not, how much, and the head."""
    import integrity
    return integrity.status()


class SealRequest(BaseModel):
    limit: int = Field(5000, ge=1, le=50000)


@app.post("/api/analytics/integrity/seal")
def integrity_seal(req: SealRequest) -> Dict[str, Any]:
    """Seals stored events into an append-only hash chain."""
    import integrity
    rows = list(q.stream_export(get_driver(), LogFilter(), limit=req.limit))
    return integrity.seal(rows)


@app.get("/api/analytics/integrity/verify")
def integrity_verify(expected_head: Optional[str] = None) -> Dict[str, Any]:
    """Re-derives every hash in the ledger and reports the first break.

    Pass `expected_head` - a head hash held outside this system - to detect a
    full rewrite. Without it, this proves internal consistency only, which a
    rewritten chain also has.
    """
    import integrity
    return integrity.verify(expected_head)


@app.get("/api/analytics/integrity/prove/{event_id}")
def integrity_prove(event_id: str) -> Dict[str, Any]:
    """A Merkle inclusion proof for one event.

    log2(block size) sibling hashes - about 9 for a 500-event block - which
    can be handed to someone who is not permitted to see the other events.
    """
    import integrity
    proof = integrity.prove_by_id(event_id)
    if not proof:
        raise HTTPException(404, 'event %s is not in the sealed ledger' % event_id)
    return proof


@app.get("/api/analytics/forward/sinks")
def forward_sinks() -> Dict[str, Any]:
    """The push destinations this build can ship events to.

    Requirement (g) needs both directions. The export endpoints are PULL - a
    SIEM has to come and fetch. Real deployments expect the opposite, so this
    reports the sinks a forwarder can be started against, and whether one is
    running.
    """
    import forwarder
    return {
        'sinks': forwarder.describe_sinks(),
        'active': _FORWARDER.health() if _FORWARDER else None,
        'delivery': 'at-least-once; deduplicate on the stable event id',
    }


class ForwardRequest(BaseModel):
    sink: str = Field(..., description="syslog | http | file")
    options: Dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(1000, ge=1, le=100000)


@app.post("/api/analytics/forward/run")
def forward_run(req: ForwardRequest) -> Dict[str, Any]:
    """Ships a filtered slice of the graph to a sink, once.

    A one-shot push rather than a daemon, deliberately: continuous forwarding
    belongs in the ingestion path next to the writer, not in the read-only
    analytics service. This is what makes the integration demonstrable and
    usable for a backfill, and it says so rather than implying more.
    """
    global _FORWARDER
    import forwarder

    try:
        sink = forwarder.build_sink(req.sink, **req.options)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc))

    fwd = forwarder.Forwarder(sink)
    _FORWARDER = fwd
    fwd.start()
    rows = q.stream_export(get_driver(), LogFilter(), limit=req.limit)
    submitted = fwd.submit_many(rows)
    fwd.stop()
    return {'submitted': submitted, 'result': fwd.health()}


@app.get("/api/analytics/ocsf/classes")
def ocsf_classes() -> Dict[str, Any]:
    """Which OCSF classes are implemented, and how the live graph distributes
    across them.

    Falsifiable on purpose, in the same spirit as the schema-coverage
    endpoint. An emitter that sends 100% of events to the catch-all class is
    OCSF in name only, and this is where that would show.
    """
    import ocsf
    driver = _driver()
    rows = q.stream_export(driver, LogFilter(), limit=OCSF_COVERAGE_SAMPLE)
    cov = ocsf.coverage(rows)
    return {
        'ocsf_version': ocsf.OCSF_VERSION,
        'implemented': ocsf.describe(),
        'sampled': cov['total'],
        'sample_limit': OCSF_COVERAGE_SAMPLE,
        'distribution': cov['classes'],
        'note': ('Events that do not clearly belong to Authentication or '
                 'Network Activity are mapped to Application Lifecycle (1008) '
                 'rather than forced into a class whose required fields would '
                 'have to be invented.'),
    }


@app.get("/api/analytics/export")
def export(
    format: str = Query('ndjson', pattern='^(ndjson|ecs|ocsf|cef|csv)$'),
    include_raw: bool = Query(True, description="Keep the verbatim original event in the output"),
    limit: Optional[int] = Query(None, ge=1, le=5_000_000),
    start: Optional[str] = Query(None, description="Inclusive first day, YYYY-MM-DD"),
    end: Optional[str] = Query(None, description="Inclusive last day, YYYY-MM-DD"),
    severity: Optional[str] = Query(None),
    source_type: Optional[str] = Query(None),
    hostname: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    """Streams normalized events out to a SIEM or Data Lake.

    The same LogFilter the dashboard uses, so "export exactly what I am
    looking at" is one request with the same query string - the filter cannot
    mean something different here than it does on screen.

    StreamingResponse over a generator: the rows are pulled from Neo4j and
    written to the socket one at a time, so the memory cost is flat whether
    the export is 200 events or 200 million. Collecting them into a list first
    is how this endpoint would take the service down on a real corpus.
    """
    driver = get_driver()
    if driver is None:
        raise HTTPException(503, "Not connected to Neo4j")

    flt = build_filter(start, end, severity, source_type, hostname, search)
    rows = q.stream_export(driver, flt, limit=limit)

    return StreamingResponse(
        exporters.stream(rows, format, include_raw=include_raw),
        media_type=exporters.MEDIA_TYPES[format],
        headers={
            'Content-Disposition': f'attachment; filename="{exporters.filename(format, flt.active)}"',
            # No Content-Length: the size is not known until the walk finishes,
            # and guessing one would truncate the transfer.
            'Cache-Control': 'no-store',
        },
    )


@app.get("/api/analytics/anomalies")
def anomalies_endpoint(
    threshold: float = Query(anomalies.DEFAULT_THRESHOLD, ge=1.0, le=20.0),
    start: Optional[str] = Query(None, description="Inclusive first day, YYYY-MM-DD"),
    end: Optional[str] = Query(None, description="Inclusive last day, YYYY-MM-DD"),
    severity: Optional[str] = Query(None),
    source_type: Optional[str] = Query(None),
    hostname: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Statistical outliers: days and hosts that are unlike themselves.

    Distinct from /insights, which states the current position from rules. This
    one has no thresholds of its own - the baseline comes from the data, so it
    keeps working on an estate whose normal volume is nothing like this one's.
    """
    flt = build_filter(start, end, severity, source_type, hostname, search)
    k = flt.cache_key()
    return cached(f'anomalies:{threshold}{":" + k if k else ""}',
                  lambda: anomalies.detect(get_driver(), flt, threshold))

if __name__ == '__main__':
    import uvicorn
    uvicorn.run("api:app", host=API_CONFIG['host'], port=API_CONFIG['port'], reload=False)
