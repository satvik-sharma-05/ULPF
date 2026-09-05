"""
batch - PIPELINE 2: bulk ingestion of log files already on disk.

    logs/ -> Parser -> Embeddings -> Neo4j

Three entrypoints, all taking a variable --path:

    python -m batch.parse_logs      --path ./logs --out output.json
    python -m batch.ingest_to_neo4j --input output.json
    python -m batch.run_pipeline    --path ./logs          # both, no intermediate file

Splitting parse from ingest is the default because they fail for unrelated
reasons: parsing is CPU-bound and needs no network, ingestion needs a reachable
Neo4j. Keeping output.json between them means a database problem never costs
you a multi-hour reparse. run_pipeline is there for when you don't want the
intermediate file at all.

Everything under core/ - parser, entity extraction, embeddings, graph writer -
is shared with the realtime pipeline, so both paths produce an identical graph.
"""
