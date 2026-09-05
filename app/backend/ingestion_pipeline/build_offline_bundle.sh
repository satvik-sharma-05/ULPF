#!/bin/bash
# build_offline_bundle.sh
#
# Run this ONCE on a CONNECTED machine (needs internet + Docker) - NOT on the
# airgapped VM. It builds the ingestion image (which bakes in BAAI/bge-m3 +
# CPU-only torch + the neo4j driver, so the airgapped VM never needs network
# access at runtime), pulls Neo4j, and saves both as tar files.
#
# Rebuild whenever the code under core/ or batch/ changes - the tar is a
# snapshot, not a live link to this directory.
set -euo pipefail
cd "$(dirname "$0")"

command -v docker >/dev/null || { echo "docker not found."; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose (v2) not found."; exit 1; }

echo "=== Building the ingestion image (downloads ~3GB: torch + BAAI/bge-m3) ==="
docker compose build consumer

echo "=== Pulling Neo4j Community Edition ==="
docker pull neo4j:5-community

echo "=== Saving images to tar files ==="
docker save log-ingestion-pipeline:latest -o ingestion_image.tar
docker save neo4j:5-community -o neo4j_image.tar

echo
echo "Done. Copy these to the airgapped VM, into the SAME ingestion_pipeline/ folder"
echo "(alongside docker-compose.yml, core/, batch/):"
echo "  - ingestion_image.tar   ($(du -h ingestion_image.tar | cut -f1))"
echo "  - neo4j_image.tar       ($(du -h neo4j_image.tar | cut -f1))"
echo "  - output.json.gz        (the parsed corpus)"
echo
echo "Then on the VM, run: ./offline_ingest_structure.sh"
