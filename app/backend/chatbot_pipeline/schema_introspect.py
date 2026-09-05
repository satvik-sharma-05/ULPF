"""
schema_introspect.py - Discover the graph schema from the live database.

The text-to-Cypher prompt has to describe the graph accurately: a model told
about a label that doesn't exist writes queries returning nothing, and one not
told about a label that does exist can't use it.

Rather than copying the ingestion pipeline's schema declaration into this
pipeline - two files that would drift apart the first time either changed -
this asks Neo4j what is actually there. That has a second advantage: it
describes the graph *as loaded*, so if an ingest run only covered part of the
corpus, the prompt reflects that instead of promising labels with no data.

Uses only built-in procedures (db.labels, db.relationshipTypes,
db.schema.visualization), so no APOC dependency.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Property keys not worth putting in the prompt: embedding is a 1024-float
# vector, and the raw/created fields aren't useful filter targets.
_NOISY_PROPERTIES = {'embedding', 'raw_message', 'created_at', 'attributes_json'}


class GraphSchema:
    """Introspected description of the graph, cached for the process lifetime."""

    def __init__(self, driver, database: str, sample_limit: int = 3):
        self.driver = driver
        self.database = database
        self.sample_limit = sample_limit
        self._description: Optional[str] = None

    # -- raw introspection --------------------------------------------------
    def _run(self, query: str, **params) -> List[Dict[str, Any]]:
        if self.driver is None:
            return []
        try:
            with self.driver.session(database=self.database) as session:
                return [dict(record) for record in session.run(query, **params)]
        except Exception as e:
            logger.debug(f"Schema introspection query failed: {e}")
            return []

    def labels(self) -> List[Tuple[str, int]]:
        rows = self._run("CALL db.labels() YIELD label RETURN label ORDER BY label")
        out: List[Tuple[str, int]] = []
        for row in rows:
            label = row['label']
            count_rows = self._run(f"MATCH (n:`{label}`) RETURN count(n) AS c")
            out.append((label, count_rows[0]['c'] if count_rows else 0))
        return sorted(out, key=lambda item: -item[1])

    def properties_for(self, label: str) -> List[str]:
        """Property keys actually present, sampled from a few nodes.

        Sampled rather than exhaustive: scanning every :Log node to enumerate
        keys would read the entire graph, and nodes of one label are uniformly
        shaped here because a single writer creates them all.
        """
        rows = self._run(
            f"MATCH (n:`{label}`) WITH n LIMIT $limit RETURN keys(n) AS keys",
            limit=self.sample_limit,
        )
        keys = {key for row in rows for key in row.get('keys', [])}
        return sorted(keys - _NOISY_PROPERTIES)

    def relationships(self) -> List[Tuple[str, str, str]]:
        """(startLabel, RELATIONSHIP, endLabel) triples actually present."""
        rows = self._run(
            """
            CALL db.schema.visualization() YIELD relationships
            UNWIND relationships AS rel
            RETURN DISTINCT
                   labels(startNode(rel))[0] AS start,
                   type(rel)                 AS type,
                   labels(endNode(rel))[0]   AS end
            """
        )
        triples = [(r['start'], r['type'], r['end'])
                   for r in rows if r.get('start') and r.get('end')]
        if triples:
            return sorted(set(triples))

        # db.schema.visualization is unavailable on some managed instances;
        # fall back to the relationship type list without endpoint labels.
        type_rows = self._run(
            "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType"
        )
        return [('?', r['relationshipType'], '?') for r in type_rows]

    # -- prompt text --------------------------------------------------------
    def description(self, refresh: bool = False) -> str:
        if self._description is not None and not refresh:
            return self._description

        label_rows = self.labels()
        if not label_rows:
            self._description = _STATIC_FALLBACK
            return self._description

        lines = ["NODE LABELS (with live node counts and properties):"]
        for label, count in label_rows:
            props = self.properties_for(label)
            lines.append(f"  (:{label})  [{count:,} nodes]  properties: {', '.join(props) or '-'}")

        lines.append("")
        lines.append("RELATIONSHIPS:")
        for start, rel_type, end in self.relationships():
            lines.append(f"  (:{start})-[:{rel_type}]->(:{end})")

        lines += [
            "",
            "NOTES",
            "- Log.timestamp is an ISO-8601 string; (:Day {date}) holds 'YYYY-MM-DD'",
            "  and is the cheapest way to filter by date.",
            "- Log.severity is EMERGENCY|FATAL|ALERT|CRITICAL|ERROR|WARNING|NOTICE|INFO|DEBUG.",
            "  Log.severity_score orders them: 1 is most severe, 7 least.",
            "- Log.attributes_json holds format-specific fields as a JSON string.",
        ]
        self._description = "\n".join(lines)
        return self._description


# Used when the database is unreachable or empty, so the prompt is never blank.
_STATIC_FALLBACK = """NODE LABELS:
  (:Log {id, timestamp, day, hostname, source_type, process, pid, component,
         severity, severity_score, message, confidence, matched_format})
  (:Host {name})  (:Process {name})  (:Component {name})
  (:SourceType {name})  (:Severity {level, score})  (:Day {date})
  (:User {name})  (:IPAddress {address})  (:Device {id})  (:Task {id})
  (:Operation {id})  (:Session {id})  (:Trace {id})  (:File {path})

RELATIONSHIPS:
  (:Host)-[:EMITTED]->(:Log)          (:Log)-[:HAS_SEVERITY]->(:Severity)
  (:Log)-[:ON_DAY]->(:Day)            (:Log)-[:EMITTED_BY]->(:Process)
  (:Log)-[:FROM_SOURCE]->(:SourceType)
  (:Log)-[:PERFORMED_BY]->(:User)     (:Log)-[:INVOLVES_IP]->(:IPAddress)
  (:Log)-[:REFERENCES_DEVICE]->(:Device)
  (:Log)-[:PART_OF_OPERATION]->(:Operation)
"""
