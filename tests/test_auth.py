"""Tests for API key Bearer authentication — middleware dispatch logic."""

from __future__ import annotations

import json

import pytest
from starlette.datastructures import Headers
from starlette.responses import JSONResponse


def _build_dispatch(api_key: str = "test-key"):
    """Return a dispatch callable matching main()'s middleware logic.

    When api_key is empty, returns a no-op pass-through (mirrors the real
    server behaviour where no middleware is added).
    """

    if not api_key:
        async def _passthrough(request, call_next):
            return await call_next(request)
        return _passthrough

    async def _check(request, call_next):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"error": "unauthorized", "message": "Missing or invalid Authorization header"},
                status_code=401,
            )
        token = auth_header.removeprefix("Bearer ")
        if token != api_key:
            return JSONResponse(
                {"error": "unauthorized", "message": "Invalid API key"},
                status_code=401,
            )
        return await call_next(request)

    return _check


class MockRequest:
    """Minimal stand-in for starlette.Request."""

    def __init__(self, auth_header: str | None = None):
        headers = {}
        if auth_header:
            headers["Authorization"] = auth_header
        self.headers = Headers(headers)


@pytest.mark.anyio
class TestAuthDispatch:
    """Middleware dispatch logic — no live server needed."""

    @staticmethod
    async def _ok_next(_request):
        return JSONResponse({"ok": True}, status_code=200)

    async def test_no_auth_returns_401(self):
        dispatch = _build_dispatch("my-secret-key")
        resp = await dispatch(MockRequest(), self._ok_next)
        assert resp.status_code == 401
        body = json.loads(resp.body)
        assert "unauthorized" in body.get("error", "")

    async def test_wrong_key_returns_401(self):
        dispatch = _build_dispatch("my-secret-key")
        resp = await dispatch(MockRequest("Bearer wrong-key"), self._ok_next)
        assert resp.status_code == 401

    async def test_wrong_scheme_returns_401(self):
        dispatch = _build_dispatch("my-secret-key")
        resp = await dispatch(MockRequest("Basic abc123"), self._ok_next)
        assert resp.status_code == 401

    async def test_valid_key_calls_next(self):
        dispatch = _build_dispatch("my-secret-key")
        resp = await dispatch(MockRequest("Bearer my-secret-key"), self._ok_next)
        assert resp.status_code == 200
        assert json.loads(resp.body)["ok"] is True

    async def test_empty_key_passes_through(self):
        """When api_key is empty, no middleware — always allow."""
        dispatch = _build_dispatch(api_key="")
        resp = await dispatch(MockRequest(), self._ok_next)
        assert resp.status_code == 200

    async def test_empty_key_still_passes_with_wrong_header(self):
        dispatch = _build_dispatch(api_key="")
        resp = await dispatch(MockRequest("Bearer some-key"), self._ok_next)
        assert resp.status_code == 200
