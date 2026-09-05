"""
kafka_consumer.py - Kafka Consumer wrapper with confluent-kafka & offline fallback

Consumes parsed/raw log messages from Kafka topic with batch handling and offset management.
"""

import json
import logging
from typing import List, Dict, Any, Optional

from core.config import KAFKA_CONFIG, PIPELINE_CONFIG

logger = logging.getLogger(__name__)

try:
    from confluent_kafka import Consumer, KafkaError
    HAS_CONFLUENT_KAFKA = True
except ImportError:
    HAS_CONFLUENT_KAFKA = False
    logger.warning("confluent_kafka package not available. Using mock Kafka Consumer mode.")


class KafkaConsumer:
    def __init__(self, topic: Optional[str] = None):
        self.bootstrap_servers = ','.join(KAFKA_CONFIG['bootstrap_servers'])
        self.topic = topic or KAFKA_CONFIG['topic_parsed_logs']
        self.group_id = KAFKA_CONFIG['consumer_group']
        self.consumer = None
        self.is_connected = False
        self.enabled = PIPELINE_CONFIG['enable_kafka']
        self._last_msg = None

    def connect(self) -> bool:
        if not self.enabled:
            logger.info("Kafka consumer is disabled in PIPELINE_CONFIG.")
            return False

        if not HAS_CONFLUENT_KAFKA:
            self.is_connected = True
            logger.info("confluent_kafka missing; consumer operating in mock mode.")
            return True

        try:
            conf = {
                'bootstrap.servers': self.bootstrap_servers,
                'group.id': self.group_id,
                'auto.offset.reset': KAFKA_CONFIG.get('auto_offset_reset', 'earliest'),
                'enable.auto.commit': KAFKA_CONFIG.get('enable_auto_commit', False),
                'security.protocol': KAFKA_CONFIG.get('security_protocol', 'PLAINTEXT'),
            }
            self.consumer = Consumer(conf)
            self.consumer.subscribe([self.topic])
            self.is_connected = True
            logger.info(f"Subscribed Kafka consumer to topic '{self.topic}' (Group: {self.group_id})")
            return True
        except Exception as e:
            logger.error(f"Kafka consumer connection failed: {e}")
            self.is_connected = False
            return False

    def poll(self, timeout: float = 5.0) -> List[Dict[str, Any]]:
        """Polls for available messages from subscribed Kafka topic."""
        if not self.enabled or not HAS_CONFLUENT_KAFKA or self.consumer is None:
            return []

        if not self.is_connected and not self.connect():
            return []

        messages = []
        try:
            msg = self.consumer.poll(timeout)
            if msg is None:
                return messages
            if msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    logger.error(f"Consumer poll error: {msg.error()}")
                return messages

            raw_val = msg.value().decode('utf-8')
            parsed_val = json.loads(raw_val)
            messages.append(parsed_val)
            self._last_msg = msg
        except Exception as e:
            logger.error(f"Error parsing consumed Kafka message: {e}")
        return messages

    def commit(self):
        """Commits the offset of the most recently polled message.

        Call only after that message has been fully processed & persisted,
        so a crash mid-processing leads to a safe re-delivery instead of data loss.
        """
        if not self.enabled or self.consumer is None or self._last_msg is None:
            return
        try:
            self.consumer.commit(message=self._last_msg, asynchronous=False)
        except Exception as e:
            logger.error(f"Failed to commit Kafka offset: {e}")

    def close(self):
        if self.consumer:
            try:
                self.consumer.close()
                logger.info("Kafka consumer closed gracefully.")
            except Exception as e:
                logger.error(f"Error closing Kafka consumer: {e}")
