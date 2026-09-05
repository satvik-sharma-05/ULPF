#!/bin/bash
# offline_start_chatbot.sh - Run on the AIRGAPPED VM.
#
# Loads the pre-built images (from build_offline_bundle.sh) and starts the
# chatbot stack: the FastAPI backend and the React frontend. No internet
# access is used or needed anywhere in this script.
#
# Ollama is NOT started here - it's expected to already be running natively
# on this VM (systemd / `ollama serve`, port 11434, model qwen3:14b already
# pulled). Both containers run with `network_mode: host` specifically so they
# can reach it: `ollama serve` binds to 127.0.0.1 by default, which only a
# truly host-networked container can reach (host.docker.internal only routes
# to the host's external address, not literally 127.0.0.1).
#
# PREREQUISITE: the ingestion stack's Neo4j must already be running and
# populated - reached here via its host-published port (localhost:7687), not
# a shared Docker network. If you haven't already:
#
#   cd ../ingestion_pipeline && ./offline_ingest_structure.sh
#
# Usage:
#   ./offline_start_chatbot.sh
#   NEO4J_PASSWORD=mypassword ./offline_start_chatbot.sh
set -euo pipefail
cd "$(dirname "$0")"

export NEO4J_PASSWORD="${NEO4J_PASSWORD:-sandboxpassword}"
OLLAMA_HOST_URL="${OLLAMA_HOST:-http://localhost:11434}"
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3:14b}"

echo "=== 1. Verifying prerequisites ==="
command -v docker >/dev/null || { echo "docker not found on PATH."; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose (v2) not found."; exit 1; }

if ! docker ps --format '{{.Names}}' | grep -q '^sandbox-neo4j$'; then
    echo "Warning: sandbox-neo4j isn't currently running. The backend will start anyway"
    echo "but every question will fail until Neo4j is up:"
    echo "  cd ../ingestion_pipeline && docker compose --profile sandbox up -d sandbox-neo4j"
fi

echo "=== 2. Checking the native Ollama is reachable and has ${OLLAMA_MODEL} ==="
if ! curl -sf "${OLLAMA_HOST_URL}/api/tags" >/dev/null 2>&1; then
    echo "Could not reach Ollama at ${OLLAMA_HOST_URL}."
    echo "Check it's running: systemctl status ollama   (or however it's managed here)"
    exit 1
fi
if ! curl -s "${OLLAMA_HOST_URL}/api/tags" | grep -q "$OLLAMA_MODEL"; then
    echo "Ollama is up, but '${OLLAMA_MODEL}' isn't in its model list."
    echo "Check with: ollama list"
    echo "Continuing anyway - the chatbot will just get empty LLM responses until this is fixed."
fi
echo "Ollama OK at ${OLLAMA_HOST_URL}."

echo "=== 3. Loading pre-built images (skipped for any tar not present - assumed already loaded) ==="
for tar in chatbot_backend_image.tar chatbot_frontend_image.tar; do
    if [ -f "$tar" ]; then
        echo "Loading $tar ..."
        docker load -i "$tar"
    else
        echo "  (no $tar here - assuming its image is already loaded)"
    fi
done
for image in chatbot-backend:latest chatbot-frontend:latest; do
    docker image inspect "$image" >/dev/null 2>&1 \
        || { echo "$image is not loaded. Run build_offline_bundle.sh on a connected machine first."; exit 1; }
done

echo "=== 4. Starting the chatbot stack (backend, frontend) ==="
docker compose up -d

echo "=== 5. Waiting for the backend to be ready ==="
for i in $(seq 1 30); do
    if curl -sf http://localhost:8000/api/health >/dev/null 2>&1; then
        echo "Backend is ready."
        break
    fi
    [ "$i" -eq 30 ] && { echo "Backend did not become ready in time. Check: docker compose logs backend"; exit 1; }
    sleep 3
done

echo
echo "=== Health check ==="
curl -s http://localhost:8000/api/health && echo
echo
echo "=== Done ==="
echo "Frontend : http://<vm-ip>:3000"
echo "Backend  : http://<vm-ip>:8000/api/health"
echo
echo "Logs: docker compose logs -f backend    (or frontend)"
echo "Stop: docker compose down"
