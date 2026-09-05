"""
kafka_producer.py - Kafka Producer wrapper with confluent-kafka & offline fallback

Sends messages to Kafka cluster with delivery callbacks and connection error handling.
"""

import json
import logging
from typing import Dict, Any, Optional

from core.config import KAFKA_CONFIG, PIPELINE_CONFIG

logger = logging.getLogger(__name__)

# Confluent Kafka import with graceful fallback for offline dev/test environments
try:
    from confluent_kafka import Producer
    HAS_CONFLUENT_KAFKA = True
except ImportError:
    HAS_CONFLUENT_KAFKA = False
    logger.warning("confluent_kafka package not available. Using mock Kafka Producer mode.")


class KafkaProducer:
    def __init__(self):
        self.bootstrap_servers = ','.join(KAFKA_CONFIG['bootstrap_servers'])
        self.producer = None
        self.is_connected = False
        self.enabled = PIPELINE_CONFIG['enable_kafka']

    def connect(self) -> bool:
        if not self.enabled:
            logger.info("Kafka is disabled in PIPELINE_CONFIG.")
            return False

        if not HAS_CONFLUENT_KAFKA:
            logger.info("confluent_kafka library missing; operating in mock mode.")
            self.is_connected = True
            return True

        try:
            conf = {
                'bootstrap.servers': self.bootstrap_servers,
                'client.id': 'log-ingestor-producer',
                'acks': 'all',
                'retries': 5,
                'security.protocol': KAFKA_CONFIG.get('security_protocol', 'PLAINTEXT'),
            }
            self.producer = Producer(conf)
            self.is_connected = True
            logger.info(f"Connected to Kafka brokers: {self.bootstrap_servers}")
            return True
        except Exception as e:
            logger.error(f"Kafka producer connection failed: {e}")
            self.is_connected = False
            return False

    def send(self, topic: str, data: Dict[str, Any], key: Optional[str] = None) -> bool:
        """Sends data dictionary as JSON payload to specified topic."""
        if not self.enabled:
            logger.debug(f"[Mock Kafka Producer] Topic: {topic} | Key: {key} | Data: {data.get('id', 'N/A')}")
            return True

        if not self.is_connected and not self.connect():
            logger.warning(f"Kafka unavailable. Dropping message or switching to mock send.")
            return False

        if not HAS_CONFLUENT_KAFKA or self.producer is None:
            logger.debug(f"[Mock Kafka Producer] Topic: {topic} | Key: {key} | Data: {data.get('id', 'N/A')}")
            return True

        try:
            payload = json.dumps(data).encode('utf-8')
            msg_key = key.encode('utf-8') if key else None
            
            self.producer.produce(
                topic=topic,
                key=msg_key,
                value=payload,
                callback=self._delivery_report
            )
            self.producer.poll(0)
            return True
        except Exception as e:
            logger.error(f"Kafka send failed for topic {topic}: {e}")
            return False

    def flush(self, timeout: float = 5.0):
        if self.producer:
            self.producer.flush(timeout)

    def _delivery_report(self, err, msg):
        if err:
            logger.error(f"Message delivery failed: {err}")
        else:
            logger.debug(f"Message delivered to {msg.topic()} [{msg.partition()}] at offset {msg.offset()}")
