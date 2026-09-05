"""
entity_resolver.py - Resolves a bare name string (as a question or an LLM
planner naturally supplies it - "VM-125", "esx-host-07", "vpxuser") to the
actual (label, key property, value) it corresponds to in the graph.

Tools like graph_neighbors and path_traversal need to know which node LABEL a
name belongs to before they can build a query against it - Cypher can't match
"whatever label this happens to be" without either scanning every node (far
too expensive here: 100M+ nodes) or knowing the label ahead of time.

Rather than hardcoding "Host uses .name, VM uses .moref, Device uses .id, ..."
- a second copy of the schema this pipeline deliberately doesn't otherwise
keep (see schema_introspect.py's own reasoning for the same choice) - this
discovers the (label, key) pairs live from Neo4j's own uniqueness constraints.
Every label the ingestion pipeline writes has exactly one, by design
(ingestion's graph_schema.py CONSTRAINTS), so `SHOW CONSTRAINTS` *is* the
authoritative "how do I look this label up by name" list, with no separate
copy that could drift.

Resolution never does a full node scan:
  1. exact match on each candidate label's key property - index-backed via
     that label's own uniqueness constraint, so trying ~35 candidates costs
     ~35 index seeks, not a scan of anything.
  2. only if nothing matched: a case-insensitive substring search, but scoped
     to one label at a time (never database-wide) and capped. Safe because
     these candidate labels are all low-cardinality entity types (hundreds to
     low thousands of nodes) - nothing like the 100M+ :Log population.
"""

import logging
from typing import Any, Dict, List, NamedTuple, Optional

logger = logging.getLogger(__name__)

# Never sensible "look this entity up by name" targets: Log/Day aren't named
# entities (Log.id is an opaque hash, not something a question would quote),
# and Severity/SourceType are small enumerated vocabularies, not names.
_EXCLUDED_LABELS = {'Log', 'Day', 'Severity', 'SourceType'}

_CONSTRAINT_QUERY = """
SHOW CONSTRAINTS YIELD labelsOrTypes, properties, type
WHERE type = 'UNIQUENESS' AND size(labelsOrTypes) = 1 AND size(properties) = 1
RETURN labelsOrTypes[0] AS label, properties[0] AS key
"""

# Used only if `SHOW CONSTRAINTS` itself fails (very old Neo4j, or a
# permissions restriction) - a fixed guess covering the labels this project's
# ingestion pipeline is known to create, so resolution still works rather
# than silently returning nothing.
_FALLBACK_CANDIDATES = [
    {'label': 'Host', 'key': 'name'}, {'label': 'VM', 'key': 'moref'},
    {'label': 'User', 'key': 'name'}, {'label': 'Device', 'key': 'id'},
    {'label': 'Datastore', 'key': 'name'}, {'label': 'ManagedObject', 'key': 'moref'},
    {'label': 'Process', 'key': 'name'}, {'label': 'Component', 'key': 'name'},
    {'label': 'Pod', 'key': 'name'}, {'label': 'IPAddress', 'key': 'address'},
    {'label': 'Domain', 'key': 'name'}, {'label': 'K8sService', 'key': 'name'},
    {'label': 'Namespace', 'key': 'name'}, {'label': 'Task', 'key': 'id'},
    {'label': 'Operation', 'key': 'id'}, {'label': 'SecurityID', 'key': 'sid'},
]


class ResolvedEntity(NamedTuple):
    label: str
    key: str
    value: str   # the actual stored value - may differ in case/exact form from the input


class EntityResolver:
    def __init__(self, driver, database: str):
        self.driver = driver
        self.database = database
        self._candidates: Optional[List[Dict[str, str]]] = None

    def _candidate_labels(self) -> List[Dict[str, str]]:
        """(label, key) pairs to try, cached for the process lifetime - same
        reasoning as GraphSchema.description()'s cache in schema_introspect.py."""
        if self._candidates is not None:
            return self._candidates
        candidates: List[Dict[str, str]] = []
        if self.driver is not None:
            try:
                with self.driver.session(database=self.database) as session:
                    for record in session.run(_CONSTRAINT_QUERY):
                        label, key = record['label'], record['key']
                        if label not in _EXCLUDED_LABELS:
                            candidates.append({'label': label, 'key': key})
            except Exception as e:
                logger.warning(f"Could not read constraints for entity resolution ({e}); "
                               f"falling back to a fixed label list.")
        self._candidates = candidates or list(_FALLBACK_CANDIDATES)
        return self._candidates

    def resolve(self, value: str) -> Optional[ResolvedEntity]:
        """Finds which node this name actually refers to, or None.

        Note on `VM.moref`: it's a vSphere managed-object reference pulled
        from log text (e.g. `vm-1203`), not a human-assigned name - a question
        about "VM-125" will only resolve if that literal string appears
        somewhere in the corpus. This is a real, honest limitation of what the
        graph actually stores, not a resolver bug; the substring fallback
        below is what gives partial/differently-cased references a chance.
        """
        value = (value or '').strip()
        if not value:
            return None
        if self.driver is None:
            return None

        candidates = self._candidate_labels()

        with self.driver.session(database=self.database) as session:
            for c in candidates:
                label, key = c['label'], c['key']
                try:
                    record = session.run(
                        f"MATCH (n:`{label}`) WHERE n.`{key}` = $value "
                        f"RETURN n.`{key}` AS value LIMIT 1",
                        value=value,
                    ).single()
                except Exception as e:
                    logger.debug(f"Exact-match lookup on :{label}.{key} failed: {e}")
                    continue
                if record:
                    return ResolvedEntity(label=label, key=key, value=record['value'])

            for c in candidates:
                label, key = c['label'], c['key']
                try:
                    record = session.run(
                        f"MATCH (n:`{label}`) WHERE toLower(toString(n.`{key}`)) CONTAINS toLower($value) "
                        f"RETURN n.`{key}` AS value LIMIT 1",
                        value=value,
                    ).single()
                except Exception as e:
                    logger.debug(f"Substring lookup on :{label}.{key} failed: {e}")
                    continue
                if record:
                    return ResolvedEntity(label=label, key=key, value=record['value'])
        return None
