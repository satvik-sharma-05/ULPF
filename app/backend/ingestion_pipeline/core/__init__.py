"""
core - everything both ingestion pipelines share.

The realtime pipeline (DNIF -> Kafka -> Neo4j) and the batch pipeline
(logs -> parser -> Neo4j) differ only in where records come from. Parsing,
entity extraction, embedding and graph writing are identical, and all live
here so the two paths can never diverge in how they interpret a log or shape
the graph.

    from core import LogParser, Neo4jWriter, EmbeddingGenerator, reassemble

Every name is resolved *lazily* (PEP 562 module __getattr__). That matters:
importing the embedding generator pulls in sentence-transformers -> transformers
-> torch, which is ~2GB and can fail outright on a machine with a broken or
CPU-incompatible torch build. The parser itself is pure standard library, so
`python -m batch.parse_logs` must be able to run on a bare Python install with
none of that present. Eager imports here would have silently made torch a hard
requirement of parsing.
"""

import importlib
from typing import Any

# Public name -> the submodule that defines it. Nothing is imported until the
# name is actually used.
_EXPORTS = {
    'LogParser': 'parser',
    'Neo4jWriter': 'neo4j_writer',
    'HAS_NEO4J_DRIVER': 'neo4j_writer',
    'EmbeddingGenerator': 'embeddings',
    'reassemble': 'multiline',
    'reassemble_text': 'multiline',
    'StreamingJSONArrayWriter': 'jsonio',
    'stream_read_records': 'jsonio',
    'schema_description': 'graph_schema',
    'ENTITY_SPECS': 'graph_schema',
    'DERIVED_RELATIONSHIPS': 'graph_schema',
    'entity_spec': 'graph_schema',
    'KAFKA_CONFIG': 'config',
    'NEO4J_CONFIG': 'config',
    'DNIF_CONFIG': 'config',
    'EMBEDDING_CONFIG': 'config',
    'PIPELINE_CONFIG': 'config',
    'QWEN_CONFIG': 'config',
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module 'core' has no attribute {name!r}")
    module = importlib.import_module(f'.{module_name}', __name__)
    value = getattr(module, name)
    globals()[name] = value          # cache, so this runs once per name
    return value


def __dir__():
    return sorted(__all__)
