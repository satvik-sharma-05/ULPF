# Analytics Pipeline

Read-only dashboard API over the log graph `ingestion_pipeline` built - volume,
rate, severity, alerts, and which host/source/component is driving errors,
plus a handful of rule-based **insights** derived from those same numbers.

Standalone by design, same rule as `chatbot_pipeline`: the only dependency is
a populated Neo4j. It does not import `../ingestion_pipeline` or
`../chatbot_pipeline`, never writes to the graph, and needs no LLM or
embedding model - every metric is a Cypher aggregate, every insight is plain
Python over those aggregates.

```bash
cd analytics_pipeline
pip install -r requirements.txt
cp .env.example .env && nano .env      # NEO4J_URI, NEO4J_PASSWORD
uvicorn api:app --reload --port 8010
```

The dashboard at `LA/frontend`'s **Analytics** tab talks to this service. See
[`../frontend/README.md`](../frontend/) / the top-level README for how the two
run together.

---

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /api/analytics/overview` | total logs/hosts/users/sources, total alerts, total errors, corpus time range |
| `GET /api/analytics/timeseries?days=N` | logs/alerts/errors per calendar day, most recent N days |
| `GET /api/analytics/timeseries/day?day=YYYY-MM-DD&granularity=hour\|minute` | sub-day volume, for rate-style panels |
| `GET /api/analytics/severity` | log count per severity level |
| `GET /api/analytics/sources` | log + error count per source type (NSX, ESXi, Kubernetes, ...) |
| `GET /api/analytics/hosts?limit=N&metric=total\|errors` | top hosts by volume or by error count |
| `GET /api/analytics/processes?limit=N` | top processes by volume |
| `GET /api/analytics/error-components?limit=N` | components ranked by error volume specifically |
| `GET /api/analytics/parse-coverage?limit=N` | corpus share per parser detector (`matched_format`) |
| `GET /api/analytics/insights` | rule-based observations (see below) |
| `GET /api/analytics/dashboard` | everything above, bundled into one response for first paint |
| `GET /api/analytics/health` | `{"neo4j": bool}` |

All aggregation runs on properties `core/graph_schema.py`'s `INDEXES` already
cover (`timestamp`, `severity_score`, `source_type`, `hostname`, `day`,
`matched_format`) - an aggregate still has to touch every row it aggregates
over (there's no way around that for "count per day" on the full corpus), but
every filter here at least uses an index seek, not a full label scan.

## Insights

`insights.py` turns the raw aggregates above into short, specific
observations - which source produces the most errors, which host has the
highest error rate (above a minimum volume, so a 2-log host doesn't "win" at
a meaningless 50%), whether error rate is trending up or down week over week,
how concentrated the error count is on one host, and the busiest/worst day in
range.

Deliberately rule-based, not LLM-generated: every number quoted is computed
directly from the same aggregates the dashboard's charts already show, so an
insight can never contradict the chart next to it, and this pipeline needs no
model to load or LLM to reach - it degrades to "no insights yet" only when
the graph itself is empty, never when an LLM is unreachable.

## Caching

Aggregation results are cached in-process for `ANALYTICS_CACHE_TTL_SECONDS`
(default 30s). This is a batch-ingested historical corpus, not a
live-updating stream (see `ingestion_pipeline/README.md`) - the graph doesn't
change between two dashboard polls a few seconds apart, so a short TTL turns
a multi-panel dashboard with auto-refresh into a handful of Neo4j round trips
instead of one full-corpus aggregation per panel per poll. The cache is a
plain process-local dict: this API is meant to run as one instance in front
of one Neo4j, not horizontally scaled.

## Deployment

Same shape as `chatbot_pipeline`: `Dockerfile` builds a small image (no torch,
no baked model weights - just FastAPI + the Neo4j driver), `docker-compose.yml`
runs it with `network_mode: host` so it reaches Neo4j via `localhost` without
any custom network wiring, and it loads into an airgapped VM the same way -
`docker save`/`docker load`, or folded into `build_offline_images.ps1` /
`DEPLOYMENT.md`'s transfer step alongside the other three images.

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| Every number is 0 / "not connected" | Neo4j unreachable | Check `NEO4J_URI`/credentials; `nc -zv <host> 7687` |
| `database 'logs_db' not found` | Community Edition has only `neo4j` | Set `NEO4J_DATABASE=neo4j` |
| Insights list is empty | Graph has no `:Log` nodes yet, or nothing crosses the insight thresholds | Run the ingestion pipeline first; thresholds are tunable via `ANALYTICS_MIN_VOLUME`/`ANALYTICS_TREND_WINDOW_DAYS` |
| Dashboard looks stale after a fresh ingest | In-process cache | Wait `ANALYTICS_CACHE_TTL_SECONDS` (default 30s), or restart the API |
| `/timeseries/day` returns nothing for a valid day | No logs have that exact `Log.day` value | `GET /api/analytics/timeseries` first to see which days actually have data |
