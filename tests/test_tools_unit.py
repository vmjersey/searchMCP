"""Deterministic unit tests for OpenSearchTools using a mocked client.

These tests do not require a live OpenSearch instance.  They exercise every
method in tools.py plus every exception branch of safe_call(), so the whole
module is covered without depending on network state.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from opensearchpy import NotFoundError, RequestError, TransportError

from search_mcp.tools import OpenSearchTools


@pytest.fixture
def tools() -> OpenSearchTools:
    return OpenSearchTools(MagicMock())


def _c(tools: OpenSearchTools) -> MagicMock:
    """Return the mocked underlying OpenSearch client."""
    return tools._client


# ── Cluster / Server info ──────────────────────────────────────────────


class TestClusterInfo:
    def test_cluster_health(self, tools) -> None:
        _c(tools).cluster.health.return_value = {"status": "green"}
        assert tools.cluster_health() == {"status": "green"}
        _c(tools).cluster.health.assert_called_once_with()

    def test_cluster_stats(self, tools) -> None:
        _c(tools).cluster.stats.return_value = {"_all": {}}
        assert tools.cluster_stats() == {"_all": {}}
        _c(tools).cluster.stats.assert_called_once_with()

    def test_node_info(self, tools) -> None:
        _c(tools).nodes.info.return_value = {"nodes": {}}
        assert tools.node_info() == {"nodes": {}}

    def test_server_info(self, tools) -> None:
        _c(tools).info.return_value = {"version": {"number": "3.8.0"}}
        assert tools.server_info() == {"version": {"number": "3.8.0"}}


# ── Index operations ───────────────────────────────────────────────────


class TestIndexOps:
    def test_list_indices_maps_fields(self, tools) -> None:
        _c(tools).cat.indices.return_value = [
            {
                "index": "a",
                "health": "green",
                "status": "open",
                "docs.count": "10",
                "docs.deleted": "1",
                "store.size": "1kb",
                "pri.store.size": "1kb",
                "pri": "1",
                "rep": "0",
                "uuid": "u",
            }
        ]
        out = tools.list_indices("a*")
        _c(tools).cat.indices.assert_called_once_with(index="a*", format="json")
        assert out[0]["index"] == "a"
        assert out[0]["health"] == "green"
        assert out[0]["docs_count"] == "10"
        assert out[0]["docs_deleted"] == "1"
        assert out[0]["store_size"] == "1kb"
        assert out[0]["primary_store_size"] == "1kb"
        assert out[0]["number_of_shards"] == "1"
        assert out[0]["number_of_replicas"] == "0"
        assert out[0]["uuid"] == "u"

    def test_list_indices_partial_entry_defaults(self, tools) -> None:
        _c(tools).cat.indices.return_value = [{}]
        out = tools.list_indices()
        assert out[0]["index"] == ""
        assert out[0]["health"] == ""
        assert out[0]["docs_count"] == "0"
        assert out[0]["store_size"] == "0b"

    def test_get_index_mapping(self, tools) -> None:
        _c(tools).indices.get_mapping.return_value = {"a": {}}
        assert tools.get_index_mapping("a") == {"a": {}}
        _c(tools).indices.get_mapping.assert_called_once_with(index="a")

    def test_get_index_settings(self, tools) -> None:
        _c(tools).indices.get_settings.return_value = {"a": {}}
        assert tools.get_index_settings("a") == {"a": {}}
        _c(tools).indices.get_settings.assert_called_once_with(index="a")

    def test_create_index_with_mappings_and_settings(self, tools) -> None:
        _c(tools).indices.create.return_value = {"acknowledged": True}
        r = tools.create_index("a", {"properties": {}}, {"number_of_shards": 2})
        assert r == {"acknowledged": True}
        _c(tools).indices.create.assert_called_once_with(
            index="a",
            body={"mappings": {"properties": {}}, "settings": {"number_of_shards": 2}},
        )

    def test_create_index_mappings_only(self, tools) -> None:
        tools.create_index("a", mappings={"properties": {}})
        _c(tools).indices.create.assert_called_once_with(
            index="a", body={"mappings": {"properties": {}}}
        )

    def test_create_index_empty_body(self, tools) -> None:
        tools.create_index("a")
        _c(tools).indices.create.assert_called_once_with(index="a", body={})

    def test_delete_index(self, tools) -> None:
        tools.delete_index("a")
        _c(tools).indices.delete.assert_called_once_with(index="a")

    def test_index_exists_true(self, tools) -> None:
        _c(tools).indices.exists.return_value = True
        assert tools.index_exists("a") is True
        _c(tools).indices.exists.assert_called_once_with(index="a")

    def test_index_exists_false(self, tools) -> None:
        _c(tools).indices.exists.return_value = False
        assert tools.index_exists("a") is False


# ── Document operations ────────────────────────────────────────────────


class TestDocumentOps:
    def test_get_document(self, tools) -> None:
        _c(tools).get.return_value = {"found": True}
        assert tools.get_document("a", "id1") == {"found": True}
        _c(tools).get.assert_called_once_with(index="a", id="id1")

    def test_index_document_with_id(self, tools) -> None:
        tools.index_document("a", "id1", {"k": "v"}, "true")
        _c(tools).index.assert_called_once_with(
            index="a", body={"k": "v"}, refresh="true", id="id1"
        )

    def test_index_document_without_id(self, tools) -> None:
        tools.index_document("a", None, {"k": "v"})
        _c(tools).index.assert_called_once_with(
            index="a", body={"k": "v"}, refresh="false"
        )

    def test_update_document(self, tools) -> None:
        tools.update_document("a", "id2", {"v": 99}, "true")
        _c(tools).update.assert_called_once_with(
            index="a", id="id2", body={"doc": {"v": 99}}, refresh="true"
        )

    def test_delete_document(self, tools) -> None:
        tools.delete_document("a", "id3", "true")
        _c(tools).delete.assert_called_once_with(index="a", id="id3", refresh="true")

    def test_search_basic(self, tools) -> None:
        _c(tools).search.return_value = {"hits": {"hits": []}}
        tools.search("a", size=5, from_=2)
        _c(tools).search.assert_called_once_with(
            index="a", body={}, size=5, from_=2
        )

    def test_search_with_query_sort_source(self, tools) -> None:
        tools.search("a", {"match": {"x": "y"}}, 10, 0, "field:asc", ["f1", "f2"])
        _c(tools).search.assert_called_once_with(
            index="a",
            body={"query": {"match": {"x": "y"}}, "sort": "field:asc"},
            size=10,
            from_=0,
            _source=["f1", "f2"],
        )

    def test_count_without_query(self, tools) -> None:
        _c(tools).count.return_value = {"count": 3}
        assert tools.count_documents("a") == {"count": 3}
        _c(tools).count.assert_called_once_with(index="a")

    def test_count_with_query(self, tools) -> None:
        _c(tools).count.return_value = {"count": 1}
        assert tools.count_documents("a", {"match_all": {}}) == {"count": 1}
        _c(tools).count.assert_called_once_with(
            index="a", body={"query": {"match_all": {}}}
        )


# ── Bulk ───────────────────────────────────────────────────────────────


class TestBulk:
    def test_bulk_with_id_field(self, tools) -> None:
        with patch("search_mcp.tools.bulk", return_value=(3, [])) as mock_bulk:
            docs = [
                {"key": "a", "v": 1},
                {"key": "b", "v": 2},
                {"v": 3},  # missing id_field -> no _id
            ]
            r = tools.bulk_index("idx", docs, id_field="key", refresh="true")
        assert r == {"success_count": 3, "errors": []}
        assert mock_bulk.call_args[1]["refresh"] == "true"
        actions = list(mock_bulk.call_args[0][1])  # consume the generator
        assert actions[0]["_index"] == "idx"
        assert actions[0]["_source"] == {"key": "a", "v": 1}
        assert actions[0]["_id"] == "a"
        assert actions[1]["_id"] == "b"
        assert "_id" not in actions[2]

    def test_bulk_without_id_field(self, tools) -> None:
        with patch("search_mcp.tools.bulk", return_value=(2, [])) as mock_bulk:
            r = tools.bulk_index("idx", [{"v": 1}, {"v": 2}] )
        assert r["success_count"] == 2
        actions = list(mock_bulk.call_args[0][1])
        assert all("_id" not in a for a in actions)

    def test_bulk_errors_truncated(self, tools) -> None:
        many_errors = [{"index": {"error": f"e{i}"}} for i in range(100)]
        with patch("search_mcp.tools.bulk", return_value=(0, many_errors)):
            r = tools.bulk_index("idx", [{"v": 1}])
        assert len(r["errors"]) == 50  # _MAX_BULK_ERRORS
        assert r["success_count"] == 0


# ── SQL plugin ─────────────────────────────────────────────────────────


class TestSQL:
    def test_sql_query(self, tools) -> None:
        _c(tools).transport.perform_request.return_value = {"datarows": []}
        r = tools.sql_query("SELECT * FROM a")
        assert r == {"datarows": []}
        _c(tools).transport.perform_request.assert_called_once_with(
            "POST", "/_plugins/_sql", body={"query": "SELECT * FROM a", "fetch_size": 10000}
        )

    def test_sql_explain(self, tools) -> None:
        _c(tools).transport.perform_request.return_value = {"from": 0}
        r = tools.sql_explain("SELECT * FROM a")
        assert r == {"from": 0}
        _c(tools).transport.perform_request.assert_called_once_with(
            "POST", "/_plugins/_sql/_explain", body={"query": "SELECT * FROM a"}
        )


# ── Field capabilities / Cat API ───────────────────────────────────────


class TestFieldCapsAndCat:
    def test_field_caps(self, tools) -> None:
        _c(tools).field_caps.return_value = {"fields": {}}
        assert tools.field_caps("a", "f*") == {"fields": {}}
        _c(tools).field_caps.assert_called_once_with(index="a", fields="f*")

    def test_cat_aliases(self, tools) -> None:
        _c(tools).cat.aliases.return_value = [{"alias": "a"}]
        assert tools.cat_aliases() == [{"alias": "a"}]
        _c(tools).cat.aliases.assert_called_once_with(format="json")

    def test_cat_health_picks_first(self, tools) -> None:
        _c(tools).cat.health.return_value = [{"status": "green"}]
        assert tools.cat_health() == {"status": "green"}

    def test_cat_health_empty(self, tools) -> None:
        _c(tools).cat.health.return_value = []
        assert tools.cat_health() == {}
        _c(tools).cat.health.assert_called_once_with(format="json")

    def test_cat_nodes(self, tools) -> None:
        _c(tools).cat.nodes.return_value = [{"name": "n1"}]
        assert tools.cat_nodes() == [{"name": "n1"}]
        _c(tools).cat.nodes.assert_called_once_with(format="json")

    def test_cat_shards_all(self, tools) -> None:
        _c(tools).cat.shards.return_value = [{"index": "a"}]
        assert tools.cat_shards() == [{"index": "a"}]
        _c(tools).cat.shards.assert_called_once_with(format="json")

    def test_cat_shards_filtered(self, tools) -> None:
        _c(tools).cat.shards.return_value = []
        assert tools.cat_shards("a") == []
        _c(tools).cat.shards.assert_called_once_with(format="json", index="a")


# ── Explain / validate / reindex ───────────────────────────────────────


class TestExplainValidateReindex:
    def test_explain_score(self, tools) -> None:
        _c(tools).explain.return_value = {"_explanation": {}}
        r = tools.explain_score("a", "id1", {"match": {"f": "v"}})
        assert r == {"_explanation": {}}
        _c(tools).explain.assert_called_once_with(
            index="a", id="id1", body={"query": {"match": {"f": "v"}}}
        )

    def test_validate_query_explain_false(self, tools) -> None:
        _c(tools).indices.validate_query.return_value = {"valid": True}
        r = tools.validate_query("a", {"match_all": {}})
        assert r == {"valid": True}
        _c(tools).indices.validate_query.assert_called_once_with(
            index="a", body={"query": {"match_all": {}}}
        )

    def test_validate_query_explain_true(self, tools) -> None:
        _c(tools).indices.validate_query.return_value = {"valid": True}
        tools.validate_query("a", {"match_all": {}}, explain=True)
        _c(tools).indices.validate_query.assert_called_once_with(
            index="a", body={"query": {"match_all": {}}}, explain=True
        )

    def test_reindex_without_query(self, tools) -> None:
        _c(tools).reindex.return_value = {"task": "t1"}
        r = tools.reindex("src", "dst")
        assert r == {"task": "t1"}
        _c(tools).reindex.assert_called_once_with(
            body={"source": {"index": "src"}, "dest": {"index": "dst"}},
            wait_for_completion=True,
        )

    def test_reindex_with_query_not_wait(self, tools) -> None:
        _c(tools).reindex.return_value = {"task": "t2"}
        tools.reindex("src", "dst", {"term": {"k": 1}}, wait_for_completion=False)
        _c(tools).reindex.assert_called_once_with(
            body={"source": {"index": "src", "query": {"term": {"k": 1}}}, "dest": {"index": "dst"}},
            wait_for_completion=False,
        )


# ── safe_call branches ─────────────────────────────────────────────────


class TestSafeCall:
    def test_returns_bool_as_result(self) -> None:
        assert OpenSearchTools.safe_call(lambda: True) == {"result": True}
        assert OpenSearchTools.safe_call(lambda: False) == {"result": False}

    def test_returns_result_body_when_present(self) -> None:
        obj = MagicMock()
        obj.body = {"acknowledged": True}
        assert OpenSearchTools.safe_call(lambda: obj) == {"acknowledged": True}

    def test_returns_plain_result(self) -> None:
        assert OpenSearchTools.safe_call(lambda: {"x": 1}) == {"x": 1}

    def test_not_found_error(self) -> None:
        def boom():
            raise NotFoundError(404, "missing", {"index": "a"})

        r = OpenSearchTools.safe_call(boom)
        assert r["error"] == "not_found"
        assert "index" in r["message"]

    def test_request_error(self) -> None:
        def boom():
            raise RequestError(400, "bad", {"reason": "nope"})

        r = OpenSearchTools.safe_call(boom)
        assert r["error"] == "bad_request"
        assert r["message"] == "{'reason': 'nope'}"

    def test_transport_error(self) -> None:
        def boom():
            raise TransportError(503, "down")

        r = OpenSearchTools.safe_call(boom)
        assert r["error"] == "transport_error"
        assert r["status"] == 503

    def test_generic_exception(self) -> None:
        def boom():
            raise ValueError("boom")

        r = OpenSearchTools.safe_call(boom)
        assert r["error"] == "ValueError"
        assert r["message"] == "boom"