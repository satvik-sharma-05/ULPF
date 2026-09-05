"""
embeddings.py - Embeds the user's question for vector search.

Deliberately a copy of the ingestion pipeline's generator rather than an import
from it: this pipeline is standalone and must deploy without the ingestion
codebase present. The contract between them is the *vector space*, not the
code - EMBEDDING_MODEL and EMBEDDING_DIM here must match whatever produced the
vectors on (:Log).embedding, or the nearest-neighbour results are meaningless.

Unlike the ingestion side there is no hash-based fallback. A fake vector would
return confident, arbitrary "most relevant logs"; failing to load the model
returns None instead, and graph_rag.py falls back to full-text search, which is
weaker but honest.
"""

import logging
from typing import List, Optional

from config import EMBEDDING_CONFIG

logger = logging.getLogger(__name__)

# Broad except on purpose: `import sentence_transformers` transitively imports
# torch, and a torch that is installed but unusable (missing VC++ runtime,
# unsupported CPU, mismatched CUDA build) raises OSError from a library loader
# rather than ImportError. Catching only ImportError would crash the chatbot at
# import time instead of degrading to full-text search.
try:
    from sentence_transformers import SentenceTransformer
    HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    HAS_SENTENCE_TRANSFORMERS = False
    logger.info("sentence_transformers not installed; using full-text search.")
except Exception as e:
    HAS_SENTENCE_TRANSFORMERS = False
    logger.warning(
        f"sentence_transformers failed to load ({type(e).__name__}: {e}) - usually a "
        f"broken torch install. Falling back to full-text search."
    )


class QuestionEmbedder:
    def __init__(self):
        self.model_name = EMBEDDING_CONFIG['model_name']
        self.target_dim = EMBEDDING_CONFIG['embedding_dim']
        self.normalize = EMBEDDING_CONFIG['normalize_embeddings']
        self.device = self._resolve_device(EMBEDDING_CONFIG['device'])
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
            # A broken torch raises OSError here, not ImportError.
            pass
        return 'cpu'

    def _load(self) -> None:
        if not HAS_SENTENCE_TRANSFORMERS:
            return
        try:
            logger.info(f"Loading '{self.model_name}' on {self.device}...")
            self.model = SentenceTransformer(self.model_name, device=self.device)
        except Exception as e:
            logger.warning(f"Could not load '{self.model_name}' ({e}); "
                           f"GraphRAG will use full-text search instead.")
            self.model = None

    @property
    def available(self) -> bool:
        return self.model is not None

    def embed(self, text: str) -> Optional[List[float]]:
        """Returns the query vector, or None if no model is loaded."""
        if not self.model or not text:
            return None
        try:
            vector = self.model.encode(
                text, normalize_embeddings=self.normalize, show_progress_bar=False
            )
            values = vector.tolist() if hasattr(vector, 'tolist') else list(vector)
        except Exception as e:
            logger.warning(f"Embedding failed: {e}")
            return None

        # The vector index rejects a query whose length differs from the indexed
        # dimension, so a mismatch is corrected here rather than erroring - but
        # it means the configured EMBEDDING_MODEL is not the one that built the
        # index, which is worth surfacing.
        if len(values) != self.target_dim:
            logger.warning(
                f"'{self.model_name}' produced {len(values)} dims but the index "
                f"expects {self.target_dim} - check EMBEDDING_MODEL matches ingestion."
            )
            if len(values) < self.target_dim:
                values = values + [0.0] * (self.target_dim - len(values))
            else:
                values = values[:self.target_dim]
        return values


# Process-wide singleton: graph_rag.py's GraphRAG and tools.py's vector_search
# both need a QuestionEmbedder, and loading BAAI/bge-m3 twice would double the
# ~2GB memory cost and load time for a model that's stateless and safe to
# share. Whichever caller asks first triggers the load; everyone after reuses
# the same instance (or the same "unavailable" verdict, without retrying the
# load on every call).
_shared_embedder: Optional['QuestionEmbedder'] = None
_shared_embedder_attempted = False


def get_shared_embedder() -> Optional['QuestionEmbedder']:
    global _shared_embedder, _shared_embedder_attempted
    if not _shared_embedder_attempted:
        _shared_embedder_attempted = True
        try:
            candidate = QuestionEmbedder()
            _shared_embedder = candidate if candidate.available else None
        except Exception as e:
            logger.warning(f"Embedding model unavailable ({e}); using text search.")
            _shared_embedder = None
    return _shared_embedder
