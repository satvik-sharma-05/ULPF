"""
graph_rag.py - Semantic retrieval over the log graph.

Plain vector RAG would return N log lines that look like the question and stop
there. The reason this is *Graph*RAG: after the vector index finds the seed
logs, it walks one hop out to the entities each seed touches (host, process,
user, device, task, operation...) and pulls in the other logs sharing those
entities. That neighbourhood is usually where the actual answer is - the log
that *explains* a failure rarely contains the same words as the question, but
it almost always shares an opID, a device or a host with the log that does.

Retrieval degrades in two steps so the chatbot stays useful as capability drops:
  1. native vector index over (:Log).embedding      - needs --embed at ingest
  2. full-text index over (:Log).message            - always available
  3. plain CONTAINS substring match                 - last resort
"""

import logging
import re
from typing import Any, Dict, List, NamedTuple, Optional

from config import CHATBOT_CONFIG, EMBEDDING_CONFIG
from llm import LLMClient

logger = logging.getLogger(__name__)


class RetrievedContext(NamedTuple):
    logs: List[Dict[str, Any]]
    neighbours: List[Dict[str, Any]]
    method: str


# Index names come from config so they can track whatever the ingestion
# pipeline created. Cypher can't parameterize a procedure's index-name
# argument, so they are interpolated - the values are operator-supplied
# configuration, never user input from a question.
_VECTOR_QUERY = """
CALL db.index.vector.queryNodes('{index}', $k, $embedding)
YIELD node AS l, score
RETURN l.id AS id, l.timestamp AS timestamp, l.hostname AS hostname,
       l.source_type AS source_type, l.process AS process, l.severity AS severity,
       l.message AS message, score
ORDER BY score DESC
"""

_FULLTEXT_QUERY = """
CALL db.index.fulltext.queryNodes('{index}', $query, {{limit: $k}})
YIELD node AS l, score
RETURN l.id AS id, l.timestamp AS timestamp, l.hostname AS hostname,
       l.source_type AS source_type, l.process AS process, l.severity AS severity,
       l.message AS message, score
ORDER BY score DESC
"""

_SUBSTRING_QUERY = """
MATCH (l:Log)
WHERE toLower(l.message) CONTAINS toLower($term)
RETURN l.id AS id, l.timestamp AS timestamp, l.hostname AS hostname,
       l.source_type AS source_type, l.process AS process, l.severity AS severity,
       l.message AS message, 0.0 AS score
ORDER BY l.severity_score ASC
LIMIT $k
"""

# One hop out from the seeds, through any entity edge, back to other logs.
# Ordered by severity so the neighbourhood surfaces problems, not INFO noise.
_NEIGHBOUR_QUERY = """
MATCH (seed:Log) WHERE seed.id IN $ids
MATCH (seed)-[r]->(e)
WHERE NOT e:Severity AND NOT e:Day AND NOT e:SourceType
MATCH (e)<-[]-(related:Log)
WHERE NOT related.id IN $ids
RETURN DISTINCT related.id AS id, related.timestamp AS timestamp,
       related.hostname AS hostname, related.process AS process,
       related.severity AS severity, related.message AS message,
       related.severity_score AS severity_score,
       labels(e)[0] AS via_label, type(r) AS via_relationship
// severity_score, not severity: ordering by the label would sort
// alphabetically ("CRITICAL" < "INFO" < "WARNING") and bury the worst logs.
ORDER BY severity_score ASC
LIMIT $limit
"""

_ANSWER_SYSTEM = (
    "You are an experienced infrastructure engineer analysing logs.\n\n"
    "There are two different kinds of question, and they have different rules:\n\n"
    "1. QUESTIONS OF FACT about this environment - what happened, on which host, "
    "when, which component. Answer these ONLY from the log excerpts provided. "
    "If the excerpts don't contain the answer, say so plainly. "
    "Never invent a hostname, count, timestamp or cause.\n\n"
    "   CRITICAL: the excerpts are a SMALL RETRIEVED SAMPLE, not the whole "
    "database. Never state a total, a count, a ranking or a 'top N' as if it "
    "described the full dataset - counting the lines you were given and "
    "presenting that as the answer is wrong. For any question asking how many, "
    "how often, which is most/least, or for a ranking, say that it needs a "
    "structural query and suggest re-asking in Text-to-Cypher mode. You may "
    "still describe what the sample happens to show, clearly labelled as a "
    "sample.\n\n"
    "2. QUESTIONS OF EXPERTISE - what this error means, why it typically happens, "
    "how to fix or mitigate it, what to check next. Answer these from your own "
    "knowledge of the technology involved (Kubernetes, ESXi, NSX, Linux, "
    "PostgreSQL, and so on). The logs are evidence of the symptom; they are not "
    "expected to contain the remedy, and refusing to answer because the fix "
    "isn't written in a log line is unhelpful and wrong.\n\n"
    "When you use your own knowledge, mark that section clearly (a heading such "
    "as 'Likely cause' or 'How to fix' is enough) so the reader can tell which "
    "parts are observed from their data and which are general guidance. Be "
    "concrete: name the actual command, config key, or file path where you can, "
    "and say what to verify first. If a fix depends on their specific setup, say "
    "what you'd need to know.\n\n"
    "Format in Markdown for a chat UI. Keep it tight - no preamble."
)


class GraphRAG:
    def __init__(self, driver=None, database: str = None, llm: LLMClient = None,
                 embedder=None, config: Dict[str, Any] = None):
        self.driver = driver
        self.database = database
        self.llm = llm or LLMClient()
        self._embedder = embedder
        cfg = config or CHATBOT_CONFIG
        self.top_k = cfg.get('graphrag_top_k', 8)
        self.neighbour_limit = cfg.get('graphrag_neighbour_limit', 16)
        self.max_context_chars = cfg.get('max_context_chars', 12000)
        self.vector_index = EMBEDDING_CONFIG.get('vector_index', 'log_embedding_index')
        self.fulltext_index = EMBEDDING_CONFIG.get('fulltext_index', 'log_message_fulltext')

    @property
    def embedder(self):
        """Loaded lazily via the process-wide shared singleton (embeddings.py)
        - tools.py's vector_search tool needs the identical model instance, and
        loading BAAI/bge-m3 twice would double the ~2GB memory cost for no
        benefit, since the model is stateless."""
        if self._embedder is None:
            from embeddings import get_shared_embedder
            self._embedder = get_shared_embedder() or False
        return self._embedder or None

    # -- retrieval ---------------------------------------------------------
    def _run(self, query: str, **params) -> List[Dict[str, Any]]:
        if self.driver is None:
            return []
        with self.driver.session(database=self.database) as session:
            return [dict(record) for record in session.run(query, **params)]

    def retrieve(self, question: str, top_k: Optional[int] = None) -> RetrievedContext:
        k = top_k or self.top_k
        logs: List[Dict[str, Any]] = []
        method = 'none'

        embedder = self.embedder
        if embedder is not None:
            try:
                vector = embedder.embed(question)
                if vector:
                    logs = self._run(
                        _VECTOR_QUERY.format(index=self.vector_index), k=k, embedding=vector
                    )
                    method = 'vector'
            except Exception as e:
                logger.info(f"Vector search unavailable ({e}); falling back to full-text.")

        if not logs:
            try:
                # Lucene syntax: OR the terms so a multi-word question still
                # matches logs containing only some of them. Lucene operators in
                # the question itself would be a syntax error, so they're stripped.
                cleaned_words = [
                    re.sub(r'[+\-!(){}\[\]^"~*?:\\/]', ' ', word).strip()
                    for word in question.split() if len(word) > 2
                ]
                terms = ' OR '.join(w for w in cleaned_words if w)
                if terms:
                    logs = self._run(
                        _FULLTEXT_QUERY.format(index=self.fulltext_index), k=k, query=terms
                    )
                    method = 'fulltext'
            except Exception as e:
                logger.info(f"Full-text search unavailable ({e}); falling back to substring.")

        if not logs:
            # Trailing punctuation matters here, unlike the Lucene path above:
            # CONTAINS is a literal substring match, so "storage?" (from "is
            # storage healthy?") would never match the word "storage" sitting
            # in ordinary log text.
            candidates = [w.strip('?.,!:;"\'()') for w in question.split()]
            candidates = [w for w in candidates if len(w) > 3] or candidates
            longest = max(candidates, key=len, default=question)
            logs = self._run(_SUBSTRING_QUERY, term=longest, k=k)
            method = 'substring'

        neighbours: List[Dict[str, Any]] = []
        if logs:
            ids = [row['id'] for row in logs if row.get('id')]
            try:
                neighbours = self._run(_NEIGHBOUR_QUERY, ids=ids, limit=self.neighbour_limit)
            except Exception as e:
                logger.debug(f"Neighbour expansion failed: {e}")

        return RetrievedContext(logs, neighbours, method)

    # -- generation --------------------------------------------------------
    def build_context(self, context: RetrievedContext) -> str:
        lines = ["=== MOST RELEVANT LOGS ==="]
        for row in context.logs:
            lines.append(
                f"[{row.get('timestamp')}] {row.get('severity')} "
                f"{row.get('hostname')}/{row.get('process')}: {row.get('message')}"
            )
        if context.neighbours:
            lines.append("")
            lines.append("=== RELATED LOGS (share a host, user, device, task or operation) ===")
            for row in context.neighbours:
                lines.append(
                    f"[{row.get('timestamp')}] {row.get('severity')} "
                    f"{row.get('hostname')}/{row.get('process')} "
                    f"(linked via {row.get('via_label')}): {row.get('message')}"
                )
        return "\n".join(lines)[:self.max_context_chars]

    def answer(self, question: str, top_k: Optional[int] = None) -> Dict[str, Any]:
        context = self.retrieve(question, top_k=top_k)
        if not context.logs:
            return {
                'answer': "No logs in the graph matched that question.",
                'context': context,
                'grounded': False,
            }

        context_text = self.build_context(context)
        prompt = (
            f"{context_text}\n\n"
            f"QUESTION: {question}\n\n"
            f"Answer from the logs above. Cite hostnames, processes and timestamps."
        )
        answer = self.llm.complete(prompt, system=_ANSWER_SYSTEM, temperature=0.1)

        if not answer:
            # No model: return the evidence itself rather than nothing. The
            # retrieval is the genuinely hard part and it already succeeded.
            answer = (
                "No LLM is configured, so here are the most relevant log lines "
                f"(retrieved via {context.method} search):\n\n{context_text}"
            )
        return {'answer': answer, 'context': context, 'grounded': True}
