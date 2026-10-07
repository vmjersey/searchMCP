"""Unit tests for the MCP server module (server.py).

Covers the @mcp.tool wrapper functions, resources, lifespan, and the main()
entrypoint using mocked OpenSearch clients — no live server required.
"""

from __future__ import annotations

import json
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from search_mcp import server
from search_mcp.tools import OpenSearchTools


class FakeCtx:
    """Minimal stand-in for the MCP Context used by tool wrappers."""

    def __init__(self, tools: OpenSearchTools) -> None:
        self.request_context = types.SimpleNamespace(lifespan_context={"tools": tools})


@pytest.fixture
def tools() -> OpenSearchTools:
    return OpenSearchTools(MagicMock())


def _ctx(tools: OpenSearchTools) -> FakeCtx:
    return FakeCtx(tools)


def _c(tools: OpenSearchTools) -> MagicMock:
    return tools._client


# ── Cluster / server info wrappers ─────────────────────────────────────


class TestClusterWrappers:
    def test_cluster_health(self, tools) -> None:
        _c(tools).cluster.health.return_value = {"status": "green"}
        out = json.loads(server.cluster_health(_ctx(tools)))
        assert out == {"status": "green"}

    def test_cluster_stats(self, tools) -> None:
        _c(tools).cluster.stats.return_value = {"_all": {}}
        out = json.loads(server.cluster_stats(_ctx(tools)))
        assert out == {"_all": {}}

    def test_node_info(self, tools) -> None:
        _c(tools).nodes.info.return_value = {"nodes": {}}
        out = json.loads(server.node_info(_ctx(tools)))
        assert out == {"nodes": {}}

    def test_server_info(self, tools) -> None:
        _c(tools).info.return_value = {"version": {"number": "3.8.0"}}
        out = json.loads(server.server_info(_ctx(tools)))
        assert out["version"]["number"] == "3.8.0"


# ── Index wrappers ─────────────────────────────────────────────────────


class TestIndexWrappers:
    def test_list_indices(self, tools) -> None:
        _c(tools).cat.indices.return_value = [{"index": "a", "docs.count": "1"}]
        out = json.loads(server.list_indices(_ctx(tools), "a*"))
        assert out[0]["index"] == "a"

    def test_get_index_mapping(self, tools) -> None:
        _c(tools).indices.get_mapping.return_value = {"a": {}}
        out = json.loads(server.get_index_mapping(_ctx(tools), "a"))
        assert "a" in out

    def test_get_index_settings(self, tools) -> None:
        _c(tools).indices.get_settings.return_value = {"a": {}}
        out = json.loads(server.get_index_settings(_ctx(tools), "a"))
        assert "a" in out

    def test_create_index_with_strings(self, tools) -> None:
        _c(tools).indices.create.return_value = {"acknowledged": True}
        out = json.loads(
            server.create_index(
                _ctx(tools),
                "a",
                mappings='{"properties": {}}',
                settings='{"number_of_shards": 2}',
            )
        )
        assert out == {"acknowledged": True}
        _c(tools).indices.create.assert_called_once_with(
            index="a",
            body={"mappings": {"properties": {}}, "settings": {"number_of_shards": 2}},
        )

    def test_create_index_with_dicts(self, tools) -> None:
        _c(tools).indices.create.return_value = {"acknowledged": True}
        server.create_index(_ctx(tools), "a", mappings={"properties": {}})
        _c(tools).indices.create.assert_called_once_with(
            index="a", body={"mappings": {"properties": {}}}
        )

    def test_delete_index(self, tools) -> None:
        _c(tools).indices.delete.return_value = {"acknowledged": True}
        out = json.loads(server.delete_index(_ctx(tools), "a"))
        assert out["acknowledged"] is True

    def test_index_exists(self, tools) -> None:
        _c(tools).indices.exists.return_value = True
        out = json.loads(server.index_exists(_ctx(tools), "a"))
        assert out == {"result": True}


# ── Document wrappers ──────────────────────────────────────────────────


class TestDocumentWrappers:
    def test_search_with_json_query(self, tools) -> None:
        _c(tools).search.return_value = {"hits": {"hits": []}}
        out = json.loads(
            server.search(
                _ctx(tools),
                "a",
                query='{"match": {"name": "x"}}',
                size=20,
                from_=5,
                sort="name:asc",
            )
        )
        assert out == {"hits": {"hits": []}}
        _c(tools).search.assert_called_once_with(
            index="a",
            body={"query": {"match": {"name": "x"}}, "sort": "name:asc"},
            size=20,
            from_=5,
        )

    def test_search_clamps_size(self, tools) -> None:
        _c(tools).search.return_value = {"hits": {"hits": []}}
        server.search(_ctx(tools), "a", size=999999)
        assert _c(tools).search.call_args[1]["size"] == server.MAX_SEARCH_SIZE

    def test_search_source_field_list(self, tools) -> None:
        _c(tools).search.return_value = {"hits": {"hits": []}}
        server.search(_ctx(tools), "a", source=["title", "path", "size_bytes"])
        assert _c(tools).search.call_args[1]["_source"] == ["title", "path", "size_bytes"]

    def test_search_source_field_string(self, tools) -> None:
        _c(tools).search.return_value = {"hits": {"hits": []}}
        server.search(_ctx(tools), "a", source="title")
        assert _c(tools).search.call_args[1]["_source"] == "title"

    def test_search_source_omitted_by_default(self, tools) -> None:
        _c(tools).search.return_value = {"hits": {"hits": []}}
        server.search(_ctx(tools), "a")
        assert "_source" not in _c(tools).search.call_args[1]

    def test_get_document(self, tools) -> None:
        _c(tools).get.return_value = {"found": True}
        out = json.loads(server.get_document(_ctx(tools), "a", "id1"))
        assert out["found"] is True

    def test_index_document(self, tools) -> None:
        _c(tools).index.return_value = {"result": "created"}
        out = json.loads(
            server.index_document(_ctx(tools), "a", '{"k": "v"}', doc_id="id1", refresh="true")
        )
        assert out["result"] == "created"
        _c(tools).index.assert_called_once_with(
            index="a", body={"k": "v"}, refresh="true", id="id1"
        )

    def test_update_document(self, tools) -> None:
        _c(tools).update.return_value = {"result": "updated"}
        out = json.loads(server.update_document(_ctx(tools), "a", "id2", '{"v": 1}'))
        assert out["result"] == "updated"
        _c(tools).update.assert_called_once_with(
            index="a", id="id2", body={"doc": {"v": 1}}, refresh="false"
        )

    def test_delete_document(self, tools) -> None:
        _c(tools).delete.return_value = {"result": "deleted"}
        out = json.loads(server.delete_document(_ctx(tools), "a", "id3", "true"))
        assert out["result"] == "deleted"

    def test_count_documents(self, tools) -> None:
        _c(tools).count.return_value = {"count": 2}
        out = json.loads(server.count_documents(_ctx(tools), "a", query='{"match_all": {}}'))
        assert out["count"] == 2
        _c(tools).count.assert_called_once_with(index="a", body={"query": {"match_all": {}}})


# ── SQL / field / explain / validate wrappers ──────────────────────────


class TestSQLAndAnalysisWrappers:
    def test_sql_query(self, tools) -> None:
        _c(tools).transport.perform_request.return_value = {"datarows": []}
        out = json.loads(server.sql_query(_ctx(tools), "SELECT * FROM a"))
        assert out == {"datarows": []}

    def test_sql_explain(self, tools) -> None:
        _c(tools).transport.perform_request.return_value = {"from": 0}
        out = json.loads(server.sql_explain(_ctx(tools), "SELECT * FROM a"))
        assert out == {"from": 0}

    def test_field_caps(self, tools) -> None:
        _c(tools).field_caps.return_value = {"fields": {}}
        out = json.loads(server.field_caps(_ctx(tools), "a", "f*"))
        assert out == {"fields": {}}

    def test_explain_score(self, tools) -> None:
        _c(tools).explain.return_value = {"_explanation": {}}
        out = json.loads(server.explain_score(_ctx(tools), "a", "id", '{"match": {}}'))
        assert out == {"_explanation": {}}
        _c(tools).explain.assert_called_once_with(
            index="a", id="id", body={"query": {"match": {}}}
        )

    def test_validate_query(self, tools) -> None:
        _c(tools).indices.validate_query.return_value = {"valid": True}
        out = json.loads(server.validate_query(_ctx(tools), "a", '{"match_all": {}}', explain=True))
        assert out == {"valid": True}
        _c(tools).indices.validate_query.assert_called_once_with(
            index="a", body={"query": {"match_all": {}}}, explain=True
        )


# ── Cat API wrappers ───────────────────────────────────────────────────


class TestCatWrappers:
    def test_cat_indices(self, tools) -> None:
        _c(tools).cat.indices.return_value = [{"index": "a"}]
        out = json.loads(server.cat_indices(_ctx(tools)))
        assert out[0]["index"] == "a"

    def test_cat_aliases(self, tools) -> None:
        _c(tools).cat.aliases.return_value = [{"alias": "a"}]
        out = json.loads(server.cat_aliases(_ctx(tools)))
        assert out == [{"alias": "a"}]

    def test_cat_nodes(self, tools) -> None:
        _c(tools).cat.nodes.return_value = [{"name": "n1"}]
        out = json.loads(server.cat_nodes(_ctx(tools)))
        assert out == [{"name": "n1"}]

    def test_cat_shards(self, tools) -> None:
        _c(tools).cat.shards.return_value = [{"index": "a"}]
        out = json.loads(server.cat_shards(_ctx(tools), "a"))
        assert out == [{"index": "a"}]


# ── Bulk / reindex wrappers ────────────────────────────────────────────


class TestBulkReindexWrappers:
    def test_bulk_index(self, tools) -> None:
        with patch("search_mcp.tools.bulk", return_value=(1, [])) as mock_bulk:
            out = json.loads(
                server.bulk_index(_ctx(tools), "idx", '[{"v": 1}]', id_field="k", refresh="true")
            )
        assert out == {"success_count": 1, "errors": []}
        actions = list(mock_bulk.call_args[0][1])
        assert actions[0]["_index"] == "idx"

    def test_bulk_index_non_list_documents(self, tools) -> None:
        out = json.loads(server.bulk_index(_ctx(tools), "idx", '{"a": 1}'))
        assert "must be a JSON array" in out.get("error", "")
        # tools should not have been invoked for a non-list payload
        assert not _c(tools).method_calls

    def test_reindex(self, tools) -> None:
        _c(tools).reindex.return_value = {"task": "t1"}
        out = json.loads(
            server.reindex(_ctx(tools), "src", "dst", query='{"term": {"k": 1}}', wait_for_completion=False)
        )
        assert out == {"task": "t1"}
        _c(tools).reindex.assert_called_once_with(
            body={"source": {"index": "src", "query": {"term": {"k": 1}}}, "dest": {"index": "dst"}},
            wait_for_completion=False,
        )


# ── Helpers ────────────────────────────────────────────────────────────


class TestHelpers:
    def test_set_config_path(self) -> None:
        old = server._CONFIG_PATH
        try:
            server._set_config_path("/tmp/x.toml")
            assert server._CONFIG_PATH == "/tmp/x.toml"
        finally:
            server._CONFIG_PATH = old

    def test_respond(self) -> None:
        assert json.loads(server._respond({"a": 1})) == {"a": 1}

    def test_get_tools(self, tools) -> None:
        assert server._get_tools(_ctx(tools)) is tools


# ── Resources ──────────────────────────────────────────────────────────


class TestResources:
    def _patch_client(self, monkeypatch, client: MagicMock) -> None:
        monkeypatch.setattr(
            server.ConnectionConfig, "build_client", lambda self: client
        )

    def test_resource_health(self, monkeypatch, tools) -> None:
        _c(tools).cluster.health.return_value = {"status": "green"}
        self._patch_client(monkeypatch, _c(tools))
        out = json.loads(server.resource_health())
        assert out == {"status": "green"}

    def test_resource_indices(self, monkeypatch, tools) -> None:
        _c(tools).cat.indices.return_value = [{"index": "a"}]
        self._patch_client(monkeypatch, _c(tools))
        out = json.loads(server.resource_indices())
        assert out[0]["index"] == "a"

    def test_resource_mapping(self, monkeypatch, tools) -> None:
        _c(tools).indices.get_mapping.return_value = {"a": {}}
        self._patch_client(monkeypatch, _c(tools))
        out = json.loads(server.resource_mapping("a"))
        assert "a" in out


# ── Lifespan ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestLifespan:
    async def test_lifespan_yields_tools_and_closes(self, monkeypatch, tmp_path) -> None:
        client = MagicMock()
        client.cluster.health.return_value = {"status": "green"}
        monkeypatch.setattr(server.ConnectionConfig, "build_client", lambda self: client)
        # chdir to an empty dir so auto-discovery of the repo's config.toml
        # does not mutate the global tool registry.
        orig = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            async with server.lifespan(server.mcp) as state:
                assert "tools" in state
                assert OpenSearchTools.safe_call(state["tools"].cluster_health) == {"status": "green"}
        finally:
            os.chdir(orig)
        client.transport.close.assert_called_once()

    async def test_lifespan_close_error_is_swallowed(self, monkeypatch, tmp_path) -> None:
        client = MagicMock()
        client.transport.close.side_effect = RuntimeError("close failed")
        monkeypatch.setattr(server.ConnectionConfig, "build_client", lambda self: client)
        orig = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            async with server.lifespan(server.mcp):
                pass
        finally:
            os.chdir(orig)


# ── main() entrypoint ──────────────────────────────────────────────────


class TestMain:
    def _noop_filter(self, monkeypatch) -> None:
        monkeypatch.setattr(server, "_filter_tools", lambda config_path=None: None)

    def test_main_stdio(self, monkeypatch) -> None:
        self._noop_filter(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["prog", "--transport", "stdio"])
        calls = {}

        def fake_run(**kwargs):
            calls.update(kwargs)

        monkeypatch.setattr(server.mcp, "run", fake_run)
        server.main()
        assert calls.get("transport") == "stdio"

    def test_main_http_with_api_key(self, monkeypatch) -> None:
        self._noop_filter(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["prog", "--host", "0.0.0.0", "--port", "9000"])
        calls = {}
        fake_uvicorn = types.SimpleNamespace(run=lambda app, **kw: calls.update(kw))
        monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
        monkeypatch.setattr(
            server, "load_config_raw", lambda config_path=None: {"server": {"api_key": "sekret"}}
        )
        server.main()
        assert calls.get("host") == "0.0.0.0"
        assert calls.get("port") == 9000

    def test_main_http_without_api_key(self, monkeypatch) -> None:
        self._noop_filter(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["prog", "--port", "9100"])
        calls = {}
        fake_uvicorn = types.SimpleNamespace(run=lambda app, **kw: calls.update(kw))
        monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
        monkeypatch.setattr(server, "load_config_raw", lambda config_path=None: {"server": {}})
        server.main()
        assert calls.get("port") == 9100

    def test_main_sets_config_path(self, monkeypatch, tmp_path) -> None:
        self._noop_filter(monkeypatch)
        cfg_file = tmp_path / "cfg.toml"
        cfg_file.write_text("[opensearch]\nhost = 'x'\n")
        old = server._CONFIG_PATH
        try:
            monkeypatch.setattr(sys, "argv", ["prog", "--config", str(cfg_file), "--transport", "stdio"])
            calls = {}
            monkeypatch.setattr(server.mcp, "run", lambda **kw: calls.update(kw))
            server.main()
            assert str(cfg_file) == server._CONFIG_PATH
            assert calls.get("transport") == "stdio"
        finally:
            server._CONFIG_PATH = old

    def test_main_http_bearer_middleware_enforced(self, monkeypatch) -> None:
        """The build_app() _AuthMiddleware enforces the key."""
        import asyncio

        self._noop_filter(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["prog", "--port", "9100"])
        captured: dict = {}
        fake_uvicorn = types.SimpleNamespace(run=lambda app, **kw: captured.update(app=app))
        monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
        monkeypatch.setattr(
            server, "load_config_raw", lambda config_path=None: {"server": {"api_key": "sekret"}}
        )
        server.main()
        app = captured["app"]

        mw = None
        for entry in app.user_middleware:
            if entry.cls.__name__ == "_AuthMiddleware":
                mw = entry.cls(app, api_key="sekret")
        assert mw is not None
        assert hasattr(mw, "_secret_bytes")

        class _Req:
            def __init__(self, auth=None):
                self.headers = {}
                if auth:
                    self.headers["Authorization"] = auth

        async def call_next(_request):
            return types.SimpleNamespace(status_code=200)

        # Missing header -> 401
        resp = asyncio.run(mw.dispatch(_Req(), call_next))
        assert resp.status_code == 401
        # Wrong scheme -> 401
        resp = asyncio.run(mw.dispatch(_Req("Basic abc"), call_next))
        assert resp.status_code == 401
        # Wrong key -> 401
        resp = asyncio.run(mw.dispatch(_Req("Bearer wrong"), call_next))
        assert resp.status_code == 401
        # Valid key -> passes through to call_next (200)
        resp = asyncio.run(mw.dispatch(_Req("Bearer sekret"), call_next))
        assert resp.status_code == 200


class TestMalformedJSON:
    """Malformed JSON strings must return a clean bad_request error, not raise."""

    def test_search_invalid_json(self, tools) -> None:
        out = json.loads(server.search(_ctx(tools), "a", query="{bad json"))
        assert out["error"] == "bad_request"
        assert "query" in out["message"]

    def test_index_document_invalid_json(self, tools) -> None:
        out = json.loads(server.index_document(_ctx(tools), "a", "{bad json"))
        assert out["error"] == "bad_request"
        assert "document" in out["message"]

    def test_update_document_invalid_json(self, tools) -> None:
        out = json.loads(server.update_document(_ctx(tools), "a", "id", "{bad json"))
        assert out["error"] == "bad_request"

    def test_create_index_invalid_mappings(self, tools) -> None:
        out = json.loads(server.create_index(_ctx(tools), "a", mappings="{bad json"))
        assert out["error"] == "bad_request"

    def test_count_documents_invalid_query(self, tools) -> None:
        out = json.loads(server.count_documents(_ctx(tools), "a", query="{bad json"))
        assert out["error"] == "bad_request"

    def test_explain_score_invalid_query(self, tools) -> None:
        out = json.loads(server.explain_score(_ctx(tools), "a", "id", "{bad json"))
        assert out["error"] == "bad_request"

    def test_validate_query_invalid_query(self, tools) -> None:
        out = json.loads(server.validate_query(_ctx(tools), "a", "{bad json"))
        assert out["error"] == "bad_request"

    def test_bulk_index_invalid_json(self, tools) -> None:
        out = json.loads(server.bulk_index(_ctx(tools), "idx", "{bad json"))
        assert out["error"] == "bad_request"
        assert "documents" in out["message"]

    def test_reindex_invalid_query(self, tools) -> None:
        out = json.loads(server.reindex(_ctx(tools), "src", "dst", query="{bad json"))
        assert out["error"] == "bad_request"


class TestSearchClamping:
    def test_negative_size_and_from_clamped(self, tools) -> None:
        _c(tools).search.return_value = {"hits": {"hits": []}}
        server.search(_ctx(tools), "a", size=-5, from_=-3)
        assert _c(tools).search.call_args[1]["size"] == 0
        assert _c(tools).search.call_args[1]["from_"] == 0

    def test_large_size_clamped(self, tools) -> None:
        _c(tools).search.return_value = {"hits": {"hits": []}}
        server.search(_ctx(tools), "a", size=999999)
        assert _c(tools).search.call_args[1]["size"] == server.MAX_SEARCH_SIZE
