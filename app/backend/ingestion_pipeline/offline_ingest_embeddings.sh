#!/bin/bash
# offline_ingest_embeddings.sh - SCRIPT 2, run on the AIRGAPPED VM, any time
# after offline_ingest_structure.sh has completed.
#
# Adds BAAI/bge-m3 embeddings to every :Log node that doesn't have one yet -
# needed for the chatbot pipeline's GraphRAG vector search. Works directly
# against Neo4j (not output.json.gz), which is what makes this safely
# interruptible: Ctrl-C it, reboot the VM, `docker compose down` it - rerun
# this exact script and it picks up from ./state/embed_checkpoint.json
# instead of starting over.
#
# This is a genuinely long job on CPU (~117M records) - realistically days,
# not hours, even on 8-16 cores. Run it inside `screen` or `tmux` so it
# survives your SSH session disconnecting:
#
#   screen -S embed
#   ./offline_ingest_embeddings.sh
#   [Ctrl-A then D to detach; `screen -r embed` to reattach later]
#
# Usage:
#   ./offline_ingest_embeddings.sh
#   ./offline_ingest_embeddings.sh --limit 100000     # quick test run first
set -euo pipefail
cd "$(dirname "$0")"

export NEO4J_PASSWORD="${NEO4J_PASSWORD:-sandboxpassword}"
EMBED_BATCH_SIZE="${EMBED_BATCH_SIZE:-256}"

# A bind-mounted FILE that doesn't exist yet gets silently created by Docker
# as a DIRECTORY, which would break the checkpoint entirely - so this mounts a
# directory and always has a real file to work with from the first run.
mkdir -p state

echo "=== Verifying Neo4j is up ==="
command -v docker >/dev/null || { echo "docker not found on PATH."; exit 1; }
docker compose --profile sandbox up -d sandbox-neo4j >/dev/null

for i in $(seq 1 30); do
    docker compose exec -T sandbox-neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" "RETURN 1" >/dev/null 2>&1 && break
    [ "$i" -eq 30 ] && { echo "Neo4j did not respond."; exit 1; }
    sleep 5
done

if [ -f state/embed_checkpoint.json ]; then
    echo "=== Resuming a previous run (state/embed_checkpoint.json found) ==="
else
    echo "=== Starting a fresh embedding pass ==="
fi

echo "=== Running (Ctrl-C any time; rerun this script to resume exactly where it stopped) ==="
docker compose run --rm \
    -v "$(pwd)/state:/app/state" \
    consumer \
    python -m batch.embed_logs \
        --batch-size "$EMBED_BATCH_SIZE" \
        --checkpoint /app/state/embed_checkpoint.json \
        --neo4j-uri bolt://sandbox-neo4j:7687 \
        --neo4j-user neo4j \
        --neo4j-password "$NEO4J_PASSWORD" \
        --neo4j-database neo4j \
        "$@"

echo
echo "=== Session ended (finished, interrupted, or --limit reached) ==="
echo "Rerun ./offline_ingest_embeddings.sh any time to continue - progress is saved in state/embed_checkpoint.json"
