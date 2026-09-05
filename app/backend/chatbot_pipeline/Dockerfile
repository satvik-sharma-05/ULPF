# ============================================================================
# CHATBOT BACKEND - FastAPI wrapper around LogChatbot (api.py)
#
# Bakes in BOTH BAAI/bge-m3 (embeddings) and BAAI/bge-reranker-v2-m3 (cross-
# encoder reranking) at build time, same reasoning as the ingestion pipeline's
# image: GraphRAG embeds the *question* with the identical model that embedded
# the logs, or nearest-neighbour search returns meaningless results, and the
# reranker needs its own weights present too. Baking both in here - rather
# than relying on anything already present on the target VM - means this
# image is fully self-contained: it never depends on what is or isn't sitting
# on whatever machine happens to run it, and the airgapped VM never needs
# network access to Hugging Face at query time.
# ============================================================================
FROM python:3.11-slim

LABEL description="Log Graph Chatbot API (text-to-Cypher + GraphRAG)"
LABEL version="1.0.0"

# build-essential and the cairo headers are here for one reason: rlPyCairo,
# which reportlab's renderPM needs to rasterise a chart for the Word export.
# pycairo has no manylinux wheel - it links the system cairo - so pip builds it
# from source, and python:3.11-slim ships no compiler. The build died with
# "Unknown compiler(s): [['cc'], ['gcc'], ...]" from meson.
#
# The toolchain is removed again once the wheels are built (see the purge after
# requirements-media.txt), so it costs build time rather than image size.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    pkg-config \
    libcairo2-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf_cache \
    TRANSFORMERS_CACHE=/opt/hf_cache \
    SENTENCE_TRANSFORMERS_HOME=/opt/hf_cache

# CPU-only torch first, same reasoning as the ingestion image: avoids pulling
# the multi-GB CUDA build for a VM with no GPU.
# A long multi-GB pip download over an unreliable link WILL hit a read
# timeout eventually, and a timeout here throws away the whole build - the
# chatbot image died after four minutes on exactly this, having already pulled
# torch. Retries and a generous per-read timeout make the build survive a
# blip instead of restarting from the base layer.
ENV PIP_DEFAULT_TIMEOUT=120     PIP_RETRIES=10     PIP_NO_CACHE_DIR=1

RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install sentence-transformers>=2.7.0

COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake both models in at build time (needs internet HERE, on the connected
# build machine, never again afterwards). HF_TOKEN is build-time only - it's
# never in the final image env (HF_HUB_OFFLINE below forbids any runtime call
# that would use it anyway) - it just lifts HuggingFace's anonymous-download
# rate limit, which otherwise throttles these two multi-GB pulls badly.
ARG EMBEDDING_MODEL=BAAI/bge-m3
ARG RERANKER_MODEL=BAAI/bge-reranker-v2-m3
ARG HF_TOKEN=""
ENV HF_TOKEN=${HF_TOKEN}
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}')"
RUN python -c "from sentence_transformers import CrossEncoder; CrossEncoder('${RERANKER_MODEL}')"

# Forbids any runtime network call to Hugging Face - a misconfiguration then
# fails loudly (embedder/reranker simply doesn't load, degrading to full-text
# search / unranked order) instead of silently trying to phone home from an
# airgapped VM.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1


# ============================================================================
# MEDIA CAPABILITIES - dictation, spoken answers, PPTX export
#
# Deliberately AFTER the embedding/reranker bake above. Those two layers are
# several GB of download; if these packages lived in requirements.txt, editing
# one of them would invalidate the model layers and pull both models again.
# Here, a media change rebuilds only from this line down.
#
# Both model downloads happen HERE, on the connected build machine. The
# air-gapped VM never fetches either one - HF_HUB_OFFLINE above already
# forbids it.
# ============================================================================
COPY requirements-media.txt .
# Build the wheels, then drop the toolchain. libcairo2 (the runtime library)
# stays; build-essential, pkg-config and the -dev headers do not, which keeps
# roughly 250MB out of the shipped image.
RUN pip install --no-cache-dir -r requirements-media.txt \
 && apt-get purge -y --auto-remove build-essential pkg-config libcairo2-dev \
 && apt-get update && apt-get install -y --no-install-recommends libcairo2 \
 && rm -rf /var/lib/apt/lists/*

# Whisper, converted to the CTranslate2 format faster-whisper loads. `base` is
# the deliberate choice over `small`/`medium`: on a CPU-only VM it transcribes
# a short dictated question in roughly real time, where `medium` takes several
# times longer for accuracy nobody notices on a one-sentence query.
ENV WHISPER_MODEL=/opt/models/whisper-base \
    WHISPER_COMPUTE=int8
# HF_HUB_OFFLINE=0 for this ONE command. The image sets HF_HUB_OFFLINE=1
# further up - correctly, so the running container can never phone home -
# but that ENV is inherited by every later build step too, and it blocked
# this download with "outgoing traffic has been disabled". Scoping the
# override to the RUN keeps the image-level guarantee intact.
RUN HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 python -c "\
from faster_whisper import WhisperModel; \
import shutil, os; \
m = WhisperModel('base', device='cpu', compute_type='int8', download_root='/tmp/wdl'); \
src = [os.path.join(r, d) for r, ds, _ in os.walk('/tmp/wdl') for d in ds if d == 'snapshots']; \
snap = [os.path.join(src[0], d) for d in os.listdir(src[0])][0]; \
shutil.copytree(snap, '/opt/models/whisper-base', dirs_exist_ok=True); \
print('whisper baked:', os.listdir('/opt/models/whisper-base'))" \
 && rm -rf /tmp/wdl

# Piper voice. The .json config sits beside the .onnx and is not optional -
# Piper reads the sample rate and phoneme map from it, and fails at load
# without it.
ENV PIPER_VOICE=/opt/models/piper/en_US-lessac-medium.onnx
ARG PIPER_BASE=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium
RUN mkdir -p /opt/models/piper \
 && curl -fsSL -o /opt/models/piper/en_US-lessac-medium.onnx      "${PIPER_BASE}/en_US-lessac-medium.onnx" \
 && curl -fsSL -o /opt/models/piper/en_US-lessac-medium.onnx.json "${PIPER_BASE}/en_US-lessac-medium.onnx.json"

# The vision model is NOT baked here - it is served by the Ollama container
# the chat already talks to, so it lives in that image instead. See
# VISION_MODEL in .env and the pull step in build_images.ps1.

COPY . .
# The frontend directory lives outside this build context (../frontend) and
# is served by its own nginx container, not baked in here - see
# docker-compose.yml. .dockerignore also excludes it defensively.

EXPOSE 8000

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
