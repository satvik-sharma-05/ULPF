"""
core/neo4j_writer.py - Neo4j graph writer (the pipeline's only datastore).

Writes three layers per batch, in one transaction:

  1. structural  - Host/Process/Component/SourceType/Severity/Day, plus the
                   (:Log) node itself with its embedding vector.
  2. entity      - (:Log)-[:REL]->(:TypedNode) for every extracted entity.
  3. derived     - entity-to-entity and entity-to-host edges, so the graph can
                   be traversed without routing every path through a :Log.

core/graph_schema.py owns the type -> (label, relationship) mapping, so this
module never hard-codes a label list and can't drift from the schema the
chatbot queries.

Both pipelines share this writer: the realtime consumer calls write() per Kafka
message, the batch loader calls write_batch() with hundreds at a time. Batching
is the difference between ingesting a 33GB corpus in hours rather than days -
each log otherwise costs a full network round-trip.

Embeddings live on (:Log).embedding behind a native VECTOR INDEX. There is no
second store to keep in sync.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import NEO4J_CONFIG, PIPELINE_CONFIG
from .graph_schema import CONSTRAINTS, DERIVED_RELATIONSHIPS, INDEXES, entity_spec

logger = logging.getLogger(__name__)

try:
    from neo4j import GraphDatabase
    HAS_NEO4J_DRIVER = True
except ImportError:
    HAS_NEO4J_DRIVER = False
    logger.warning("neo4j library not found. Neo4j Writer will operate in mock mode.")


# ---------------------------------------------------------------------------
# Layer 1: the structural write. Every log gets exactly these nodes and edges.
# ---------------------------------------------------------------------------
_CORE_BATCH_CYPHER = """
UNWIND $rows AS row

MERGE (l:Log {id: row.id})
  ON CREATE SET l += row.props, l.created_at = datetime()
  // Without ON MATCH SET, re-ingesting the same corpus a second time (e.g. to
  // add --embed after an initial structure-only load, or to pick up a parser
  // fix) would silently do nothing to already-existing Log nodes - the whole
  // point of a second pass. Re-applying identical input is a no-op cost, not
  // a correctness risk: props are always derived the same way from the same
  // raw log.
  ON MATCH SET l += row.props

WITH l, row
MERGE (h:Host {name: row.hostname})
  ON CREATE SET h.first_seen = row.timestamp
  ON MATCH SET  h.last_seen  = row.timestamp
MERGE (h)-[:EMITTED]->(l)

WITH l, row, h
MERGE (st:SourceType {name: row.source_type})
MERGE (l)-[:FROM_SOURCE]->(st)
MERGE (h)-[:IS_TYPE]->(st)

WITH l, row, h
MERGE (p:Process {name: row.process})
  ON CREATE SET p.first_seen = row.timestamp
MERGE (l)-[:EMITTED_BY]->(p)
MERGE (p)-[:RUNS_ON]->(h)

WITH l, row, p
MERGE (c:Component {name: row.component})
MERGE (l)-[:FROM_COMPONENT]->(c)
MERGE (c)-[:PART_OF]->(p)

WITH l, row
MERGE (s:Severity {level: row.severity})
  ON CREATE SET s.score = row.severity_score
MERGE (l)-[:HAS_SEVERITY]->(s)

// A log with an unparseable timestamp has no day to hang off, so the Day edge
// is conditional rather than creating a bogus (:Day {date: null}) node.
FOREACH (_ IN CASE WHEN row.day IS NULL THEN [] ELSE [1] END |
  MERGE (d:Day {date: row.day})
  MERGE (l)-[:ON_DAY]->(d)
)
"""

# Layer 2 and 3 statements are generated per type and cached: Cypher can't take
# a label or relationship type as a parameter, but the set of types is fixed and
# small, so building each statement once is free.
_ENTITY_CYPHER_CACHE: Dict[str, str] = {}
_DERIVED_CYPHER_CACHE: Dict[Tuple[str, str, str], str] = {}


def _entity_cypher(entity_type: str) -> str:
    cached = _ENTITY_CYPHER_CACHE.get(entity_type)
    if cached:
        return cached
    spec = entity_spec(entity_type)
    statement = f"""
UNWIND $rows AS row
MATCH (l:Log {{id: row.log_id}})
MERGE (e:{spec.label} {{{spec.key}: row.value}})
MERGE (l)-[:{spec.relationship}]->(e)
"""
    _ENTITY_CYPHER_CACHE[entity_type] = statement
    return statement


def _derived_cypher(from_type: str, relationship: str, to_type: str) -> str:
    key = (from_type, relationship, to_type)
    cached = _DERIVED_CYPHER_CACHE.get(key)
    if cached:
        return cached

    from_spec = entity_spec(from_type)
    if to_type == '@host':
        to_label, to_key = 'Host', 'name'
    else:
        to_spec = entity_spec(to_type)
        to_label, to_key = to_spec.label, to_spec.key

    # MATCH, not MERGE, on both endpoints: the entity write in layer 2 has
    # already created them inside this same transaction. MERGE here would risk
    # silently creating a node carrying only its key property if that ordering
    # ever changed.
    statement = f"""
UNWIND $rows AS row
MATCH (a:{from_spec.label} {{{from_spec.key}: row.from_value}})
MATCH (b:{to_label} {{{to_key}: row.to_value}})
MERGE (a)-[:{relationship}]->(b)
"""
    _DERIVED_CYPHER_CACHE[key] = statement
    return statement


class Neo4jWriter:
    def __init__(self):
        self.uri = NEO4J_CONFIG['uri']
        self.user = NEO4J_CONFIG['user']
        self.password = NEO4J_CONFIG['password']
        self.database = NEO4J_CONFIG['database']
        self.batch_size = NEO4J_CONFIG.get('batch_size', 500)
        self.driver = None
        self.is_connected = False
        self.enabled = PIPELINE_CONFIG['enable_neo4j']

    # -- lifecycle ----------------------------------------------------------
    def connect(self) -> bool:
        if not self.enabled:
            logger.info("Neo4j writer is disabled in PIPELINE_CONFIG.")
            return False

        if not HAS_NEO4J_DRIVER:
            self.is_connected = True
            logger.info("neo4j Python package missing. Operating in mock writer mode.")
            return True

        try:
            self.driver = GraphDatabase.driver(
                self.uri,
                auth=(self.user, self.password),
                max_connection_lifetime=NEO4J_CONFIG.get('max_connection_lifetime', 3600),
                max_connection_pool_size=NEO4J_CONFIG.get('max_connection_pool_size', 50),
            )
            with self.driver.session(database=self.database) as session:
                session.run("RETURN 1").consume()

            self.is_connected = True
            logger.info(f"Connected to Neo4j at {self.uri} (database: {self.database})")
            self.ensure_schema()
            return True
        except Exception as e:
            logger.error(f"Neo4j connection failed: {e}")
            self.is_connected = False
            return False

    def ensure_schema(self):
        """Creates every constraint/index the schema declares. Constraints are
        what make ingestion idempotent *and* fast - without a uniqueness
        constraint each MERGE degrades into a full label scan."""
        if not self.driver or not self.is_connected:
            return

        with self.driver.session(database=self.database) as session:
            for statement in CONSTRAINTS + INDEXES:
                try:
                    session.run(statement).consume()
                except Exception as e:
                    logger.debug(f"Schema statement skipped ({statement[:60]}...): {e}")

            try:
                session.run(
                    """
                    CREATE VECTOR INDEX log_embedding_index IF NOT EXISTS
                    FOR (l:Log) ON (l.embedding)
                    OPTIONS {indexConfig: {
                        `vector.dimensions`: $dim,
                        `vector.similarity_function`: 'cosine'
                    }}
                    """,
                    dim=NEO4J_CONFIG.get('embedding_dim', 1024),
                ).consume()
                logger.info("Neo4j vector index on Log.embedding is ready.")
            except Exception as e:
                logger.warning(f"Could not create vector index (needs Neo4j 5.11+): {e}")

    def close(self):
        if self.driver:
            try:
                self.driver.close()
                logger.info("Neo4j driver connection closed.")
            except Exception as e:
                logger.error(f"Error closing Neo4j driver: {e}")

    # -- payload shaping ----------------------------------------------------
    def _prepare_log(self, log: Dict[str, Any]) -> Dict[str, Any]:
        """Flattens one parsed log into the row shape _CORE_BATCH_CYPHER wants."""
        timestamp = str(log.get('timestamp') or datetime.now(timezone.utc).isoformat())
        hostname = str(log.get('hostname') or 'unknown-host')
        process = str(log.get('process') or 'unknown-process')
        attributes = log.get('attributes') or {}

        props: Dict[str, Any] = {
            'timestamp': timestamp,
            # 'event' when the source line carried a timestamp, 'ingest' when
            # it did not and this is the read time. Correlating on an ingest
            # timestamp as though it were an event time is a real analytical
            # error, so the distinction is stored rather than inferred.
            'timestamp_source': str(log.get('timestamp_source') or 'event'),
            # True when the event time is implausibly far from ingest time -
            # appliance clock skew. Recorded so analytics can exclude it and
            # an operator can see it; the timestamp itself is left alone.
            'timestamp_anomalous': bool(log.get('timestamp_anomalous')),
            'day': log.get('day'),
            'hostname': hostname,
            'source_type': str(log.get('source_type') or 'Unknown'),
            'process': process,
            'component': str(log.get('component') or process),
            'subcomponent': str(log.get('subcomponent') or ''),
            'severity': str(log.get('severity') or 'INFO'),
            'severity_score': int(log.get('severity_score') or 6),
            'message': str(log.get('message') or ''),
            'normalized_message': str(log.get('normalized_message') or ''),
            # Neo4j properties can't hold nested maps, so the free-form bag is
            # stored as JSON text - still readable via apoc.convert.fromJsonMap.
            'attributes_json': json.dumps(attributes, default=str),
            'confidence': float(log.get('confidence') or 0.0),
            'matched_format': str(log.get('matched_format') or 'generic_fallback'),
            # Lineage back-pointer to the original event. `raw_message` keeps
            # the bytes; these two say WHERE those bytes came from - which file
            # and which record within it - so a normalized (:Log) can always be
            # walked back to its source line in the untouched corpus. Together
            # with raw_message and matched_format this is the traceability
            # contract: nothing in the graph is an orphan you cannot audit.
            'source_file': str(log.get('source_file') or ''),
            'source_record': int(log.get('source_record') or 0),
        }

        # The verbatim original line, always stored: `message` is a cleaned,
        # normalized derivative (BOM-stripped, octal-unescaped, whitespace
        # trimmed) and a forensic question can need exactly what the source
        # emitted. Truncation is bounded by raw_message_max_chars rather than a
        # hard-coded 4000 - a reassembled multi-line record (Windows Security
        # Event body, Java stack trace) routinely exceeded that and was being
        # silently clipped. `raw_truncated` records when that happened, so a
        # clipped value can never be mistaken for the whole original.
        raw_message = str(log.get('raw_message') or '')
        raw_cap = NEO4J_CONFIG.get('raw_message_max_chars', 32768)
        if raw_cap and len(raw_message) > raw_cap:
            props['raw_message'] = raw_message[:raw_cap]
            props['raw_truncated'] = True
            props['raw_length'] = len(raw_message)
        else:
            props['raw_message'] = raw_message

        # HTTP method/status become first-class Log properties rather than
        # nodes: only ~10 distinct statuses exist, so a (:HttpStatus) node would
        # become a supernode with tens of millions of edges and answer nothing a
        # property can't.
        for key in ('http_method', 'http_status'):
            value = attributes.get(key)
            if value not in (None, ''):
                props[key] = str(value)

        if log.get('pid') is not None:
            try:
                props['pid'] = int(log['pid'])
            except (TypeError, ValueError):
                pass
        if log.get('embedding'):
            props['embedding'] = [float(x) for x in log['embedding']]

        return {
            'id': str(log.get('id') or ''),
            'hostname': hostname,
            'process': process,
            'component': props['component'],
            'source_type': props['source_type'],
            'severity': props['severity'],
            'severity_score': props['severity_score'],
            'timestamp': timestamp,
            'day': log.get('day'),
            'props': props,
        }

    @staticmethod
    def _entity_rows(logs: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, str]]]:
        """Groups every entity across the batch by type, so each type is one query."""
        grouped: Dict[str, List[Dict[str, str]]] = {}
        for log in logs:
            log_id = str(log.get('id') or '')
            if not log_id:
                continue
            for entity in log.get('entities') or []:
                if not isinstance(entity, dict):
                    continue
                entity_type = entity.get('type')
                value = entity.get('value')
                if not entity_type or value in (None, ''):
                    continue
                grouped.setdefault(entity_type, []).append(
                    {'log_id': log_id, 'value': str(value)}
                )
        return grouped

    @staticmethod
    def _derived_rows(
        logs: Iterable[Dict[str, Any]]
    ) -> Dict[Tuple[str, str, str], List[Dict[str, str]]]:
        """Builds entity-to-entity and entity-to-host edges for the batch.

        Only the *first* entity of each type in a log participates. A log with 8
        users and 8 devices would otherwise emit 64 edges of one type, none of
        which the log actually supports - while the common case (one user, one
        session, one device per line) is exactly right.
        """
        grouped: Dict[Tuple[str, str, str], List[Dict[str, str]]] = {}
        for log in logs:
            entities = log.get('entities') or []
            if not entities:
                continue

            first_of_type: Dict[str, str] = {}
            for entity in entities:
                if not isinstance(entity, dict):
                    continue
                entity_type, value = entity.get('type'), entity.get('value')
                if entity_type and value and entity_type not in first_of_type:
                    first_of_type[entity_type] = str(value)

            hostname = str(log.get('hostname') or 'unknown-host')
            for from_type, relationship, to_type in DERIVED_RELATIONSHIPS:
                from_value = first_of_type.get(from_type)
                if not from_value:
                    continue
                to_value = hostname if to_type == '@host' else first_of_type.get(to_type)
                if not to_value:
                    continue
                # A self-edge carries no information; the Aria logs, where the
                # host *is* the pod, would otherwise generate a pile of them.
                if from_value == to_value:
                    continue
                grouped.setdefault((from_type, relationship, to_type), []).append(
                    {'from_value': from_value, 'to_value': to_value}
                )
        return grouped

    # -- writes -------------------------------------------------------------
    def write_batch(self, logs: List[Dict[str, Any]]) -> int:
        """Writes N parsed logs in one transaction. Returns how many landed."""
        if not logs:
            return 0
        if not self.enabled:
            logger.debug(f"[Mock Neo4j Writer] Skipped batch of {len(logs)} logs")
            return len(logs)
        if not self.is_connected and not self.connect():
            logger.warning(f"[Mock Neo4j Fallback] Neo4j unreachable; dropped {len(logs)} logs")
            return 0
        if not HAS_NEO4J_DRIVER or self.driver is None:
            logger.debug(f"[Mock Neo4j Writer] Simulated batch of {len(logs)} logs")
            return len(logs)

        rows = [self._prepare_log(log) for log in logs if log.get('id')]
        if not rows:
            return 0
        entity_rows = self._entity_rows(logs)
        derived_rows = self._derived_rows(logs)

        try:
            with self.driver.session(database=self.database) as session:
                # One transaction for the whole batch, in dependency order: the
                # entity statements MATCH Log nodes the core statement just
                # created, and the derived statements MATCH entity nodes the
                # entity statements just created. Splitting these across
                # transactions would let a later stage MATCH nothing.
                def _unit_of_work(tx):
                    tx.run(_CORE_BATCH_CYPHER, rows=rows).consume()
                    for entity_type, items in entity_rows.items():
                        tx.run(_entity_cypher(entity_type), rows=items).consume()
                    for (from_type, rel, to_type), items in derived_rows.items():
                        tx.run(_derived_cypher(from_type, rel, to_type), rows=items).consume()

                session.execute_write(_unit_of_work)
            return len(rows)
        except Exception as e:
            logger.error(f"Neo4j batch write failed ({len(rows)} logs): {e}")
            return 0

    def write(self, log: Dict[str, Any]) -> Optional[str]:
        """Single-record write, for the realtime consumer role."""
        written = self.write_batch([log])
        return log.get('id') if written else None

    # -- read-back ----------------------------------------------------------
    def node_counts(self) -> List[Tuple[str, int]]:
        """Live counts straight from the database - used by the loaders to prove
        the write happened rather than trusting their own tally."""
        if not self.driver or not self.is_connected:
            return []
        results: List[Tuple[str, int]] = []
        with self.driver.session(database=self.database) as session:
            labels = [r['label'] for r in session.run("CALL db.labels() YIELD label RETURN label")]
            for label in labels:
                record = session.run(f"MATCH (n:`{label}`) RETURN count(n) AS c").single()
                if record:
                    results.append((label, record['c']))
        return sorted(results, key=lambda item: -item[1])

    def relationship_counts(self) -> List[Tuple[str, int]]:
        if not self.driver or not self.is_connected:
            return []
        with self.driver.session(database=self.database) as session:
            rows = session.run(
                "MATCH ()-[r]->() RETURN type(r) AS rel, count(r) AS count ORDER BY count DESC"
            )
            return [(r['rel'], r['count']) for r in rows]
