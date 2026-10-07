# AGENTS.md

Python MCP server exposing an OpenSearch cluster as MCP tools/resources. Single package `src/search_mcp/`, managed with `uv` (Python >=3.11, stdlib `tomllib`).

## Commands
- Run tests: `uv run pytest` (204 tests across 8 files; coverage always printed via `addopts`).
- Focused run: `uv run pytest tests/test_server_unit.py -k search`.
- Run server (dev): `uv run python3 src/search_mcp/server.py`. Only this entrypoint supports `--config/--host/--port/--transport` and applies the auth/body-limit/rate-limit middleware.
- Inspect via SDK CLI: `uv run mcp run src/search_mcp/server.py:mcp --transport streamable-http`. The `mcp run` CLI takes NO `--config`/`--port` (config is auto-discovered from cwd) and never applies the `api_key` auth middleware.
- No linter/typecheck/formatter configured — don't invent one.

## Config
- Precedence: CLI flags > `OPENSEARCH_*` / `OPENSEARCH_MCP_*` env vars > `config.toml` > defaults. Discovery order: `$OPENSEARCH_CONFIG`, `./config.toml`, `./opensearch-config.toml`, `~/.config/opensearch-mcp/config.toml`, `/etc/opensearch-mcp/config.toml`.
- The tracked `config.toml` binds `[server]` port `9163` with an `api_key` set — a default run requires `Authorization: Bearer <api_key from config.toml>`. Client connection samples: `agents/opencode.json` and `agents/hermes.json` (fill in the key).
- Non-loopback bind without an `api_key` is refused (fail-closed in `build_app`, server.py:292).
- Tool allowlist: edit `[tools].enabled_tools` in `config.toml` and restart. Applied in both `main()` and the lifespan, so it gates `mcp run` too. Unknown names silently ignored; absent key = all 27 tools.

## Layout
- `server.py` — MCP surface: 27 `@mcp.tool()` wrappers, 3 `@mcp.resource()`, `main()`, `build_app()` (Starlette middleware + `/healthz`, `/readyz`, `/metrics`). Uses the MCP SDK v2 `MCPServer` API — **not** FastMCP.
- `tools.py` — all OpenSearch API calls. Everything funnels through `OpenSearchTools.safe_call`, which normalizes `bool` → `{"result": ...}` and exceptions → `{"error": ..., "message": ...}`.
- `connection.py` — config layering + `build_client()`; `load_config_raw()` feeds tool filtering.
- `observability.py` — structured logs, Prometheus metrics, health endpoints.

## Conventions & gotchas
- Tool wrappers return JSON **strings**, not dicts (tests `json.loads()` the output).
- JSON-typed args (`query`, `document`, `doc`, `mappings`, `settings`, `documents`) are `Any`: accept a `str` or dict. Parse with `_parse_json` and guard with `@_json_errors` so malformed JSON becomes a `bad_request` response instead of raising. Wrap any new JSON-taking tool the same way (see `search`, server.py:430).
- `search` clamps `size` to 10000 and `from_ >= 0`; `bulk_index` requires `documents` to be a JSON array.
- `cat_indices` delegates to `list_indices` (both hit the cat API).
- Resources open a **fresh client per request** via `_tools_client()` (server.py:693); don't assume the lifespan client for resources.
- Edit `src/`, never `build/` (setuptools artifact). `uv.lock` is gitignored — `pyproject.toml` is the dependency source of truth.
- `agents/skill/searchmcp/SKILL.md` documents the deployed 10-tool read-only surface; keep it in sync when the allowlist or tool signatures change.

## Tests
- Integration tests (`tests/test_tools.py`) hit a real cluster at `localhost:9200` (`admin` / `YourStrongP@ssword123!`, override via `OPENSEARCH_PASSWORD`); they auto-skip when unreachable. All other suites are offline via `unittest.mock`, so the suite is green without a cluster.
- README's "44 tests" count is stale; the current suite is 204 tests.