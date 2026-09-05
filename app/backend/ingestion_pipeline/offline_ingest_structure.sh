#!/bin/bash
# offline_ingest_structure.sh - SCRIPT 1, run on the AIRGAPPED VM.
#
# Loads the pre-built images (from build_offline_bundle.sh), starts Neo4j, and
# ingests output.json.gz - structure, entities, all relationships - WITHOUT
# embeddings. This is the fast path: it gets the full graph queryable today.
# No internet access is used or needed anywhere in this script.
#
# Run offline_ingest_embeddings.sh afterwards, whenever you're ready to spend
# the (multi-day) CPU time adding semantic search.
#
# Usage:
#   ./offline_ingest_structure.sh
#   NEO4J_HEAP_SIZE=10G NEO4J_PAGECACHE_SIZE=10G ./offline_ingest_structure.sh
set -euo pipefail
cd "$(dirname "$0")"

# Sized for a 16-32GB VM: 6G+6G=12G leaves headroom for the OS and the
# ingestion container on a 16GB box, and can be raised if you have 32GB
# (e.g. NEO4J_HEAP_SIZE=10G NEO4J_PAGECACHE_SIZE=12G). This graph is large -
# ~117M Log nodes plus several hundred million entity nodes/relationships -
# so the defaults docker-compose.yml ships with (4G/2G) are undersized here.
export NEO4J_HEAP_SIZE="${NEO4J_HEAP_SIZE:-6G}"
export NEO4J_PAGECACHE_SIZE="${NEO4J_PAGECACHE_SIZE:-6G}"
export NEO4J_PASSWORD="${NEO4J_PASSWORD:-sandboxpassword}"
OUTPUT_JSON="${OUTPUT_JSON:-output.json.gz}"
BATCH_SIZE="${BATCH_SIZE:-2000}"

echo "=== 1. Verifying prerequisites ==="
[ -f "$OUTPUT_JSON" ] || { echo "Missing $OUTPUT_JSON in $(pwd) - copy it here first."; exit 1; }
command -v docker >/dev/null || { echo "docker not found on PATH."; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose (v2) not found."; exit 1; }

echo "=== 2. Loading pre-built images (skipped for any tar not present - assumed already loaded) ==="
for tar in ingestion_image.tar neo4j_image.tar; do
    if [ -f "$tar" ]; then
        echo "Loading $tar ..."
        docker load -i "$tar"
    else
        echo "  (no $tar here - assuming its image is already loaded)"
    fi
done
docker image inspect log-ingestion-pipeline:latest >/dev/null 2>&1 \
    || { echo "log-ingestion-pipeline:latest is not loaded. Run build_offline_bundle.sh on a connected machine first."; exit 1; }
docker image inspect neo4j:5-community >/dev/null 2>&1 \
    || { echo "neo4j:5-community is not loaded. Run build_offline_bundle.sh on a connected machine first."; exit 1; }

echo "=== 3. Starting Neo4j (heap=${NEO4J_HEAP_SIZE} pagecache=${NEO4J_PAGECACHE_SIZE}, persistent volume) ==="
docker compose --profile sandbox up -d sandbox-neo4j

echo "=== 4. Waiting for Neo4j to accept connections ==="
for i in $(seq 1 60); do
    if docker compose exec -T sandbox-neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" "RETURN 1" >/dev/null 2>&1; then
        echo "Neo4j is ready."
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "Neo4j did not become ready within 5 minutes."
        echo "Check: docker compose logs sandbox-neo4j"
        exit 1
    fi
    sleep 5
done

echo "=== 5. Ingesting $OUTPUT_JSON (structure + typed entities, NO embeddings) ==="
docker compose run --rm \
    -v "$(pwd)/$OUTPUT_JSON:/app/data/output.json.gz:ro" \
    consumer \
    python -m batch.ingest_to_neo4j \
        --input /app/data/output.json.gz \
        --batch-size "$BATCH_SIZE" \
        --neo4j-uri bolt://sandbox-neo4j:7687 \
        --neo4j-user neo4j \
        --neo4j-password "$NEO4J_PASSWORD" \
        --neo4j-database neo4j

echo
echo "=== Done ==="
echo "Neo4j Browser: http://<vm-ip>:7474   (user: neo4j)"
echo "The chatbot can already answer text-to-Cypher questions against this graph."
echo "For GraphRAG / semantic search, run: ./offline_ingest_embeddings.sh"
