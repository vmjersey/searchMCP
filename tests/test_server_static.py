"""Static checks for the MCP server module: tool registration, schema, entrypoint."""

from __future__ import annotations

from search_mcp import server
from search_mcp.connection import load_config_raw

EXPECTED_TOOLS = {
    "cluster_health",
    "cluster_stats",
    "node_info",
    "server_info",
    "list_indices",
    "get_index_mapping",
    "get_index_settings",
    "create_index",
    "delete_index",
    "index_exists",
    "search",
    "get_document",
    "index_document",
    "update_document",
    "delete_document",
    "count_documents",
    "sql_query",
    "sql_explain",
    "field_caps",
    "explain_score",
    "validate_query",
    "cat_indices",
    "cat_aliases",
    "cat_nodes",
    "cat_shards",
    "bulk_index",
    "reindex",
}


def _tool_listing() -> dict:
    """Return {name: Tool} from the tool manager (sync)."""
    return {t.name: t for t in server.mcp._tool_manager.list_tools()}


class TestServerRegistration:
    """The MCPServer must expose the full OpenSearch tool surface."""

    def test_all_tools_registered(self) -> None:
        names = set(_tool_listing().keys())
        assert names >= EXPECTED_TOOLS, f"missing: {EXPECTED_TOOLS - names}"

    def test_tool_listing_has_schemas(self) -> None:
        listing = _tool_listing()
        for name in ("search", "create_index", "sql_query"):
            tool = listing[name]
            assert tool.parameters is not None
            assert "type" in tool.parameters

    def test_required_params_enforced(self) -> None:
        listing = _tool_listing()
        assert "index" in listing["search"].parameters.get("required", [])
        assert "query" in listing["sql_query"].parameters.get("required", [])

    def test_entrypoint_present(self) -> None:
        assert callable(server.main)


# ── Tool filtering from config ─────────────────────────────────────────
#
# The filter modifies the global mcp._tool_manager in-place and cannot be
# undone (tools cannot be re-added).  Pure-logic tests go first;
# side-effect tests must run last and in order, so they're collected in a
# single test that pytest runs at the end of its pass.

def _parse_tool_filter(raw_config: dict) -> set[str] | None:
    """Read [tools].enabled_tools from a loaded config dict.

    Returns the set of tool names to keep, or None if the key is absent.
    """
    tools_cfg = raw_config.get("tools", {})
    enabled = tools_cfg.get("enabled_tools")
    return set(enabled) if enabled else None


class TestToolFilterParsing:
    """Pure-logic tests: no side effects on the global tool manager."""

    def test_no_tools_section(self) -> None:
        assert _parse_tool_filter({}) is None
        assert _parse_tool_filter({"connection": {}}) is None

    def test_empty_tools_section(self) -> None:
        assert _parse_tool_filter({"tools": {}}) is None

    def test_allowlist(self) -> None:
        raw = {"tools": {"enabled_tools": ["a", "b"]}}
        names = _parse_tool_filter(raw)
        assert names == {"a", "b"}

    def test_single_tool(self) -> None:
        raw = {"tools": {"enabled_tools": ["cluster_health"]}}
        names = _parse_tool_filter(raw)
        assert names == {"cluster_health"}

    def test_config_file_roundtrip(self, tmp_path) -> None:
        cfg = tmp_path / "tools_test.toml"
        cfg.write_text("""\
[tools]
enabled_tools = ["cluster_health", "search"]
""")
        raw = load_config_raw(config_path=str(cfg))
        names = _parse_tool_filter(raw)
        assert names == {"cluster_health", "search"}


class TestToolFilterLive:
    """End-to-end: applies _filter_tools to the real mcp object.

    These tests MUST run after TestToolFilterParsing because they mutate
    global state that cannot be restored (tools cannot be re-registered).
    """

    FULL_COUNT = 27

    def test_no_tools_section_is_noop(self, tmp_path) -> None:
        # chdir to an empty dir so auto-discovery doesn't find the repo's config.toml
        import os
        orig = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            n = len(server.mcp._tool_manager.list_tools())
            assert n >= self.FULL_COUNT
            server._filter_tools(config_path=None)
            assert len(server.mcp._tool_manager.list_tools()) == n
        finally:
            os.chdir(orig)

    def test_allowlist_removes_untooled(self, tmp_path) -> None:
        cfg = tmp_path / "test_config.toml"
        cfg.write_text("""\
[tools]
enabled_tools = ["cluster_health", "search"]
""")
        server._filter_tools(config_path=str(cfg))
        remaining = {t.name for t in server.mcp._tool_manager.list_tools()}
        assert remaining == {"cluster_health", "search"}
