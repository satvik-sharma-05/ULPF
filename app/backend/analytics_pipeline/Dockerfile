# ============================================================================
# ANALYTICS BACKEND - FastAPI wrapper around queries.py + insights.py
#
# Unlike the ingestion/chatbot images, this one bakes in no model weights -
# every metric here is a Cypher aggregate over the graph the ingestion
# pipeline already wrote, and every insight is computed from those aggregates
# in plain Python (see insights.py's module docstring for why that's
# deliberate, not a shortcut). That keeps this image small and the container
# starts in seconds instead of minutes.
# ============================================================================
FROM python:3.11-slim

LABEL description="Log Graph Analytics API (read-only aggregates + insights)"
LABEL version="1.0.0"

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
# A long multi-GB pip download over an unreliable link WILL hit a read
# timeout eventually, and a timeout here throws away the whole build - the
# chatbot image died after four minutes on exactly this, having already pulled
# torch. Retries and a generous per-read timeout make the build survive a
# blip instead of restarting from the base layer.
ENV PIP_DEFAULT_TIMEOUT=120     PIP_RETRIES=10     PIP_NO_CACHE_DIR=1

RUN pip install -r requirements.txt

COPY . .

EXPOSE 8010

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8010"]
