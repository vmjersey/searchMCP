"""Tests for the observability build-out: healthz/readyz/metrics endpoints,
request-ID middleware, tool tracing, and middleware exempt-path scoping."""

from __future__ import annotations

import json
import logging

import pytest
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from search_mcp import observability, server
from search_mcp.connection import ServerConfig


def _req(path: str = "/mcp", auth_header: str | None = None, content_length: str | None = None):
    headers = {}
    if auth_header:
        headers["Authorization"] = auth_header
    if content_length is not None:
        headers["Content-Length"] = content_length
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": Headers(headers).raw,
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


class _OkNext:
    async def __call__(self, request):
        return JSONResponse({"ok": True})


# ── Health / metrics route registration ───────────────────────────────


class TestHealthRoutes:
    def _app(self, **kw):
        return server.build_app(host="127.0.0.1", api_key="", **kw)

    def test_healthz_route_registered(self) -> None:
        assert any(r.path == "/healthz" for r in self._app().routes)

    def test_readyz_route_registered(self) -> None:
        assert any(r.path == "/readyz" for r in self._app().routes)

    def test_metrics_route_registered_by_default(self) -> None:
        assert any(r.path == "/metrics" for r in self._app().routes)

    def test_metrics_route_omitted_when_disabled(self) -> None:
        app = self._app(enable_metrics=False)
        assert not any(r.path == "/metrics" for r in app.routes)


class TestHealthz:
    @pytest.mark.anyio
    async def test_liveness_returns_ok(self) -> None:
        resp = await observability.healthz(_req("/healthz"))
        assert resp.status_code == 200
        assert json.loads(resp.body)["status"] == "ok"


class TestMetrics:
    def test_render_metrics_when_available(self) -> None:
        if not observability.metrics_available():
            pytest.skip("prometheus_client not installed")
        resp = observability.render_metrics()
        assert resp.status_code == 200
        body = resp.body.decode()
        assert "search_mcp_http_requests_total" in body
        assert "search_mcp_tool_calls_total" in body

    def test_observe_metrics(self) -> None:
        if not observability.metrics_available():
            pytest.skip("prometheus_client not installed")
        observability.observe_request("GET", "/healthz", 200, 0.01)
        observability.observe_tool("list_indices", "ok", 0.02)
        body = observability.render_metrics().body.decode()
        # prometheus_client sorts labels alphabetically.
        assert 'search_mcp_http_requests_total{method="GET",path="/healthz",status="200"} 1.0' in body
        assert 'search_mcp_tool_calls_total{status="ok",tool="list_indices"} 1.0' in body

    def test_metrics_disabled_503_when_lib_absent(self, monkeypatch) -> None:
        monkeypatch.setattr(observability, "_ENABLED", False)
        assert observability.render_metrics().status_code == 503


class TestReadyz:
    def test_readyz_factory_returns_callable(self) -> None:
        assert callable(observability.readyz_factory("/nonexistent/none.toml"))

    def test_readyz_503_when_client_raises(self, monkeypatch) -> None:
        class _FakeCfg:
            def build_client(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(
            "search_mcp.connection.ConnectionConfig", lambda config_path=None: _FakeCfg()
        )
        handler = observability.readyz_factory("/nonexistent/none.toml")
        resp = handler(_req("/readyz"))
        assert resp.status_code == 503

    def test_readyz_ok_when_ping_true(self, monkeypatch) -> None:
        class _FakeClient:
            def ping(self):
                return True

            transport = type("T", (), {"close": lambda self: None})()

        class _FakeCfg:
            def build_client(self):
                return _FakeClient()

        monkeypatch.setattr(
            "search_mcp.connection.ConnectionConfig", lambda config_path=None: _FakeCfg()
        )
        handler = observability.readyz_factory("/nonexistent/none.toml")
        resp = handler(_req("/readyz"))
        assert resp.status_code == 200
        assert json.loads(resp.body)["status"] == "ok"


# ── Request-ID / access-log middleware ────────────────────────────────


class TestRequestLogMiddleware:
    def test_sets_request_id_header(self) -> None:
        async def ok(_request):
            return JSONResponse({"ok": True})

        app = Starlette(routes=[Route("/x", ok)])
        app.add_middleware(observability.RequestLogMiddleware)
        with TestClient(app) as client:
            r = client.get("/x")
            assert r.status_code == 200
            assert len(r.headers["x-request-id"]) == 16

    def test_forwards_incoming_request_id(self) -> None:
        async def ok(_request):
            return JSONResponse({"ok": True})

        app = Starlette(routes=[Route("/x", ok)])
        app.add_middleware(observability.RequestLogMiddleware)
        with TestClient(app) as client:
            r = client.get("/x", headers={"x-request-id": "0123456789abcdef"})
            assert r.headers["x-request-id"] == "0123456789abcdef"


# ── Middleware exempt-path scoping─────────────────────────────────────


class TestMiddlewareExemptPaths:
    @pytest.mark.anyio
    async def test_auth_exempts_healthz(self) -> None:
        mw = server._AuthMiddleware(
            app=None,
            api_key="k",
            exempt_paths={server.healthz_path, server.readyz_path, server.metrics_path},
        )
        resp = await mw.dispatch(_req("/healthz"), _OkNext())
        assert resp.status_code == 200  # 401 if not exempted

    @pytest.mark.anyio
    async def test_auth_still_enforced_on_mcp(self) -> None:
        mw = server._AuthMiddleware(
            app=None,
            api_key="k",
            exempt_paths={server.healthz_path, server.readyz_path, server.metrics_path},
        )
        resp = await mw.dispatch(_req("/mcp"), _OkNext())
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_body_limit_exempts_metrics(self) -> None:
        mw = server._BodyLimitMiddleware(app=None, max_body_size=16, exempt_paths={server.metrics_path})
        resp = await mw.dispatch(_req("/metrics", content_length="99999999"), _OkNext())
        assert resp.status_code == 200  # 413 if not exempted


class TestToolTracing:
    def test__call_logs_and_observes(self, monkeypatch) -> None:
        logged: list[dict] = []
        calls: list[tuple] = []

        class _Rec(logging.Handler):
            def emit(self, record):
                logged.append(record.__dict__.copy())

        def _sample_tool(_namespace):
            return {"ok": True}

        logger = logging.getLogger("search_mcp.server")
        logger.setLevel(logging.INFO)
        h = _Rec()
        logger.addHandler(h)
        try:
            # server.py imports `observability` at module top, so patch the shared module.
            monkeypatch.setattr(
                observability,
                "observe_tool",
                lambda tool, status, duration: calls.append((tool, status, duration)),
            )
            out = server._call(_sample_tool, "x")
            assert json.loads(out) == {"ok": True}
            assert any(c[0] == "_sample_tool" and c[1] == "ok" for c in calls)
            tool_recs = [r for r in logged if "tool" in r and r.get("tool") == "_sample_tool"]
            assert tool_recs
            assert "latency_ms" in tool_recs[0]
        finally:
            logger.removeHandler(h)


# ── ServerConfig observability knobs ──────────────────────────────────


class TestServerConfigObservability:
    def test_defaults(self, monkeypatch) -> None:
        for k in ("OPENSEARCH_MCP_STRUCTURED_LOGS", "OPENSEARCH_MCP_ENABLE_METRICS"):
            monkeypatch.delenv(k, raising=False)
        cfg = ServerConfig(config_path="/nonexistent/none.toml")
        assert cfg.structured_logs is False
        assert cfg.enable_metrics is True

    def test_env_overrides(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENSEARCH_MCP_STRUCTURED_LOGS", "true")
        monkeypatch.setenv("OPENSEARCH_MCP_ENABLE_METRICS", "false")
        cfg = ServerConfig(config_path="/nonexistent/none.toml")
        assert cfg.structured_logs is True
        assert cfg.enable_metrics is False

    def test_config_file_values(self, tmp_path) -> None:
        cfg = tmp_path / "obs.toml"
        cfg.write_text("[server]\nstructured_logs = true\nenable_metrics = false\n")
        s = ServerConfig(config_path=str(cfg))
        assert s.structured_logs is True
        assert s.enable_metrics is False


# ── Structured logging formatter ──────────────────────────────────────


class TestJsonFormatter:
    def test_formats_fields_and_request_id(self) -> None:
        fmt = observability.JsonFormatter()
        rec = logging.LogRecord("m", logging.INFO, "f.py", 1, "hello", (), None)
        rec.tool = "list_indices"
        rec.latency_ms = 12.5
        payload = json.loads(fmt.format(rec))
        assert payload["message"] == "hello"
        assert payload["tool"] == "list_indices"
        assert payload["latency_ms"] == 12.5
        assert payload["level"] == "INFO"
        assert "request_id" in payload

    def test_setup_logging_swaps_formatter(self) -> None:
        h = logging.StreamHandler()
        observability.logger.addHandler(h)
        try:
            observability.setup_logging(structured=True)
            assert isinstance(h.formatter, observability.JsonFormatter)
        finally:
            observability.logger.removeHandler(h)


# ── main() observability wiring ───────────────────────────────────────


class TestMainWiring:
    def test_main_passes_observability_args(self, monkeypatch) -> None:
        import sys
        import types

        monkeypatch.setattr(server, "_filter_tools", lambda config_path=None: None)
        monkeypatch.setattr(sys, "argv", ["prog", "--port", "9200"])
        captured: dict = {}
        fake_uvicorn = types.SimpleNamespace(run=lambda app, **kw: captured.update(app=app))
        monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
        monkeypatch.setattr(
            server,
            "load_config_raw",
            lambda config_path=None: {"server": {"structured_logs": True, "enable_metrics": False}},
        )
        server.main()
        assert captured["app"] is not None