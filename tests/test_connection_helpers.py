"""Unit tests for connection.py helper functions and edge cases.

Covers the small config-coercion helpers, TOML load failure paths, and the
explicit-kwarg branches of ServerConfig that the main connection tests do not
reach.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from search_mcp.connection import (
    ConnectionConfig,
    ServerConfig,
    _as_bool,
    _as_int,
    _as_str,
    _env_flag,
    _load_toml,
    load_config_raw,
)


class TestCoercionHelpers:
    def test_env_flag_true_variants(self) -> None:
        for val in ("true", "1", "yes", "TRUE", "Yes"):
            with patch.dict(os.environ, {"FLAG": val}, clear=True):
                assert _env_flag("FLAG") is True

    def test_env_flag_false_variants(self) -> None:
        for val in ("false", "0", "no", "banana", ""):
            with patch.dict(os.environ, {"FLAG": val}, clear=True):
                assert _env_flag("FLAG") is False

    def test_env_flag_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert _env_flag("MISSING") is False
            assert _env_flag("MISSING", "true") is True

    def test_as_str(self) -> None:
        assert _as_str(None, "dflt") == "dflt"
        assert _as_str(True, "dflt") == "true"
        assert _as_str(False, "dflt") == "false"
        assert _as_str(42, "dflt") == "42"
        assert _as_str("keep", "dflt") == "keep"

    def test_as_int(self) -> None:
        assert _as_int(None, 7) == 7
        assert _as_int("42", 7) == 42
        assert _as_int("not-a-number", 7) == 7  # ValueError -> default
        assert _as_int(None, 7) == 7
        assert _as_int([1, 2], 7) == 7  # TypeError -> default

    def test_as_bool(self) -> None:
        assert _as_bool(None, True) is True
        assert _as_bool(True, False) is True
        assert _as_bool("yes", False) is True
        assert _as_bool("0", True) is False
        assert _as_bool(5, False) is True  # non-str, non-bool -> bool(val)
        assert _as_bool(0, True) is False


class TestLoadTomlFailures:
    def test_missing_file_returns_empty(self) -> None:
        assert _load_toml("/nonexistent/path/config.toml") == {}

    def test_invalid_toml_returns_empty(self, tmp_path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text("this is [ not valid toml ==")
        assert _load_toml(str(bad)) == {}

    def test_load_config_raw_none(self) -> None:
        # No config path and no discovered file -> {}
        with tempfile.TemporaryDirectory() as tmp:
            orig = Path.cwd()
            try:
                os.chdir(tmp)
                with patch.dict(os.environ, {}, clear=True):
                    assert load_config_raw() == {}
            finally:
                os.chdir(orig)


class TestServerConfigExplicitKwargs:
    def test_explicit_host_and_transport(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            sc = ServerConfig(host="10.0.0.1", transport="sse")
            assert sc.host == "10.0.0.1"
            assert sc.transport == "sse"

    def test_explicit_port(self) -> None:
        sc = ServerConfig(port=1234)
        assert sc.port == 1234

    def test_config_file_non_string_values(self, tmp_path) -> None:
        # host/transport coerced through _as_str even when TOML has odd types
        cfg = tmp_path / "srv.toml"
        cfg.write_text("[server]\nhost = 5\nport = '9000'\ntransport = true\n")
        sc = ServerConfig(config_path=str(cfg))
        assert sc.host == "5"
        assert sc.port == 9000
        assert sc.transport == "true"


class TestConnectionConfigBuildClientBranches:
    def test_build_client_http_no_auth(self) -> None:
        cfg = ConnectionConfig(
            host="h", port=9200, username="", password="", use_ssl=False
        )
        client = cfg.build_client()
        h = client.transport.hosts[0]
        assert h["scheme"] == "http"
        assert "http_auth" not in client.transport.kwargs

    def test_build_client_https_verify(self) -> None:
        # use_ssl=True + verify_certs=True -> no ssl_context override
        cfg = ConnectionConfig(use_ssl=True, verify_certs=True, username="u", password="p")
        client = cfg.build_client()
        assert client.transport.hosts[0]["scheme"] == "https"
        assert "ssl_context" not in client.transport.kwargs
        assert client.transport.kwargs["http_auth"] == ("u", "p")