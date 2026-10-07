"""Tests for the OpenSearch connection module — config file, env vars, overrides."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from search_mcp.connection import ConnectionConfig, ServerConfig


class TestConnectionConfig:
    """ConnectionConfig with layered precedence."""

    def test_defaults(self) -> None:
        """Built-in defaults when nothing else is set and no config.toml present."""
        with tempfile.TemporaryDirectory() as tmp:
            orig = Path.cwd()
            try:
                os.chdir(tmp)
                with patch.dict(os.environ, {}, clear=True):
                    cfg = ConnectionConfig()
                    assert cfg.host == "localhost"
                    assert cfg.port == 9200
                    assert cfg.use_ssl is True
                    assert cfg.verify_certs is False
                    assert cfg.username == "admin"
                    assert cfg.password == ""
                    assert cfg.timeout == 30
            finally:
                os.chdir(orig)

    def test_env_vars(self) -> None:
        """OPENSEARCH_* env vars override defaults."""
        with patch.dict(os.environ, {
            "OPENSEARCH_HOST": "10.0.0.1",
            "OPENSEARCH_PORT": "9201",
            "OPENSEARCH_SSL": "true",
            "OPENSEARCH_USERNAME": "testuser",
            "OPENSEARCH_PASSWORD": "testpass",
            "OPENSEARCH_TIMEOUT": "60",
        }, clear=True), tempfile.TemporaryDirectory() as tmp:
            orig = Path.cwd()
            try:
                os.chdir(tmp)
                cfg = ConnectionConfig()
                assert cfg.host == "10.0.0.1"
                assert cfg.port == 9201
                assert cfg.use_ssl is True
                assert cfg.username == "testuser"
                assert cfg.password == "testpass"
                assert cfg.timeout == 60
            finally:
                os.chdir(orig)

    def test_explicit_kwargs_override_env(self) -> None:
        """Explicit constructor args beat environment variables."""
        with patch.dict(os.environ, {"OPENSEARCH_HOST": "env-host", "OPENSEARCH_PORT": "9999"}, clear=True), tempfile.TemporaryDirectory() as tmp:
                orig = Path.cwd()
                try:
                    os.chdir(tmp)
                    cfg = ConnectionConfig(host="explicit-host", port=1234)
                    assert cfg.host == "explicit-host"
                    assert cfg.port == 1234
                finally:
                    os.chdir(orig)

    def test_config_file(self) -> None:
        """TOML config file loads connection settings."""
        toml = """\
[opensearch]
host = "cfg-host"
port = 9999
username = "cfguser"
password = "cfgpass"
timeout = 120
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(toml)
            cfg_path = f.name
        try:
            cfg = ConnectionConfig(config_path=cfg_path)
            assert cfg.host == "cfg-host"
            assert cfg.port == 9999
            assert cfg.username == "cfguser"
            assert cfg.password == "cfgpass"
            assert cfg.timeout == 120
        finally:
            os.unlink(cfg_path)

    def test_config_file_overridden_by_env(self) -> None:
        """Env vars win over config file values."""
        toml = """\
[opensearch]
host = "cfg-host"
port = 9999
username = "cfguser"
password = "cfgpass"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(toml)
            cfg_path = f.name
        try:
            with patch.dict(os.environ, {"OPENSEARCH_HOST": "env-host"}, clear=True):
                cfg = ConnectionConfig(config_path=cfg_path)
                assert cfg.host == "env-host"   # env wins
                assert cfg.port == 9999         # from config
                assert cfg.username == "cfguser"
        finally:
            os.unlink(cfg_path)

    def test_config_file_discovery(self) -> None:
        """If no config_path given, discovery finds ./config.toml in cwd."""
        toml = """\
[opensearch]
host = "discovered"
port = 7777
username = "discuser"
password = "discpass"
"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "config.toml"
            cfg_file.write_text(toml)
            orig = Path.cwd()
            try:
                os.chdir(tmp)
                cfg = ConnectionConfig()
                assert cfg.host == "discovered"
                assert cfg.port == 7777
            finally:
                os.chdir(orig)

    def test_build_client(self) -> None:
        """Client is constructed with correct params."""
        cfg = ConnectionConfig(
            host="localhost",
            port=9200,
            username="admin",
            password="test",
            use_ssl=True,
            verify_certs=False,
        )
        client = cfg.build_client()
        h = client.transport.hosts[0]
        assert h["host"] == "localhost"
        assert h["port"] == 9200
        assert h["scheme"] == "https"


class TestServerConfig:
    """ServerConfig — MCP server binding config."""

    def test_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orig = Path.cwd()
            try:
                os.chdir(tmp)
                with patch.dict(os.environ, {}, clear=True):
                    sc = ServerConfig()
                    assert sc.host == "127.0.0.1"
                    assert sc.port == 8000
                    assert sc.transport == "streamable-http"
            finally:
                os.chdir(orig)

    def test_from_config_file(self) -> None:
        toml = """\
[server]
host = "0.0.0.0"
port = 9000
transport = "sse"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(toml)
            cfg_path = f.name
        try:
            sc = ServerConfig(config_path=cfg_path)
            assert sc.host == "0.0.0.0"
            assert sc.port == 9000
            assert sc.transport == "sse"
        finally:
            os.unlink(cfg_path)

    def test_from_env(self) -> None:
        with patch.dict(os.environ, {
            "OPENSEARCH_MCP_HOST": "0.0.0.0",
            "OPENSEARCH_MCP_PORT": "9001",
            "OPENSEARCH_MCP_TRANSPORT": "stdio",
        }, clear=True), tempfile.TemporaryDirectory() as tmp:
            orig = Path.cwd()
            try:
                os.chdir(tmp)
                sc = ServerConfig()
                assert sc.host == "0.0.0.0"
                assert sc.port == 9001
                assert sc.transport == "stdio"
            finally:
                os.chdir(orig)

    def test_explicit_overrides(self) -> None:
        with patch.dict(os.environ, {"OPENSEARCH_MCP_PORT": "9999"}, clear=True):
            sc = ServerConfig(port=4321)
            assert sc.port == 4321  # explicit wins
            assert sc.host == "127.0.0.1"  # env var not set for host
