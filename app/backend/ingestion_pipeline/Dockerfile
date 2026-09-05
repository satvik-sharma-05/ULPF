# ============================================================================
# BASE IMAGE
# ============================================================================
FROM python:3.11-slim

# ============================================================================
# METADATA
# ============================================================================
LABEL maintainer="log-analysis-ai@example.com"
LABEL description="Log Ingestion Pipeline (DNIF -> Kafka -> Parser -> Neo4j)"
LABEL version="2.0.0"

# ============================================================================
# SYSTEM DEPENDENCIES (kept minimal - fewer packages means fewer things that
# can break when this image is loaded on an air-gapped host)
#
# gcc + librdkafka-dev are a defensive fallback for confluent-kafka: current
# versions ship prebuilt manylinux wheels with librdkafka bundled, so pip
# normally never needs to compile anything here - but python:3.11-slim has no
# compiler at all, so IF pip ever falls back to a source build (uncommon
# platform/arch, a future version dropping a wheel for this target), the build
# would otherwise fail outright with no way to recover once this step is
# reached. These two packages cost a little image size and build time in
# exchange for that failure mode simply not existing.
# ============================================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    netcat-openbsd \
    curl \
    gcc \
    librdkafka-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf_cache \
    TRANSFORMERS_CACHE=/opt/hf_cache \
    SENTENCE_TRANSFORMERS_HOME=/opt/hf_cache

# ============================================================================
# PYTHON DEPENDENCIES
# CPU-only torch first: avoids pulling the multi-GB CUDA build, since the
# target VM is air-gapped and has no GPU.
# ============================================================================
# A long multi-GB pip download over an unreliable link WILL hit a read
# timeout eventually, and a timeout here throws away the whole build - the
# chatbot image died after four minutes on exactly this, having already pulled
# torch. Retries and a generous per-read timeout make the build survive a
# blip instead of restarting from the base layer.
ENV PIP_DEFAULT_TIMEOUT=120     PIP_RETRIES=10     PIP_NO_CACHE_DIR=1

RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install -r requirements.txt

# ============================================================================
# BAKE THE EMBEDDING MODEL INTO THE IMAGE
# Downloaded here, on this internet-connected build machine, so the pipeline
# never needs network access to Hugging Face once it's running on the
# air-gapped VM. HF_HUB_OFFLINE below then forbids any runtime network call,
# so a misconfiguration fails loudly instead of silently degrading to the
# fallback hash-based embedding vectors.
# ============================================================================
ARG EMBEDDING_MODEL=BAAI/bge-m3
# Build-time only - lifts HuggingFace's anonymous-download rate limit for this
# multi-GB pull. Never in the final image's usable env (HF_HUB_OFFLINE below
# forbids any runtime call that would use it anyway).
ARG HF_TOKEN=""
ENV HF_TOKEN=${HF_TOKEN}
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}')"

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

# ============================================================================
# COPY APPLICATION CODE
# ============================================================================
COPY . .
RUN mkdir -p /app/logs /app/output

# Runtime config (roles, IPs, credentials) comes entirely from docker-compose's
# environment / .env - config.py already has the same non-secret fallback
# defaults in Python, so there's no need to (and shouldn't) bake any of it,
# least of all credentials, into image-level ENV.

EXPOSE 8000

# ============================================================================
# ENTRYPOINT
# ============================================================================
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
