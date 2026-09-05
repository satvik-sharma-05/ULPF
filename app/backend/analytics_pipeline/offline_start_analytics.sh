#!/bin/bash
# offline_start_analytics.sh - Run on the AIRGAPPED VM.
#
# Loads the pre-built analytics-backend image and starts it. No internet
# access is used or needed. Unlike chatbot_pipeline, there's no Ollama check
# here - this service never talks to an LLM, only Neo4j.
#
# PREREQUISITE: the ingestion stack's Neo4j must already be running and
# populated - reached here via its host-published port (localhost:7687), not
# a shared Docker network. If you haven't already:
#
#   cd ../ingestion_pipeline && ./offline_ingest_structure.sh
#
# Usage:
#   ./offline_start_analytics.sh
#   NEO4J_PASSWORD=mypassword ./offline_start_analytics.sh
set -euo pipefail
cd "$(dirname "$0")"

export NEO4J_PASSWORD="${NEO4J_PASSWORD:-sandboxpassword}"

echo "=== 1. Verifying prerequisites ==="
command -v docker >/dev/null || { echo "docker not found on PATH."; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose (v2) not found."; exit 1; }

if ! docker ps --format '{{.Names}}' | grep -q '^sandbox-neo4j$'; then
    echo "Warning: sandbox-neo4j isn't currently running. The backend will start anyway"
    echo "but every panel will show zeros until Neo4j is up:"
    echo "  cd ../ingestion_pipeline && docker compose --profile sandbox up -d sandbox-neo4j"
fi

echo "=== 2. Loading the pre-built image (skipped if not present - assumed already loaded) ==="
if [ -f analytics_backend_image.tar ]; then
    echo "Loading analytics_backend_image.tar ..."
    docker load -i analytics_backend_image.tar
else
    echo "  (no analytics_backend_image.tar here - assuming the image is already loaded)"
fi
docker image inspect analytics-backend:latest >/dev/null 2>&1 \
    || { echo "analytics-backend:latest is not loaded. Build it on a connected machine first."; exit 1; }

echo "=== 3. Starting the analytics backend ==="
docker compose up -d

echo "=== 4. Waiting for the backend to be ready ==="
for i in $(seq 1 30); do
    if curl -sf http://localhost:8010/api/analytics/health >/dev/null 2>&1; then
        echo "Backend is ready."
        break
    fi
    [ "$i" -eq 30 ] && { echo "Backend did not become ready in time. Check: docker compose logs analytics-backend"; exit 1; }
    sleep 2
done

echo
echo "=== Health check ==="
curl -s http://localhost:8010/api/analytics/health && echo
echo
echo "=== Done ==="
echo "Backend : http://<vm-ip>:8010/api/analytics/health"
echo "Dashboard: the Analytics tab of the chatbot frontend at http://<vm-ip>:3000"
echo
echo "Logs: docker compose logs -f analytics-backend"
echo "Stop: docker compose down"
