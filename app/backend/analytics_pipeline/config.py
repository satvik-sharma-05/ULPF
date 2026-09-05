"""
config.py - Analytics pipeline configuration.

Standalone by design, same rule as chatbot_pipeline: the only dependency is a
Neo4j instance the ingestion pipeline has already populated. This module does
not import from ../ingestion_pipeline or ../chatbot_pipeline, so the analytics
API can be deployed, scaled and restarted on its own - it only ever reads the
graph, never writes to it.
"""

import os

# Load this service's .env before ANY os.getenv below reads a default.
#
# Without this, .env only worked for the FastAPI entry point, which calls
# load_dotenv() itself. Every other way in - a test, a script, `python -c`,
# the ledger CLI - silently ignored the file and used the hard-coded
# fallbacks, so a correctly configured service still tried to reach
# bolt://neo4j.internal.example and failed with a network timeout that named nothing.
# The ingestion pipeline fixed this in core/config.py; these two did not have
# the same fix.
#
# python-dotenv is optional, and an explicit environment variable still wins
# over the file (override=False), so docker-compose's `environment:` block and
# `NEO4J_URI=... python ...` both behave exactly as before.
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
try:
    from dotenv import load_dotenv
    _ENV_LOADED = load_dotenv(_ENV_PATH, override=False)
except ImportError:
    _ENV_LOADED = False

if not _ENV_LOADED and not os.getenv('NEO4J_URI'):
    # Say so rather than silently reaching for the built-in default. That
    # default is a real enterprise address: on a machine without a .env the
    # operator would otherwise see a timeout to an IP they may not recognise,
    # with nothing pointing at the actual cause.
    import warnings
    warnings.warn(
        'No .env found at %s and NEO4J_URI is unset - falling back to the '
        'built-in defaults in this file. Copy .env.example to .env to '
        'configure this service.' % _ENV_PATH,
        RuntimeWarning, stacklevel=2)



def _bool_env(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ('true', '1', 'yes')


# ============================================================================
# NEO4J - the graph the ingestion pipeline built. Read-only access is enough;
# every query in queries.py is a MATCH/RETURN, never a write.
# ============================================================================
NEO4J_CONFIG = {
    'uri': os.getenv('NEO4J_URI', 'bolt://localhost:7687'),
    'user': os.getenv('NEO4J_USER', 'neo4j'),
    'password': os.getenv('NEO4J_PASSWORD', 'password'),
    'database': os.getenv('NEO4J_DATABASE', 'neo4j'),
    'max_connection_lifetime': 3600,
    'max_connection_pool_size': 10,
}

# ============================================================================
# ANALYTICS - tunables for aggregation queries. Kept small and explicit so a
# dashboard request against a multi-hundred-million-node graph has a known
# worst case instead of an unbounded full scan.
# ============================================================================
ANALYTICS_CONFIG = {
    # A host/source/process needs at least this many logs before its error
    # rate is reported as an insight - otherwise one host with 2 logs and 1
    # error "wins" every ranking at a meaningless 50% rate.
    'min_volume_for_rate': int(os.getenv('ANALYTICS_MIN_VOLUME', '20')),
    'top_n_default': int(os.getenv('ANALYTICS_TOP_N', '10')),
    'timeseries_max_days': int(os.getenv('ANALYTICS_TIMESERIES_MAX_DAYS', '90')),
    'insights_trend_window_days': int(os.getenv('ANALYTICS_TREND_WINDOW_DAYS', '7')),
    # How long an aggregation response is cached in-process before being
    # recomputed - the graph in this project is a batch-ingested corpus, not a
    # live-updating stream, so a short cache turns a dashboard with several
    # panels + auto-refresh into a handful of Neo4j round trips instead of one
    # per panel per poll.
    'cache_ttl_seconds': int(os.getenv('ANALYTICS_CACHE_TTL_SECONDS', '30')),
}

# Severity bands, matching ingestion_pipeline/core/parsers/base.py's
# SEVERITY_SCORE (1=EMERGENCY/FATAL/ALERT, 2=CRITICAL, 3=ERROR, 4=WARNING,
# 5=NOTICE, 6=INFO, 7=DEBUG). Duplicated here deliberately as small integer
# literals, not entity/schema structure - low risk of drifting, and importing
# across the pipeline boundary would break the "standalone" contract.
ALERT_MAX_SCORE = 2   # EMERGENCY / FATAL / ALERT / CRITICAL
ERROR_MAX_SCORE = 3   # + ERROR
WARNING_MAX_SCORE = 4  # + WARNING - the widest band still considered actionable

# ============================================================================
# ALERT LEVELS - graded severity bands, so "alerts" is not one undifferentiated
# bucket. An operator triages by level: P1 pages someone, P3 goes on a list.
#
# Ordered most severe first. `max_score` is inclusive and bands are evaluated
# in order, so each level owns exactly the scores not claimed by a higher one.
# Anything above WARNING (NOTICE/INFO/DEBUG) is not an alert at all - it is
# normal operational chatter, and counting it would make the number useless.
# ============================================================================
ALERT_LEVELS = [
    {
        'id': 'critical',
        'label': 'Critical',
        'priority': 'P1',
        'min_score': 1,
        'max_score': ALERT_MAX_SCORE,
        'severities': ['EMERGENCY', 'FATAL', 'ALERT', 'CRITICAL'],
        'description': 'Service-affecting or data-threatening. Investigate immediately.',
    },
    {
        'id': 'error',
        'label': 'Error',
        'priority': 'P2',
        'min_score': ERROR_MAX_SCORE,
        'max_score': ERROR_MAX_SCORE,
        'severities': ['ERROR'],
        'description': 'An operation failed. Needs attention, not necessarily this minute.',
    },
    {
        'id': 'warning',
        'label': 'Warning',
        'priority': 'P3',
        'min_score': WARNING_MAX_SCORE,
        'max_score': WARNING_MAX_SCORE,
        'severities': ['WARNING'],
        'description': 'Degraded or unexpected, but nothing failed outright. Worth a look.',
    },
]

# Everything at or below this score counts as "an alert" in the headline stat.
ALERT_BAND_MAX_SCORE = WARNING_MAX_SCORE


def level_for_score(score):
    """Which alert level a severity_score falls into, or None if it isn't an
    alert at all."""
    if score is None:
        return None
    for level in ALERT_LEVELS:
        if level['min_score'] <= score <= level['max_score']:
            return level['id']
    return None

# ============================================================================
# API - the FastAPI backend the analytics dashboard talks to.
# ============================================================================
API_CONFIG = {
    'host': os.getenv('ANALYTICS_API_HOST', '0.0.0.0'),
    'port': int(os.getenv('ANALYTICS_API_PORT', '8010')),
    'cors_origins': [o.strip() for o in os.getenv('ANALYTICS_API_CORS_ORIGINS', '*').split(',') if o.strip()],
}
