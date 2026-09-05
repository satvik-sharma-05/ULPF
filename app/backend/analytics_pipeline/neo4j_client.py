"""
neo4j_client.py - A single read-only driver connection, shared by every
query in queries.py.

Connecting never raises: if Neo4j is unreachable at startup, `connect()`
returns None and every endpoint degrades to reporting "not connected" instead
of crashing the process - same reliability contract chatbot_pipeline uses.
"""

import logging
from typing import Any, Dict, List, Optional

from config import NEO4J_CONFIG

logger = logging.getLogger(__name__)


def connect():
    try:
        from neo4j import GraphDatabase
    except ImportError:
        logger.error("neo4j driver not installed - run: pip install neo4j")
        return None
    try:
        driver = GraphDatabase.driver(
            NEO4J_CONFIG['uri'],
            auth=(NEO4J_CONFIG['user'], NEO4J_CONFIG['password']),
            max_connection_lifetime=NEO4J_CONFIG['max_connection_lifetime'],
            max_connection_pool_size=NEO4J_CONFIG['max_connection_pool_size'],
        )
        with driver.session(database=NEO4J_CONFIG['database']) as session:
            session.run("RETURN 1").consume()
        return driver
    except Exception as e:
        logger.error(f"Could not connect to Neo4j at {NEO4J_CONFIG['uri']}: {e}")
        return None


def run_read(driver, query: str, **params) -> List[Dict[str, Any]]:
    """Runs a single read query and returns plain dicts. Never raises past
    this point - a bad/slow query degrades one panel of the dashboard, not
    the whole response."""
    if driver is None:
        return []
    try:
        with driver.session(database=NEO4J_CONFIG['database']) as session:
            result = session.run(query, **params)
            return [dict(record) for record in result]
    except Exception as e:
        logger.warning(f"Analytics query failed: {e}\n  query={query.strip()[:200]}")
        return []


def run_read_one(driver, query: str, **params) -> Optional[Dict[str, Any]]:
    rows = run_read(driver, query, **params)
    return rows[0] if rows else None


def run_read_stream(driver, query: str, **params):
    """Yields rows one at a time, holding the session open for the walk.

    run_read() materialises the whole result into a list, which is right for a
    dashboard panel returning 20 rows and catastrophic for an export of ten
    million events. This keeps the server-side cursor open and hands each
    record straight to the caller, so an export costs a constant amount of
    memory no matter how large the result is.

    Same never-raise contract as run_read: a failure mid-walk ends the stream
    with what has already been sent rather than propagating out of a response
    that has begun streaming - by then the status line is long gone and there
    is no way to turn it into an error.
    """
    if driver is None:
        return
    try:
        with driver.session(database=NEO4J_CONFIG['database']) as session:
            for record in session.run(query, **params):
                yield dict(record)
    except Exception as e:
        logger.warning(f"Analytics stream failed: {e} | query={query.strip()[:200]}")
