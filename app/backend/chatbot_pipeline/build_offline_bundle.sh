#!/bin/bash
# build_offline_bundle.sh
#
# Run this ONCE on a CONNECTED Ubuntu machine (needs internet + Docker) - NOT
# on the airgapped VM. Builds the two chatbot-stack images (backend, frontend)
# and saves them as tar files to carry across the air gap.
#
# Ollama is NOT built here: it already runs natively on the target VM
# (systemd / `ollama serve`, qwen3:14b already pulled) - there is nothing to
# build or transfer for it.
#
# Rebuild whenever chatbot_pipeline/ or ../frontend/ code changes - the tars
# are a snapshot, not a live link to these directories.
set -euo pipefail
cd "$(dirname "$0")"

command -v docker >/dev/null || { echo "docker not found."; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose (v2) not found."; exit 1; }

echo "=== 1. Building the backend image (downloads ~4GB: torch + BAAI/bge-m3 + BAAI/bge-reranker-v2-m3) ==="
docker compose build backend

echo "=== 2. Building the frontend image ==="
docker compose build frontend

echo "=== 3. Saving images to tar files ==="
docker save chatbot-backend:latest  -o chatbot_backend_image.tar
docker save chatbot-frontend:latest -o chatbot_frontend_image.tar

echo
echo "Done. Copy these to the airgapped VM, into the SAME chatbot_pipeline/ folder"
echo "(alongside docker-compose.yml) - the ingestion_pipeline/ folder should already"
echo "be there too, with the graph already ingested:"
echo "  - chatbot_backend_image.tar   ($(du -h chatbot_backend_image.tar | cut -f1))"
echo "  - chatbot_frontend_image.tar  ($(du -h chatbot_frontend_image.tar | cut -f1))"
echo
echo "Then on the VM, run: ./offline_start_chatbot.sh"
