"""
api.py - Control-plane API for the ingestion pipeline.

    uvicorn api:app --host 0.0.0.0 --port 8020

This is what the frontend's mode switcher and "Add your custom logs" upload
talk to. It lives in ingestion_pipeline because ingestion is what it does:
the chatbot and analytics backends stay read-only and know nothing about it.

    GET  /api/ingest/modes           the three modes, with per-mode validation
    GET  /api/ingest/health          Neo4j reachability + active mode
    POST /api/ingest/upload          upload one log file -> parse -> embed -> Neo4j
    GET  /api/ingest/jobs            recent upload jobs
    GET  /api/ingest/jobs/{job_id}   one job's status/summary
    POST /api/ingest/sample          kick off a sample-corpus ingest (sample mode)
    POST /api/ingest/parse           dry-run: parse text, store nothing

Uploads run in a background thread with an in-process job registry, because
parsing + embedding a file takes longer than an HTTP request should hold
open. That registry is deliberately simple and has real limits, stated
plainly rather than papered over: jobs are lost on restart, and it assumes a
single API instance. Both are correct for the single-VM deployment this
project targets (see DEPLOYMENT.md); a multi-instance deployment would need
a real queue, which is a different piece of work, not a bigger dict.
"""

import logging
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'),
                    format='%(asctime)s [%(levelname)s] %(name)s - %(message)s')
logger = logging.getLogger("ingestion.api")

from core import modes
from core.structured_readers import SUPPORTED_FORMATS

app = FastAPI(title="Log Ingestion Control API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv('INGEST_API_CORS_ORIGINS', '*').split(',') if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Bounded so a long-running server can't accumulate job records without limit.
MAX_JOBS_RETAINED = 50
MAX_UPLOAD_BYTES = int(os.getenv('INGEST_MAX_UPLOAD_MB', '200')) * 1024 * 1024

_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()
# One ingest at a time. Two concurrent ingests would each load their own copy
# of BAAI/bge-m3 (several GB of RAM apiece) and contend for the same Neo4j
# write locks - serializing them is both cheaper and more predictable.
_ingest_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_job(job: Dict[str, Any]) -> None:
    with _jobs_lock:
        _jobs[job['job_id']] = job
        if len(_jobs) > MAX_JOBS_RETAINED:
            oldest = sorted(_jobs.values(), key=lambda j: j['created_at'])[0]
            _jobs.pop(oldest['job_id'], None)


def _update_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job.update(fields)


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------
@app.get("/api/ingest/modes")
def list_modes() -> Dict[str, Any]:
    return {
        'current': modes.current_mode(),
        'modes': modes.describe_all(),
        'supported_upload_formats': list(SUPPORTED_FORMATS),
    }


class ModeRequest(BaseModel):
    mode: str = Field(..., description="sample | production | custom")


@app.post("/api/ingest/mode")
def set_mode(request: ModeRequest) -> Dict[str, Any]:
    """Switches the active mode at runtime, for this process.

    Process-local and NOT written to .env - a restart returns to whatever
    APP_MODE the operator configured. See core/modes.set_mode() for why.
    """
    try:
        modes.set_mode(request.mode)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        'current': modes.current_mode(),
        'modes': modes.describe_all(),
        'persisted': False,
        'note': 'Applies to this running service only; a restart returns to the configured APP_MODE.',
    }


@app.get("/api/ingest/health")
def health() -> Dict[str, Any]:
    from core.neo4j_writer import HAS_NEO4J_DRIVER, Neo4jWriter

    neo4j_ok = False
    detail = None
    if not HAS_NEO4J_DRIVER:
        detail = "neo4j driver not installed"
    else:
        writer = Neo4jWriter()
        try:
            neo4j_ok = bool(writer.connect()) and writer.driver is not None
            if not neo4j_ok:
                detail = f"could not reach {writer.uri}"
        except Exception as e:
            detail = str(e)
        finally:
            writer.close()

    mode = modes.current_mode()
    return {
        'neo4j': neo4j_ok,
        'detail': detail,
        'mode': mode,
        'mode_problems': modes.validate(mode),
        'embeddings_enabled': modes.embeddings_enabled(mode),
        'status': 'ok' if neo4j_ok else 'degraded',
    }


# ---------------------------------------------------------------------------
# Custom-log upload
# ---------------------------------------------------------------------------
def _run_ingest(job_id: str, text: str, filename: str, fmt: str,
                embed: bool, wipe: bool) -> None:
    """Background worker for one upload. Never raises out of the thread - any
    failure is recorded on the job so the UI can show why."""
    from batch.ingest_custom import ingest_text
    from core.neo4j_writer import Neo4jWriter

    if not _ingest_lock.acquire(blocking=False):
        _update_job(job_id, status='failed', finished_at=_now(),
                    error='Another ingest is already running - retry when it finishes.')
        return

    writer = None
    try:
        _update_job(job_id, status='running', started_at=_now())
        writer = Neo4jWriter()
        if not writer.connect() or writer.driver is None:
            _update_job(job_id, status='failed', finished_at=_now(),
                        error=f"Could not connect to Neo4j at {writer.uri}")
            return

        if wipe:
            from batch.ingest_to_neo4j import wipe_database
            from core.config import NEO4J_CONFIG
            wipe_database(writer, NEO4J_CONFIG)

        def progress(parsed: int, written: int) -> None:
            _update_job(job_id, records_parsed=parsed, records_written=written)

        summary = ingest_text(
            text=text, filename=filename, writer=writer,
            format_hint=fmt, embed=embed, progress=progress,
        )
        _update_job(job_id, status='completed', finished_at=_now(), summary=summary,
                    records_parsed=summary['records_parsed'],
                    records_written=summary['records_written'])
        logger.info(f"Upload job {job_id} completed: {summary['records_written']} records written")
    except Exception as e:
        logger.exception(f"Upload job {job_id} failed")
        _update_job(job_id, status='failed', finished_at=_now(), error=str(e))
    finally:
        if writer is not None:
            writer.close()
        _ingest_lock.release()


@app.post("/api/ingest/upload")
async def upload(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    format: str = Form('auto'),
    embed: bool = Form(True),
    wipe: bool = Form(False),
) -> Dict[str, Any]:
    if format not in SUPPORTED_FORMATS:
        raise HTTPException(400, f"Unknown format {format!r}. Supported: {list(SUPPORTED_FORMATS)}")

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"File is {len(raw) / 1024 / 1024:.1f}MB, over the "
            f"{MAX_UPLOAD_BYTES / 1024 / 1024:.0f}MB limit. Split it, or raise "
            f"INGEST_MAX_UPLOAD_MB."
        )
    if not raw.strip():
        raise HTTPException(400, "The uploaded file is empty.")

    text = raw.decode('utf-8', errors='ignore')
    filename = os.path.basename(file.filename or 'upload.log')

    from core.structured_readers import detect_format_label
    job = {
        'job_id': uuid.uuid4().hex[:12],
        'filename': filename,
        'size_bytes': len(raw),
        'detected_format': detect_format_label(filename, text[:2048], format),
        'requested_format': format,
        'embed': embed,
        'wipe': wipe,
        'status': 'queued',
        'created_at': _now(),
        'started_at': None,
        'finished_at': None,
        'records_parsed': 0,
        'records_written': 0,
        'summary': None,
        'error': None,
    }
    _record_job(job)
    background.add_task(_run_ingest, job['job_id'], text, filename, format, embed, wipe)
    return job


@app.get("/api/ingest/jobs")
def list_jobs() -> List[Dict[str, Any]]:
    with _jobs_lock:
        return sorted(_jobs.values(), key=lambda j: j['created_at'], reverse=True)


@app.get("/api/ingest/jobs/{job_id}")
def get_job(job_id: str) -> Dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"No such job: {job_id}")
    return job


# ---------------------------------------------------------------------------
# Sample-corpus ingest
# ---------------------------------------------------------------------------
def _run_sample_ingest(job_id: str, path: str, limit: Optional[int], embed: bool, wipe: bool) -> None:
    """Ingests the bundled corpus - either a pre-parsed output.json[.gz] or a
    raw log folder - through the same writer everything else uses."""
    from core.neo4j_writer import Neo4jWriter

    if not _ingest_lock.acquire(blocking=False):
        _update_job(job_id, status='failed', finished_at=_now(),
                    error='Another ingest is already running - retry when it finishes.')
        return

    writer = None
    try:
        _update_job(job_id, status='running', started_at=_now())
        writer = Neo4jWriter()
        if not writer.connect() or writer.driver is None:
            _update_job(job_id, status='failed', finished_at=_now(),
                        error=f"Could not connect to Neo4j at {writer.uri}")
            return

        if wipe:
            from batch.ingest_to_neo4j import wipe_database
            from core.config import NEO4J_CONFIG
            wipe_database(writer, NEO4J_CONFIG)

        embedder = None
        if embed:
            from core.embeddings import EmbeddingGenerator
            embedder = EmbeddingGenerator()

        from batch.ingest_to_neo4j import batched
        from core.jsonio import stream_read_records

        written = 0
        seen = 0

        def stream():
            nonlocal seen
            for record in stream_read_records(path):
                if limit and seen >= limit:
                    return
                seen += 1
                yield record

        for batch in batched(stream(), 500):
            if embedder:
                texts = [r.get('normalized_message') or r.get('message') or '' for r in batch]
                for record, vector in zip(batch, embedder.generate_batch(texts)):
                    record['embedding'] = vector
            written += writer.write_batch(batch)
            _update_job(job_id, records_parsed=seen, records_written=written)

        _update_job(job_id, status='completed', finished_at=_now(),
                    records_parsed=seen, records_written=written,
                    summary={
                        'records_parsed': seen, 'records_written': written,
                        'embeddings': bool(embedder),
                        'embedding_model_loaded': bool(embedder and embedder.is_loaded),
                        'source': path,
                    })
    except Exception as e:
        logger.exception(f"Sample ingest job {job_id} failed")
        _update_job(job_id, status='failed', finished_at=_now(), error=str(e))
    finally:
        if writer is not None:
            writer.close()
        _ingest_lock.release()


@app.post("/api/ingest/sample")
def ingest_sample(
    background: BackgroundTasks,
    limit: Optional[int] = None,
    embed: bool = True,
    wipe: bool = False,
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """Ingests the bundled sample corpus. `path` defaults to whichever
    pre-parsed corpus file is present next to this pipeline."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [path] if path else [
        os.path.join(here, 'output.json.gz'),
        os.path.join(here, 'output.json'),
    ]
    resolved = next((c for c in candidates if c and os.path.isfile(c)), None)
    if not resolved:
        raise HTTPException(
            404,
            "No parsed sample corpus found. Expected output.json.gz (or output.json) "
            "in ingestion_pipeline/. Produce one with: "
            "python -m batch.parse_logs --path ./logs --out output.json.gz"
        )

    job = {
        'job_id': uuid.uuid4().hex[:12],
        'filename': os.path.basename(resolved),
        'size_bytes': os.path.getsize(resolved),
        'detected_format': 'parsed corpus (JSON)',
        'requested_format': 'json',
        'embed': embed,
        'wipe': wipe,
        'status': 'queued',
        'created_at': _now(),
        'started_at': None,
        'finished_at': None,
        'records_parsed': 0,
        'records_written': 0,
        'summary': None,
        'error': None,
    }
    _record_job(job)
    background.add_task(_run_sample_ingest, job['job_id'], resolved, limit, embed, wipe)
    return job



# ============================================================================
# DRY-RUN PARSE  -  the Parser Lab
# ============================================================================
#  Paste or drop a log, see exactly what the framework makes of it, without
#  any of it being stored. This is the one screen that shows all four of the
#  core requirements at once and lets you check them yourself rather than
#  taking a coverage table's word for it:
#
#    (a) the raw record comes back verbatim beside the parsed one
#    (b) the source-specific attributes the detector pulled out
#    (c) the same normalized fields for every source, whatever it was
#    (d) the id that links the two, computed the same way ingestion does
#    (e) an unknown source still produces a usable record - `generic_fallback`
#        is called out rather than hidden, so its limits are visible too
#
#  Deliberately NOT an ingest: no Neo4j driver is touched, no embedding is
#  generated, nothing is written. Someone evaluating a new log source must be
#  able to try it against production without contaminating the graph, and a
#  tool that quietly persists what you paste into it cannot be used that way.
PARSE_MAX_BYTES = int(os.getenv('INGEST_PARSE_MAX_KB', '2048')) * 1024
PARSE_MAX_RECORDS = int(os.getenv('INGEST_PARSE_MAX_RECORDS', '5000'))
PARSE_DEFAULT_DETAIL = 200


class ParseRequest(BaseModel):
    text: str = Field(..., description="Raw log text - one or more records.")
    format: str = Field('auto', description=f"One of {list(SUPPORTED_FORMATS)}")
    filename: str = Field('pasted.log',
                          description="Only used for format sniffing.")
    limit: int = Field(PARSE_DEFAULT_DETAIL, ge=1, le=PARSE_MAX_RECORDS,
                       description="How many parsed records to return in full.")


@app.post("/api/ingest/parse")
def parse_preview(req: ParseRequest) -> Dict[str, Any]:
    """Parse text and return the normalized records. Stores nothing."""
    if req.format not in SUPPORTED_FORMATS:
        raise HTTPException(
            400, f"Unknown format {req.format!r}. Supported: {list(SUPPORTED_FORMATS)}")

    text = req.text
    if not text.strip():
        raise HTTPException(400, "Nothing to parse.")
    encoded = len(text.encode('utf-8', errors='ignore'))
    if encoded > PARSE_MAX_BYTES:
        raise HTTPException(
            413,
            f"{encoded / 1024:.0f}KB of text, over the "
            f"{PARSE_MAX_BYTES / 1024:.0f}KB preview limit. This screen is for "
            f"trying a source out; use Modes -> Custom to ingest a whole file.")

    from core.parser import LogParser
    from core.structured_readers import detect_format_label, iter_records

    detected = detect_format_label(req.filename, text[:2048], req.format)
    parser = LogParser()

    records: List[Dict[str, Any]] = []
    formats: Dict[str, int] = {}
    # How often each schema field actually gets a value - the honest read on
    # "normalized into a common taxonomy", since a field that is null on every
    # record is not normalization, it is an empty column.
    populated: Dict[str, int] = {}
    parsed_count = 0
    truncated = False

    started = datetime.now(timezone.utc)
    for raw_record in iter_records(text, req.filename, req.format):
        if not raw_record.strip():
            continue
        if parsed_count >= PARSE_MAX_RECORDS:
            truncated = True
            break
        parsed_count += 1
        result = parser.parse(raw_record)

        fmt = result.get('matched_format', 'generic_fallback')
        formats[fmt] = formats.get(fmt, 0) + 1
        for field in ('timestamp', 'hostname', 'source_type', 'process',
                      'component', 'severity', 'message'):
            value = result.get(field)
            if value not in (None, '', 'unknown-host', 'Unknown'):
                populated[field] = populated.get(field, 0) + 1
        if result.get('attributes'):
            populated['attributes'] = populated.get('attributes', 0) + 1

        if len(records) < req.limit:
            records.append({
                'ordinal': parsed_count,
                # Verbatim, straight from the input - requirement (a). Nothing
                # here is reconstructed from the parsed fields.
                'raw': raw_record,
                'raw_lines': len(raw_record.splitlines()),
                'matched_format': fmt,
                'is_fallback': fmt == 'generic_fallback',
                'normalized': {
                    'id': result.get('id'),
                    'timestamp': result.get('timestamp'),
                    'timestamp_source': result.get('timestamp_source'),
                    'timestamp_anomalous': result.get('timestamp_anomalous'),
                    'hostname': result.get('hostname'),
                    'source_type': result.get('source_type'),
                    'process': result.get('process'),
                    'pid': result.get('pid'),
                    'component': result.get('component'),
                    'severity': result.get('severity'),
                    'severity_score': result.get('severity_score'),
                    'message': result.get('message'),
                    'normalized_message': result.get('normalized_message'),
                    'confidence': result.get('confidence'),
                },
                'attributes': result.get('attributes') or {},
                'entities': result.get('entities') or {},
                # Proof of (a): the record the graph would store still contains
                # the input byte for byte. The UI asserts this rather than
                # claiming it.
                'raw_preserved': result.get('raw_message') == raw_record,
            })

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    fallback = formats.get('generic_fallback', 0)
    return {
        'summary': {
            'records': parsed_count,
            'returned': len(records),
            'truncated': truncated,
            'detected_format': detected,
            'requested_format': req.format,
            'distinct_formats': len(formats),
            'named': parsed_count - fallback,
            'fallback': fallback,
            'coverage_pct': round(100.0 * (parsed_count - fallback) / parsed_count, 1)
                            if parsed_count else 0.0,
            'raw_preserved_all': all(r['raw_preserved'] for r in records),
            'elapsed_ms': round(elapsed * 1000, 1),
            'records_per_sec': int(parsed_count / elapsed) if elapsed > 0 else None,
            'field_coverage': {
                field: {'count': populated.get(field, 0),
                        'pct': round(100.0 * populated.get(field, 0) / parsed_count, 1)
                               if parsed_count else 0.0}
                for field in ('timestamp', 'hostname', 'source_type', 'process',
                              'component', 'severity', 'message', 'attributes')
            },
        },
        'formats': [{'name': k, 'count': v}
                    for k, v in sorted(formats.items(), key=lambda kv: -kv[1])],
        'records': records,
    }


if __name__ == '__main__':
    import uvicorn
    uvicorn.run("api:app", host=os.getenv('INGEST_API_HOST', '0.0.0.0'),
                port=int(os.getenv('INGEST_API_PORT', '8020')), reload=False)
