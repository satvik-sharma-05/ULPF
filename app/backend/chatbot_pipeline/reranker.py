"""
reranker.py - Cross-encoder reranking of pooled evidence (BAAI/bge-reranker-v2-m3).

The planner can call several tools per question - vector_search might return 8
logs, graph_neighbors another 20 entities, temporal_window 50 more logs. Naive
concatenation in call order means whichever tool happened to run first crowds
out better evidence a later tool found, and the final-answer prompt gets
truncated by max_context_chars with no notion of which pieces actually matter
most for *this* question.

A cross-encoder fixes this by scoring (question, candidate_text) pairs jointly
- it sees both texts at once, unlike the bi-encoder scores vector_search/
fulltext_search already return (those embed the question and each candidate
independently, then compare vectors - cheap enough to rank thousands of
candidates cheaply, but less precise about any one pair). Reranking the
already-small pool the planner gathered (tens of items, not millions) is
exactly where a cross-encoder's extra precision is worth its extra cost.

Uses sentence-transformers' CrossEncoder - already a dependency for
embeddings.py, so this needs no new package, just a second (smaller) model.
Degrades the same way every other model in this codebase does: if it isn't
downloaded or torch is broken, reranking is skipped and the pool keeps
whatever order it already had - never a crash, never a blocked answer.
"""

import logging
from typing import Any, Dict, List, Optional

from config import RERANKER_CONFIG

logger = logging.getLogger(__name__)

try:
    from sentence_transformers import CrossEncoder
    HAS_CROSS_ENCODER = True
except ImportError:
    HAS_CROSS_ENCODER = False
    logger.info("sentence_transformers not installed; reranking disabled.")
except Exception as e:
    # Same reasoning as embeddings.py's guard: a broken torch install raises
    # OSError from a DLL/shared-object loader here, not ImportError.
    HAS_CROSS_ENCODER = False
    logger.warning(f"sentence_transformers failed to load ({type(e).__name__}: {e}); reranking disabled.")


class Reranker:
    def __init__(self):
        self.model_name = RERANKER_CONFIG['model_name']
        self.device = self._resolve_device(RERANKER_CONFIG['device'])
        self.model = None
        self._load()

    @staticmethod
    def _resolve_device(requested: str) -> str:
        if requested != 'auto':
            return requested
        try:
            import torch
            if torch.cuda.is_available():
                return 'cuda'
            if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                return 'mps'
        except Exception:
            pass
        return 'cpu'

    def _load(self) -> None:
        if not HAS_CROSS_ENCODER:
            return
        try:
            logger.info(f"Loading reranker '{self.model_name}' on {self.device}...")
            self.model = CrossEncoder(self.model_name, device=self.device)
        except Exception as e:
            logger.warning(f"Could not load reranker '{self.model_name}' ({e}); "
                           f"evidence will keep its retrieval order instead of being reranked.")
            self.model = None

    @property
    def available(self) -> bool:
        return self.model is not None

    def rerank(self, query: str, candidates: List[Dict[str, Any]], text_key: str = 'message',
              top_n: Optional[int] = None) -> List[Dict[str, Any]]:
        """Scores each candidate against the query and returns the top_n, each
        with a `rerank_score` added, highest first.

        Falls back to returning the input unchanged (just trimmed to top_n) if
        the model isn't available or candidates are too few/malformed to
        bother - reranking one or two items can't reorder anything useful.
        """
        top_n = top_n or RERANKER_CONFIG['top_n']
        if not candidates:
            return candidates
        if not self.available or len(candidates) <= 1:
            return candidates[:top_n]

        pairs, indices = [], []
        for i, c in enumerate(candidates):
            text = c.get(text_key) or c.get('message') or c.get('note') or ''
            if text:
                pairs.append((query, str(text)[:2000]))
                indices.append(i)

        if not pairs:
            return candidates[:top_n]

        try:
            scores = self.model.predict(pairs)
        except Exception as e:
            logger.warning(f"Reranking failed ({e}); keeping original order.")
            return candidates[:top_n]

        scored = [dict(candidates[idx], rerank_score=float(score))
                 for idx, score in zip(indices, scores)]
        # Candidates with no text to score (rare - a tool result missing every
        # expected field) are kept at the tail rather than dropped, so a
        # partially-broken result still surfaces instead of vanishing silently.
        scored_indices = set(indices)
        unscored = [candidates[i] for i in range(len(candidates)) if i not in scored_indices]

        scored.sort(key=lambda c: c['rerank_score'], reverse=True)
        return (scored + unscored)[:top_n]


# Process-wide singleton, same reasoning as embeddings.get_shared_embedder():
# only one Planner instance needs this per process in practice, but a shared,
# lazily-loaded instance means the model is never loaded more than once
# regardless of how many callers ask.
_shared_reranker: Optional['Reranker'] = None
_shared_reranker_attempted = False


def get_shared_reranker() -> Optional[Reranker]:
    global _shared_reranker, _shared_reranker_attempted
    if not RERANKER_CONFIG.get('enabled', True):
        return None
    if not _shared_reranker_attempted:
        _shared_reranker_attempted = True
        try:
            candidate = Reranker()
            _shared_reranker = candidate if candidate.available else None
        except Exception as e:
            logger.warning(f"Reranker unavailable ({e}); evidence will keep its retrieval order.")
            _shared_reranker = None
    return _shared_reranker
