#!/usr/bin/env sh
# Load every shipped image on the air-gapped VM. Nothing is downloaded.
set -e
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found. Install Docker Engine on this VM first." >&2
  exit 1
fi

if [ ! -d images ] || [ -z "$(ls -A images/*.tar 2>/dev/null)" ]; then
  echo "images/ is missing or empty." >&2
  echo "Build them on an internet-connected machine with build_images.ps1," >&2
  echo "then copy this whole folder across again." >&2
  exit 1
fi

for tar in images/*.tar; do
  echo "==> loading $tar"
  docker load -i "$tar"
done

echo
echo "Loaded:"
docker images --format '  {{.Repository}}:{{.Tag}}  {{.Size}}' | grep -E 'ulpf-|neo4j|ollama' || true

# ---------------------------------------------------------------------------
# The Ollama weights are NOT inside the images.
#
# They are mounted (see docker-compose.yml), because baking them produced a
# 26GB image and a 26GB tar - the same weights shipped twice. Mounted, they
# travel once, as files. Which means they have to actually be here, and the
# most likely reason chat fails on a fresh deployment is that this folder was
# left behind during the copy. Checked loudly rather than discovered on the
# first question.
# ---------------------------------------------------------------------------
MODELS=backend/ollama/models
echo
if [ -d "$MODELS/blobs" ] && [ -n "$(ls -A "$MODELS/blobs" 2>/dev/null)" ]; then
  echo "Ollama model store: present ($(du -sh "$MODELS" 2>/dev/null | cut -f1))"
else
  echo "WARNING: $MODELS is empty or missing." >&2
  echo "  Everything else will start, and the chat will fail on the first" >&2
  echo "  question because Ollama has no model to answer with." >&2
  echo "  Copy the model store from the build machine into $MODELS," >&2
  echo "  or run 'ollama pull <model>' there and copy ~/.ollama/models." >&2
fi

echo
echo "Next:"
echo "  1. edit the CHANGE_ME lines in backend/*/.env   (Neo4j password)"
echo "  2. export NEO4J_PASSWORD='<same password>'"
echo "  3. docker compose up -d"
