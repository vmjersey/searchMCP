"""MCP tool implementations for OpenSearch."""

from __future__ import annotations

import logging
from typing import Any

from opensearchpy import NotFoundError, OpenSearch, RequestError, TransportError
from opensearchpy.helpers import bulk

_SQL_FETCH_SIZE = 10000
_MAX_BULK_ERRORS = 50

logger = logging.getLogger(__name__)


class OpenSearchTools:
    """Collection of tool implementations for OpenSearch operations."""

    def __init__(self, client: OpenSearch) -> None:
        self._client = client

    # ── Cluster / Server Info ──────────────────────────────────────────

    def cluster_health(self) -> dict[str, Any]:
        """Get cluster health status."""
        return self._client.cluster.health()

    def cluster_stats(self) -> dict[str, Any]:
        """Get cluster statistics."""
        return self._client.cluster.stats()

    def node_info(self) -> dict[str, Any]:
        """Get node information."""
        return self._client.nodes.info()

    def server_info(self) -> dict[str, Any]:
        """Get OpenSearch server info."""
        return self._client.info()

    # ── Index Operations ───────────────────────────────────────────────

    def list_indices(self, pattern: str = "*") -> list[dict[str, Any]]:
        """List indices matching pattern with basic stats using cat API."""
        raw = self._client.cat.indices(index=pattern, format="json")
        result = []
        for entry in raw:
            result.append({
                "index": entry.get("index", ""),
                "health": entry.get("health", ""),
                "status": entry.get("status", ""),
                "docs_count": entry.get("docs.count", "0"),
                "docs_deleted": entry.get("docs.deleted", "0"),
                "store_size": entry.get("store.size", "0b"),
                "primary_store_size": entry.get("pri.store.size", "0b"),
                "number_of_shards": entry.get("pri", "0"),
                "number_of_replicas": entry.get("rep", "0"),
                "uuid": entry.get("uuid", ""),
            })
        return result

    def get_index_mapping(self, index: str) -> dict[str, Any]:
        """Get mapping for a specific index."""
        return self._client.indices.get_mapping(index=index)

    def get_index_settings(self, index: str) -> dict[str, Any]:
        """Get settings for a specific index."""
        return self._client.indices.get_settings(index=index)

    def create_index(
        self,
        index: str,
        mappings: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create an index with optional mappings and settings."""
        body: dict[str, Any] = {}
        if mappings:
            body["mappings"] = mappings
        if settings:
            body["settings"] = settings
        return self._client.indices.create(index=index, body=body)

    def delete_index(self, index: str) -> dict[str, Any]:
        """Delete an index."""
        return self._client.indices.delete(index=index)

    def index_exists(self, index: str) -> bool:
        """Check if an index exists."""
        return self._client.indices.exists(index=index)

    # ── Document Operations ────────────────────────────────────────────

    def search(
        self,
        index: str,
        query: dict[str, Any] | None = None,
        size: int = 10,
        from_: int = 0,
        sort: str | None = None,
        source: str | list[str] | None = None,
    ) -> dict[str, Any]:
        """Search documents in an index."""
        body: dict[str, Any] = {}
        if query:
            body["query"] = query
        if sort:
            body["sort"] = sort
        kwargs: dict[str, Any] = {
            "index": index,
            "body": body,
            "size": size,
            "from_": from_,
        }
        if source is not None:
            kwargs["_source"] = source
        return self._client.search(**kwargs)

    def get_document(self, index: str, doc_id: str) -> dict[str, Any]:
        """Get a document by ID."""
        return self._client.get(index=index, id=doc_id)

    def index_document(
        self,
        index: str,
        doc_id: str | None,
        document: dict[str, Any],
        refresh: str = "false",
    ) -> dict[str, Any]:
        """Index a document (create or full replace)."""
        kwargs: dict[str, Any] = {"index": index, "body": document, "refresh": refresh}
        if doc_id:
            kwargs["id"] = doc_id
        return self._client.index(**kwargs)

    def update_document(
        self,
        index: str,
        doc_id: str,
        doc: dict[str, Any],
        refresh: str = "false",
    ) -> dict[str, Any]:
        """Update a document by ID (partial update)."""
        return self._client.update(
            index=index, id=doc_id, body={"doc": doc}, refresh=refresh
        )

    def delete_document(self, index: str, doc_id: str, refresh: str = "false") -> dict[str, Any]:
        """Delete a document by ID."""
        return self._client.delete(index=index, id=doc_id, refresh=refresh)

    def count_documents(self, index: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        """Count documents in an index, optionally matching a query."""
        kwargs: dict[str, Any] = {"index": index}
        if query:
            kwargs["body"] = {"query": query}
        return self._client.count(**kwargs)

    def bulk_index(
        self,
        index: str,
        documents: list[dict[str, Any]],
        id_field: str | None = None,
        refresh: str = "false",
    ) -> dict[str, Any]:
        """Bulk index multiple documents and return a summary of successes/errors."""
        def gen():
            for doc in documents:
                action = {"_index": index, "_source": doc}
                if id_field and id_field in doc:
                    action["_id"] = doc[id_field]
                yield action

        success, errors = bulk(self._client, gen(), refresh=refresh, raise_on_error=False)
        return {"success_count": success, "errors": errors[:_MAX_BULK_ERRORS] if errors else []}

    # ── SQL Plugin ─────────────────────────────────────────────────────

    def sql_query(self, query: str) -> dict[str, Any]:
        """Execute an OpenSearch SQL query."""
        return self._client.transport.perform_request(
            "POST", "/_plugins/_sql", body={"query": query, "fetch_size": _SQL_FETCH_SIZE}
        )

    def sql_explain(self, query: str) -> dict[str, Any]:
        """Explain how an OpenSearch SQL query is translated."""
        return self._client.transport.perform_request(
            "POST", "/_plugins/_sql/_explain", body={"query": query}
        )

    # ── Field capabilities ─────────────────────────────────────────────

    def field_caps(self, index: str, fields: str = "*") -> dict[str, Any]:
        """Get field capabilities for an index."""
        return self._client.field_caps(index=index, fields=fields)

    # ── Cat API helpers ────────────────────────────────────────────────

    def cat_aliases(self) -> list[dict[str, Any]]:
        """List all aliases using cat API."""
        return self._client.cat.aliases(format="json")

    def cat_health(self) -> dict[str, Any]:
        """Cluster health via cat API."""
        result = self._client.cat.health(format="json")
        return result[0] if result else {}

    def cat_nodes(self) -> list[dict[str, Any]]:
        """List nodes via cat API."""
        return self._client.cat.nodes(format="json")

    def cat_shards(self, index: str | None = None) -> list[dict[str, Any]]:
        """List shards via cat API."""
        kwargs: dict[str, Any] = {"format": "json"}
        if index:
            kwargs["index"] = index
        return self._client.cat.shards(**kwargs)

    # ── Explain / Validate ─────────────────────────────────────────────

    def explain_score(
        self, index: str, doc_id: str, query: dict[str, Any]
    ) -> dict[str, Any]:
        """Explain why a document matches (or not) a query."""
        return self._client.explain(index=index, id=doc_id, body={"query": query})

    def validate_query(
        self, index: str, query: dict[str, Any], explain: bool = False
    ) -> dict[str, Any]:
        """Validate a query without executing it."""
        kwargs: dict[str, Any] = {"index": index, "body": {"query": query}}
        if explain:
            kwargs["explain"] = True
        return self._client.indices.validate_query(**kwargs)

    # ── Reindex ────────────────────────────────────────────────────────

    def reindex(
        self,
        source_index: str,
        dest_index: str,
        query: dict[str, Any] | None = None,
        wait_for_completion: bool = True,
    ) -> dict[str, Any]:
        """Reindex documents from source to destination."""
        body: dict[str, Any] = {"source": {"index": source_index}, "dest": {"index": dest_index}}
        if query:
            body["source"]["query"] = query
        return self._client.reindex(body=body, wait_for_completion=wait_for_completion)

    # ── Error wrapper ──────────────────────────────────────────────────

    @staticmethod
    def safe_call(fn, *args, **kwargs) -> dict[str, Any]:
        """Wrap a call and return a consistent error dict on failure."""
        try:
            result = fn(*args, **kwargs)
            if isinstance(result, bool):
                return {"result": result}
            if hasattr(result, "body"):
                return result.body
            return result
        except NotFoundError as e:
            return {"error": "not_found", "message": str(e.info) if hasattr(e, 'info') else str(e)}
        except RequestError as e:
            return {"error": "bad_request", "message": str(e.info) if hasattr(e, 'info') else str(e)}
        except TransportError as e:
            return {"error": "transport_error", "status": e.status_code, "message": str(e)}
        except Exception as e:
            logger.exception("Unexpected error in %s", getattr(fn, "__qualname__", repr(fn)))
            return {"error": type(e).__name__, "message": str(e)}
