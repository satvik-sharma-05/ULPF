"""
config.py - PLUG & PLAY CONFIGURATION
Only edit this file or set environment variables in .env to set your credentials and IPs.

Neo4j is the only datastore. PostgreSQL was removed from this pipeline: the
graph holds the normalized logs, the entity relationships *and* the embedding
vectors (Neo4j 5.11+ has a native vector index), so a second database bought
nothing but a second copy of the same rows to keep in sync.
"""

import os

# Load ingestion_pipeline/.env before ANY os.getenv below reads a default.
#
# Without this, .env only ever worked for the FastAPI entry points (which call
# load_dotenv() themselves) - every `python -m batch.*` command silently
# ignored it and used the hard-coded defaults here instead, so a correctly
# configured .env still produced "Could not connect to bolt://neo4j.internal.example".
# Both READMEs tell you to configure this pipeline by editing .env, so it has
# to be honored by the batch CLI too, not just the API.
#
# python-dotenv is optional (the parser's zero-dependency guarantee), and an
# explicit environment variable still wins over the file - override=False -
# so `NEO4J_URI=... python -m batch.run_pipeline` and docker-compose's
# `environment:` block both keep working exactly as before.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'),
                override=False)
except ImportError:
    pass


def _bool_env(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ('true', '1', 'yes')


# ============================================================================
# 0. SERVICE ROLE - which part of the pipeline this container instance runs
#    'producer'   -> polls DNIF and pushes raw logs onto Kafka
#    'consumer'   -> reads Kafka, parses, embeds, writes Neo4j
#    'standalone' -> does everything in a single process, bypassing Kafka
#                    (useful for local testing without a Kafka cluster)
# ============================================================================
SERVICE_ROLE = os.getenv('SERVICE_ROLE', 'standalone').strip().lower()

# ============================================================================
# 1. KAFKA CONFIGURATION - real broker IPs provided by the infra team
# ============================================================================
KAFKA_CONFIG = {
    'bootstrap_servers': os.getenv(
        'KAFKA_BOOTSTRAP_SERVERS',
        'localhost:9092'
    ).split(','),
    'topic_raw_logs': os.getenv('KAFKA_TOPIC_RAW_LOGS', 'raw-logs'),
    'topic_parsed_logs': os.getenv('KAFKA_TOPIC_PARSED_LOGS', 'parsed-logs'),
    'topic_insights': os.getenv('KAFKA_TOPIC_INSIGHTS', 'insights'),
    'consumer_group': os.getenv('KAFKA_CONSUMER_GROUP', 'log-ingestor-group'),
    'security_protocol': os.getenv('KAFKA_SECURITY_PROTOCOL', 'PLAINTEXT'),
    'auto_offset_reset': os.getenv('KAFKA_AUTO_OFFSET_RESET', 'earliest'),
    'enable_auto_commit': False,  # consumer commits manually after a log is fully processed
    'max_poll_records': 500,
    'session_timeout_ms': 30000,
}

# ============================================================================
# 2. NEO4J CONFIGURATION - the single datastore for this pipeline
# ============================================================================
NEO4J_CONFIG = {
    'uri': os.getenv('NEO4J_URI', 'bolt://localhost:7687'),
    'user': os.getenv('NEO4J_USER', 'neo4j'),
    'password': os.getenv('NEO4J_PASSWORD', 'password'),  # CHANGE THIS!
    # Community Edition only ever has a database called "neo4j" - a custom
    # name here is rejected unless the target is Enterprise Edition.
    'database': os.getenv('NEO4J_DATABASE', 'neo4j'),
    'max_connection_lifetime': 3600,
    'max_connection_pool_size': 50,
    'embedding_dim': int(os.getenv('EMBEDDING_DIM', '1024')),
    # How many parsed logs go into a single write transaction. Larger batches
    # are dramatically faster over the wire but hold locks longer; 500 is a
    # good balance for the corpus this pipeline ingests.
    'batch_size': int(os.getenv('NEO4J_BATCH_SIZE', '500')),
    # Maximum characters of the ORIGINAL raw log line stored on
    # (:Log).raw_message. The raw line is always kept - it is the only
    # verbatim record of what the source actually emitted, and the parsed
    # `message` is a cleaned/normalized derivative that can lose detail a
    # forensic question later needs.
    #
    # A cap exists because a single record is not always a single line: a
    # Windows Security Event body or a Java stack trace reassembled by
    # core/multiline.py can run to tens of KB, and storing those in full
    # multiplies the graph's on-disk size (see DEPLOYMENT.md's sizing table)
    # for text that is already largely captured in `message` and `attributes`.
    # Set RAW_MESSAGE_MAX_CHARS=0 for genuinely unlimited storage.
    'raw_message_max_chars': int(os.getenv('RAW_MESSAGE_MAX_CHARS', '32768')),
}

# ============================================================================
# 3. DNIF CONFIGURATION - Plug your DNIF details
# ============================================================================
DNIF_CONFIG = {
    'dnif_api_url': os.getenv('DNIF_API_URL', 'http://your-dnif-server:8080'),
    'dnif_api_key': os.getenv('DNIF_API_KEY', 'your-api-key-here'),
    'dnif_tenant': os.getenv('DNIF_TENANT', 'default'),
    'poll_interval': int(os.getenv('DNIF_POLL_INTERVAL', '5')),
    'batch_size': int(os.getenv('DNIF_BATCH_SIZE', '100')),
    'timeout': int(os.getenv('DNIF_TIMEOUT', '30')),
    # When DNIF isn't configured, the producer replays real lines from this
    # folder instead of fabricating logs - so the realtime path is exercised
    # with the same shapes the parser actually has to handle. Defaults to the
    # local corpus the batch pipeline loads from.
    'replay_path': os.getenv('DNIF_REPLAY_PATH', './logs'),
}

# ============================================================================
# 4. EMBEDDING CONFIGURATION - vectors are stored on (:Log).embedding in Neo4j
# ============================================================================
EMBEDDING_CONFIG = {
    'model_name': os.getenv('EMBEDDING_MODEL', 'BAAI/bge-m3'),
    'device': os.getenv('EMBEDDING_DEVICE', 'auto'),
    'normalize_embeddings': True,
    'embedding_dim': int(os.getenv('EMBEDDING_DIM', '1024')),
    'batch_size': int(os.getenv('EMBEDDING_BATCH_SIZE', '32')),
    'store_in_neo4j': True,
    'offline_fallback': True,
}

# ============================================================================
# 5. OPTIONAL LLM FALLBACK FOR THE PARSER (off by default)
#    Only used when ENABLE_QWEN_FALLBACK=true, to fill in fields the regex
#    detectors genuinely couldn't extract. The chatbot has its own, separate
#    LLM configuration - see ../chatbot_pipeline/config.py.
# ============================================================================
QWEN_CONFIG = {
    'ollama_host': os.getenv('OLLAMA_HOST', 'http://localhost:11434'),
    'model_name': os.getenv('OLLAMA_MODEL', 'qwen3:14b'),
    'timeout': int(os.getenv('QWEN_TIMEOUT', '120')),
    'confidence_threshold': float(os.getenv('QWEN_CONFIDENCE_THRESHOLD', '0.7')),
}
OLLAMA_CONFIG = QWEN_CONFIG

# ============================================================================
# 6. PIPELINE TOGGLES
# ============================================================================
PIPELINE_CONFIG = {
    'enable_kafka': _bool_env('ENABLE_KAFKA', 'true'),
    'enable_embeddings': _bool_env('ENABLE_EMBEDDINGS', 'true'),
    'enable_neo4j': _bool_env('ENABLE_NEO4J', 'true'),
    'enable_dnif_extractor': _bool_env('ENABLE_DNIF_EXTRACTOR', 'true'),
    'enable_qwen_fallback': _bool_env('ENABLE_QWEN_FALLBACK', 'false'),
    'test_mode': _bool_env('TEST_MODE', 'false'),
    'chunk_size': int(os.getenv('CHUNK_SIZE', '1000')),
    'max_workers': int(os.getenv('MAX_WORKERS', '4')),
    'checkpoint_file': os.getenv('CHECKPOINT_FILE', './output/ingestion_checkpoint.json'),
    'poll_timeout_seconds': float(os.getenv('KAFKA_POLL_TIMEOUT', '2.0')),
}

# ============================================================================
# 7. LOGGING
# ============================================================================
LOGGING_CONFIG = {
    'level': os.getenv('LOG_LEVEL', 'INFO'),
    'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    'file': '/app/logs/ingestion.log',
}
