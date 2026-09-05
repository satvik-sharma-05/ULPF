"""
chatbot/classifier.py - Routes a question to a retrieval strategy.

The two retrieval methods answer genuinely different question shapes, and using
the wrong one gives a confidently wrong answer:

  TEXT2CYPHER - the graph knows the answer exactly. Counts, filters, tops,
                time ranges, "which host", "how many". A vector search over log
                text cannot count anything, so semantic retrieval fails here.
  GRAPHRAG    - the answer is spread across log *text*, not the structure.
                "Why did X fail", "what happened around Y", "explain Z". Cypher
                can't answer these because the discriminating information is in
                the message body, not in a property you could filter on.
  HYBRID      - the question needs both an exact figure and surrounding
                narrative, or the router isn't confident enough to commit.

Two-stage routing: a fast deterministic scorer runs first, and the LLM is only
consulted when the scorer is unsure. That keeps the common cases instant and
offline-capable, and means the router still works with no model available.
"""

import logging
import re
from typing import Any, Dict, List, NamedTuple

from config import CHATBOT_CONFIG
from llm import LLMClient

logger = logging.getLogger(__name__)

TEXT2CYPHER = 'text2cypher'
GRAPHRAG = 'graphrag'
HYBRID = 'hybrid'


class Route(NamedTuple):
    strategy: str
    confidence: float
    reason: str


# Signals that the answer is a computable fact about graph structure.
_STRUCTURAL_PATTERNS: List[tuple] = [
    (r'\bhow many\b', 3.0), (r'\bcount\b', 3.0), (r'\bnumber of\b', 2.5),
    (r'\btop\b|\bmost\b|\bhighest\b|\blowest\b|\bbusiest\b|\bnoisiest\b', 2.5),
    (r'\blist\b|\bshow me all\b|\bwhich\b|\bwhat are the\b', 2.0),
    (r'\baverage\b|\bsum\b|\btotal\b|\bper (?:host|day|process|severity)\b', 2.5),
    (r'\bbetween\b.*\band\b|\bon \d{4}-\d{2}-\d{2}\b|\blast \d+ (?:days?|hours?)\b', 2.0),
    (r'\bgroup(?:ed)? by\b|\bbreakdown\b|\bdistribution\b', 2.5),
    (r'\bmore than\b|\bfewer than\b|\bat least\b|\bexceed', 2.0),
    (r'\bconnected to\b|\brelated to\b|\bshared\b|\bin common\b', 1.5),
]

# Signals that the answer lives in the prose of the log messages.
_SEMANTIC_PATTERNS: List[tuple] = [
    (r'\bwhy\b', 3.0), (r'\bexplain\b|\bdescribe\b', 2.5),
    (r'\broot cause\b|\bcause of\b|\bdiagnos', 3.0),
    (r'\bwhat happened\b|\bwhat went wrong\b|\bwhat caused\b', 3.0),
    (r'\bsimilar\b|\blike this\b|\banything about\b', 2.5),
    (r'\bsummar(?:ise|ize|y)\b|\boverview\b', 2.0),
    (r'\btroubleshoot\b|\bdebug\b|\binvestigate\b', 2.0),
    (r'\bmean(?:s|ing)?\b|\bunderstand\b', 1.5),
]

_SYSTEM_PROMPT = (
    "You classify questions about an infrastructure log knowledge graph. "
    "Answer with JSON only."
)


class QueryClassifier:
    def __init__(self, llm: LLMClient = None, config: Dict[str, Any] = None):
        self.llm = llm or LLMClient()
        cfg = config or CHATBOT_CONFIG
        self.threshold = cfg.get('router_confidence_threshold', 0.6)

    # -- stage 1: deterministic -------------------------------------------
    def _score(self, question: str) -> Dict[str, float]:
        text = question.lower()
        structural = sum(weight for pattern, weight in _STRUCTURAL_PATTERNS
                         if re.search(pattern, text))
        semantic = sum(weight for pattern, weight in _SEMANTIC_PATTERNS
                       if re.search(pattern, text))
        return {'structural': structural, 'semantic': semantic}

    def _heuristic_route(self, question: str) -> Route:
        scores = self._score(question)
        structural, semantic = scores['structural'], scores['semantic']
        total = structural + semantic

        if total == 0:
            return Route(HYBRID, 0.3, "No strong structural or semantic signal in the question.")

        # Both kinds of signal present and comparable -> genuinely a both-worlds
        # question ("how many errors and why did they happen").
        if structural and semantic and min(structural, semantic) / max(structural, semantic) > 0.5:
            return Route(HYBRID, 0.75,
                         f"Mixed signals (structural={structural:.1f}, semantic={semantic:.1f}).")

        if structural > semantic:
            confidence = min(0.95, 0.55 + (structural - semantic) / 10)
            return Route(TEXT2CYPHER, confidence,
                         f"Question asks for a computed/filtered fact (score {structural:.1f}).")

        confidence = min(0.95, 0.55 + (semantic - structural) / 10)
        return Route(GRAPHRAG, confidence,
                     f"Question asks for explanation/context (score {semantic:.1f}).")

    # -- stage 2: LLM tie-break -------------------------------------------
    def _llm_route(self, question: str) -> Route:
        prompt = f"""Classify how to answer this question about a Neo4j graph of infrastructure logs.

Strategies:
- "text2cypher": answerable by a Cypher query over graph structure (counts, filters, rankings, time ranges, relationships).
- "graphrag": needs semantic search over log message text (why something happened, explanations, summaries).
- "hybrid": needs both an exact figure and narrative context.

Question: {question}

Reply with JSON: {{"strategy": "...", "confidence": 0.0-1.0, "reason": "..."}}"""

        result = self.llm.complete_json(prompt, system=_SYSTEM_PROMPT)
        if not result:
            return None
        strategy = str(result.get('strategy', '')).lower().strip()
        if strategy not in (TEXT2CYPHER, GRAPHRAG, HYBRID):
            return None
        try:
            confidence = float(result.get('confidence', 0.6))
        except (TypeError, ValueError):
            confidence = 0.6
        return Route(strategy, max(0.0, min(confidence, 1.0)),
                     str(result.get('reason', 'LLM classification'))[:200])

    # -- public -----------------------------------------------------------
    def classify(self, question: str) -> Route:
        heuristic = self._heuristic_route(question)
        if heuristic.confidence >= self.threshold:
            return heuristic

        llm_route = self._llm_route(question)
        if llm_route:
            logger.debug(f"Router: heuristic unsure ({heuristic.confidence:.2f}), LLM said {llm_route.strategy}")
            return llm_route

        # No model and no clear heuristic: hybrid is the safe default, since it
        # runs both retrievers rather than betting on the wrong one.
        return Route(HYBRID, heuristic.confidence,
                     heuristic.reason + " Defaulted to hybrid (no LLM available to break the tie).")
