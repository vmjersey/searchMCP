---
name: searchmcp
description: SearchMCP — query the OpenSearch cluster behind the MCP server and its 27 tools (10 exposed). Use when the user asks about the OpenSearch/Elasticsearch data: which indices exist, an index's mapping or field types, searching or counting documents (e.g. analyzer-docs), reading a doc by id, running SQL over an index, or checking cluster/version health. Trigger words: SearchMCP, OpenSearch, Elasticsearch, index, mapping, field types, query DSL, match/term/bool query, analyzer-docs, cluster health, document.
---

# SearchMCP — Driving the OpenSearch Cluster

SearchMCP is an MCP server in front of an **OpenSearch** cluster (current deployment: OpenSearch 3.8.0, cluster `docker-cluster`, single node). Every tool returns pretty-printed JSON. This skill's core operation is to **map the surface before you dig**: orient on cluster → enumerate indices → read the schema of the ones you'll touch → then run queries you can already justify.

## deployment facts

- **10 of the 27 server tools are exposed** (read-only allowlist set in `config.toml [tools].enabled_tools`). The other 17 (all writes, cat API, analysis extras) exist in code but are **not reachable from this session** — if the user needs one, enable it in `config.toml` and restart the server.
- Tools are reached by name only; there are no namespaces. Resolve the index first, then the doc/query.
- Errors come back as a shaped dict — always read the top-level keys before narrating success:
  - `{"error": "not_found", "message": ...}` — missing index or document
  - `{"error": "bad_request", "message": ...}` — malformed query/body
  - `{"error": "transport_error", "status": N, "message": ...}` — connection or HTTP-level failure
  - `{"error": "<TypeName>", "message": ...}` — anything else; read the message

## mapping the surface — the steps

Follow these in order. Each step ends when its criterion is met; never skip a step just because the user named an index.

1. **Orient** — call `cluster_health` (status, node/shard counts, `unassigned_shards`) and `server_info` (version/distribution).
   Done when: you know the cluster is reachable and its status/version.
2. **Enumerate** — call `list_indices` with the broadest pattern that covers the topic (e.g. `"analyzer-*"` or `"*"`).
   Done when: you have the index names the user could be referring to, with doc counts.
3. **Read the schema** of the exact index(es) you'll query — `get_index_mapping` for the field types, plus `field_caps` when you need searchable/aggregatable flags.
   Done when: you know which fields are `keyword` vs `text` vs numeric — this decides the query clauses you can use.
4. **Dig** — search, count, or read documents to answer the actual question.
   Done when: the user's question is answered from data, not unsupported inference.
5. **Verify before you over-claim** — for anything expensive or surprising, run `validate_query` (with `explain=true`) first and confirm `valid` before executing at scale.

## tool reference

Signatures as exposed to the agent. `query`/`document` params accept either a JSON **string** or a structured **object** — the server parses strings with `json.loads`.

### cluster + server

| Tool | Args | Returns (real shapes) |
|---|---|---|
| `cluster_health` | — | `{"cluster_name": ..., "status": "yellow", "number_of_nodes": 1, "active_shards": 8, "unassigned_shards": 1, "active_shards_percent_as_number": 88.9, ...}` |
| `server_info` | — | `{"name": ..., "cluster_name": "docker-cluster", "version": {"distribution": "opensearch", "number": "3.8.0", "build_type": "tar", "lucene_version": "10.5.0"}, "tagline": "..."}` |

### indices

| Tool | Args | Returns (real shapes) |
|---|---|---|
| `list_indices` | `pattern` (default `"*"`, supports `analyzer-*`) | flat array: `[{"index": "analyzer-docs", "health": "yellow", "status": "open", "docs_count": "13", "docs_deleted": "0", "store_size": "123.8kb", "primary_store_size": "123.8kb", "number_of_shards": "1", "number_of_replicas": "1", "uuid": ...}]` |
| `get_index_mapping` | `index` (required) | `{"<index>": {"mappings": {"properties": {"<field>": {"type": "..."}}}}}` |
| `get_index_settings` | `index` | index-level settings dict *(not exposed today)* |
| `create_index` / `delete_index` / `index_exists` | `index` (+ `mappings`/`settings` for create) | acknowledges / `{"result": true\|false}` *(not exposed today)* |

`list_indices` returns **strings** for counts/sizes (`"13"`, `"123.8kb"`) — cast before math. The live catalog:

| index | docs | store |
|---|---|---|
| `analyzer-docs` | 13 | 123.8kb |

### documents

| Tool | Args | Returns |
|---|---|---|
| `search` | `index` (req), `query` (opt DSL, default match-all), `size` (default 10, **capped at 10000**), `from_` (default 0), `sort` (e.g. `"size_bytes:desc"`, single field) | `{"took": 7, "hits": {"total": {"value": 13, "relation": "eq"}, "hits": [{"_index": ..., "_id": ..., "_score": ..., "_source": {<full doc>}}]}}` |
| `get_document` | `index`, `doc_id` | `{"_index": ..., "_id": ..., "_version": 1, "found": true, "_source": {<full doc>}}` |
| `count_documents` | `index` (req), `query` (opt DSL) | `{"count": 13, "_shards": {...}}` |
| `index_document` / `update_document` / `delete_document` / `bulk_index` | JSON body + optional `doc_id`, `id_field`, `refresh` (`"false"`, `"true"`, `"wait_for"`) | opensearch-py response dicts *(not exposed today)* |

- Omit `query` to match all docs. Return `size` is the page length — page with `from_` (deep pagination is unreliable past ~10k; use SQL or scroll if needed).
- `search` hits live at `hits.hits[]`, full document at `_source`. Doc `_id`s are often meaningful (e.g. `VOTE_MODE.md`).

### sql

| Tool | Args | Returns |
|---|---|---|
| `sql_query` | `query` (req, SQL string) | `{"schema": [{"name": "...", "type": "keyword\|text\|long"}], "datarows": [[...]], "total": N}` — fetch_size (10000) page size, `size`/`page` params in the body for paging |
| `sql_explain` | `query` | the SQL→DSL translation tree *(not exposed today)* |

- The **SQL table name is the index name**. Quote names with hyphens: `SELECT * FROM "analyzer-docs" LIMIT 5`.
- Returns **`schema` + `datarows`**, not `hits`. Prefer SQL for aggregate/summary questions over Query DSL.
- Invalid SQL / unknown index → error dict (e.g. `bad_request`), not an empty result.

### analysis + validation

| Tool | Args | Returns |
|---|---|---|
| `field_caps` | `index` (req), `fields` (default `"*"`, comma-separated patterns) | `{"indices": ["<index>"], "fields": {"<field>": {"<type>": {"type": ..., "searchable": true, "aggregatable": false}}}}` |
| `validate_query` | `index`, `query` (req), `explain` (default false) | `{"_shards": {...}, "valid": true, "explanations": [{"index": ..., "valid": true, "explanation": "title:vote title:mode"}]}` |
| `explain_score` | `index`, `doc_id`, `query` | why a doc matches a query *(not exposed today)* |

Use `field_caps` on live data before designing a query: it tells you whether `title` is analyzable `text` (search, not aggregatable) vs `filename`/`path` exact `keyword` (aggregatable).

## query DSL quick reference

Build clause dicts and omit the top-level `query` wrapper (the server adds it):

| Goal | Clause |
|---|---|
| Full-text search on `text` field | `{"match": {"title": "vote mode"}}` |
| Exact match on `keyword` field | `{"term": {"filename": "VOTE_MODE.md"}}` |
| Phrase | `{"match_phrase": {"content": "majority vote"}}` |
| Numeric/date range | `{"range": {"size_bytes": {"gte": 5000}}}` |
| Compose | `{"bool": {"must": [...], "filter": [...], "should": [...], "must_not": [...]}}` |
| Prefix / fuzzy | `{"prefix": {"filename": "VOTE"}}` |

- `match` on a `keyword` field matches the whole analyzed term — prefer `term` for keywords, `match`/`match_phrase` for `text`.
- `text` fields are **not aggregatable**; `keyword` and `long` fields are. `field_caps` confirms per-field.
- Combine `count_documents` with a query when the user asks "how many" — it is cheaper than `search` and returns an exact `count`.

## recipes

- **"What's in index X?"** — `list_indices`, then `search` with no query (`size=10`) to see the doc shape, then `count_documents` for the total.
- **"Find docs about / matching ..."** — `field_caps` the index, pick `text` fields for `match`, run `search` with `size` sized to the answer (default 10 is small — raise it).
- **"How many / summarize / tabulate"** — `sql_query`: `SELECT title, filename, num_lines FROM "analyzer-docs" WHERE size_bytes > 5000 ORDER BY num_lines DESC LIMIT 20`.
- **"Schema/field types"** — `get_index_mapping` then `field_caps` on the same index.
- **"Is this query legal?"** — `validate_query` with `explain=true`, trust `valid` (and read `explanations[].explanation` for intent).
- **"Why did doc D match?"** — `get_document` for the source, `explain_score` for the score breakdown.
- **"Is the cluster healthy?"** — `cluster_health`; `status` of `yellow`/`red` with nonzero `unassigned_shards` or `relocating_shards` is the story to surface.

## categories already covered

Rule of thumb when a table exists for a tool category: prefer it over a bespoke sequence.
- **Surface discovery**: cluster_health / server_info / list_indices / get_index_mapping / field_caps — one pass yields context for everything else.
- **Document access**: search / get_document / count_documents — read-only; safe to run.
- **SQL**: sql_query — prefer for aggregations and summaries.
- **Write + admin** (not exposed): create/delete index, index/update/delete/bulk document, reindex, cat_* — need config changes to reach.