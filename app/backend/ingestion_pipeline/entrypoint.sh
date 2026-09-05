#!/bin/bash
# ============================================================================
# INGESTION PIPELINE ENTRYPOINT
# Starts the realtime role (DNIF -> Kafka -> Neo4j) named by SERVICE_ROLE.
# Any command passed to `docker run`/`docker exec` runs instead - that is how
# the batch pipeline is invoked inside a container.
# ============================================================================

set -e

SERVICE_ROLE=${SERVICE_ROLE:-consumer}

echo "========================================="
echo "  Log Ingestion Pipeline"
echo "  Version: 2.0.0"
echo "  Role: $SERVICE_ROLE"
echo "  Started at: $(date)"
echo "========================================="
echo ""
echo "[INIT] Python: $(python --version 2>&1)"
mkdir -p /app/logs /app/output
echo "[OK] Directories ready"

# An explicit command (batch.parse_logs, batch.run_pipeline, a shell) runs
# instead - the dependency waits and realtime banner below don't apply to it.
if [ "$#" -gt 0 ]; then
    echo ""
    exec "$@"
fi

# ============================================================================
# WAIT FOR DEPENDENCIES (only the ones this role actually talks to)
# ============================================================================
echo ""
echo "[INIT] Checking dependencies..."

wait_for_tcp() {
    local label="$1" host="$2" port="$3"
    echo "[$label] Waiting for $host:$port..."
    for i in $(seq 1 30); do
        if nc -z "$host" "$port" 2>/dev/null; then
            echo "[$label] Connected!"
            return 0
        fi
        if [ "$i" -eq 30 ]; then
            echo "[$label] Warning: could not reach $host:$port after 60s, continuing anyway"
        fi
        sleep 2
    done
}

if [ "$ENABLE_KAFKA" = "true" ]; then
    FIRST_BROKER=${KAFKA_BOOTSTRAP_SERVERS%%,*}
    wait_for_tcp "KAFKA" "${FIRST_BROKER%%:*}" "${FIRST_BROKER##*:}"
fi

# Only the consumer writes to Neo4j; the producer just fills Kafka.
if [ "$ENABLE_NEO4J" = "true" ] && [ "$SERVICE_ROLE" != "producer" ]; then
    NEO4J_HOST=${NEO4J_URI#bolt://}
    NEO4J_HOST=${NEO4J_HOST#neo4j://}
    NEO4J_HOST=${NEO4J_HOST%:*}
    wait_for_tcp "NEO4J" "$NEO4J_HOST" "7687"
fi

echo ""
echo "[CONFIG] Pipeline configuration:"
echo "  Role: $SERVICE_ROLE"
echo "  Kafka: $KAFKA_BOOTSTRAP_SERVERS"
echo "  Neo4j: $NEO4J_URI"
echo "  Embeddings: $EMBEDDING_MODEL"
echo "  Log Level: $LOG_LEVEL"
echo ""
echo "========================================="
echo "[START] Realtime pipeline (role: $SERVICE_ROLE)..."
echo "========================================="
echo ""

exec python -m realtime.run --role "$SERVICE_ROLE"
