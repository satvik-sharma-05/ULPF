"""
core/embeddings.py - BAAI/bge-m3 embedding generator.

Generates the 1024-dimensional vectors stored on (:Log).embedding in Neo4j,
behind a native VECTOR INDEX - that index is what the chatbot's GraphRAG
retrieval searches. Both ingestion pipelines use this module: the realtime
consumer embeds each Kafka message, the batch loader embeds in batches.

Device is auto-detected (cuda / mps / cpu). If the model weights can't be
loaded at all, it falls back to a deterministic hash-derived unit vector so
the pipeline still runs end to end - but note that those vectors carry no
semantic meaning, so vector search degrades to nonsense rather than failing
loudly. The fallback exists to keep a dev box working, not for production;
`--embed` runs on a machine without the model are the case to watch for.
"""

import math
import random
import hashlib
import logging
from typing import List, Dict, Any, Union

from .config import EMBEDDING_CONFIG

logger = logging.getLogger(__name__)

# SentenceTransformer import check
# Broad except on purpose. `import sentence_transformers` transitively imports
# torch, and a torch that is installed but unusable - a missing VC++ runtime, a
# CPU without the instructions the wheel was built for, a mismatched CUDA build
# - raises OSError from a DLL/shared-object loader, not ImportError. Catching
# only ImportError let that crash the whole process at import time, taking the
# pure-stdlib parser down with it.
try:
    from sentence_transformers import SentenceTransformer
    HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    HAS_SENTENCE_TRANSFORMERS = False
    logger.info("sentence_transformers not installed; embeddings will use the fallback vector.")
except Exception as e:
    HAS_SENTENCE_TRANSFORMERS = False
    logger.warning(
        f"sentence_transformers is installed but failed to load ({type(e).__name__}: {e}). "
        f"This is usually a broken torch install. Embeddings will use the fallback vector - "
        f"fix torch before relying on semantic search."
    )


class EmbeddingGenerator:
    def __init__(self):
        self.model_name = EMBEDDING_CONFIG['model_name']
        self.target_dim = EMBEDDING_CONFIG['embedding_dim']  # 1024 for BAAI/bge-m3
        self.normalize = EMBEDDING_CONFIG['normalize_embeddings']
        self.batch_size = EMBEDDING_CONFIG['batch_size']
        self.device = self._resolve_device(EMBEDDING_CONFIG['device'])
        self.model = None
        self.is_loaded = False

        self._initialize_model()

    def _resolve_device(self, requested_device: str) -> str:
        if requested_device != 'auto':
            return requested_device
        try:
            import torch
            if torch.cuda.is_available():
                return 'cuda'
            if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                return 'mps'
        except Exception:
            # Same reasoning as the import guard above: a broken torch raises
            # OSError here, not ImportError. Falling back to 'cpu' is harmless
            # because the model load is guarded too.
            pass
        return 'cpu'

    def _initialize_model(self):
        if not HAS_SENTENCE_TRANSFORMERS:
            logger.info("Using deterministic fallback vector generator for BAAI/bge-m3 embeddings.")
            return

        try:
            logger.info(f"Loading embedding model '{self.model_name}' on device '{self.device}'...")
            self.model = SentenceTransformer(self.model_name, device=self.device)
            self.is_loaded = True
            logger.info(f"Successfully loaded '{self.model_name}' model.")
        except Exception as e:
            logger.warning(
                f"Failed to load '{self.model_name}' ({e}). "
                f"Using offline deterministic 1024-dim embedding fallback."
            )
            self.is_loaded = False

    def generate(self, text: str) -> List[float]:
        """Generates a 1024-dimensional embedding vector for a single log string."""
        if not text:
            text = "empty log message"

        if self.is_loaded and self.model:
            try:
                vec = self.model.encode(
                    text,
                    normalize_embeddings=self.normalize,
                    show_progress_bar=False
                )
                vec_list = vec.tolist() if hasattr(vec, 'tolist') else list(vec)
                return self._ensure_dimension(vec_list)
            except Exception as e:
                logger.error(f"Error during model encoding: {e}. Falling back to offline generator.")

        return self._generate_fallback_vector(text)

    def generate_batch(self, texts: List[str]) -> List[List[float]]:
        """Generates embeddings for a list of text strings."""
        if not texts:
            return []

        if self.is_loaded and self.model:
            try:
                embeddings = self.model.encode(
                    texts,
                    batch_size=self.batch_size,
                    normalize_embeddings=self.normalize,
                    show_progress_bar=False
                )
                results = []
                for vec in embeddings:
                    vec_list = vec.tolist() if hasattr(vec, 'tolist') else list(vec)
                    results.append(self._ensure_dimension(vec_list))
                return results
            except Exception as e:
                logger.error(f"Batch encoding failed ({e}). Using fallback for batch.")

        return [self._generate_fallback_vector(t) for t in texts]

    def enrich_log(self, log_data: Dict[str, Any]) -> Dict[str, Any]:
        """Attaches 1024-dim vector embedding to the log dictionary."""
        text_to_embed = log_data.get('normalized_message') or log_data.get('message') or log_data.get('raw_message', '')
        embedding_vector = self.generate(text_to_embed)
        log_data['embedding'] = embedding_vector
        return log_data

    def _ensure_dimension(self, vector: List[float]) -> List[float]:
        """Ensures vector size matches target dimension (1024)."""
        current_len = len(vector)
        if current_len == self.target_dim:
            return vector
        elif current_len < self.target_dim:
            padding = [0.0] * (self.target_dim - current_len)
            return vector + padding
        else:
            return vector[:self.target_dim]

    def _generate_fallback_vector(self, text: str) -> List[float]:
        """Generates a deterministic 1024-dim L2-normalized float vector based on text hash."""
        seed = int(hashlib.sha256(text.encode('utf-8')).hexdigest(), 16) % (2**32)
        rng = random.Random(seed)
        vec = [rng.gauss(0, 1) for _ in range(self.target_dim)]
        
        # Normalize to unit length L2 norm
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

