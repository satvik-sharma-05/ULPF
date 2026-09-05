"""
consumer_service.py - Kafka -> Parser -> Embeddings -> Neo4j consumer role.

Reads raw log events off the Kafka `raw-logs` topic (populated by
producer_service.py), parses them, generates embeddings, persists the
result to Neo4j, and republishes the enriched record to `parsed-logs` for any
downstream stream consumer (live dashboard, websocket feed, etc). The Kafka
offset is only committed after the log has been fully persisted, so a crash
mid-processing causes a safe re-delivery instead of silent data loss.

Neo4j is the only datastore - the embedding vector goes onto (:Log).embedding
behind a native vector index, so there is nothing else to keep in sync.
"""

import logging

from core.config import KAFKA_CONFIG, PIPELINE_CONFIG
from .base_service import BaseService
from .kafka_consumer import KafkaConsumer
from .kafka_producer import KafkaProducer
from core.parser import LogParser
from core.embeddings import EmbeddingGenerator
from core.neo4j_writer import Neo4jWriter

logger = logging.getLogger("ConsumerService")


class ConsumerService(BaseService):
    service_name = "consumer"

    def __init__(self):
        super().__init__()
        self.consumer = KafkaConsumer(topic=KAFKA_CONFIG['topic_raw_logs'])
        self.producer = KafkaProducer()
        self.parser = LogParser()
        self.embedding_gen = EmbeddingGenerator() if PIPELINE_CONFIG['enable_embeddings'] else None
        self.neo4j_writer = Neo4jWriter() if PIPELINE_CONFIG['enable_neo4j'] else None

    def run(self):
        self.consumer.connect()
        self.producer.connect()
        if self.neo4j_writer:
            self.neo4j_writer.connect()

        logger.info("Consumer entering Kafka poll loop...")
        poll_timeout = PIPELINE_CONFIG['poll_timeout_seconds']

        try:
            while self.running:
                messages = self.consumer.poll(timeout=poll_timeout)
                for raw_item in messages:
                    success = self._process_single_log(raw_item)
                    if success:
                        self.consumer.commit()

                if self.processed_count and self.processed_count % 10 == 0:
                    self._save_checkpoint()
        except KeyboardInterrupt:
            logger.info("Consumer interrupted by user.")
        finally:
            self.shutdown()

    def _process_single_log(self, raw_item):
        try:
            parsed_log = self.parser.parse(raw_item)

            if self.embedding_gen:
                parsed_log = self.embedding_gen.enrich_log(parsed_log)

            if self.neo4j_writer:
                self.neo4j_writer.write(parsed_log)

            self.producer.send(
                KAFKA_CONFIG['topic_parsed_logs'],
                parsed_log,
                key=parsed_log.get('id')
            )

            self.processed_count += 1
            logger.info(
                f"[Processed #{self.processed_count}] Log ID: {parsed_log['id']} | "
                f"Host: {parsed_log['hostname']} | Source: {parsed_log['source_type']} | "
                f"Process: {parsed_log['process']} | "
                f"Severity: {parsed_log['severity']} | Conf: {parsed_log['confidence']}"
            )
            return True
        except Exception as e:
            self.error_count += 1
            logger.error(f"Failed to process log item: {e}", exc_info=True)
            return False

    def shutdown(self):
        self.running = False
        self.producer.flush()
        self.consumer.close()
        if self.neo4j_writer:
            self.neo4j_writer.close()
        super().shutdown()
