# searchMCP

MCP (Model Context Protocol) server for interacting with an **OpenSearch** database.
Provides tools and resources for cluster management, index/document CRUD, search,
SQL queries, and more — exposed as a standards-compliant MCP server.

## Install & run (recommended for users)

The **`searchMCP`** command is the primary way to run the MCP server. It is
installed as a console script alongside the package and supports the full set
of runtime flags: `--config`, `--host`, `--port`, and `--transport`.

Install from source (Python 3.11+):

```bash
pip install .          # from the repo root
# or, to pick up code changes without reinstalling:
pip install -e .
```

Then start the server. It auto-discovers `./config.toml` in the current
directory, or the file at `$OPENSEARCH_CONFIG`:

```bash
searchMCP                                            # run with ./config.toml (auto-discovered)
searchMCP --port 8001 --transport streamable-http   # override the port / transport on the fly
searchMCP --config /path/to/config.toml             # use an explicit config file
searchMCP --host 0.0.0.0 --port 9000                # override the bind host / port
```

By default the server listens on http://127.0.0.1:8000 and exposes
`/healthz`, `/readyz`, and `/metrics` for health checks and Prometheus
metrics.

All runtime flags apply to the `searchMCP` console script (and to direct
execution via `uv run python3 src/search_mcp/server.py`):

| Flag          | Default            | Description                              |
|---------------|--------------------|------------------------------------------|
| `--config`    | (auto-discovered)  | Path to the TOML config file             |
| `--host`      | `127.0.0.1`        | Bind host for the HTTP server            |
| `--port`      | `8000`             | HTTP port                                |
| `--transport` | `streamable-http`  | MCP transport (`streamable-http`, `stdio`, `sse`) |

> **Authentication caveat:** the API-key / rate-limit / body-limit middleware
> is applied by this entrypoint only (see
> [Authentication](#authentication-api-key)). If you set `[server].api_key`
> in `config.toml`, clients must send `Authorization: Bearer <key>` to reach
> the MCP endpoint; requests without it get `401`.

## Developer quick start (uv)

`uv` is for developers iterating on the source — it manages the virtualenv,
lockfile, and dependency pins. Users should prefer the installed `searchMCP`
command above.

```bash
cd <repo-root>   # repo root (contains config.toml, src/, pyproject.toml)
```

### Option A — `mcp run` (dev/inspection, no `--config` flag)

The `mcp run` CLI does **NOT** accept `--config` — supply the config file
via auto-discovery or the `OPENSEARCH_CONFIG` env var instead:

```bash
# A1. Auto-discovery: run from the project root (picks up ./config.toml)
uv run mcp run src/search_mcp/server.py:mcp --transport streamable-http

# A2. Explicit path via env var:
OPENSEARCH_CONFIG=/path/to/config.toml \
  uv run mcp run src/search_mcp/server.py:mcp --transport streamable-http

# A3. Env vars (no config file at all):
OPENSEARCH_PASSWORD='YourStrongP@ssword123!' \
  uv run mcp run src/search_mcp/server.py:mcp --transport streamable-http
```

### Option B — direct execution (supports `--config`, `--port`, `--host`, `--transport`)

```bash
uv run python3 src/search_mcp/server.py --config config.toml --port 8001 --transport streamable-http
```

Config precedence for the MCP server port: `--port` CLI flag > `OPENSEARCH_MCP_PORT` env var > `config.toml [server].port` > built-in default (8000).

The `mcp run` CLI has no port flag — use direct execution when you need a non-default port.

Server starts on http://127.0.0.1:8000

## Configuration

### TOML config file (recommended)

Edit `config.toml` in the project root (auto-discovered) or point to any path:

```toml
[opensearch]
host = "localhost"
port = 9200
use_ssl = true
verify_certs = false
username = "admin"
password = "YourStrongP@ssword123!"
timeout = 30
```

### Environment variables (fallback)

| Variable                | Default     | Description                     |
|-------------------------|-------------|----------------------------------|
| `OPENSEARCH_CONFIG`     | —           | Path to TOML config file         |
| `OPENSEARCH_HOST`       | `localhost` | OpenSearch host                  |
| `OPENSEARCH_PORT`       | `9200`      | OpenSearch port                  |
| `OPENSEARCH_SSL`        | `true`      | Use HTTPS                        |
| `OPENSEARCH_VERIFY_CERTS` | `false`   | Verify TLS certificates          |
| `OPENSEARCH_USERNAME`   | `admin`     | HTTP basic auth username         |
| `OPENSEARCH_PASSWORD`   | (required)  | HTTP basic auth password         |
| `OPENSEARCH_TIMEOUT`    | `30`        | Request timeout (seconds)        |
| `OPENSEARCH_MCP_HOST`   | `127.0.0.1` | MCP server bind host             |
| `OPENSEARCH_MCP_PORT`   | `8000`      | MCP server port                  |
| `OPENSEARCH_MCP_TRANSPORT` | `streamable-http` | MCP transport             |
| `OPENSEARCH_MCP_API_KEY`   | `—`        | Bearer token protecting `/mcp` (empty disables) |
| `OPENSEARCH_MCP_RATE_LIMIT` | `0`      | Max requests per client per 60s (0 disables) |
| `OPENSEARCH_MCP_MAX_BODY_SIZE` | `0`   | Max HTTP request body in bytes (0 disables) |
| `OPENSEARCH_MCP_STRUCTURED_LOGS` | `false` | Emit JSON access/tool logs |
| `OPENSEARCH_MCP_ENABLE_METRICS` | `true` | Serve `/metrics` |

### Precedence

`--config` flag (direct run only) > `OPENSEARCH_*` / `OPENSEARCH_MCP_*` env vars > `config.toml` file > built-in defaults (auto-discovery: `./config.toml`, `./opensearch-config.toml`, `~/.config/opensearch-mcp/config.toml`, `/etc/opensearch-mcp/config.toml`)

## Authentication (API key)

Set `api_key` under `[server]` in `config.toml` to protect the MCP
endpoint with Bearer token authentication:

```toml
[server]
api_key = "sk-your-secret-key"
```

- Requests without an `Authorization: Bearer <key>` header → **401**.
- Requests with the wrong key → **401**.
- When the key is empty or omitted, auth is disabled (all requests pass through).

Auth works with the direct entrypoint only (`uv run python3 src/search_mcp/server.py`)
— it wraps the ASGI app before passing it to uvicorn.  The `mcp run` SDK CLI
does not support the middleware.

## Security hardening

Beyond the API key, the direct entrypoint layers limits at a single chokepoint
(`build_app()` in `server.py`):

- **Fail-closed bind guard** — refuses to start on a non-loopback host
  (`0.0.0.0`, etc.) without an `api_key` set.
- **Rate limiting** — fixed-window, per client (keyed by the hashed
  credential, else by client IP). `[server].rate_limit` = max requests per
  client per 60 s; `0` disables.
- **Body-size cap** — rejects requests whose `Content-Length` exceeds
  `[server].max_body_size` (bytes) with `413`; `0` disables.

These come from `[server]` in `config.toml` (or
`OPENSEARCH_MCP_RATE_LIMIT` / `OPENSEARCH_MCP_MAX_BODY_SIZE` env vars) and are
enabled by default in the shipped `config.toml`.

## Observability & runtime endpoints

- `GET /healthz` — liveness (process up). Public, no auth.
- `GET /readyz` — readiness; pings OpenSearch and returns `200`/`503`. Public.
- `GET /metrics` — Prometheus text format (HTTP + tool request/duration
  counters). `[server].enable_metrics` (default `true`); stays `503` if
  `prometheus-client` is not importable. Public.
- Every response echoes a **correlation `x-request-id`** (client-provided or
  generated hex); access and tool logs carry it.
- `[server].structured_logs = true` switches uvicorn + app logs to one JSON
  object per line.

## Tool visibility (config)

Control which tools the MCP server exposes via the `[tools]` section in
`config.toml`. Write to the config file, restart the server, done.

```toml
[tools]
enabled_tools = ["cluster_health", "search", "get_document"]
```

Rules:
- Key absent → all 27 tools registered (default).
- Key present → only the named tools are kept; everything else removed.
- Unknown tool names silently ignored.

For a **read-only** deployment (block all write operations):

```toml
[tools]
enabled_tools = ["cluster_health", "server_info", "list_indices", "get_index_mapping",
                 "search", "get_document", "count_documents", "sql_query",
                 "field_caps", "validate_query"]
```

The filter is applied in `main()` (direct execution) and in the lifespan
(`mcp run`), so the filtered surface is enforced before the first
`tools/list` request.

## Tools (27 total)

### Cluster
- `cluster_health` — cluster status, node/shard counts
- `cluster_stats` — comprehensive cluster statistics
- `node_info` — per-node metadata
- `server_info` — OpenSearch version and distribution

### Indices
- `list_indices` — catalog with doc counts and store sizes
- `get_index_mapping` — field mappings
- `get_index_settings` — index configuration
- `create_index` / `delete_index` / `index_exists`

### Documents
- `search` — full Query DSL with pagination and sorting (pass `source` as a
  field name or list to return only those fields instead of full documents)
- `get_document` — fetch by ID
- `index_document` — create or full-replace
- `update_document` — partial merge
- `delete_document` — remove by ID
- `count_documents` — count with optional filter
- `bulk_index` — batch insert with configurable ID field

### SQL Plugin
- `sql_query` — run SQL against OpenSearch
- `sql_explain` — show Query DSL translation

### Analysis
- `field_caps` — field types, aggregatable/searchable flags
- `explain_score` — why a document matched a query
- `validate_query` — dry-run query validation

### Cat API
- `cat_indices` / `cat_aliases` / `cat_nodes` / `cat_shards`

### Data Movement
- `reindex` — copy documents between indices with optional filter

## Examples

Tool calls return a uniform envelope: success is `{"result": ...}`, failures
are `{"error": "...", "message": "..."}`. JSON-typed arguments (`query`,
`document`, ...) accept either a dict or a JSON string.

`search` — match on `title`, return 10 hits including only the `id` field:

```json
{"index": "books", "query": {"match": {"title": "OpenSearch"}}, "size": 10, "source": ["id"]}
```

`index_document` — create or full-replace document `1`:

```json
{"index": "books", "doc_id": "1", "document": {"title": "Deep Learning", "author": "Goodfellow"}}
```

`count_documents` — count documents matching a query:

```json
{"index": "books", "query": {"match": {"author": "Goodfellow"}}}
```

`get_document` — fetch a document by ID:

```json
{"index": "books", "doc_id": "1"}
```

## Resources

| URI                                       | Description                 |
|-------------------------------------------|-----------------------------|
| `opensearch://health`                     | Cluster health snapshot     |
| `opensearch://indices`                    | Full index catalog          |
| `opensearch://index/{index}/mapping`      | Mapping for a specific index|

## Connecting an MCP client

Ready-made client configs live in `agents/`:

- `agents/opencode.json` — OpenCode client entry.
- `agents/hermes.json` — Hermes client entry; points at
  `http://127.0.0.1:9163/mcp`, matching the `[server].port` in the shipped
  `config.toml`.

Both assume the server is already running and, if `[server].api_key` is set,
that you've replaced the `Bearer <api_key>` placeholder with your key. Then
enable the searchMCP entry in your MCP client:

```bash
searchMCP            # start the server (or uv run python3 src/search_mcp/server.py)
```

## Tests

```bash
cd <repo-root>
uv run pytest -v
```

204 tests across 9 suites (`uv run pytest -v`, 99% line coverage)

## Architecture

```
src/search_mcp/
  __init__.py
  connection.py     — ConnectionConfig / ServerConfig with file/env/kwarg layering, client factory
  tools.py          — OpenSearchTools: all API calls + safe_call wrapper
  server.py         — MCPServer: 27 @mcp.tool(), 3 @mcp.resource(), build_app() chokepoint
  observability.py  — structured JSON logs, request-ID, /healthz /readyz /metrics
config.toml         — default configuration file (auto-discovered)
tests/              — 9 suites, 204 tests
  test_auth.py  test_connection.py  test_connection_helpers.py
  test_observability.py  test_server_hardening.py  test_server_static.py
  test_server_unit.py  test_tools.py  test_tools_unit.py
```
