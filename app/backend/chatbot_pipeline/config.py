"""
config.py - Chatbot pipeline configuration.

This pipeline is deliberately standalone: its only dependency is a Neo4j
instance that the ingestion pipeline has already populated. It does not import
from ../ingestion_pipeline, so it can be deployed, scaled and restarted on its
own - and it discovers the graph schema by introspecting the live database
(see schema_introspect.py) rather than duplicating the ingestion pipeline's
schema declaration, which would silently drift the moment either side changed.
"""

import os

# Load this service's .env before ANY os.getenv below reads a default.
#
# Without this, .env only worked for the FastAPI entry point, which calls
# load_dotenv() itself. Every other way in - a test, a script, `python -c`,
# the ledger CLI - silently ignored the file and used the hard-coded
# fallbacks, so a correctly configured service still tried to reach
# bolt://neo4j.internal.example and failed with a network timeout that named nothing.
# The ingestion pipeline fixed this in core/config.py; these two did not have
# the same fix.
#
# python-dotenv is optional, and an explicit environment variable still wins
# over the file (override=False), so docker-compose's `environment:` block and
# `NEO4J_URI=... python ...` both behave exactly as before.
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
try:
    from dotenv import load_dotenv
    _ENV_LOADED = load_dotenv(_ENV_PATH, override=False)
except ImportError:
    _ENV_LOADED = False

if not _ENV_LOADED and not os.getenv('NEO4J_URI'):
    # Say so rather than silently reaching for the built-in default. That
    # default is a real enterprise address: on a machine without a .env the
    # operator would otherwise see a timeout to an IP they may not recognise,
    # with nothing pointing at the actual cause.
    import warnings
    warnings.warn(
        'No .env found at %s and NEO4J_URI is unset - falling back to the '
        'built-in defaults in this file. Copy .env.example to .env to '
        'configure this service.' % _ENV_PATH,
        RuntimeWarning, stacklevel=2)



def _bool_env(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ('true', '1', 'yes')


# ============================================================================
# NEO4J - the graph the ingestion pipeline built
# Community Edition only ever has a database named "neo4j"; a custom name is
# rejected unless the target is Enterprise Edition.
# ============================================================================
NEO4J_CONFIG = {
    'uri': os.getenv('NEO4J_URI', 'bolt://localhost:7687'),
    'user': os.getenv('NEO4J_USER', 'neo4j'),
    'password': os.getenv('NEO4J_PASSWORD', 'password'),
    'database': os.getenv('NEO4J_DATABASE', 'neo4j'),
    'max_connection_lifetime': 3600,
    'max_connection_pool_size': 20,
}

# ============================================================================
# LLM - used for routing, Cypher generation and answer synthesis.
# Every stage has a deterministic fallback, so the chatbot still answers
# (with retrieved rows and log lines instead of prose) when none is reachable.
# ============================================================================
LLM_CONFIG = {
    'provider': os.getenv('CHATBOT_LLM_PROVIDER', 'ollama'),
    'ollama_host': os.getenv('OLLAMA_HOST', 'http://localhost:11434'),
    'ollama_model': os.getenv('OLLAMA_MODEL', 'qwen3:14b'),
    # OPEN_ROUTER_API_KEY accepted as an alias since that's the name already
    # used in some local .env files here; OPENROUTER_API_KEY wins if both are set.
    'openrouter_api_key': os.getenv('OPENROUTER_API_KEY', os.getenv('OPEN_ROUTER_API_KEY', '')),
    'openrouter_model': os.getenv('OPENROUTER_MODEL', 'openai/gpt-4o-mini'),
    'groq_api_key': os.getenv('GROQ_API_KEY', ''),
    # Groq retires hosted models fairly aggressively - the previous default
    # (llama-3.3-70b-versatile) now 404s with model_not_found on a perfectly
    # valid key. If this one goes the same way, list what your key can
    # actually reach with:
    #   curl -H "Authorization: Bearer $GROQ_API_KEY" \
    #        https://api.groq.com/openai/v1/models
    'groq_model': os.getenv('GROQ_MODEL', 'openai/gpt-oss-120b'),
    'timeout': int(os.getenv('CHATBOT_TIMEOUT', '120')),
}

# ============================================================================
# EMBEDDINGS - must match what the ingestion pipeline stored on (:Log).embedding.
# A different model or dimension here silently returns meaningless neighbours:
# the vector index accepts any 1024-float query, it just can't tell you the
# vectors came from a different embedding space.
# ============================================================================
EMBEDDING_CONFIG = {
    'model_name': os.getenv('EMBEDDING_MODEL', 'BAAI/bge-m3'),
    'device': os.getenv('EMBEDDING_DEVICE', 'auto'),
    'embedding_dim': int(os.getenv('EMBEDDING_DIM', '1024')),
    'normalize_embeddings': True,
    'vector_index': os.getenv('NEO4J_VECTOR_INDEX', 'log_embedding_index'),
    'fulltext_index': os.getenv('NEO4J_FULLTEXT_INDEX', 'log_message_fulltext'),
}

# ============================================================================
# RERANKER - cross-encoder re-scoring of pooled evidence before it reaches the
# final-answer prompt. A cross-encoder scores (question, candidate) pairs
# jointly, which is more accurate than the bi-encoder similarity scores
# vector_search/fulltext_search already return (those embed the question and
# each candidate independently, then compare vectors - cheap enough to rank
# thousands of candidates, but less precise). The planner can call several
# tools per question; without a reranker, the final context is just each
# tool's own top-k concatenated in call order, so noisy or marginal results
# from an early tool call crowd out better evidence a later tool found.
#
# Uses sentence-transformers' CrossEncoder, already a dependency for
# embeddings - no new package needed, just a second model.
# ============================================================================
RERANKER_CONFIG = {
    'enabled': _bool_env('CHATBOT_RERANKER_ENABLED', 'true'),
    'model_name': os.getenv('RERANKER_MODEL', 'BAAI/bge-reranker-v2-m3'),
    'device': os.getenv('RERANKER_DEVICE', 'auto'),
    # How many of the pooled candidates survive reranking into the final
    # answer prompt - independent of how many each individual tool retrieved.
    'top_n': int(os.getenv('CHATBOT_RERANKER_TOP_N', '15')),
}

# ============================================================================
# RETRIEVAL / ROUTING
# ============================================================================
CHATBOT_CONFIG = {
    # Below this confidence the router stops guessing between text2cypher and
    # graphrag, and runs both instead.
    'router_confidence_threshold': float(os.getenv('CHATBOT_ROUTER_THRESHOLD', '0.6')),
    'graphrag_top_k': int(os.getenv('CHATBOT_GRAPHRAG_TOP_K', '8')),
    'graphrag_neighbour_limit': int(os.getenv('CHATBOT_GRAPHRAG_NEIGHBOURS', '16')),
    'text2cypher_row_limit': int(os.getenv('CHATBOT_CYPHER_ROW_LIMIT', '100')),
    'max_context_chars': int(os.getenv('CHATBOT_MAX_CONTEXT_CHARS', '12000')),
    # Introspected schema is cached for the process lifetime; the graph's shape
    # changes only when the ingestion pipeline adds a new entity type.
    'introspect_schema': _bool_env('CHATBOT_INTROSPECT_SCHEMA', 'true'),
    'schema_sample_limit': int(os.getenv('CHATBOT_SCHEMA_SAMPLE', '3')),

    # Planner (planner.py) - drives the "auto" mode. Hard caps bound the
    # worst case cost of a plan that never converges: each round costs one LLM
    # call for planning plus however many tool calls it requests, so
    # max_rounds x (tools per round) is the real ceiling on latency.
    'planner_max_rounds': int(os.getenv('CHATBOT_PLANNER_MAX_ROUNDS', '3')),
    'planner_max_tool_calls': int(os.getenv('CHATBOT_PLANNER_MAX_TOOL_CALLS', '6')),
    'planner_max_rows_per_tool': int(os.getenv('CHATBOT_PLANNER_MAX_ROWS_PER_TOOL', '20')),
}

# ============================================================================
# API - the FastAPI backend the demo frontend talks to (api.py). Not used by
# chat.py, which talks to LogChatbot directly.
# ============================================================================
API_CONFIG = {
    'host': os.getenv('API_HOST', '0.0.0.0'),
    'port': int(os.getenv('API_PORT', '8000')),
    # Comma-separated list, or '*' for any origin. '*' is fine for a demo on a
    # private VM; tighten this before putting the API anywhere less trusted.
    'cors_origins': [o.strip() for o in os.getenv('API_CORS_ORIGINS', '*').split(',') if o.strip()],
}
