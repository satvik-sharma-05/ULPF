"""
producer_service.py - DNIF -> Kafka producer role.

Polls the DNIF API for new raw log events and pushes each one, untouched,
onto the Kafka `raw-logs` topic. Deliberately does no parsing here - that
work belongs to consumer_service.py so it can be scaled independently and
so no log is ever lost between fetch and parse (Kafka durably buffers it).
"""

import time
import logging

from core.config import KAFKA_CONFIG, DNIF_CONFIG
from .base_service import BaseService
from .dnif_api import DNIFClient
from .kafka_producer import KafkaProducer

logger = logging.getLogger("ProducerService")


class ProducerService(BaseService):
    service_name = "producer"

    def __init__(self):
        super().__init__()
        self.dnif_client = DNIFClient()
        self.producer = KafkaProducer()

    def run(self):
        self.producer.connect()
        logger.info("Producer entering DNIF poll loop...")
        poll_interval = DNIF_CONFIG['poll_interval']

        try:
            while self.running:
                loop_start = time.time()

                raw_logs = self.dnif_client.fetch_logs(limit=DNIF_CONFIG['batch_size'])
                if raw_logs:
                    logger.info(f"Retrieved {len(raw_logs)} raw log items from DNIF.")

                for raw_item in raw_logs:
                    if not self.running:
                        break
                    try:
                        self.producer.send(KAFKA_CONFIG['topic_raw_logs'], raw_item)
                        self.processed_count += 1
                    except Exception as e:
                        self.error_count += 1
                        logger.error(f"Failed to publish raw log to Kafka: {e}", exc_info=True)

                if self.processed_count and self.processed_count % 10 == 0:
                    self._save_checkpoint()

                elapsed = time.time() - loop_start
                time.sleep(max(0.1, poll_interval - elapsed))
        except KeyboardInterrupt:
            logger.info("Producer interrupted by user.")
        finally:
            self.shutdown()

    def shutdown(self):
        self.running = False
        self.producer.flush()
        super().shutdown()
