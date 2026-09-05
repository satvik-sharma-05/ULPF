# Chatbot Pipeline — Features, Methods & Shortcomings

What the log-graph chatbot can actually do today, how it does it, and where the
real edges of that capability are. This is the honest reference — see
`README.md` for setup/usage and architectural reasoning.

---

## 0. Verified in live testing (2026-07-30, local dev box)

Everything below this section is the design reference: what the code is built
to do. This section is different — it's the subset that was actually run
end-to-end against a real ingested graph, not just read in the source.

**Confirmed working, live:**
- Batch ingestion with embeddings: 5,000 records from the real corpus (`batch.ingest_to_neo4j --embed`) →
  4,667 `:Log` nodes after merge, **100% with embeddings**, plus a full spread
  of typed entities (`Thread`, `Trace`, `Host`, `Component`, ...) and their
  relationships — confirmed by direct Cypher query against the running
  database, not just the ingest script's own report.
- `vector_search`: a real question ("which hosts produced the most errors?")
  returned genuinely on-topic results — top hits at 0.73–0.75 cosine
  similarity, correctly surfacing ESXi `Hostd` error logs.
- **Graceful degradation, adversarially tested for free**: Ollama crashed
  mid-test from real host RAM pressure (not simulated) — `llama-server`
  failed to allocate its buffer and returned HTTP 500. The planner caught
  it, fell back to `vector_search` + `text2cypher` exactly as section 3
  below claims, returned HTTP 200 with usable evidence and a clear "No LLM
  available" note instead of hanging or crashing. This is the one
  reliability claim in this document that got a genuine failure to react
  to, not just a code read.
- `/api/health` correctly reported live Neo4j and Ollama reachability.
- `chatbot-backend` and `chatbot-frontend` containers both start clean,
  serve `/api/ask` and `/api/health`, and the frontend correctly proxies to
  the backend.

**Not yet exercised** (design/code-verified only — not contradicted by
testing, just not observed firing):
- `graph_neighbors`, `path_traversal`, `temporal_window`,
  `parent_child_retrieval`, `query_template`, `pattern_match`,
  `fulltext_search`, reranking, and `text2cypher`'s actual generated-query
  quality — none of these got a live run with a working LLM.
- The planner's real multi-round, multi-tool behavior. The one live question
  above fell straight to the deterministic single-round fallback because
  Ollama was down at that moment, so a case like *"Root Cause Analysis"*
  below (`vector_search` + `graph_neighbors` + `path_traversal` +
  `temporal_window` together) is architecturally wired but wasn't observed
  actually firing as a chain.
- Every row in section 4's capability table beyond what "Log Investigation"
  already covers (`vector_search`/`fulltext_search`) — those ratings remain
  this pipeline's own documented design/scope, unchanged by this test round
  in either direction.

---

## 1. The four ways a question gets answered

| Mode | What runs | When to use it |
|---|---|---|
| `text2cypher` | Question → generated Cypher (read-only guarded) → rows | Structural/counting/filtering/ranking questions |
| `graphrag` | Vector/fulltext seed logs → one-hop entity expansion → LLM synthesis | Vague, descriptive, "why/explain" questions |
| `hybrid` | Runs both of the above and combines them | When you're not sure which fits |
| `auto` (**planner**) | An LLM (qwen3:14b) decides which tool(s) to call, across up to 3 rounds, then fuses the evidence | Compound questions needing more than one retrieval method |

`auto` is the only mode that can genuinely combine methods per-question — the
other three are fixed strategies picked up front.

---

## 2. The 9 tools the planner (and the other modes) draw on

| Tool | Does | Needs |
|---|---|---|
| `vector_search` | Semantic search over log message text via the embedding index | Embeddings ingested (`--embed` or `embed_logs.py`) — else falls back to fulltext |
| `fulltext_search` | Exact keyword/error-code search | Always available (Neo4j fulltext index) |
| `graph_neighbors` | 1-3 hop structural traversal out from one named entity | Entity resolvable via live schema constraints |
| `path_traversal` | Shortest structural path between two named entities (up to 6 hops) | Same as above, two entities |
| `temporal_window` | Chronologically ordered logs in a time range | Always available |
| `parent_child_retrieval` | Expand one already-known log into every other log sharing its operation/trace/task id | A log id from an earlier tool call in the same conversation |
| `query_template` | 3 deterministic Cypher templates: `top_hosts_by_severity`, `count_by_severity`, `host_inventory` | Always available |
| `pattern_match` | 5 curated vSphere/VMware failure signatures: `ha_restart`, `apd`, `host_isolation`, `network_partition`, `snapshot_failure` | Always available (fulltext index) |
| `text2cypher` | Fallback for anything with no matching template | LLM reachable |

`graph_neighbors`/`path_traversal` deliberately traverse **only** a whitelist
of low-cardinality structural relationships — never the six relationship
types that touch `:Log` directly (117M edges in the reference corpus), which
would blow past any hop limit before returning.

**Reranking (Context Fusion):** before pooled evidence reaches the
final-answer prompt, `reranker.py` deduplicates by log id and scores the pool
with a cross-encoder (`BAAI/bge-reranker-v2-m3`) against the actual question —
so results from one tool call don't crowd out better evidence from another.
Only applies to free-text log evidence; tabular/entity results (`graph_neighbors`,
`text2cypher`, `query_template`) keep their own tool's ordering.

---

## 3. Reliability guarantees (actually enforced, not aspirational)

- Every tool explicitly reports "no database connection" rather than
  misreporting it as "0 results" or "not found"
- An unknown/malformed tool call from the LLM is skipped and logged — one bad
  planning round doesn't abort the question
- If planning produces nothing usable across every round, a fixed
  `vector_search` + `text2cypher` fallback still runs
- Hard caps (`CHATBOT_PLANNER_MAX_ROUNDS`, `CHATBOT_PLANNER_MAX_TOOL_CALLS`)
  bound worst-case latency/cost
- Named entities resolve against Neo4j's live `SHOW CONSTRAINTS`, never a
  hardcoded copy of the schema that could drift
- Every external dependency (Neo4j, Ollama, embedding model, reranker) has a
  defined degraded-mode behavior — the chatbot always returns *something*,
  never a hard crash

---

## 4. Feature capability — what's real vs approximated vs missing

Measured against a realistic "AI ops engineer" feature list.

### ✅ Fully covered — the exact combination of methods genuinely runs

| Feature | Example | Methods |
|---|---|---|
| Root Cause Analysis | *"Why did VM-125 shut down?"* | Planner: `vector_search` + `graph_neighbors` + `path_traversal` + `temporal_window` |
| Backtracking | *"What caused this failure?"* | `graph_neighbors` + `path_traversal` |
| Dependency Analysis | *"What depends on this datastore?"* | `graph_neighbors` |
| Impact Analysis | *"Which VMs were affected by ESXi-05 failure?"* | `graph_neighbors` + `text2cypher` |
| Log Investigation | *"Find logs related to this issue"* | `vector_search` + `fulltext_search` |
| Time-Based Investigation | *"What happened between 2 PM and 4 PM?"* | `temporal_window` + filters + `vector_search` |
| Incident Timeline | *"Create timeline of outage"* | `temporal_window` (ordered) + LLM narration |
| Infrastructure Relationship Queries | *"Which host runs VM-125?"* | `graph_neighbors` / `text2cypher` |
| Aggregation Queries | *"Which host has most failures?"* | `query_template` + `text2cypher` fallback |
| Unknown Graph Questions | *"Which VMs moved after storage failure?"* | Planner: `vector_search`/`temporal_window` + `text2cypher` |

*(Caveat that applies to all ten equally: quality still depends on qwen3:14b
picking sensible tools/params per question — the mechanism is 100% real, a
perfect answer every time isn't guaranteed for any LLM-driven system.)*

### ⚠️ Partially covered — approximated, not the literal feature

| Feature | Gap |
|---|---|
| Blast Radius Analysis | Traversal capped at 1-3/1-6 hops with no redundancy/HA modeling — real connectivity, not a cascading-failure simulator |
| Error Explanation | `fulltext_search`+`vector_search`+`pattern_match` cover it except the "Documentation RAG" half — no vendor KB corpus is ingested anywhere in this project |
| Inventory Queries | Only `host_inventory` exists as a template; "VMs in Cluster-A" falls through to `text2cypher`, not a dedicated template |
| RCA Report Generation | Context fusion + LLM generation produces a prose answer, not a structured multi-section report |
| Health Summary | `count_by_severity`/`top_hosts_by_severity` approximate it; no single unified "overall health" aggregate |
| Change Correlation | `temporal_window` + severity filter approximates "before failure"; there's no distinct "change event" node type |

### ❌ Not covered — real, standing gaps

| Feature | Why |
|---|---|
| Similar Incident Search | `vector_search` finds similar *individual log lines*, not "has this incident happened before" — no incident-level embeddings, no graph similarity clustering |
| Configuration Lookup | The graph is built from log text, not vCenter inventory — no rich VM config (CPU/memory/network) to query |
| Conversation Follow-up | `/api/ask` is stateless — no session id, no server-side history |

---

## 5. What would close the remaining gaps

Each of these is a scoped, standalone piece of work, not a rewrite:

- **Documentation RAG** — ingest a VMware KB/doc corpus into a second,
  chunked-document embedding index
- **Incident-level similarity** — define what "one incident" is (e.g. a
  cluster of correlated logs), embed it as a unit, add a similarity tool
- **Conversation memory** — thread a conversation id from the frontend,
  add a history buffer or entity-tracking layer to `/api/ask`
- **VM configuration data** — would need a source other than logs (e.g. a
  vCenter inventory pull), since configuration rarely appears in log text
- **Structured RCA report / health-summary templates** — new `query_template`
  entries and a dedicated answer-formatting prompt, not new retrieval methods
