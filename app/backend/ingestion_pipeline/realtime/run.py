"""
realtime/run.py - Entrypoint for PIPELINE 1 (DNIF -> Kafka -> Neo4j).

    python -m realtime.run --role producer     # DNIF  -> Kafka
    python -m realtime.run --role consumer     # Kafka -> parse -> embed -> Neo4j

The role also comes from SERVICE_ROLE, which is how docker-compose sets it, so
the same image runs both containers:

    SERVICE_ROLE=producer python -m realtime.run

Run one producer and one or more consumers. Scaling consumers is the supported
way to keep up with DNIF volume:

    docker compose up -d --scale consumer=3

To load log files already on disk, use the batch pipeline instead:

    python -m batch.run_pipeline --path ./logs
"""

import argparse
import logging
import os
import signal
import sys

# Lets the module run as a plain script from the pipeline root as well as via
# `python -m realtime.run`, so the docker entrypoint and a shell both work.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROLES = ('producer', 'consumer')


def build_service(role: str):
    from realtime.consumer_service import ConsumerService
    from realtime.producer_service import ProducerService

    return {'producer': ProducerService, 'consumer': ConsumerService}[role]()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('--role', choices=ROLES, default=None,
                    help="Which half of the pipeline to run (default: $SERVICE_ROLE)")
    ap.add_argument('--log-level', default=os.getenv('LOG_LEVEL', 'INFO'))
    args = ap.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format='%(asctime)s [%(levelname)s] %(name)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logger = logging.getLogger("realtime")

    role = (args.role or os.getenv('SERVICE_ROLE', '')).strip().lower()
    if role not in ROLES:
        # No silent default: running a producer when a consumer was intended
        # would quietly fill Kafka and write nothing to the graph.
        logger.error(
            f"Unknown or missing role {role!r}. Pass --role producer|consumer, "
            f"or set SERVICE_ROLE."
        )
        sys.exit(2)

    logger.info("=" * 65)
    logger.info(f"Realtime ingestion pipeline - role: {role}")
    logger.info("DNIF -> Kafka -> Parser -> Embeddings -> Neo4j")
    logger.info("=" * 65)

    service = build_service(role)

    def handle_signal(sig, _frame):
        logger.info(f"Received signal {sig}; shutting down cleanly...")
        service.request_stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    service.run()


if __name__ == '__main__':
    main()
