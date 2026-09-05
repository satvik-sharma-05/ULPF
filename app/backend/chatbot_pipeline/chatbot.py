"""
chatbot/chatbot.py - The orchestrator.

    from chatbot import LogChatbot
    bot = LogChatbot()
    print(bot.ask("Which hosts produced the most errors on 2026-06-15?").answer)

Two ways a question gets answered:

  - `force_route` given ("text2cypher"/"graphrag"/"hybrid") - a single fixed
    strategy runs, bypassing all planning. This is what the frontend's manual
    mode buttons use: for a demo you want to show each retrieval strategy on
    demand, not leave it to an LLM's judgment about which one to use.
  - no `force_route` ("auto") - an LLM planner (planner.py) decides which
    tool(s) to call, possibly several in sequence, and fuses whatever it
    gathers into one answer. This replaced a simpler "classify once into
    text2cypher OR graphrag" router: a compound question like "why did
    storage fail and which hosts were affected" genuinely needs graph
    traversal AND vector search AND possibly a time window together, not one
    strategy picked up front.

If the text-to-Cypher path (manual mode) produces no usable rows, the answer
falls through to GraphRAG instead of reporting an empty result - a bad guess
should cost latency, not the answer.
"""

import logging
from typing import Any, Dict, List, NamedTuple, Optional

from config import CHATBOT_CONFIG, EMBEDDING_CONFIG, NEO4J_CONFIG
from classifier import GRAPHRAG, TEXT2CYPHER, Route
from graph_rag import GraphRAG
from llm import LLMClient
from planner import Planner
from schema_introspect import GraphSchema
from text_to_cypher import TextToCypher

logger = logging.getLogger(__name__)

PLANNER = 'planner'


class ChatResponse(NamedTuple):
    question: str
    answer: str
    route: Route
    cypher: Optional[str] = None
    rows: Optional[List[Dict[str, Any]]] = None
    retrieval_method: Optional[str] = None
    # Present whenever GraphRAG ran (graphrag or hybrid routes): the seed logs
    # the vector/full-text/substring search found, and the one-hop neighbours
    # pulled in via shared entities. Exists so a caller (the demo frontend, in
    # particular) can show *what was retrieved*, not just the final prose -
    # the retrieval is the part worth being transparent about, since it's
    # where a wrong answer would actually originate.
    context: Optional[Dict[str, List[Dict[str, Any]]]] = None
    # Present only for the "auto" (planner) route: the sequence of tool calls
    # the planner made - [{tool, params, ok, note}, ...] - so a caller can show
    # *what the agent decided to do*, not just its final answer. This is the
    # single most useful thing to surface for a demo of agentic retrieval:
    # a wrong answer is much easier to diagnose from "it called graph_neighbors
    # on the wrong entity" than from the prose alone.
    plan: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None

    def render(self) -> str:
        parts = [f"Route: {self.route.strategy} "
                 f"(confidence {self.route.confidence:.2f}) - {self.route.reason}"]
        if self.plan:
            parts.append("\nPlan:")
            for i, step in enumerate(self.plan, 1):
                status = "ok" if step.get('ok') else "failed"
                parts.append(f"  {i}. {step.get('tool')}({step.get('params')}) -> {status}: {step.get('note')}")
        if self.cypher:
            parts.append(f"\nCypher:\n{self.cypher}")
        if self.retrieval_method:
            parts.append(f"\nRetrieval: {self.retrieval_method}")
        if self.error:
            parts.append(f"\nNote: {self.error}")
        parts.append(f"\n{self.answer}")
        return "\n".join(parts)


_SYNTHESIS_SYSTEM = (
    "You are an experienced infrastructure engineer analysing logs.\n\n"
    "Two kinds of question, two different rules:\n\n"
    "1. FACTS about this environment (what happened, which host, how many, when): "
    "answer ONLY from the query results and log excerpts given. Cite hosts, "
    "processes and timestamps. Never invent a hostname, count or timestamp that "
    "isn't in the evidence.\n\n"
    "2. EXPERTISE (what this error means, why it happens, how to fix it, what to "
    "check next): answer from your own knowledge of the technology involved. The "
    "logs show the symptom; they are not expected to contain the remedy, and "
    "refusing to advise because the fix isn't written in a log line is unhelpful. "
    "Mark such sections clearly (e.g. 'Likely cause', 'How to fix') so the reader "
    "can tell observed data from general guidance, and be concrete - name the "
    "command, config key or file path, and say what to verify first.\n\n"
    "Be thorough on the data too: don't stop at the bare number the question asked "
    "for. Note what stands out (an outlier, a skew toward one host/severity, "
    "something notably absent).\n\n"
    "Format the answer in Markdown, rendered directly in a chat UI:\n"
    "- A breakdown across more than 2-3 items (hosts, severities, source types, ...) "
    "goes in a Markdown table, not a prose list of numbers.\n"
    "- Bold the specific figures and entity names that answer the question "
    "(counts, host names, severities), not whole sentences.\n"
    "- Use bullet or numbered lists for enumerations, short paragraphs otherwise.\n"
    "- No code fences around the answer itself - only around literal Cypher/log text."
)


class LogChatbot:
    def __init__(self, driver=None, database: Optional[str] = None,
                 config: Optional[Dict[str, Any]] = None):
        self.config = config or CHATBOT_CONFIG
        self.database = database or NEO4J_CONFIG['database']
        self.driver = driver or self._connect()

        self.llm = LLMClient()
        # Introspected once from the live database and cached, so text-to-Cypher
        # describes the graph that actually exists rather than a hard-coded list
        # that could have drifted from what the ingestion pipeline built.
        self.schema = GraphSchema(
            self.driver, self.database,
            sample_limit=self.config.get('schema_sample_limit', 3),
        ) if self.config.get('introspect_schema', True) else None
        self.text_to_cypher = TextToCypher(
            driver=self.driver, database=self.database, schema=self.schema,
            llm=self.llm, config=self.config,
        )
        self.graph_rag = GraphRAG(
            driver=self.driver, database=self.database, llm=self.llm, config=self.config
        )
        # Drives "auto" mode - see the module docstring. Shares this same
        # TextToCypher/schema instance rather than building its own, so
        # text2cypher behaves identically whether the planner calls it as a
        # tool or a manual "Text -> Cypher" button calls it directly.
        self.planner = Planner(
            driver=self.driver, database=self.database, llm=self.llm,
            text_to_cypher=self.text_to_cypher, schema=self.schema,
            vector_index=EMBEDDING_CONFIG.get('vector_index', 'log_embedding_index'),
            fulltext_index=EMBEDDING_CONFIG.get('fulltext_index', 'log_message_fulltext'),
            config=self.config,
        )

    def schema_summary(self) -> str:
        """The schema the chatbot believes it is querying - useful for
        debugging an answer that looks wrong."""
        return self.schema.description() if self.schema else "(schema introspection disabled)"

    @staticmethod
    def _connect():
        try:
            from neo4j import GraphDatabase
        except ImportError:
            logger.error("neo4j driver not installed - run: pip install neo4j")
            return None
        try:
            driver = GraphDatabase.driver(
                NEO4J_CONFIG['uri'],
                auth=(NEO4J_CONFIG['user'], NEO4J_CONFIG['password']),
            )
            with driver.session(database=NEO4J_CONFIG['database']) as session:
                session.run("RETURN 1").consume()
            return driver
        except Exception as e:
            logger.error(f"Could not connect to Neo4j at {NEO4J_CONFIG['uri']}: {e}")
            return None

    # -- rendering ---------------------------------------------------------
    @staticmethod
    def _rows_to_text(rows: List[Dict[str, Any]], max_rows: int = 40) -> str:
        if not rows:
            return "(no rows)"
        lines = []
        for row in rows[:max_rows]:
            lines.append(" | ".join(f"{k}={v}" for k, v in row.items()))
        if len(rows) > max_rows:
            lines.append(f"... and {len(rows) - max_rows} more rows")
        return "\n".join(lines)

    def _summarize_rows(self, question: str, cypher: str, rows: List[Dict[str, Any]]) -> str:
        table = self._rows_to_text(rows)
        answer = self.llm.complete(
            f"QUESTION: {question}\n\nCYPHER RUN:\n{cypher}\n\nRESULTS:\n{table}\n\n"
            f"Answer the question from these results in two or three sentences.",
            system=_SYNTHESIS_SYSTEM, temperature=0.1,
        )
        # Without a model the rows themselves are the answer - they are exact,
        # which is the whole point of having routed here.
        return answer or f"Query returned {len(rows)} row(s):\n{table}"

    @staticmethod
    def _context_dict(rag_context) -> Dict[str, List[Dict[str, Any]]]:
        return {'logs': rag_context.logs, 'neighbours': rag_context.neighbours}

    # -- strategies --------------------------------------------------------
    def _answer_text2cypher(self, question: str, route: Route) -> ChatResponse:
        result = self.text_to_cypher.answer(question)

        if result.error or not result.rows:
            note = result.error or "The generated query returned no rows."
            logger.info(f"text2cypher unproductive ({note}); falling back to GraphRAG.")
            fallback = self.graph_rag.answer(question)
            return ChatResponse(
                question=question, answer=fallback['answer'], route=route,
                cypher=result.cypher, rows=result.rows,
                retrieval_method=f"{fallback['context'].method} (fallback)",
                context=self._context_dict(fallback['context']),
                error=note,
            )

        # A generated query always gets a LIMIT appended (text_to_cypher's
        # enforce_limit) so an unbounded `RETURN l` can't stream the whole
        # graph. On a single-row aggregate that's a no-op, but on a GROUPED
        # aggregate - "count per host", 290 hosts - it silently returns only
        # the first N, and the model would then summarize a truncated set as
        # if it were complete. Saying so is the difference between a capped
        # answer and a wrong one.
        truncated = len(result.rows) >= self.config.get('text2cypher_row_limit', 100)
        note = (
            f"Showing the first {len(result.rows)} rows - the query hit the row limit, "
            f"so any total or ranking below may be incomplete."
        ) if truncated else None

        return ChatResponse(
            question=question,
            answer=self._summarize_rows(question, result.cypher, result.rows),
            route=route, cypher=result.cypher, rows=result.rows,
            error=note,
        )

    def _answer_graphrag(self, question: str, route: Route) -> ChatResponse:
        result = self.graph_rag.answer(question)
        return ChatResponse(
            question=question, answer=result['answer'], route=route,
            retrieval_method=result['context'].method,
            context=self._context_dict(result['context']),
        )

    def _answer_hybrid(self, question: str, route: Route) -> ChatResponse:
        cypher_result = self.text_to_cypher.answer(question)
        rag_context = self.graph_rag.retrieve(question)

        sections = []
        if cypher_result.rows:
            sections.append(
                f"=== QUERY RESULTS ===\nCypher:\n{cypher_result.cypher}\n\n"
                f"{self._rows_to_text(cypher_result.rows)}"
            )
        if rag_context.logs:
            sections.append(self.graph_rag.build_context(rag_context))

        if not sections:
            return ChatResponse(
                question=question,
                answer="Nothing in the graph matched that question.",
                route=route, cypher=cypher_result.cypher,
                error=cypher_result.error,
            )

        context_text = "\n\n".join(sections)[:self.config.get('max_context_chars', 12000)]
        answer = self.llm.complete(
            f"{context_text}\n\nQUESTION: {question}\n\n"
            f"Answer using both the query results and the log excerpts above.",
            system=_SYNTHESIS_SYSTEM, temperature=0.1,
        )
        return ChatResponse(
            question=question,
            answer=answer or context_text,
            route=route, cypher=cypher_result.cypher, rows=cypher_result.rows,
            retrieval_method=rag_context.method,
            context=self._context_dict(rag_context),
        )

    def _answer_planner(self, question: str) -> ChatResponse:
        result = self.planner.run(question)
        successful_tools = [step['tool'] for step in result.trace if step.get('ok')]
        reason = (f"planner called: {', '.join(successful_tools)}" if successful_tools
                 else "planner made no successful tool calls")
        route = Route(PLANNER, 1.0 if result.grounded else 0.0, reason)
        return ChatResponse(
            question=question, answer=result.answer, route=route,
            rows=result.rows, plan=result.trace,
            error=None if result.grounded else "no evidence could be gathered",
        )

    # -- conversation memory ------------------------------------------------
    def _standalone_question(self, question: str, history: List[Dict[str, str]]) -> str:
        """Rewrites a follow-up into a self-contained question.

        Conversation memory is done by REWRITING the question rather than by
        threading history through every retrieval path. "and how many are
        ESXi?" is meaningless to text-to-Cypher, the planner and the vector
        search alike - all three need the subject restored. Rewriting once, up
        front, fixes every downstream path at a single point and leaves the
        rest of the pipeline exactly as stateless (and as testable) as it was.

        Degrades to the raw question whenever it can't do better: no history,
        no LLM, or an implausible rewrite. A bad rewrite would silently answer
        a different question than the one asked, so the guards below are
        deliberately conservative.
        """
        if not history:
            return question
        recent = history[-6:]   # 3 exchanges is plenty to resolve a pronoun
        transcript = "\n".join(
            f"{m['role'].upper()}: {m['content'][:500]}" for m in recent
        )
        rewritten = self.llm.complete(
            f"Conversation so far:\n{transcript}\n\n"
            f"Follow-up question: {question}\n\n"
            f"Rewrite the follow-up as a single self-contained question that keeps "
            f"the same meaning, resolving any pronouns or implied subject from the "
            f"conversation. If it is already self-contained, repeat it unchanged. "
            f"Reply with the question only.",
            system="You rewrite follow-up questions to be self-contained. Output one question, nothing else.",
            temperature=0.0,
        )
        if not rewritten:
            return question
        rewritten = rewritten.strip().strip('"').split('\n')[0].strip()
        # Guard against a model that editorializes instead of rewriting: a
        # rewrite should be a question of comparable length, not a paragraph.
        if not rewritten or len(rewritten) > max(300, len(question) * 6):
            return question
        if rewritten.lower() != question.lower():
            logger.info(f"Follow-up rewritten: {question!r} -> {rewritten!r}")
        return rewritten

    # -- public ------------------------------------------------------------
    def ask(self, question: str, force_route: Optional[str] = None,
            history: Optional[List[Dict[str, str]]] = None,
            extra_context: Optional[str] = None) -> ChatResponse:
        """Answer a question. `force_route` ("text2cypher"/"graphrag"/"hybrid")
        picks one fixed strategy directly, bypassing the planner entirely -
        what the frontend's manual mode buttons use. With no force_route
        ("auto"), the planner decides what to do; see the module docstring.

        `history` is prior [{role, content}] turns of the same session; it is
        used to rewrite a follow-up into a standalone question (see
        _standalone_question) and is otherwise not threaded downstream.

        `extra_context` is pinned text prepended to the retrieval question -
        used by a log-scoped session so "why did this happen?" carries the
        actual log with it instead of relying on the model to have remembered
        it.
        """
        if self.driver is None:
            return ChatResponse(
                question=question, route=Route(force_route or PLANNER, 0.0, "no database connection"),
                answer="Not connected to Neo4j - check NEO4J_URI/credentials in .env.",
                error="no database connection",
            )

        # The rewritten form drives retrieval; the ORIGINAL is what gets echoed
        # back and stored, so the transcript reads as the user actually typed it.
        asked = self._standalone_question(question, history or [])
        if extra_context:
            asked = f"{extra_context}\n\n{asked}"

        if not force_route:
            logger.info("Routing to the planner (auto mode).")
            response = self._answer_planner(asked)
            return response._replace(question=question)

        route = Route(force_route, 1.0, "strategy forced by caller")
        logger.info(f"Routed to {route.strategy} (forced).")
        if route.strategy == TEXT2CYPHER:
            response = self._answer_text2cypher(asked, route)
        elif route.strategy == GRAPHRAG:
            response = self._answer_graphrag(asked, route)
        else:
            response = self._answer_hybrid(asked, route)
        return response._replace(question=question)

    def close(self):
        if self.driver:
            self.driver.close()
