"""
realtime - PIPELINE 1: live ingestion.

    DNIF API -> Kafka (raw-logs) -> Parser -> Embeddings -> Neo4j
                ^producer role^     ^^^^^^ consumer role ^^^^^^

Kafka sits between fetching and parsing on purpose: it is a durable buffer, so
the parse/embed/write stage can crash, restart, or scale to several replicas
without ever losing a log that was already pulled from DNIF. That only works if
fetching and parsing are separate processes, which is why this ships as two
roles rather than one loop.

For loading log *files* that already exist on disk, use ../batch instead - it
shares this pipeline's parser, entity extraction, embeddings and graph writer,
and differs only in where records come from.
"""

from .base_service import BaseService
from .consumer_service import ConsumerService
from .dnif_api import DNIFClient
from .kafka_consumer import KafkaConsumer
from .kafka_producer import KafkaProducer
from .producer_service import ProducerService

__all__ = [
    'BaseService', 'ProducerService', 'ConsumerService',
    'DNIFClient', 'KafkaProducer', 'KafkaConsumer',
]
