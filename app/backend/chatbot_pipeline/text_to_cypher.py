"""
text_to_cypher.py - Natural language -> Cypher -> rows.

The schema handed to the model is introspected from the live database
(schema_introspect.py), so the model is never told about a label that doesn't
exist - and never misses one that does because a hard-coded list went stale.

Everything the model produces is treated as untrusted and passes a read-only
guard before it reaches the database: an LLM asked for a "query" will
occasionally emit DETACH DELETE, and this runs against the real log store. The
guard rejects rather than sanitizes - silently rewriting a destructive query
into a different one would give a confidently wrong answer to the user.
"""

import logging
import re
from typing import Any, Dict, List, NamedTuple, Optional

from config import CHATBOT_CONFIG
from llm import LLMClient

logger = logging.getLogger(__name__)


class CypherResult(NamedTuple):
    cypher: Optional[str]
    rows: List[Dict[str, Any]]
    error: Optional[str]


# Any clause that could modify data or schema. Checked as whole words so a
# property legitimately named e.g. "created_at" doesn't trip the CREATE rule.
_WRITE_CLAUSES = re.compile(
    r'\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|FOREACH|LOAD\s+CSV|'
    r'CALL\s*\{[^}]*\b(?:CREATE|MERGE|DELETE|SET)\b)\b',
    re.IGNORECASE,
)
# Procedure namespaces that can write, change config, or reach the filesystem
# and network. dbms.* also exposes credentials and cluster administration.
_DANGEROUS_PROCEDURES = re.compile(
    r'\b(apoc\.(?:create|merge|refactor|trigger|periodic|load|export|import)|'
    r'dbms\.|db\.index\.fulltext\.drop|db\.create)',
    re.IGNORECASE,
)
_CYPHER_FENCE = re.compile(r'```(?:cypher|sql)?\s*(.*?)```', re.DOTALL | re.IGNORECASE)

_SYSTEM_PROMPT = (
    "You translate questions into read-only Neo4j Cypher. "
    "Output only the Cypher query, no prose and no code fences."
)


def is_read_only(cypher: str) -> tuple:
    """(ok, reason). The gate between a generated string and the database."""
    if not cypher or not cypher.strip():
        return False, "empty query"
    # Strip string literals first: a message body containing the word "delete"
    # is data, not a clause, and must not trigger a rejection.
    without_literals = re.sub(r"'[^']*'|\"[^\"]*\"", "''", cypher)
    match = _WRITE_CLAUSES.search(without_literals)
    if match:
        return False, f"contains a write clause ({match.group(1).strip()})"
    match = _DANGEROUS_PROCEDURES.search(without_literals)
    if match:
        return False, f"calls a non-read-only procedure ({match.group(1)})"
    return True, ""


def enforce_limit(cypher: str, limit: int) -> str:
    """Adds a LIMIT when the model didn't. A `MATCH (l:Log) RETURN l` against
    this graph would otherwise stream tens of millions of rows into the prompt.

    There used to be an exception here for "an aggregate with no ORDER BY
    returns one row, so skip the LIMIT" - that's wrong whenever RETURN mixes an
    aggregate with a plain column, e.g. `RETURN h.name, count(l)`: Cypher's
    implicit grouping makes that one row *per host*, not one row overall, and
    "how many X per Y" is exactly the shape this chatbot is asked constantly.
    Appending LIMIT is always safe even for a genuinely single-row aggregate
    (LIMIT on one row is a no-op), so there is no case where skipping it helps.
    """
    if re.search(r'\bLIMIT\s+\d+\s*;?\s*$', cypher, re.IGNORECASE):
        return cypher
    return cypher.rstrip().rstrip(';') + f"\nLIMIT {limit}"


_CLAUSE_START = re.compile(r'\b(MATCH|OPTIONAL|WITH|CALL|UNWIND|RETURN)\b', re.IGNORECASE)


def extract_cypher(raw: str) -> str:
    """Pulls the query out of whatever wrapper the model returned."""
    fenced = _CYPHER_FENCE.search(raw)
    if fenced:
        text = fenced.group(1).strip()
    else:
        # Otherwise drop any leading prose and start at the first real clause.
        # "OPTIONAL" must be in this alternation, not just "MATCH": searching for
        # MATCH alone finds it *inside* "OPTIONAL MATCH" too (at the later
        # position), silently truncating the leading OPTIONAL and turning an
        # optional pattern into a mandatory one - a real semantic change, not a
        # cosmetic one. Since OPTIONAL starts earlier in the string than MATCH
        # does here, adding it to the alternation makes re.search's leftmost-match
        # rule pick it up correctly.
        match = re.search(r'\b(OPTIONAL|MATCH|WITH|CALL|UNWIND|RETURN|PROFILE|EXPLAIN)\b',
                          raw, re.IGNORECASE)
        text = raw[match.start():].strip() if match else raw.strip()

    # Models occasionally repeat the query verbatim, or follow it with prose
    # explaining it - both put a second clause-start keyword after the first
    # RETURN, which Neo4j rejects outright ("RETURN can only be used at the
    # end of the query") rather than just executing the first statement. This
    # system only ever wants one terminal RETURN, so once we're past it, cut
    # at the next MATCH/OPTIONAL/WITH/CALL/UNWIND/RETURN or a blank line -
    # ORDER BY/SKIP/LIMIT aren't in that set, so a RETURN's own trailing
    # clauses are preserved.
    first_return = re.search(r'\bRETURN\b', text, re.IGNORECASE)
    if first_return:
        rest = text[first_return.end():]
        cut = re.search(r'\n\s*\n|```', rest)
        clause = _CLAUSE_START.search(rest)
        if clause and (cut is None or clause.start() < cut.start()):
            cut = clause
        if cut:
            text = text[:first_return.end() + cut.start()].rstrip()
    return text


_PROMPT_TEMPLATE = """Translate the question into ONE read-only Cypher query for this graph.

{schema}

RULES
- Read-only: never CREATE, MERGE, SET, DELETE or DROP.
- Return named columns (RETURN h.name AS host, count(l) AS logs), never whole nodes.
- Log timestamps are ISO-8601 strings; (:Day {{date}}) holds 'YYYY-MM-DD' for date filters.
- severity is one of EMERGENCY, FATAL, ALERT, CRITICAL, ERROR, WARNING, NOTICE, INFO, DEBUG.
  severity_score orders them: 1 is most severe, 7 least.
- Always include a LIMIT unless the query returns a single aggregate.
- To find the Host that produced a log, use (h:Host)-[:EMITTED]->(l:Log) - never
  (l:Log)-[:EMITTED_BY]->(h:Host). EMITTED_BY only ever connects Log to Process
  ((l:Log)-[:EMITTED_BY]->(p:Process)), not to Host.
- A Host node's name property is `name`, not `hostname` - use h.name, never
  h.hostname (that property doesn't exist and silently returns null). Log's own
  hostname property is spelled `hostname` (l.hostname) - the two labels use
  different property names for the same concept; always check {schema} for the
  exact property list of the label you're actually returning from.

EXAMPLES
Q: How many ERROR logs did each host produce?
A: MATCH (h:Host)-[:EMITTED]->(l:Log) WHERE l.severity = 'ERROR'
   RETURN h.name AS host, count(l) AS errors ORDER BY errors DESC LIMIT 25

Q: Which users appear on more than one host?
A: MATCH (u:User)<-[:PERFORMED_BY]-(l:Log)<-[:EMITTED]-(h:Host)
   WITH u, collect(DISTINCT h.name) AS hosts
   WHERE size(hosts) > 1
   RETURN u.name AS user, hosts, size(hosts) AS host_count ORDER BY host_count DESC LIMIT 25

Q: What storage devices were mentioned in errors on 2026-06-15?
A: MATCH (l:Log)-[:ON_DAY]->(:Day {{date: '2026-06-15'}})
   MATCH (l)-[:REFERENCES_DEVICE]->(d:Device)
   WHERE l.severity_score <= 3
   RETURN d.id AS device, count(l) AS mentions ORDER BY mentions DESC LIMIT 25

QUESTION: {question}
CYPHER:"""


class TextToCypher:
    def __init__(self, driver=None, database: str = None, schema=None,
                 llm: LLMClient = None, config: Dict[str, Any] = None):
        self.driver = driver
        self.database = database
        # A GraphSchema (schema_introspect.py). It caches its description, so
        # the introspection queries run once per process, not once per question.
        self.schema = schema
        self.llm = llm or LLMClient()
        cfg = config or CHATBOT_CONFIG
        self.row_limit = cfg.get('text2cypher_row_limit', 100)

    def _schema_text(self) -> str:
        if self.schema is None:
            return "(schema unavailable - the database could not be introspected)"
        return self.schema.description()

    def generate(self, question: str) -> Optional[str]:
        raw = self.llm.complete(
            _PROMPT_TEMPLATE.format(schema=self._schema_text(), question=question),
            system=_SYSTEM_PROMPT,
        )
        if not raw:
            return None
        return enforce_limit(extract_cypher(raw), self.row_limit)

    def run(self, cypher: str) -> CypherResult:
        ok, reason = is_read_only(cypher)
        if not ok:
            return CypherResult(cypher, [], f"Refused to run generated query: {reason}")
        if self.driver is None:
            return CypherResult(cypher, [], "No Neo4j connection available.")

        try:
            with self.driver.session(database=self.database) as session:
                result = session.run(cypher)
                rows = [dict(record) for record in result]
            return CypherResult(cypher, rows, None)
        except Exception as e:
            return CypherResult(cypher, [], f"Cypher execution failed: {e}")

    def answer(self, question: str) -> CypherResult:
        cypher = self.generate(question)
        if not cypher:
            return CypherResult(
                None, [],
                "No LLM available to translate the question into Cypher. "
                "Start Ollama (OLLAMA_HOST) or ask a question the GraphRAG path can handle."
            )
        return self.run(cypher)
