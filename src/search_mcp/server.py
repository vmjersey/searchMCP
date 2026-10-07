"""MCP server for OpenSearch database interaction.

Provides tools and resources to interact with an OpenSearch cluster:
cluster health, index management, document CRUD, search, SQL queries, and more.

Config precedence: --config CLI flag > OPENSEARCH_* env vars > config.toml > defaults
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from search_mcp.connection import ConnectionConfig, ServerConfig, load_config_raw
from search_mcp.tools import OpenSearchTools

_CONFIG_PATH: str | None = None

MAX_SEARCH_SIZE = 10000

# Observability endpoint paths (public; exempt from auth/body-limit middleware).
healthz_path = "/healthz"
readyz_path = "/readyz"
metrics_path = "/metrics"

logger = logging.getLogger(__name__)


def _is_loopback(host: str) -> bool:
    """Return True if host is a loopback address or localhost."""
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    # Any 127.x.x.x is loopback.
    return host.startswith("127.")


def _set_config_path(path: str | None) -> None:
    global _CONFIG_PATH
    _CONFIG_PATH = path


# ── Lifespan ───────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[dict[str, Any]]:
    """Initialize OpenSearch client on startup, clean up on shutdown."""
    _filter_tools(config_path=_CONFIG_PATH)
    cfg = ConnectionConfig(config_path=_CONFIG_PATH)
    client = cfg.build_client()
    tools = OpenSearchTools(client)
    try:
        yield {"tools": tools}
    finally:
        try:
            client.transport.close()
        except Exception:
            logger.debug("OpenSearch client close failed", exc_info=True)


mcp = MCPServer("searchMCP", lifespan=lifespan)


def _get_tools(ctx: Context) -> OpenSearchTools:
    """Get OpenSearchTools from lifespan context."""
    return ctx.request_context.lifespan_context["tools"]


def _respond(result: Any) -> str:
    """Serialize a tool result (or error dict) as pretty JSON."""
    return json.dumps(result, indent=2, default=str)


def _call(method: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    """Invoke a bound tool method, normalize errors, and observe/trace it."""
    from search_mcp import observability

    tool = getattr(method, "__name__", "unknown")
    start = time.perf_counter()
    try:
        result = OpenSearchTools.safe_call(method, *args, **kwargs)
        status = "ok" if not (isinstance(result, dict) and "error" in result) else "error"
        return _respond(result)
    finally:
        duration_ms = (time.perf_counter() - start) * 1000.0
        observability.observe_tool(tool, status, duration_ms / 1000.0)
        logger.info(
            "tool_call",
            extra={"tool": tool, "status": status, "latency_ms": round(duration_ms, 2)},
        )


class _InvalidJSON(Exception):
    """Raised when a JSON-string tool argument cannot be parsed."""

    def __init__(self, field: str) -> None:
        super().__init__(f"Invalid JSON for argument '{field}'")
        self.field = field


def _parse_json(value: Any, field: str) -> Any:
    """Parse a JSON-string tool argument; non-strings pass through unchanged."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise _InvalidJSON(field) from exc


def _json_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Guard a tool wrapper: turn malformed-JSON args into a clean error response."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except _InvalidJSON as exc:
            return _respond({"error": "bad_request", "message": str(exc)})

    return wrapper


def _filter_tools(config_path: str | None = None) -> None:
    """Remove tools not in the config's enabled_tools list.

    If config.toml has a ``[tools]`` section with ``enabled_tools``,
    only the tools named in that list are kept; everything else is removed.
    If ``enabled_tools`` is absent or the config file has no ``[tools]``
    section, all tools remain (no-op).
    """
    raw = load_config_raw(config_path)
    tools_cfg = raw.get("tools", {})
    enabled_tools = tools_cfg.get("enabled_tools")

    if not enabled_tools:
        return  # no restrictions

    # Allowlist: keep only the named tools
    keep = set(enabled_tools)
    all_names = {t.name for t in mcp._tool_manager.list_tools()}
    to_remove = all_names - keep
    for name in sorted(to_remove):
        mcp._tool_manager.remove_tool(name)
    logger.info("Tools enabled: %d kept, %d removed", len(keep & all_names), len(to_remove))


# ── HTTP middleware (auth / body size / rate limit) ────────────────────


class _AuthMiddleware(BaseHTTPMiddleware):
    """Constant-time Bearer token check, applied at the app chokepoint."""

    def __init__(self, app: Any, api_key: str, exempt_paths: set[str] | None = None) -> None:
        super().__init__(app)
        self._api_key = api_key
        self._secret_bytes = api_key.encode("utf-8")
        self._exempt_paths = exempt_paths or set()

    async def dispatch(self, request: Request, call_next: Callable) -> Any:
        if self._exempt_paths and request.url.path in self._exempt_paths:
            return await call_next(request)
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"error": "unauthorized", "message": "Missing or invalid Authorization header"},
                status_code=401,
            )
        token = auth_header.removeprefix("Bearer ").encode("utf-8")
        # Constant-time compare — never leak length/content timing.
        if not hmac.compare_digest(token, self._secret_bytes):
            return JSONResponse(
                {"error": "unauthorized", "message": "Invalid API key"},
                status_code=401,
            )
        return await call_next(request)


class _BodyLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose declared Content-Length exceeds the cap."""

    def __init__(self, app: Any, max_body_size: int, exempt_paths: set[str] | None = None) -> None:
        super().__init__(app)
        self._max_body_size = max_body_size
        self._exempt_paths = exempt_paths or set()

    async def dispatch(self, request: Request, call_next: Callable) -> Any:
        if self._exempt_paths and request.url.path in self._exempt_paths:
            return await call_next(request)
        try:
            length = request.headers.get("content-length")
            if length is not None and int(length) > self._max_body_size:
                return JSONResponse(
                    {"error": "payload_too_large",
                     "message": f"Request body exceeds {self._max_body_size} bytes"},
                    status_code=413,
                )
        except (ValueError, TypeError):
            pass  # malformed header -> let the app handle it
        return await call_next(request)


class _RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window per-client rate limiting.

    Keyed by the (sha-256 hashed) credential when present, else by client IP.
    Bounded memory: per-window buckets are pruned on each request.
    """

    def __init__(self, app: Any, limit: int, window_seconds: float = 60.0) -> None:
        super().__init__(app)
        self._limit = limit
        self._window = window_seconds
        self._buckets: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _client_key(request: Request) -> str:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.removeprefix("Bearer ")
            return "cred:" + hashlib.sha256(token.encode("utf-8")).hexdigest()
        host = ""
        if request.client is not None:
            host = request.client.host or ""
        return "ip:" + host

    def _prune(self, now: float) -> None:
        for key in [k for k, bucket in self._buckets.items() if now - bucket["window_start"] > self._window]:
            self._buckets.pop(key, None)

    async def dispatch(self, request: Request, call_next: Callable) -> Any:
        key = self._client_key(request)
        now = time.monotonic()

        bucket = self._buckets.get(key)
        if bucket is None or now - bucket["window_start"] >= self._window:
            self._buckets[key] = {"window_start": now, "count": 1}
        else:
            bucket["count"] += 1
            if bucket["count"] > self._limit:
                return JSONResponse(
                    {"error": "rate_limited", "message": "Too many requests"},
                    status_code=429,
                )

        self._prune(now)
        return await call_next(request)


def build_app(
    host: str = "127.0.0.1",
    api_key: str = "",
    rate_limit: int = 0,
    max_body_size: int = 0,
    streamable_http_path: str = "/mcp",
    config_path: str | None = None,
    structured_logs: bool = False,
    enable_metrics: bool = True,
) -> Any:
    """Build the hardened MCP Starlette app.

    Single chokepoint every HTTP entrypoint must go through. Enforces:
      - fail-closed binding: refuse a non-loopback bind without an api_key
      - Bearer auth (constant time) when api_key is set
      - a request-body size cap when max_body_size > 0
      - per-client rate limiting when rate_limit > 0
    Adds observability:
      - /healthz liveness and /readyz readiness (pings OpenSearch)
      - /metrics Prometheus endpoint when enable_metrics is on and the lib exists
      - request-ID correlation + per-request access logs
    These runtime/health endpoints are exempt from auth so probes and scrapers
    work without credentials.
    0 for rate_limit/max_body_size disables that control.
    """
    from search_mcp import observability

    if not _is_loopback(host) and not api_key:
        raise ValueError(
            f"Refusing to run the MCP server on a non-loopback address (host={host!r}) "
            "without an API key. Set [server].api_key (or OPENSEARCH_MCP_API_KEY) "
            "or bind to a loopback host."
        )

    observability.setup_logging(structured_logs)

    public_paths = {healthz_path, readyz_path, metrics_path}

    app = mcp.streamable_http_app(streamable_http_path=streamable_http_path, host=host)

    # Runtime/observability routes (public).
    app.add_route("/healthz", observability.healthz, methods=["GET"])
    app.add_route(readyz_path, observability.readyz_factory(config_path), methods=["GET"])
    if enable_metrics:
        app.add_route(metrics_path, observability.render_metrics, methods=["GET"])

    # Middleware order (added inner-to-outer): body cap < auth < rate limit < request log.
    if max_body_size > 0:
        app.add_middleware(_BodyLimitMiddleware, max_body_size=max_body_size, exempt_paths=public_paths)
    if api_key:
        app.add_middleware(_AuthMiddleware, api_key=api_key, exempt_paths=public_paths)
        logger.info("API key authentication enabled")
    if rate_limit > 0:
        app.add_middleware(_RateLimitMiddleware, limit=rate_limit)
        logger.info("Rate limiting enabled: %d requests/60s per client", rate_limit)
    app.add_middleware(observability.RequestLogMiddleware)

    return app


# ── Cluster Tools ──────────────────────────────────────────────────────


@mcp.tool()
def cluster_health(ctx: Context) -> str:
    """Get the current OpenSearch cluster health status (green/yellow/red, node count, shards)."""
    return _call(_get_tools(ctx).cluster_health)


@mcp.tool()
def cluster_stats(ctx: Context) -> str:
    """Get comprehensive cluster statistics (indices, shards, docs, store size)."""
    return _call(_get_tools(ctx).cluster_stats)


@mcp.tool()
def node_info(ctx: Context) -> str:
    """Get information about all nodes in the cluster."""
    return _call(_get_tools(ctx).node_info)


@mcp.tool()
def server_info(ctx: Context) -> str:
    """Get OpenSearch server version and distribution info."""
    return _call(_get_tools(ctx).server_info)


# ── Index Tools ────────────────────────────────────────────────────────


@mcp.tool()
def list_indices(ctx: Context, pattern: str = "*") -> str:
    """List all indices matching a pattern with doc count and store size.

    Args:
        pattern: Index pattern to match (default: '*' for all)
    """
    return _call(_get_tools(ctx).list_indices, pattern)


@mcp.tool()
def get_index_mapping(ctx: Context, index: str) -> str:
    """Get the mapping (schema) for a specific index.

    Args:
        index: Name of the index
    """
    return _call(_get_tools(ctx).get_index_mapping, index)


@mcp.tool()
def get_index_settings(ctx: Context, index: str) -> str:
    """Get the settings for a specific index.

    Args:
        index: Name of the index
    """
    return _call(_get_tools(ctx).get_index_settings, index)


@mcp.tool()
@_json_errors
def create_index(
    ctx: Context,
    index: str,
    mappings: Any = None,
    settings: Any = None,
) -> str:
    """Create a new index with optional mappings and settings.

    Args:
        index: Name of the index to create
        mappings: Optional JSON string or dict of field mappings
        settings: Optional JSON string or dict of index settings
    """
    t = _get_tools(ctx)
    parsed_mappings = _parse_json(mappings, "mappings")
    parsed_settings = _parse_json(settings, "settings")
    return _call(t.create_index, index, parsed_mappings, parsed_settings)


@mcp.tool()
def delete_index(ctx: Context, index: str) -> str:
    """Delete an index.

    Args:
        index: Name of the index to delete
    """
    return _call(_get_tools(ctx).delete_index, index)


@mcp.tool()
def index_exists(ctx: Context, index: str) -> str:
    """Check if an index exists.

    Args:
        index: Name of the index
    """
    return _call(_get_tools(ctx).index_exists, index)


# ── Document Tools ─────────────────────────────────────────────────────


@mcp.tool()
@_json_errors
def search(
    ctx: Context,
    index: str,
    query: Any = None,
    size: int = 10,
    from_: int = 0,
    sort: str | None = None,
    source: str | list[str] | None = None,
) -> str:
    """Search documents in an index using OpenSearch Query DSL.

    Args:
        index: Index or comma-separated indices to search
        query: Optional JSON string or dict of the query DSL
               (e.g. '{"match": {"field": "value"}}').
               Omit to match all documents
        size: Number of results to return (default: 10, max: 10000)
        from_: Starting offset (default: 0)
        sort: Sort field and direction (e.g. 'field:asc')
        source: Optional field subset to return per document, as a field
                name (str) or list of field names. Passed to the OpenSearch
                ``_source`` parameter. Restricting _source is the standard way
                to keep large results small (e.g. a list of just the metadata
                fields instead of full document content).
    """
    t = _get_tools(ctx)
    parsed_query = _parse_json(query, "query")
    size = max(0, min(size, MAX_SEARCH_SIZE))
    from_ = max(0, from_)
    return _call(t.search, index, parsed_query, size, from_, sort, source=source)


@mcp.tool()
def get_document(ctx: Context, index: str, doc_id: str) -> str:
    """Get a document by its ID.

    Args:
        index: Name of the index
        doc_id: Document ID
    """
    return _call(_get_tools(ctx).get_document, index, doc_id)


@mcp.tool()
@_json_errors
def index_document(
    ctx: Context,
    index: str,
    document: str,
    doc_id: str | None = None,
    refresh: str = "false",
) -> str:
    """Index a document (create or full replace).

    Args:
        index: Name of the index
        document: JSON string of the document body
        doc_id: Optional document ID (auto-generated if omitted)
        refresh: Refresh policy ('true', 'false', or 'wait_for')
    """
    parsed_doc = _parse_json(document, "document")
    return _call(_get_tools(ctx).index_document, index, doc_id, parsed_doc, refresh)


@mcp.tool()
@_json_errors
def update_document(
    ctx: Context,
    index: str,
    doc_id: str,
    doc: str,
    refresh: str = "false",
) -> str:
    """Partially update a document by ID.

    Args:
        index: Name of the index
        doc_id: Document ID to update
        doc: JSON string of fields to update (merged into existing document)
        refresh: Refresh policy ('true', 'false', or 'wait_for')
    """
    parsed_doc = _parse_json(doc, "doc")
    return _call(_get_tools(ctx).update_document, index, doc_id, parsed_doc, refresh)


@mcp.tool()
def delete_document(ctx: Context, index: str, doc_id: str, refresh: str = "false") -> str:
    """Delete a document by ID.

    Args:
        index: Name of the index
        doc_id: Document ID to delete
        refresh: Refresh policy ('true', 'false', or 'wait_for')
    """
    return _call(_get_tools(ctx).delete_document, index, doc_id, refresh)


@mcp.tool()
@_json_errors
def count_documents(ctx: Context, index: str, query: Any = None) -> str:
    """Count documents in an index, optionally matching a query.

    Args:
        index: Name of the index
        query: Optional JSON string or dict query DSL to filter documents
    """
    t = _get_tools(ctx)
    parsed_query = _parse_json(query, "query")
    return _call(t.count_documents, index, parsed_query)


# ── SQL Plugin Tools ───────────────────────────────────────────────────


@mcp.tool()
def sql_query(ctx: Context, query: str) -> str:
    """Execute an OpenSearch SQL query.

    Args:
        query: SQL query string (e.g. 'SELECT * FROM my_index LIMIT 10')
    """
    return _call(_get_tools(ctx).sql_query, query)


@mcp.tool()
def sql_explain(ctx: Context, query: str) -> str:
    """Explain how an OpenSearch SQL query is translated to Query DSL.

    Args:
        query: SQL query string to explain
    """
    return _call(_get_tools(ctx).sql_explain, query)


# ── Field & Analysis Tools ─────────────────────────────────────────────


@mcp.tool()
def field_caps(ctx: Context, index: str, fields: str = "*") -> str:
    """Get field capabilities (types, aggregatable, searchable) for an index.

    Args:
        index: Name of the index
        fields: Comma-separated field patterns (default: '*' for all)
    """
    return _call(_get_tools(ctx).field_caps, index, fields)


@mcp.tool()
@_json_errors
def explain_score(ctx: Context, index: str, doc_id: str, query: str) -> str:
    """Explain why a specific document matches (or not) a query.

    Args:
        index: Name of the index
        doc_id: Document ID
        query: JSON query DSL
    """
    parsed_query = _parse_json(query, "query")
    return _call(_get_tools(ctx).explain_score, index, doc_id, parsed_query)


@mcp.tool()
@_json_errors
def validate_query(ctx: Context, index: str, query: str, explain: bool = False) -> str:
    """Validate a query without executing it.

    Args:
        index: Name of the index
        query: JSON query DSL to validate
        explain: If True, also return explanation of how the query is executed
    """
    parsed_query = _parse_json(query, "query")
    return _call(_get_tools(ctx).validate_query, index, parsed_query, explain)


# ── Cat API Tools ──────────────────────────────────────────────────────


@mcp.tool()
def cat_indices(ctx: Context) -> str:
    """Fast index listing via cat API (index name, status, docs, store size)."""
    return _call(_get_tools(ctx).list_indices)


@mcp.tool()
def cat_aliases(ctx: Context) -> str:
    """List all aliases via cat API."""
    return _call(_get_tools(ctx).cat_aliases)


@mcp.tool()
def cat_nodes(ctx: Context) -> str:
    """List nodes with disk, heap, and load info via cat API."""
    return _call(_get_tools(ctx).cat_nodes)


@mcp.tool()
def cat_shards(ctx: Context, index: str | None = None) -> str:
    """List shard allocation across nodes via cat API.

    Args:
        index: Optional index name to filter shards
    """
    return _call(_get_tools(ctx).cat_shards, index)


# ── Bulk ───────────────────────────────────────────────────────────────


@mcp.tool()
@_json_errors
def bulk_index(
    ctx: Context,
    index: str,
    documents: str,
    id_field: str | None = None,
    refresh: str = "false",
) -> str:
    """Bulk index multiple documents.

    Args:
        index: Target index name
        documents: JSON array of document objects
        id_field: Optional field name to use as document ID
        refresh: Refresh policy ('true', 'false', or 'wait_for')
    """
    parsed_docs = _parse_json(documents, "documents")
    if not isinstance(parsed_docs, list):
        return _respond({"error": "documents must be a JSON array"})
    return _call(_get_tools(ctx).bulk_index, index, parsed_docs, id_field, refresh)


# ── Reindex ────────────────────────────────────────────────────────────


@mcp.tool()
@_json_errors
def reindex(
    ctx: Context,
    source_index: str,
    dest_index: str,
    query: Any = None,
    wait_for_completion: bool = True,
) -> str:
    """Reindex documents from source to destination index.

    Args:
        source_index: Source index name
        dest_index: Destination index name
        query: Optional JSON string or dict query DSL to filter which documents to reindex
        wait_for_completion: Wait for reindex to complete (default: True)
    """
    t = _get_tools(ctx)
    parsed_query = _parse_json(query, "query")
    return _call(t.reindex, source_index, dest_index, parsed_query, wait_for_completion)


# ── Resources ──────────────────────────────────────────────────────────
# Read-only data access: cluster info, index catalog, and documents.


@contextmanager
def _tools_client() -> Iterator[OpenSearchTools]:
    """Yield an OpenSearchTools instance, closing its client afterwards."""
    cfg = ConnectionConfig(config_path=_CONFIG_PATH)
    client = cfg.build_client()
    try:
        yield OpenSearchTools(client)
    finally:
        client.transport.close()


@mcp.resource("opensearch://health")
def resource_health() -> str:
    """Cluster health as a resource."""
    with _tools_client() as tools:
        return _respond(OpenSearchTools.safe_call(tools.cluster_health))


@mcp.resource("opensearch://indices")
def resource_indices() -> str:
    """Index catalog as a resource."""
    with _tools_client() as tools:
        return _respond(OpenSearchTools.safe_call(tools.list_indices))


@mcp.resource("opensearch://index/{index}/mapping")
def resource_mapping(index: str) -> str:
    """Index mapping as a resource."""
    with _tools_client() as tools:
        return _respond(OpenSearchTools.safe_call(tools.get_index_mapping, index))


# ── Entry point ────────────────────────────────────────────────────────


def _structured_logs_from_env_or_config() -> bool:
    """Early check for structured logging before ServerConfig is constructed."""
    env = os.environ.get("OPENSEARCH_MCP_STRUCTURED_LOGS")
    if env is not None:
        return env.lower() in ("true", "1", "yes")
    raw = load_config_raw(_CONFIG_PATH)
    return bool(raw.get("server", {}).get("structured_logs", False))


def _json_logging_config() -> dict[str, Any]:
    """Return a uvicorn log_config that emits JSON via JsonFormatter.

    Replaces uvicorn's default rich console setup so every log line (including
    our access/tool records) is a single parseable JSON object on one line.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {"()": "search_mcp.observability.JsonFormatter"},
        },
        "handlers": {
            "default": {"formatter": "json", "class": "logging.StreamHandler", "stream": "ext://sys.stderr"},
            "access": {"formatter": "json", "class": "logging.StreamHandler", "stream": "ext://sys.stdout"},
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO", "handlers": ["default"], "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        },
        "root": {"handlers": ["default"], "level": "INFO"},
    }


def main() -> None:
    """Run the OpenSearch MCP server.

    Parses --config / --port before delegating to mcp.run().
    Server bind host/port/transport come from [server] in config.toml
    (overridable via OPENSEARCH_MCP_* env vars or --port/--transport flags).
    Note: `mcp run` CLI cannot set the port; use this entrypoint directly
    (uv run python3 src/search_mcp/server.py) to control the port.
    """
    parser = argparse.ArgumentParser(description="OpenSearch MCP Server")
    parser.add_argument(
        "--config",
        help="Path to TOML config file (default: ./config.toml or $OPENSEARCH_CONFIG)",
        default=None,
    )
    parser.add_argument(
        "--host",
        help="MCP server bind host (default: 127.0.0.1 or config [server].host)",
        default=None,
    )
    parser.add_argument(
        "--port",
        type=int,
        help="MCP server port (default: 8000 or config [server].port)",
        default=None,
    )
    parser.add_argument(
        "--transport",
        help="MCP transport (streamable-http, stdio, sse)",
        default=None,
    )
    args = parser.parse_args()

    if args.config:
        _set_config_path(args.config)

    if _structured_logs_from_env_or_config():
        import logging.config

        logging.config.dictConfig(_json_logging_config())

    _filter_tools(config_path=_CONFIG_PATH)

    server_cfg = ServerConfig(
        host=args.host,
        port=args.port,
        transport=args.transport,
        config_path=_CONFIG_PATH,
    )

    if server_cfg.transport == "stdio":
        mcp.run(transport="stdio")
        return

    import uvicorn

    app = build_app(
        host=server_cfg.host,
        api_key=server_cfg.api_key,
        rate_limit=server_cfg.rate_limit,
        max_body_size=server_cfg.max_body_size,
        config_path=_CONFIG_PATH,
        structured_logs=server_cfg.structured_logs,
        enable_metrics=server_cfg.enable_metrics,
    )
    if server_cfg.structured_logs:
        uvicorn.run(
            app,
            host=server_cfg.host,
            port=server_cfg.port,
            log_level="info",
            log_config=_json_logging_config(),
        )
    else:
        uvicorn.run(app, host=server_cfg.host, port=server_cfg.port, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    main()
