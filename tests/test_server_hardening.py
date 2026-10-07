"""Tests for HTTP hardening: build_app fail-closed bind, auth, body cap, rate limit."""

from __future__ import annotations

import json

import pytest
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse

from search_mcp import server


def _req(auth_header: str | None = None, content_length: str | None = None, host: str = "127.0.0.1"):
    headers = {}
    if auth_header:
        headers["Authorization"] = auth_header
    if content_length is not None:
        headers["Content-Length"] = content_length
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": Headers(headers).raw,
        "client": (host, 12345),
    }
    return Request(scope)


class _OkNext:
    async def __call__(self, request):
        return JSONResponse({"ok": True}, status_code=200)


# ── build_app fail-closed bind guard ───────────────────────────────────


class TestBuildAppBindGuard:
    def test_loopback_without_key_ok(self) -> None:
        app = server.build_app(host="127.0.0.1", api_key="")
        assert app is not None

    def test_localhost_without_key_ok(self) -> None:
        server.build_app(host="localhost", api_key="")

    def test_non_loopback_without_key_raises(self) -> None:
        with pytest.raises(ValueError, match="without an API key"):
            server.build_app(host="0.0.0.0", api_key="")

    def test_non_loopback_with_key_ok(self) -> None:
        server.build_app(host="0.0.0.0", api_key="sekret")


# ── Auth middleware (constant time, on chokepoint) ─────────────────────


class TestAuthMiddleware:
    def _mw(self):
        return server._AuthMiddleware(app=None, api_key="my-secret-key")

    @pytest.mark.anyio
    async def test_missing_header_401(self) -> None:
        resp = await self._mw().dispatch(_req(), _OkNext())
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_wrong_scheme_401(self) -> None:
        resp = await self._mw().dispatch(_req("Basic abc"), _OkNext())
        assert resp.status_code == 401
        assert json.loads(resp.body)["error"] == "unauthorized"

    @pytest.mark.anyio
    async def test_wrong_key_401(self) -> None:
        resp = await self._mw().dispatch(_req("Bearer wrong-key"), _OkNext())
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_valid_key_200(self) -> None:
        resp = await self._mw().dispatch(_req("Bearer my-secret-key"), _OkNext())
        assert resp.status_code == 200


# ── Body size cap ──────────────────────────────────────────────────────


class TestBodyLimitMiddleware:
    def _mw(self, cap: int = 1024):
        return server._BodyLimitMiddleware(app=None, max_body_size=cap)

    @pytest.mark.anyio
    async def test_under_limit_passes(self) -> None:
        resp = await self._mw().dispatch(_req(content_length="100"), _OkNext())
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_over_limit_413(self) -> None:
        resp = await self._mw(cap=1024).dispatch(_req(content_length="2000"), _OkNext())
        assert resp.status_code == 413
        assert json.loads(resp.body)["error"] == "payload_too_large"

    @pytest.mark.anyio
    async def test_missing_length_passes(self) -> None:
        resp = await self._mw().dispatch(_req(), _OkNext())
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_malformed_length_ignored(self) -> None:
        resp = await self._mw().dispatch(_req(content_length="not-a-number"), _OkNext())
        assert resp.status_code == 200


# ── Rate limiting ──────────────────────────────────────────────────────


class TestRateLimitMiddleware:
    def _mw(self, limit: int = 2, window: float = 60.0):
        return server._RateLimitMiddleware(app=None, limit=limit, window_seconds=window)

    @pytest.mark.anyio
    async def test_under_limit_passes(self) -> None:
        mw = self._mw(limit=2)
        for _ in range(2):
            resp = await mw.dispatch(_req(), _OkNext())
            assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_over_limit_429(self) -> None:
        mw = self._mw(limit=2)
        for _ in range(2):
            await mw.dispatch(_req(), _OkNext())
        resp = await mw.dispatch(_req(), _OkNext())
        assert resp.status_code == 429
        assert json.loads(resp.body)["error"] == "rate_limited"

    @pytest.mark.anyio
    async def test_distinct_clients_independent(self) -> None:
        mw = self._mw(limit=1)
        # client A exhausts its budget; client B (different IP) still passes
        await mw.dispatch(_req(host="1.1.1.1"), _OkNext())
        assert (await mw.dispatch(_req(host="1.1.1.1"), _OkNext())).status_code == 429
        assert (await mw.dispatch(_req(host="2.2.2.2"), _OkNext())).status_code == 200

    @pytest.mark.anyio
    async def test_credential_keying_shares_budget(self) -> None:
        mw = self._mw(limit=1)
        key = "Bearer sekret"
        assert (await mw.dispatch(_req(key, host="1.1.1.1"), _OkNext())).status_code == 200
        # same credential from a different IP shares the same bucket
        assert (await mw.dispatch(_req(key, host="9.9.9.9"), _OkNext())).status_code == 429

    @pytest.mark.anyio
    async def test_window_resets(self) -> None:
        mw = self._mw(limit=1, window=0.0)  # zero window => always a fresh window
        for _ in range(5):
            assert (await mw.dispatch(_req(), _OkNext())).status_code == 200


# ── ServerConfig wiring ────────────────────────────────────────────────


class TestServerConfigHardening:
    def test_config_knobs_from_priority(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENSEARCH_MCP_RATE_LIMIT", "50")
        monkeypatch.setenv("OPENSEARCH_MCP_MAX_BODY_SIZE", "4096")
        monkeypatch.setenv("OPENSEARCH_MCP_API_KEY", "envkey")
        cfg = server.ServerConfig(config_path="/nonexistent/none.toml")
        assert cfg.rate_limit == 50
        assert cfg.max_body_size == 4096
        assert cfg.api_key == "envkey"

    def test_defaults_zero_knobs(self, monkeypatch) -> None:
        for k in ("OPENSEARCH_MCP_RATE_LIMIT", "OPENSEARCH_MCP_MAX_BODY_SIZE", "OPENSEARCH_MCP_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        cfg = server.ServerConfig(config_path="/nonexistent/none.toml")
        assert cfg.rate_limit == 0
        assert cfg.max_body_size == 0
        assert cfg.api_key == ""
