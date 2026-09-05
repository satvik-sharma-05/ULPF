"""
planner.py - The LLM planning loop: decides what to do, not just how to
answer one fixed way.

    Question
       |
       v
    LLM Planner  <-------------------+
       |                             |
       v                             |
    execute the tools it chose       |
       |                             |
       +--- more rounds needed? -----+
       |
       v
    Context Fusion (all gathered evidence)
       |
       v
    qwen3:14b -> final answer

This replaces the old "classify once into text2cypher OR graphrag" router for
the "auto" mode: a compound question like "why did storage fail and which
hosts were affected" genuinely needs graph_neighbors AND vector_search AND
possibly temporal_window together, not a single strategy picked up front.
Manual mode selection (the frontend's Text-to-Cypher / GraphRAG buttons)
bypasses this file entirely and stays exactly as before - this only changes
what "Auto" does.

Reliability matters more than sophistication here: qwen3:14b is prompted for
JSON via a raw completion API, not a native function-calling interface, so
planning can fail (bad JSON, hallucinated tool name, empty response) far more
often than a GPT-4-class model would. Every failure mode degrades rather than
breaks:
  - an unknown tool name or bad params -> that one step is skipped, logged,
    planning continues
  - the planner returns nothing usable across all rounds -> a fixed,
    deterministic fallback (vector_search + text2cypher) runs instead, so the
    chatbot never returns "no evidence" purely because one JSON parse failed
  - hard caps on rounds and total tool calls bound the worst-case latency/cost
    of a plan that never converges
"""

import logging
from typing import Any, Dict, List, NamedTuple, Optional

from config import RERANKER_CONFIG
from entity_resolver import EntityResolver
from reranker import get_shared_reranker
from tools import TOOL_SPECS, ToolContext, ToolResult, fulltext_search, make_text2cypher_tool, vector_search

logger = logging.getLogger(__name__)

_PLAN_SYSTEM = (
    "You are a retrieval planning agent for an infrastructure log knowledge graph. "
    "Answer with JSON only - no prose, no markdown fences, no <think> tags."
)

# Built once per Planner instance from the SAME TOOL_SPECS dict that dispatches
# calls (see _tool_specs_text below) - the prompt can name a decision rule
# about "graph traversal" or "metadata filters" freely, but the tool names it
# tells qwen it may actually call always come from self.tools, so the model
# can never be pointed at a capability that doesn't exist in tools.py.
_PLAN_PROMPT = """You are planning how to retrieve evidence for a question about an infrastructure
log knowledge graph (VMware/vSphere/Kubernetes logs). You may call tools across several rounds -
decide the next batch given what has already been tried. Stop once you have enough evidence to
answer; most questions need only one or two tool calls total.

AVAILABLE TOOLS (call these by their exact name - no other tool names exist):
{tools}

DECISION GUIDANCE:
- Root cause / "why did X happen" questions: vector_search to find the relevant logs, plus
  graph_neighbors and/or path_traversal to find what is structurally connected. Once a specific
  log id is known from an earlier step, parent_child_retrieval pulls in the rest of that incident.
- Relationship / dependency / blast-radius questions ("what depends on X", "is X connected to Y"):
  graph_neighbors (one entity) or path_traversal (two entities).
- Statistical / counting / ranking questions ("how many", "top N", "which host has the most"):
  try query_template first; fall back to text2cypher if no template fits.
- Exact lookup of a known error code, id, or quoted phrase: fulltext_search. Vague or descriptive
  wording: vector_search. Combine both if unsure which the question needs.
- Known vSphere/VMware failure signatures (HA restart, APD, host isolation, network partition,
  snapshot failure): pattern_match.
- Time-scoped questions ("what happened between T1 and T2", timelines): temporal_window.
- Host, day-range and severity filters are NOT separate tools - pass them as parameters
  (host / day_from / day_to / max_severity_score) on whichever tool above already fits.
- If nothing gathered so far answers the question and no tool obviously applies, still return your
  best single attempt rather than an empty plan - an imperfect tool call beats none.

GRAPH SCHEMA:
{schema}

QUESTION: {question}

STEPS ALREADY TAKEN AND THEIR RESULTS:
{history}

Reply with JSON only, matching this exact shape:
{{
  "intent": "<one of: root_cause, relationship, statistical, exact_lookup, temporal, pattern, unknown>",
  "confidence": <number 0.0-1.0>,
  "entities": ["<names/ids/hosts the question mentions, if any>"],
  "filters": {{"host": null, "day_from": null, "day_to": null, "max_severity_score": null}},
  "retrieval_plan": [
    {{"tool": "<exact name from AVAILABLE TOOLS>", "reason": "<why this tool, one short sentence>", "parameters": {{...}}}}
  ],
  "done": true|false
}}

- "retrieval_plan" is the next batch of tool calls to make - can be empty.
- Set "done": true once this batch (plus anything already done) should be enough to answer.
- Set "done": false only if you already know you will need another round after seeing these results.
"""

_ANSWER_SYSTEM = (
    "You are an infrastructure operations analyst. Answer only from the evidence provided below. "
    "Cite hosts, processes, timestamps and entity names. If the evidence doesn't answer the "
    "question, say so plainly rather than guessing.\n\n"
    "Be thorough, not just correct: don't stop at the bare number the question asked for. "
    "Note what stands out in the data (an outlier, a skew toward one host/severity, "
    "something notably absent), and where it's genuinely useful, add one sentence of "
    "plausible operational context for infrastructure logs like these - but never invent "
    "a fact (a host name, a cause, a count) that isn't in the evidence given.\n\n"
    "Format the answer in Markdown, rendered directly in a chat UI:\n"
    "- A breakdown across more than 2-3 items (hosts, severities, source types, ...) "
    "goes in a Markdown table, not a prose list of numbers.\n"
    "- Bold the specific figures and entity names that answer the question "
    "(counts, host names, severities), not whole sentences.\n"
    "- Use bullet or numbered lists for enumerations, short paragraphs otherwise.\n"
    "- No code fences around the answer itself - only around literal Cypher/log text."
)


class PlannerResult(NamedTuple):
    answer: str
    trace: List[Dict[str, Any]]                  # [{tool, params, ok, note}, ...] - for the UI's plan trace
    rows: Optional[List[Dict[str, Any]]]
    grounded: bool                                # False only if nothing at all was retrieved


class Planner:
    def __init__(self, driver=None, database: Optional[str] = None, llm=None,
                 text_to_cypher=None, schema=None, embedder=None,
                 vector_index: str = 'log_embedding_index',
                 fulltext_index: str = 'log_message_fulltext',
                 config: Optional[Dict[str, Any]] = None):
        self.driver = driver
        self.database = database
        self.llm = llm
        self.schema = schema
        cfg = config or {}
        self.max_rounds = max(1, int(cfg.get('planner_max_rounds', 3)))
        self.max_tool_calls = max(1, int(cfg.get('planner_max_tool_calls', 6)))
        self.max_context_chars = int(cfg.get('max_context_chars', 12000))
        self.max_rows_per_tool = int(cfg.get('planner_max_rows_per_tool', 20))

        self.ctx = ToolContext(
            driver=driver, database=database,
            resolver=EntityResolver(driver, database),
            embedder=embedder, vector_index=vector_index, fulltext_index=fulltext_index,
        )
        # Fresh per-instance copies of the spec dicts - TOOL_SPECS is a shared
        # module-level object, and text2cypher's callable is bound to THIS
        # instance's TextToCypher, so mutating the shared dicts directly would
        # leak one Planner's text2cypher binding into every other instance.
        self.tools: Dict[str, Dict[str, Any]] = {name: dict(spec) for name, spec in TOOL_SPECS.items()}
        if text_to_cypher is not None:
            self.tools['text2cypher']['fn'] = make_text2cypher_tool(text_to_cypher)

    # -- prompt construction -------------------------------------------------
    def _tool_specs_text(self) -> str:
        return "\n".join(
            f"- {name}({spec['params']}): {spec['description']}"
            for name, spec in self.tools.items() if spec.get('fn') is not None
        )

    def _schema_text(self) -> str:
        if self.schema is None:
            return "(schema unavailable)"
        return self.schema.description()

    @staticmethod
    def _format_history(trace: List[Dict[str, Any]]) -> str:
        if not trace:
            return "(none yet)"
        lines = []
        for i, t in enumerate(trace, 1):
            status = "ok" if t['ok'] else "failed"
            lines.append(f"{i}. {t['tool']}({t['params']}) -> {status}: {t['note']}")
        return "\n".join(lines)

    # -- planning + execution -------------------------------------------------
    def _plan(self, question: str, trace: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        prompt = _PLAN_PROMPT.format(
            tools=self._tool_specs_text(), schema=self._schema_text(),
            question=question, history=self._format_history(trace),
        )
        return self.llm.complete_json(prompt, system=_PLAN_SYSTEM) if self.llm else None

    @staticmethod
    def _steps_from_plan(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Reads the new 'retrieval_plan' key, but a raw JSON-completion model
        occasionally reverts to a simpler shape it has seen elsewhere in the
        prompt (or from an earlier round's own output echoed back) - accepting
        the old 'steps' key too costs nothing and avoids treating a merely
        differently-shaped plan as a planning failure."""
        steps = plan.get('retrieval_plan')
        if not isinstance(steps, list):
            steps = plan.get('steps')
        return steps if isinstance(steps, list) else []

    def _execute_step(self, step: Dict[str, Any]) -> Optional[ToolResult]:
        name = str(step.get('tool', '')).strip()
        spec = self.tools.get(name)
        if not spec or spec.get('fn') is None:
            logger.info(f"Planner requested unknown/unavailable tool {name!r}; skipping.")
            return None
        params = step.get('parameters')
        if not isinstance(params, dict):
            params = step.get('params')
        if not isinstance(params, dict):
            params = {}
        try:
            return spec['fn'](self.ctx, **params)
        except TypeError as e:
            logger.info(f"Planner called {name} with invalid params {params}: {e}")
            return None
        except Exception as e:
            logger.warning(f"Tool {name} raised an unexpected error: {e}")
            return None

    @staticmethod
    def _is_log_shaped(evidence: List[Dict[str, Any]]) -> bool:
        """True for evidence rows carrying free log message text (vector_search,
        fulltext_search, temporal_window, pattern_match, parent_child_retrieval -
        anything built on _LOG_FIELDS) as opposed to tabular/entity rows
        (graph_neighbors, path_traversal, text2cypher, query_template), which a
        text-vs-question cross-encoder has nothing meaningful to score."""
        return bool(evidence) and 'message' in evidence[0]

    def _record_result(self, name: str, result: Optional[ToolResult],
                       log_rows: List[Dict[str, Any]], structural_texts: List[str],
                       all_rows: List[Dict[str, Any]]) -> None:
        if result is None:
            return
        if result.ok and result.evidence:
            capped = result.evidence[: self.max_rows_per_tool]
            all_rows.extend(capped)
            if self._is_log_shaped(result.evidence):
                log_rows.extend(capped)
            elif result.context_text:
                structural_texts.append(result.context_text)
        elif result.ok and result.context_text:
            structural_texts.append(result.context_text)

    def _pool_log_evidence(self, question: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Dedupes by log id (parent_child_retrieval and vector_search often
        surface the same log) then reranks the pool with the cross-encoder so
        the final answer prompt sees the most relevant evidence first,
        regardless of which tool or round it came from. Falls back to a plain
        cap on the dedupe order if the reranker isn't available - never blocks
        an answer on a model that may not be downloaded."""
        deduped, seen = [], set()
        for row in rows:
            rid = row.get('id')
            if rid is not None:
                if rid in seen:
                    continue
                seen.add(rid)
            deduped.append(row)
        reranker = get_shared_reranker()
        if reranker is not None:
            return reranker.rerank(question, deduped, text_key='message')
        return deduped[: RERANKER_CONFIG['top_n']]

    @staticmethod
    def _format_log_block(rows: List[Dict[str, Any]]) -> str:
        if not rows:
            return ""
        lines = ["Retrieved log evidence (ranked by relevance to the question):"]
        for row in rows:
            lines.append(
                f"  [{row.get('timestamp')}] {row.get('severity')} "
                f"{row.get('hostname')}/{row.get('process')}: {row.get('message')}"
            )
        return "\n".join(lines)

    def _fallback(self, question: str, trace: List[Dict[str, Any]],
                 log_rows: List[Dict[str, Any]], structural_texts: List[str],
                 all_rows: List[Dict[str, Any]]) -> None:
        """Runs when planning produced nothing usable across every round - a
        fixed, deterministic sequence so the chatbot still answers instead of
        reporting an empty result purely because a 14B model's JSON planning
        had an off round.

        vector_search always runs first but, on a graph ingested without
        --embed (or before embed_logs.py has reached these rows), the vector
        index exists but is empty - db.index.vector.queryNodes on an empty
        index returns 0 rows, not an error, so vector_search reports ok=True
        with no evidence rather than failing. Without an explicit check here
        the fallback would silently waste its first call on a query that can
        never succeed yet, so fulltext_search only runs if vector_search
        didn't actually return anything - the same vector-then-fulltext
        degrade graph_rag.py's GraphRAG already does for the same reason."""
        logger.info("Planner produced no usable evidence; falling back to vector_search + text2cypher.")
        steps = [('vector_search', vector_search(self.ctx, question))]
        if not steps[0][1] or not steps[0][1].evidence:
            steps.append(('fulltext_search', fulltext_search(self.ctx, question)))
        steps.append(('text2cypher', self._execute_step({'tool': 'text2cypher', 'parameters': {'question': question}})))

        for name, result in steps:
            if result is None:
                continue
            trace.append({'tool': name, 'params': {'question': question}, 'reason': 'deterministic fallback',
                          'ok': result.ok, 'note': result.note})
            self._record_result(name, result, log_rows, structural_texts, all_rows)

    # -- public ---------------------------------------------------------------
    def run(self, question: str) -> PlannerResult:
        trace: List[Dict[str, Any]] = []
        log_rows: List[Dict[str, Any]] = []
        structural_texts: List[str] = []
        all_rows: List[Dict[str, Any]] = []
        tool_calls_made = 0

        if self.llm is not None:
            for _round in range(self.max_rounds):
                if tool_calls_made >= self.max_tool_calls:
                    break
                plan = self._plan(question, trace)
                steps = self._steps_from_plan(plan) if plan else []
                if not steps:
                    break

                budget = self.max_tool_calls - tool_calls_made
                for step in steps[:budget]:
                    result = self._execute_step(step)
                    tool_calls_made += 1
                    name = step.get('tool')
                    params = step.get('parameters') if isinstance(step.get('parameters'), dict) else step.get('params') or {}
                    if result is not None:
                        trace.append({
                            'tool': name, 'params': params, 'reason': step.get('reason', ''),
                            'ok': result.ok, 'note': result.note,
                        })
                    self._record_result(name, result, log_rows, structural_texts, all_rows)
                    if tool_calls_made >= self.max_tool_calls:
                        break

                if plan.get('done'):
                    break

        if not log_rows and not structural_texts:
            self._fallback(question, trace, log_rows, structural_texts, all_rows)

        if not log_rows and not structural_texts:
            return PlannerResult(
                answer="No evidence could be gathered in the graph to answer this question.",
                trace=trace, rows=all_rows or None, grounded=False,
            )

        evidence_texts = list(structural_texts)
        if log_rows:
            pooled = self._pool_log_evidence(question, log_rows)
            log_block = self._format_log_block(pooled)
            if log_block:
                evidence_texts.insert(0, log_block)

        context_text = "\n\n".join(evidence_texts)[: self.max_context_chars]
        answer = None
        if self.llm is not None:
            answer = self.llm.complete(
                f"{context_text}\n\nQUESTION: {question}\n\nAnswer using the evidence above.",
                system=_ANSWER_SYSTEM, temperature=0.1,
            )
        if not answer:
            # No model available: the retrieved evidence itself is the answer -
            # it's exact/real, unlike a synthesized sentence would need to be.
            answer = f"No LLM available to summarize - here is the gathered evidence:\n\n{context_text}"

        return PlannerResult(answer=answer, trace=trace, rows=all_rows or None, grounded=True)
