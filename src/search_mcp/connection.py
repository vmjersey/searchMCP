"""OpenSearch connection management — config file, env vars, and defaults."""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opensearchpy import OpenSearch


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in ("true", "1", "yes")


_CONFIG_PATH_HINTS = [
    os.environ.get("OPENSEARCH_CONFIG"),          # explicit env override
    "./config.toml",
    "./opensearch-config.toml",
    "~/.config/opensearch-mcp/config.toml",
    "/etc/opensearch-mcp/config.toml",
]


def _find_config() -> str | None:
    """Return the first config file found, or None."""
    for hint in _CONFIG_PATH_HINTS:
        if hint is None:
            continue
        p = Path(hint).expanduser().resolve()
        if p.is_file():
            return str(p)
    return None


def _load_toml(path: str) -> dict[str, Any]:
    """Load a TOML file; returns {} on parse failure."""
    try:
        import tomllib  # stdlib in Python 3.11+
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (FileNotFoundError, PermissionError, ValueError, ImportError):
        return {}


def _as_str(val: Any, default: str) -> str:
    if val is None:
        return default
    if isinstance(val, bool):
        return str(val).lower()
    return str(val)


def _as_int(val: Any, default: int) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _as_bool(val: Any, default: bool) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes")
    return bool(val)


def _resolve_config_path(config_path: str | None = None) -> str | None:
    """Return the config path from explicit arg, env var, or auto-discovery."""
    return config_path or os.environ.get("OPENSEARCH_CONFIG") or _find_config()


@dataclass
class ConnectionConfig:
    """Configuration for the OpenSearch connection.

    Precedence (highest to lowest):
        1. Explicit kwargs passed to constructor
        2. OPENSEARCH_* environment variables
        3. config.toml file (auto-discovered or OPENSEARCH_CONFIG)
        4. Built-in defaults
    """

    host: str = "localhost"
    port: int = 9200
    use_ssl: bool = True
    verify_certs: bool = False
    username: str = "admin"
    password: str = ""
    timeout: int = 30

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        use_ssl: bool | None = None,
        verify_certs: bool | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: int | None = None,
        config_path: str | None = None,
    ) -> None:
        # 1. Start with built-in defaults
        cfg: dict[str, Any] = {
            "host": "localhost",
            "port": 9200,
            "use_ssl": True,
            "verify_certs": False,
            "username": "admin",
            "password": "",
            "timeout": 30,
        }

        # 2. Overlay config file values
        cpath = _resolve_config_path(config_path)
        if cpath:
            raw = _load_toml(cpath)
            conn = raw.get("opensearch", {})
            cfg["host"] = _as_str(conn.get("host"), cfg["host"])
            cfg["port"] = _as_int(conn.get("port"), cfg["port"])
            cfg["use_ssl"] = _as_bool(conn.get("use_ssl"), cfg["use_ssl"])
            cfg["verify_certs"] = _as_bool(conn.get("verify_certs"), cfg["verify_certs"])
            cfg["username"] = _as_str(conn.get("username"), cfg["username"])
            cfg["password"] = _as_str(conn.get("password"), cfg["password"])
            cfg["timeout"] = _as_int(conn.get("timeout"), cfg["timeout"])

        # 3. Overlay environment variables
        env_overrides: dict[str, str] = {
            "host": "OPENSEARCH_HOST",
            "port": "OPENSEARCH_PORT",
            "use_ssl": "OPENSEARCH_SSL",
            "verify_certs": "OPENSEARCH_VERIFY_CERTS",
            "username": "OPENSEARCH_USERNAME",
            "password": "OPENSEARCH_PASSWORD",
            "timeout": "OPENSEARCH_TIMEOUT",
        }
        for key, env in env_overrides.items():
            val = os.environ.get(env)
            if val is not None:
                if key in ("use_ssl", "verify_certs"):
                    cfg[key] = _as_bool(val, cfg[key])
                elif key in ("port", "timeout"):
                    cfg[key] = _as_int(val, cfg[key])
                else:
                    cfg[key] = val

        # 4. Overlay explicit kwargs
        explicit = {
            "host": host,
            "port": port,
            "use_ssl": use_ssl,
            "verify_certs": verify_certs,
            "username": username,
            "password": password,
            "timeout": timeout,
        }
        for key, val in explicit.items():
            if val is not None:
                cfg[key] = val

        # Apply to self
        self.host = cfg["host"]
        self.port = cfg["port"]
        self.use_ssl = cfg["use_ssl"]
        self.verify_certs = cfg["verify_certs"]
        self.username = cfg["username"]
        self.password = cfg["password"]
        self.timeout = cfg["timeout"]

    def build_client(self) -> OpenSearch:
        """Build and return an OpenSearch client."""
        kwargs: dict = {
            "hosts": [{"host": self.host, "port": self.port, "scheme": "https" if self.use_ssl else "http"}],
            "timeout": self.timeout,
        }
        if self.username and self.password:
            kwargs["http_auth"] = (self.username, self.password)

        if self.use_ssl and not self.verify_certs:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            kwargs["ssl_context"] = ctx

        return OpenSearch(**kwargs)


@dataclass
class ServerConfig:
    """Configuration for the MCP server binding (not the OpenSearch connection).

    Precedence (highest to lowest):
        1. Explicit kwargs passed to constructor
        2. OPENSEARCH_MCP_* environment variables
        3. [server] section of config.toml
        4. Built-in defaults

    Security knobs:
        api_key:      Bearer token required for HTTP auth (empty disables).
                      A non-loopback bind without an api_key is refused.
        rate_limit:   Max requests per client per 60s window (0 disables).
        max_body_size: Max HTTP request body in bytes (0 disables).
    """

    host: str = "127.0.0.1"
    port: int = 8000
    transport: str = "streamable-http"
    api_key: str = ""
    rate_limit: int = 0
    max_body_size: int = 0
    structured_logs: bool = False
    enable_metrics: bool = True

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        transport: str | None = None,
        api_key: str | None = None,
        rate_limit: int | None = None,
        max_body_size: int | None = None,
        structured_logs: bool | None = None,
        enable_metrics: bool | None = None,
        config_path: str | None = None,
    ) -> None:
        cfg: dict[str, Any] = {
            "host": "127.0.0.1",
            "port": 8000,
            "transport": "streamable-http",
            "api_key": "",
            "rate_limit": 0,
            "max_body_size": 0,
            "structured_logs": False,
            "enable_metrics": True,
        }

        # 1. config file [server] section
        cpath = _resolve_config_path(config_path)
        if cpath:
            raw = _load_toml(cpath)
            srv = raw.get("server", {})
            cfg["host"] = _as_str(srv.get("host"), cfg["host"])
            cfg["port"] = _as_int(srv.get("port"), cfg["port"])
            cfg["transport"] = _as_str(srv.get("transport"), cfg["transport"])
            cfg["api_key"] = _as_str(srv.get("api_key"), cfg["api_key"])
            cfg["rate_limit"] = _as_int(srv.get("rate_limit"), cfg["rate_limit"])
            cfg["max_body_size"] = _as_int(srv.get("max_body_size"), cfg["max_body_size"])
            cfg["structured_logs"] = _as_bool(srv.get("structured_logs"), cfg["structured_logs"])
            cfg["enable_metrics"] = _as_bool(srv.get("enable_metrics"), cfg["enable_metrics"])

        # 2. environment variables
        env_map: dict[str, str] = {
            "host": "OPENSEARCH_MCP_HOST",
            "port": "OPENSEARCH_MCP_PORT",
            "transport": "OPENSEARCH_MCP_TRANSPORT",
            "api_key": "OPENSEARCH_MCP_API_KEY",
        }
        for key, env in env_map.items():
            val = os.environ.get(env)
            if val is not None:
                cfg[key] = _as_int(val, cfg[key]) if key == "port" else val
        for key, env in (("rate_limit", "OPENSEARCH_MCP_RATE_LIMIT"),
                         ("max_body_size", "OPENSEARCH_MCP_MAX_BODY_SIZE")):
            val = os.environ.get(env)
            if val is not None:
                cfg[key] = _as_int(val, cfg[key])
        if os.environ.get("OPENSEARCH_MCP_STRUCTURED_LOGS") is not None:
            cfg["structured_logs"] = _as_bool(
                os.environ["OPENSEARCH_MCP_STRUCTURED_LOGS"], cfg["structured_logs"]
            )
        if os.environ.get("OPENSEARCH_MCP_ENABLE_METRICS") is not None:
            cfg["enable_metrics"] = _as_bool(
                os.environ["OPENSEARCH_MCP_ENABLE_METRICS"], cfg["enable_metrics"]
            )

        # 3. explicit kwargs
        explicit = {
            "host": host,
            "port": port,
            "transport": transport,
            "api_key": api_key,
            "rate_limit": rate_limit,
            "max_body_size": max_body_size,
            "structured_logs": structured_logs,
            "enable_metrics": enable_metrics,
        }
        for key, val in explicit.items():
            if val is not None:
                cfg[key] = val

        self.host = cfg["host"]
        self.port = cfg["port"]
        self.transport = cfg["transport"]
        self.api_key = cfg["api_key"]
        self.rate_limit = cfg["rate_limit"]
        self.max_body_size = cfg["max_body_size"]
        self.structured_logs = cfg["structured_logs"]
        self.enable_metrics = cfg["enable_metrics"]


# ── Config helpers used by both ConnectionConfig and tool filtering ─────


def load_config_raw(config_path: str | None = None) -> dict[str, Any]:
    """Load the config TOML using the same discovery as ConnectionConfig.

    Returns the raw parsed dict (or {} if no config found / parse error).
    """
    cpath = _resolve_config_path(config_path)
    if cpath:
        return _load_toml(cpath)
    return {}
