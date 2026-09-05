#!/bin/bash
# offline_ingest_sample.sh - Run on the AIRGAPPED VM (or any Docker host).
#
# Quick-scale alternative to offline_ingest_structure.sh: ingests only the
# first LIMIT records of output.json.gz, WITH embeddings, wiping any existing
# data first. Takes minutes, not hours - the right choice when the goal is
# validating/demoing the chatbot rather than loading the full corpus.
#
# --embed has no checkpoint/resume logic (see ingest_to_neo4j.py), which is
# exactly why this is unsuitable for the full corpus but is fine here: at a
# few thousand records it finishes long before a resume could ever be needed.
#
# For the full corpus, use offline_ingest_structure.sh (structure only, fast)
# followed by offline_ingest_embeddings.sh (adds embeddings, resumable,
# realistically days on CPU) instead of this script.
#
# Usage:
#   ./offline_ingest_sample.sh                  # first 5000 records
#   LIMIT=20000 ./offline_ingest_sample.sh      # first 20000 records
#   NEO4J_PASSWORD=mypassword LIMIT=2000 ./offline_ingest_sample.sh
set -euo pipefail
cd "$(dirname "$0")"

export NEO4J_PASSWORD="${NEO4J_PASSWORD:-sandboxpassword}"
OUTPUT_JSON="${OUTPUT_JSON:-output.json.gz}"
LIMIT="${LIMIT:-5000}"

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
    || { echo "log-ingestion-pipeline:latest is not loaded. Run build_offline_images.ps1 on a connected machine first."; exit 1; }
docker image inspect neo4j:5-community >/dev/null 2>&1 \
    || { echo "neo4j:5-community is not loaded. Run build_offline_images.ps1 on a connected machine first."; exit 1; }

echo "=== 3. Starting Neo4j (sandbox profile, persistent volume) ==="
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

echo "=== 5. Wiping existing data and ingesting the first $LIMIT records of $OUTPUT_JSON, WITH embeddings ==="
docker compose run --rm \
    -v "$(pwd)/$OUTPUT_JSON:/app/data/output.json.gz:ro" \
    consumer \
    python -m batch.ingest_to_neo4j \
        --input /app/data/output.json.gz \
        --limit "$LIMIT" \
        --embed \
        --wipe \
        --neo4j-uri bolt://sandbox-neo4j:7687 \
        --neo4j-user neo4j \
        --neo4j-password "$NEO4J_PASSWORD" \
        --neo4j-database neo4j

echo
echo "=== Done ==="
echo "Neo4j Browser: http://<vm-ip>:7474   (user: neo4j)"
echo "Next: cd ../chatbot_pipeline && ./offline_start_chatbot.sh"
