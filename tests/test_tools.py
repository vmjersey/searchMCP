"""Integration tests for OpenSearchTools against the live server.

These tests require an OpenSearch instance at localhost:9200 with
credentials admin / YourStrongP@ssword123! (set via env vars).
"""

from __future__ import annotations

import os

import pytest

from search_mcp.connection import ConnectionConfig
from search_mcp.tools import OpenSearchTools

# Skip the whole module when no live OpenSearch is reachable at localhost:9200.
# The unit tests (test_tools_unit.py) cover the same methods with mocks, so the
# suite stays green in environments without a cluster.
try:
    _probe = ConnectionConfig(
        host="localhost",
        port=9200,
        password=os.environ.get("OPENSEARCH_PASSWORD", "YourStrongP@ssword123!"),
    )
    _probe.build_client().info()
except Exception:  # noqa: BLE001 - live-probe skip guard
    pytest.skip("Live OpenSearch not reachable at localhost:9200", allow_module_level=True)


@pytest.fixture(scope="module")
def tools() -> OpenSearchTools:
    """Connection to the test OpenSearch instance."""
    cfg = ConnectionConfig(
        host="localhost",
        port=9200,
        password=os.environ.get("OPENSEARCH_PASSWORD", "YourStrongP@ssword123!"),
    )
    client = cfg.build_client()
    return OpenSearchTools(client)


# ── Cluster / Info ─────────────────────────────────────────────────────


class TestCluster:
    def test_cluster_health(self, tools: OpenSearchTools) -> None:
        result = OpenSearchTools.safe_call(tools.cluster_health)
        assert isinstance(result, dict)
        assert result.get("status") in ("green", "yellow", "red")
        assert result.get("number_of_nodes", 0) >= 1

    def test_server_info(self, tools: OpenSearchTools) -> None:
        result = OpenSearchTools.safe_call(tools.server_info)
        v = result.get("version", {})
        assert v.get("distribution") == "opensearch"
        assert v.get("number", "").startswith("3.")


# ── Index Operations ───────────────────────────────────────────────────


class TestIndices:
    def test_list_indices(self, tools: OpenSearchTools) -> None:
        result = OpenSearchTools.safe_call(tools.list_indices)
        assert isinstance(result, list)
        assert len(result) >= 1
        idx = result[0]
        assert "index" in idx
        assert "docs_count" in idx

    def test_create_get_delete_cycle(self, tools: OpenSearchTools) -> None:
        idx_name = "test_pytest_idx"
        # Clean from prior runs
        tools._client.indices.delete(index=idx_name, ignore=[404])

        # Create
        mappings = {"properties": {"title": {"type": "text"}, "count": {"type": "integer"}}}
        r = OpenSearchTools.safe_call(tools.create_index, idx_name, mappings)
        assert r.get("acknowledged") is True

        # Exists
        assert OpenSearchTools.safe_call(tools.index_exists, idx_name) == {"result": True}

        # Get mapping
        r = OpenSearchTools.safe_call(tools.get_index_mapping, idx_name)
        assert idx_name in r
        props = r[idx_name]["mappings"]["properties"]
        assert props["title"]["type"] == "text"

        # Delete
        r = OpenSearchTools.safe_call(tools.delete_index, idx_name)
        assert r.get("acknowledged") is True

        # Exists after delete
        assert OpenSearchTools.safe_call(tools.index_exists, idx_name) == {"result": False}


# ── Document CRUD ──────────────────────────────────────────────────────


class TestDocuments:
    def _setup(self, tools: OpenSearchTools) -> str:
        idx = "test_pytest_docs"
        tools._client.indices.delete(index=idx, ignore=[404])
        tools._client.indices.create(
            index=idx,
            body={"mappings": {"properties": {"name": {"type": "text"}, "value": {"type": "integer"}}}},
        )
        return idx

    def test_index_and_get(self, tools: OpenSearchTools) -> None:
        idx = self._setup(tools)
        doc = {"name": "alice", "value": 100}
        r = OpenSearchTools.safe_call(tools.index_document, idx, "d1", doc, "true")
        assert r.get("result") == "created"

        r = OpenSearchTools.safe_call(tools.get_document, idx, "d1")
        assert r.get("found") is True
        assert r["_source"]["name"] == "alice"

        tools._client.indices.delete(index=idx)

    def test_update_document(self, tools: OpenSearchTools) -> None:
        idx = self._setup(tools)
        tools._client.index(index=idx, id="d2", body={"name": "bob", "value": 1}, refresh="true")

        r = OpenSearchTools.safe_call(tools.update_document, idx, "d2", {"value": 99}, "true")
        assert r.get("result") == "updated"

        r = OpenSearchTools.safe_call(tools.get_document, idx, "d2")
        assert r["_source"]["value"] == 99

        tools._client.indices.delete(index=idx)

    def test_delete_document(self, tools: OpenSearchTools) -> None:
        idx = self._setup(tools)
        tools._client.index(index=idx, id="d3", body={"name": "carol"}, refresh="true")

        r = OpenSearchTools.safe_call(tools.delete_document, idx, "d3", "true")
        assert r.get("result") == "deleted"

        r = OpenSearchTools.safe_call(tools.get_document, idx, "d3")
        assert "error" in r and r["error"] == "not_found"

        tools._client.indices.delete(index=idx)

    def test_search(self, tools: OpenSearchTools) -> None:
        idx = self._setup(tools)
        tools._client.index(index=idx, id="s1", body={"name": "xavier", "value": 7}, refresh="true")
        tools._client.index(index=idx, id="s2", body={"name": "yara", "value": 42}, refresh="true")

        r = OpenSearchTools.safe_call(tools.search, idx, None, 10)
        hits = r.get("hits", {}).get("hits", [])
        assert len(hits) == 2

        r = OpenSearchTools.safe_call(tools.search, idx, {"match": {"name": "xavier"}}, 10)
        hits = r.get("hits", {}).get("hits", [])
        assert len(hits) == 1
        assert hits[0]["_source"]["name"] == "xavier"

        tools._client.indices.delete(index=idx)

    def test_count(self, tools: OpenSearchTools) -> None:
        idx = self._setup(tools)
        tools._client.index(index=idx, id="c1", body={"name": "zoe"}, refresh="true")

        r = OpenSearchTools.safe_call(tools.count_documents, idx)
        assert r.get("count") == 1

        tools._client.indices.delete(index=idx)


# ── SQL ---


class TestSQL:
    def test_sql_query(self, tools: OpenSearchTools) -> None:
        idx = "test_pytest_sql"
        tools._client.indices.delete(index=idx, ignore=[404])
        tools._client.index(index=idx, id="sq1", body={"msg": "hello"}, refresh="true")

        r = OpenSearchTools.safe_call(tools.sql_query, f"SELECT * FROM {idx} LIMIT 1")
        assert "datarows" in r or "schema" in r

        tools._client.indices.delete(index=idx)

    def test_sql_explain(self, tools: OpenSearchTools) -> None:
        idx = "test_pytest_sql"
        tools._client.indices.delete(index=idx, ignore=[404])
        tools._client.index(index=idx, id="sq2", body={"msg": "world"}, refresh="true")

        r = OpenSearchTools.safe_call(tools.sql_explain, f"SELECT * FROM {idx}")
        assert isinstance(r, dict)
        assert len(r) > 0

        tools._client.indices.delete(index=idx)


# ── Cat API ────────────────────────────────────────────────────────────


class TestCatAPI:
    def test_cat_nodes(self, tools: OpenSearchTools) -> None:
        result = OpenSearchTools.safe_call(tools.cat_nodes)
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_cat_shards(self, tools: OpenSearchTools) -> None:
        result = OpenSearchTools.safe_call(tools.cat_shards)
        assert isinstance(result, list)


# ── Edge cases ─────────────────────────────────────────────────────────


class TestErrorHandling:
    def test_get_nonexistent_document(self, tools: OpenSearchTools) -> None:
        r = OpenSearchTools.safe_call(tools.get_document, "nonexistent_index", "x")
        assert "error" in r

    def test_search_nonexistent_index(self, tools: OpenSearchTools) -> None:
        r = OpenSearchTools.safe_call(tools.search, "nonexistent_index")
        assert "error" in r

    def test_sql_invalid_query(self, tools: OpenSearchTools) -> None:
        r = OpenSearchTools.safe_call(tools.sql_query, "SELECT FOO FROM BAR")
        assert "error" in r
