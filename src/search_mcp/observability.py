"""Observability: structured logs, request correlation, health & metrics.

Provides:

  - opt-in JSON structured logging (stdlib formatter, no dependency)
  - a request-ID + access-log middleware (method, path, status, latency)
  - /healthz (liveness) and /readyz (readiness -> pings OpenSearch)
  - Prometheus /metrics (only when `prometheus_client` is importable)

Everything is safe to import with no side effects; enabling knobs happen at
app-build time.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Correlation ID propagated through a single request's log lines.
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


def get_request_id() -> str:
    """Return the active request ID (empty outside a request)."""
    return _request_id.get()


# ── Structured logging ────────────────────────────────────────────────


class JsonFormatter(logging.Formatter):
    """Format a LogRecord as a single line of JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": _request_id.get(),
            "message": record.getMessage(),
        }
        for key in ("tool", "method", "path", "status", "latency_ms"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(structured: bool) -> None:
    """Set a JSON formatter on the server loggers' handlers if structured is on.

    When structured logging is requested the caller normally hands uvicorn a
    JSON ``log_config`` (see server.main), which installs JsonFormatter on the
    uvicorn + root loggers.  This helper additionally swaps any plain-text
    formatter already present on this module's logger so direct embeddings stay
    consistent.
    """
    if not structured:
        return
    fmt = JsonFormatter()
    for handler in logger.handlers:
        if not isinstance(handler.formatter, JsonFormatter):
            handler.setFormatter(fmt)


# ── Metrics (guarded) ─────────────────────────────────────────────────

_ENABLED = False
try:
    from prometheus_client import (  # type: ignore
        CONTENT_TYPE_LATEST,
        Counter,
        Histogram,
        generate_latest,
    )

    _ENABLED = True
except Exception:  # noqa: BLE001  # pragma: no cover - prometheus_client absent
    _ENABLED = False

if _ENABLED:
    _HTTP_REQUESTS = Counter(
        "search_mcp_http_requests_total",
        "HTTP requests handled.",
        ["method", "path", "status"],
    )
    _HTTP_DURATION = Histogram(
        "search_mcp_http_request_duration_seconds",
        "HTTP request duration.",
        ["method", "path"],
    )
    _TOOL_CALLS = Counter(
        "search_mcp_tool_calls_total",
        "Tool invocations.",
        ["tool", "status"],
    )
    _TOOL_DURATION = Histogram(
        "search_mcp_tool_duration_seconds",
        "Tool execution duration.",
        ["tool"],
    )


def metrics_available() -> bool:
    """True when prometheus_client is installed and metrics can be served."""
    return _ENABLED


def observe_request(method: str, path: str, status: int, duration: float) -> None:
    if not _ENABLED:
        return
    _HTTP_REQUESTS.labels(method, path, str(status)).inc()
    _HTTP_DURATION.labels(method, path).observe(duration)


def observe_tool(tool: str, status: str, duration: float) -> None:
    if not _ENABLED:
        return
    _TOOL_CALLS.labels(tool, status).inc()
    _TOOL_DURATION.labels(tool).observe(duration)


def render_metrics(_request: Request | None = None) -> Response:
    if not _ENABLED:
        return JSONResponse({"error": "metrics_disabled"}, status_code=503)
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


# ── HTTP middleware ───────────────────────────────────────────────────


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Generate/forward a request ID and log an access line per request."""

    def __init__(self, app: Any) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        rid = request.headers.get("x-request-id") or _new_request_id()
        token = _request_id.set(rid)
        start = time.perf_counter()
        response: Response | None = None
        try:
            response = await call_next(request)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            status = getattr(response, "status_code", 0)

            observe_request(
                request.method, request.url.path, status, duration_ms / 1000.0
            )
            logger.info(
                "http_request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "latency_ms": round(duration_ms, 2),
                },
            )
            _request_id.reset(token)

        # Ensure a correlation header is always present on the response.
        if isinstance(response, Response) and "x-request-id" not in response.headers:
            response.headers["x-request-id"] = rid
        return response


def _new_request_id() -> str:
    import secrets

    return secrets.token_hex(8)


# ── Health endpoints ──────────────────────────────────────────────────


async def healthz(_request: Request) -> JSONResponse:
    """Liveness probe: process is up."""
    return JSONResponse({"status": "ok"})


def readyz_factory(config_path: str | None) -> Callable:
    """Build a readiness handler that pings OpenSearch on demand.

    Returns a fresh handler bound to the resolved config path.
    """

    from search_mcp.connection import ConnectionConfig

    def handler(_request: Request) -> JSONResponse:
        try:
            client = ConnectionConfig(config_path=config_path).build_client()
            try:
                ok = client.ping()
            finally:
                client.transport.close()
            if ok:
                return JSONResponse({"status": "ok"})
            return JSONResponse(
                {"status": "unavailable", "detail": "opensearch_unreachable"},
                status_code=503,
            )
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - defensive
            return JSONResponse(
                {"status": "unavailable", "detail": str(exc)},
                status_code=503,
            )

    return handler