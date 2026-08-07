# -*- coding: utf-8 -*-
"""Strict Graph/S3 source-adapter proofs for bounded maintenance reindex."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import re
import sys
import types

import pytest

_MEMORY_ID = "memory-a"
_BUCKET = "source-bucket"


class _Body:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def read(self, amount: int | None = None) -> bytes:
        if amount is None:
            return self._content
        return self._content[:amount]


class _InventoryClient:
    def __init__(self, *, pages: dict, heads: dict, contents: dict) -> None:
        self.pages = copy.deepcopy(pages)
        self.heads = copy.deepcopy(heads)
        self.contents = dict(contents)
        self.list_calls: list[dict] = []
        self.head_calls: list[str] = []
        self.get_calls: list[str] = []
        self.put_calls: list[dict] = []

    def list_objects_v2(self, **params):
        self.list_calls.append(dict(params))
        token = params.get("ContinuationToken")
        return copy.deepcopy(self.pages[token])

    def head_object(self, *, Bucket: str, Key: str):
        assert Bucket == _BUCKET
        self.head_calls.append(Key)
        return copy.deepcopy(self.heads[Key])

    def get_object(self, *, Bucket: str, Key: str):
        assert Bucket == _BUCKET
        self.get_calls.append(Key)
        content = self.contents[Key]
        return {"Body": _Body(content), "ContentLength": len(content)}

    def put_object(self, **params):
        self.put_calls.append(copy.deepcopy(params))
        return {}


def _storage_class(monkeypatch: pytest.MonkeyPatch):
    from tests.fakes.inference_fakes import apply_graph_memory_baseline_env

    apply_graph_memory_baseline_env(monkeypatch)
    from mcp_memory.core.storage import StorageService

    return StorageService


def _storage(client: _InventoryClient, storage_class):
    StorageService = storage_class
    service = object.__new__(StorageService)
    service._bucket = _BUCKET
    service._client_v4 = client
    service._client = client
    return service


def _source_fixture():
    content = b"retained source bytes"
    digest = hashlib.sha256(content).hexdigest()
    document_key = f"{_MEMORY_ID}/documents/{digest[:8]}_source.txt"
    ontology = b"name: general\n"
    ontology_hash = hashlib.sha256(ontology).hexdigest()
    ontology_name = "_ontology_general.yaml"
    ontology_key = (
        f"{_MEMORY_ID}/documents/{ontology_hash[:8]}_{ontology_name}"
    )
    pages = {
        None: {
            "Contents": [{"Key": document_key, "Size": len(content)}],
            "IsTruncated": True,
            "NextContinuationToken": "page-2",
        },
        "page-2": {
            "Contents": [{"Key": ontology_key, "Size": len(ontology)}],
            "IsTruncated": False,
        },
    }
    heads = {
        document_key: {
            "ContentLength": len(content),
            "Metadata": {
                "memory_id": _MEMORY_ID,
                "original_filename": "source.txt",
                "doc_hash": digest,
            },
        },
        ontology_key: {
            "ContentLength": len(ontology),
            "Metadata": {
                "memory_id": _MEMORY_ID,
                "original_filename": ontology_name,
                "doc_hash": ontology_hash,
                "type": "ontology",
                "ontology_name": "general",
            },
        },
    }
    contents = {document_key: content, ontology_key: ontology}
    return document_key, content, pages, heads, contents


async def test_storage_inventory_is_exhaustive_and_returns_config_for_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_key, content, pages, heads, contents = _source_fixture()
    client = _InventoryClient(pages=pages, heads=heads, contents=contents)
    storage = _storage(client, _storage_class(monkeypatch))

    result = await storage.list_reindex_objects(_MEMORY_ID)
    loaded = await storage.read_reindex_object(
        _MEMORY_ID,
        document_key,
        len(content),
    )

    assert result == [
        {
            "key": document_key,
            "uri": f"s3://{_BUCKET}/{document_key}",
            "size_bytes": len(content),
            "metadata": heads[document_key]["Metadata"],
        },
        {
            "key": next(reversed(heads)),
            "uri": f"s3://{_BUCKET}/{next(reversed(heads))}",
            "size_bytes": pages["page-2"]["Contents"][0]["Size"],
            "metadata": heads[next(reversed(heads))]["Metadata"],
        },
    ]
    assert loaded == content
    assert [call.get("ContinuationToken") for call in client.list_calls] == [
        None,
        "page-2",
    ]
    assert client.head_calls == list(heads)
    assert client.get_calls == [document_key]


async def test_storage_reindex_calls_are_offloaded_from_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_key, content, pages, heads, contents = _source_fixture()
    client = _InventoryClient(pages=pages, heads=heads, contents=contents)
    storage = _storage(client, _storage_class(monkeypatch))
    from mcp_memory.core import storage as storage_module

    offloaded: list[str] = []

    async def run_inline_for_proof(callable_, *args, **kwargs):
        offloaded.append(getattr(callable_, "__name__", type(callable_).__name__))
        return callable_(*args, **kwargs)

    monkeypatch.setattr(storage_module.asyncio, "to_thread", run_inline_for_proof)

    await storage.list_reindex_objects(_MEMORY_ID)
    await storage.read_reindex_object(_MEMORY_ID, document_key, len(content))

    assert offloaded == [
        "list_objects_v2",
        "head_object",
        "list_objects_v2",
        "head_object",
        "get_object",
        "read_and_close",
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda pages, _heads: pages[None].pop("NextContinuationToken"),
        lambda pages, _heads: pages[None].pop("IsTruncated"),
        lambda pages, _heads: pages[None].update({"IsTruncated": False}),
        lambda _pages, heads: heads[next(iter(heads))].update(
            {"ContentLength": 999}
        ),
        lambda _pages, heads: heads[next(reversed(heads))]["Metadata"].update(
            {"ontology_name": 7}
        ),
    ],
    ids=(
        "missing-page-token",
        "missing-truncated-flag",
        "terminal-page-token",
        "head-size-mismatch",
        "non-string-metadata",
    ),
)
async def test_storage_inventory_fails_closed_on_unverifiable_shapes(
    mutate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _document_key, _content, pages, heads, contents = _source_fixture()
    mutate(pages, heads)
    storage = _storage(
        _InventoryClient(pages=pages, heads=heads, contents=contents),
        _storage_class(monkeypatch),
    )

    with pytest.raises(RuntimeError, match="invalid source inventory"):
        await storage.list_reindex_objects(_MEMORY_ID)


@pytest.mark.parametrize(
    ("mutation", "expected_list_calls", "expected_head_calls"),
    [
        ("empty-truncated", 1, 0),
        ("repeated-token", 2, 1),
        ("repeated-key", 2, 1),
    ],
)
async def test_storage_inventory_rejects_nonprogressing_pagination(
    mutation: str,
    expected_list_calls: int,
    expected_head_calls: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _document_key, _content, pages, heads, contents = _source_fixture()
    if mutation == "empty-truncated":
        pages[None]["Contents"] = []
    elif mutation == "repeated-token":
        pages["page-2"].update(
            {"IsTruncated": True, "NextContinuationToken": "page-2"}
        )
    else:
        pages["page-2"]["Contents"] = list(pages[None]["Contents"])
    client = _InventoryClient(pages=pages, heads=heads, contents=contents)
    storage = _storage(client, _storage_class(monkeypatch))

    with pytest.raises(RuntimeError, match="invalid source inventory"):
        await storage.list_reindex_objects(_MEMORY_ID)

    assert len(client.list_calls) == expected_list_calls
    assert len(client.head_calls) == expected_head_calls


@pytest.mark.parametrize("limit_kind", ["count", "volume"])
async def test_storage_inventory_caps_before_head_amplification(
    limit_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    StorageService = _storage_class(monkeypatch)
    from mcp_memory.core import maintenance as maintenance_module
    from mcp_memory.core import storage as storage_module

    keys = [
        f"{_MEMORY_ID}/documents/source-{index}.txt"
        for index in range(2)
    ]
    pages = {
        None: {
            "Contents": [
                {"Key": keys[0], "Size": 6},
                {"Key": keys[1], "Size": 5},
            ],
            "IsTruncated": False,
        }
    }
    heads = {
        key: {"ContentLength": size, "Metadata": {}}
        for key, size in zip(keys, (6, 5))
    }
    client = _InventoryClient(pages=pages, heads=heads, contents={})
    storage = _storage(client, StorageService)
    if limit_kind == "count":
        monkeypatch.setattr(storage_module, "MAX_REINDEX_SOURCE_OBJECTS", 1)
    else:
        monkeypatch.setattr(storage_module, "MAX_REINDEX_SOURCE_TOTAL_BYTES", 10)

    with pytest.raises(maintenance_module.ReindexSourceLimitExceeded):
        await storage.list_reindex_objects(_MEMORY_ID)

    assert client.head_calls == []


async def test_storage_read_rejects_cross_namespace_key_before_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_key, _content, pages, heads, contents = _source_fixture()
    client = _InventoryClient(pages=pages, heads=heads, contents=contents)
    storage = _storage(client, _storage_class(monkeypatch))

    with pytest.raises(PermissionError):
        await storage.read_reindex_object(
            _MEMORY_ID,
            document_key.replace(_MEMORY_ID, "other-memory", 1),
            len(_content),
        )

    assert client.get_calls == []


async def test_storage_read_rejects_boolean_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_key, _content, pages, heads, contents = _source_fixture()
    client = _InventoryClient(pages=pages, heads=heads, contents=contents)
    storage = _storage(client, _storage_class(monkeypatch))

    def malformed_get(*, Bucket: str, Key: str):
        assert Bucket == _BUCKET
        assert Key == document_key
        return {"Body": _Body(b"x"), "ContentLength": True}

    monkeypatch.setattr(client, "get_object", malformed_get)

    with pytest.raises(RuntimeError, match="invalid source object response"):
        await storage.read_reindex_object(_MEMORY_ID, document_key, 1)


async def test_upload_cannot_override_retained_source_ownership_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _InventoryClient(pages={}, heads={}, contents={})
    storage = _storage(client, _storage_class(monkeypatch))
    content = b"source"

    result = await storage.upload_document(
        memory_id=_MEMORY_ID,
        filename="source.txt",
        content=content,
        metadata={
            "memory_id": "victim",
            "original_filename": "forged.txt",
            "doc_hash": "0" * 64,
            "uploaded_at": "forged",
            "type": "user-type",
        },
    )

    assert result["hash"] == hashlib.sha256(content).hexdigest()
    metadata = client.put_calls[0]["Metadata"]
    assert metadata["memory_id"] == _MEMORY_ID
    assert metadata["original_filename"] == "source.txt"
    assert metadata["doc_hash"] == result["hash"]
    assert metadata["uploaded_at"] != "forged"
    assert metadata["type"] == "user-type"


class _Rows:
    def __init__(
        self,
        rows: list[dict],
        *,
        deferred_failure: str | None = None,
        success_before_failure: bool = False,
        on_consume=None,
        on_success=None,
    ) -> None:
        self._rows = iter(copy.deepcopy(rows))
        self._deferred_failure = deferred_failure
        self._success_before_failure = success_before_failure
        self._on_consume = on_consume
        self._on_success = on_success

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._rows)
        except StopIteration:
            raise StopAsyncIteration from None

    async def single(self):
        try:
            return next(self._rows)
        except StopIteration:
            return None

    async def consume(self):
        if self._on_consume is not None:
            self._on_consume()
        if self._success_before_failure and self._on_success is not None:
            self._on_success()
        if self._deferred_failure is not None:
            raise RuntimeError(self._deferred_failure)
        if not self._success_before_failure and self._on_success is not None:
            self._on_success()
        return types.SimpleNamespace()


_MARKER_CONSTRAINT = {
    "name": "hivemind_schema_migration_id_unique",
    "type": "UNIQUENESS",
    "entityType": "NODE",
    "labelsOrTypes": ["HivemindSchemaMigration"],
    "properties": ["id"],
}
_DOCUMENT_CONSTRAINT = {
    "name": "document_source_path_unique",
    "type": "UNIQUENESS",
    "entityType": "NODE",
    "labelsOrTypes": ["Document"],
    "properties": ["memory_id", "source_path"],
}


class _GraphSchemaState:
    def __init__(
        self,
        *,
        marker_version: int | None = None,
        marker_constraint: dict | None = None,
        document_constraint: dict | None = None,
    ) -> None:
        self.marker_constraint = copy.deepcopy(marker_constraint)
        self.marker_constraint_ready = self.marker_constraint == _MARKER_CONSTRAINT
        self.marker_version = marker_version
        self.document_constraint = copy.deepcopy(document_constraint)
        self.document_constraint_ready = (
            self.document_constraint == _DOCUMENT_CONSTRAINT
        )


class _GraphSession:
    def __init__(
        self,
        rows: list[dict],
        *,
        constraint_failures: int = 0,
        normalization_failures: int = 0,
        deferred_constraint_failures: int = 0,
        ambiguous_constraint_failures: int = 0,
        deferred_normalization_failures: int = 0,
        deferred_lookup_failures: int = 0,
        deferred_fulltext_failures: int = 0,
        deferred_marker_constraint_failures: int = 0,
        deferred_marker_catalog_failures: int = 0,
        deferred_document_catalog_failures: int = 0,
        deferred_marker_read_failures: int = 0,
        deferred_marker_write_failures: int = 0,
        ambiguous_marker_write_failures: int = 0,
        normalization_batches: list[int] | None = None,
        block_pattern: str | None = None,
        schema_state: _GraphSchemaState | None = None,
        marker_catalog_rows: list[dict] | None = None,
        document_catalog_rows: list[dict] | None = None,
    ) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict]] = []
        self.constraint_failures = constraint_failures
        self.normalization_failures = normalization_failures
        self.deferred_constraint_failures = deferred_constraint_failures
        self.ambiguous_constraint_failures = ambiguous_constraint_failures
        self.deferred_normalization_failures = deferred_normalization_failures
        self.deferred_lookup_failures = deferred_lookup_failures
        self.deferred_fulltext_failures = deferred_fulltext_failures
        self.deferred_marker_constraint_failures = (
            deferred_marker_constraint_failures
        )
        self.deferred_marker_catalog_failures = deferred_marker_catalog_failures
        self.deferred_document_catalog_failures = deferred_document_catalog_failures
        self.deferred_marker_read_failures = deferred_marker_read_failures
        self.deferred_marker_write_failures = deferred_marker_write_failures
        self.ambiguous_marker_write_failures = ambiguous_marker_write_failures
        self.normalization_batches = list(normalization_batches or [0])
        self.consumed_queries: list[str] = []
        self.schema_state = schema_state or _GraphSchemaState()
        self.marker_catalog_rows = copy.deepcopy(marker_catalog_rows)
        self.document_catalog_rows = copy.deepcopy(document_catalog_rows)
        self.block_pattern = block_pattern
        self.block_entered = asyncio.Event()
        self.block_release = asyncio.Event()

    async def run(self, query: str, **params):
        self.calls.append((query, dict(params)))
        if self.block_pattern is not None and self.block_pattern in query:
            self.block_entered.set()
            await self.block_release.wait()
        if (
            "SET d.source_path = null" in query
            and self.normalization_failures
        ):
            self.normalization_failures -= 1
            raise RuntimeError("planted normalization backend detail")
        if (
            "CREATE CONSTRAINT document_source_path_unique" in query
            and self.constraint_failures
        ):
            self.constraint_failures -= 1
            raise RuntimeError("planted legacy duplicate detail")
        deferred_failure = None
        success_before_failure = False
        on_success = None
        rows = self.rows
        if "CREATE INDEX document_memory_id_id" in query and self.deferred_lookup_failures:
            self.deferred_lookup_failures -= 1
            deferred_failure = "planted deferred lookup backend detail"
        elif "CREATE FULLTEXT INDEX" in query and self.deferred_fulltext_failures:
            self.deferred_fulltext_failures -= 1
            deferred_failure = "planted deferred fulltext backend detail"
        elif (
            "SET d.source_path = null" in query
            and self.deferred_normalization_failures
        ):
            self.deferred_normalization_failures -= 1
            deferred_failure = "planted deferred normalization backend detail"
        elif (
            "CREATE CONSTRAINT document_source_path_unique" in query
            and self.deferred_constraint_failures
        ):
            self.deferred_constraint_failures -= 1
            deferred_failure = "planted deferred legacy duplicate detail"
        elif (
            "CREATE CONSTRAINT document_source_path_unique" in query
            and self.ambiguous_constraint_failures
        ):
            self.ambiguous_constraint_failures -= 1
            deferred_failure = "planted ambiguous document constraint ack"
            success_before_failure = True
        elif (
            "CREATE CONSTRAINT hivemind_schema_migration_id_unique" in query
            and self.deferred_marker_constraint_failures
        ):
            self.deferred_marker_constraint_failures -= 1
            deferred_failure = "planted deferred marker constraint detail"
        elif (
            "SHOW CONSTRAINTS" in query
            and params.get("constraint_properties") == ["id"]
            and self.deferred_marker_catalog_failures
        ):
            self.deferred_marker_catalog_failures -= 1
            deferred_failure = "planted deferred marker catalog detail"
        elif (
            "SHOW CONSTRAINTS" in query
            and params.get("constraint_properties")
            == ["memory_id", "source_path"]
            and self.deferred_document_catalog_failures
        ):
            self.deferred_document_catalog_failures -= 1
            deferred_failure = "planted deferred document catalog detail"
        elif (
            "MATCH (m:HivemindSchemaMigration" in query
            and self.deferred_marker_read_failures
        ):
            self.deferred_marker_read_failures -= 1
            deferred_failure = "planted deferred marker read detail"
        elif (
            "MERGE (m:HivemindSchemaMigration" in query
            and self.deferred_marker_write_failures
        ):
            self.deferred_marker_write_failures -= 1
            deferred_failure = "planted deferred marker write detail"
        elif (
            "MERGE (m:HivemindSchemaMigration" in query
            and self.ambiguous_marker_write_failures
        ):
            self.ambiguous_marker_write_failures -= 1
            deferred_failure = "planted ambiguous marker write ack"
            success_before_failure = True
        if "RETURN count(d) AS normalized" in query:
            normalized = (
                self.normalization_batches.pop(0)
                if self.normalization_batches
                else 0
            )
            rows = [{"normalized": normalized}]
        elif "CREATE CONSTRAINT hivemind_schema_migration_id_unique" in query:
            def install_marker_constraint() -> None:
                if self.schema_state.marker_constraint is None:
                    self.schema_state.marker_constraint = copy.deepcopy(
                        _MARKER_CONSTRAINT
                    )
                self.schema_state.marker_constraint_ready = (
                    self.schema_state.marker_constraint == _MARKER_CONSTRAINT
                )

            on_success = install_marker_constraint
        elif "SHOW CONSTRAINTS" in query:
            catalog_override = None
            if params["constraint_properties"] == ["id"]:
                catalog_override = self.marker_catalog_rows
            elif params["constraint_properties"] == ["memory_id", "source_path"]:
                catalog_override = self.document_catalog_rows
            if catalog_override is not None:
                rows = catalog_override
                return _Rows(
                    rows,
                    deferred_failure=deferred_failure,
                    success_before_failure=success_before_failure,
                    on_consume=lambda: self.consumed_queries.append(query),
                )
            constraints = []
            for candidate in (
                self.schema_state.marker_constraint,
                self.schema_state.document_constraint,
            ):
                if (
                    candidate is not None
                    and candidate.get("entityType") == "NODE"
                    and candidate.get("type")
                    in {"UNIQUENESS", "NODE_PROPERTY_UNIQUENESS"}
                    and candidate.get("labelsOrTypes")
                    == params["labels_or_types"]
                    and candidate.get("properties")
                    == params["constraint_properties"]
                ):
                    constraints.append(candidate)
            rows = constraints[:2]
        elif "MATCH (m:HivemindSchemaMigration" in query:
            rows = (
                []
                if self.schema_state.marker_version is None
                else [
                    {
                        "marker": {
                            "id": params["migration_id"],
                            "version": self.schema_state.marker_version,
                        }
                    }
                ]
            )
        elif "CREATE CONSTRAINT document_source_path_unique" in query:
            def install_document_constraint() -> None:
                if self.schema_state.document_constraint is None:
                    self.schema_state.document_constraint = copy.deepcopy(
                        _DOCUMENT_CONSTRAINT
                    )
                self.schema_state.document_constraint_ready = (
                    self.schema_state.document_constraint == _DOCUMENT_CONSTRAINT
                )

            on_success = install_document_constraint
        elif "MERGE (m:HivemindSchemaMigration" in query:
            version = (
                params["migration_version"]
                if self.schema_state.marker_version is None
                else self.schema_state.marker_version
            )
            rows = [
                {
                    "migration_id": params["migration_id"],
                    "version": version,
                }
            ]

            def publish_marker() -> None:
                if self.schema_state.marker_version is None:
                    self.schema_state.marker_version = params["migration_version"]

            on_success = publish_marker
        return _Rows(
            rows,
            deferred_failure=deferred_failure,
            success_before_failure=success_before_failure,
            on_consume=lambda: self.consumed_queries.append(query),
            on_success=on_success,
        )


class _SessionContext:
    def __init__(self, session: _GraphSession) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


async def test_graph_session_cancellation_uses_driver_cancel_without_waiting_for_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    entered = asyncio.Event()
    never = asyncio.Event()

    class _CancelAwareSession:
        def __init__(self) -> None:
            self.cancelled = False
            self.close_started = False

        def cancel(self) -> None:
            self.cancelled = True

        async def close(self) -> None:
            self.close_started = True
            await never.wait()

    session = _CancelAwareSession()
    graph = object.__new__(GraphService)
    graph._database = "neo4j"
    graph._driver = types.SimpleNamespace(
        session=lambda **_kwargs: session,
    )

    async def use_session() -> None:
        async with graph.session():
            entered.set()
            await never.wait()

    task = asyncio.create_task(use_session())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    try:
        assert session.cancelled is True
        assert session.close_started is False
    finally:
        never.set()
        with pytest.raises(asyncio.CancelledError):
            await task


def _graph_service_class(monkeypatch: pytest.MonkeyPatch):
    from tests.fakes.inference_fakes import apply_graph_memory_baseline_env

    apply_graph_memory_baseline_env(monkeypatch)
    neo4j = types.ModuleType("neo4j")
    neo4j.AsyncGraphDatabase = object
    neo4j.AsyncDriver = object
    neo4j.AsyncSession = object

    class _FakeQuery(str):
        def __new__(cls, text: str, *, timeout: float | None = None):
            value = str.__new__(cls, text)
            value.timeout = timeout
            return value

    neo4j.Query = _FakeQuery
    neo4j_exceptions = types.ModuleType("neo4j.exceptions")
    neo4j_exceptions.ServiceUnavailable = type(
        "ServiceUnavailable", (Exception,), {}
    )
    neo4j_exceptions.AuthError = type("AuthError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "neo4j", neo4j)
    monkeypatch.setitem(sys.modules, "neo4j.exceptions", neo4j_exceptions)

    from mcp_memory.core.graph import GraphService

    return GraphService


async def test_graph_inventory_preserves_exact_native_fields_without_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)

    row = {
        "memory_id": _MEMORY_ID,
        "document_id": "doc-a",
        "filename": "source.txt",
        "uri": "s3://source-bucket/source",
        "sha256": "a" * 64,
        "size_bytes": 21,
        "status": None,
        "chunk_count": None,
    }
    session = _GraphSession([row])
    graph = object.__new__(GraphService)
    graph.session = lambda: _SessionContext(session)

    result = await graph.list_reindex_documents(_MEMORY_ID)

    assert result == [row]
    assert len(session.calls) == 1
    query, params = session.calls[0]
    assert "MATCH (d:Document {memory_id: $memory_id})" in query
    assert "ORDER BY" not in query
    assert "LIMIT $limit" in query
    assert params == {"memory_id": _MEMORY_ID, "limit": 10_001}


async def test_graph_inventory_fails_closed_if_driver_exceeds_query_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.fakes.inference_fakes import apply_graph_memory_baseline_env

    apply_graph_memory_baseline_env(monkeypatch)
    from mcp_memory.core import graph as graph_module
    from mcp_memory.core.maintenance import ReindexSourceLimitExceeded

    monkeypatch.setattr(graph_module, "MAX_REINDEX_SOURCE_DOCUMENTS", 1)
    row = {
        "memory_id": _MEMORY_ID,
        "document_id": "doc-a",
        "filename": "source.txt",
        "uri": "s3://source-bucket/source",
        "sha256": "a" * 64,
        "size_bytes": 21,
        "status": None,
        "chunk_count": None,
    }
    # LIMIT is MAX+1, so the local adapter must reject that first excess row;
    # requiring MAX+2 would make the guard unreachable for a compliant driver.
    session = _GraphSession([row, row])
    graph = object.__new__(graph_module.GraphService)
    graph.session = lambda: _SessionContext(session)

    with pytest.raises(ReindexSourceLimitExceeded):
        await graph.list_reindex_documents(_MEMORY_ID)

    _query, params = session.calls[0]
    assert params == {"memory_id": _MEMORY_ID, "limit": 2}


async def test_graph_reindex_ontology_uri_is_read_from_exact_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)

    ontology_uri = f"s3://source-bucket/{_MEMORY_ID}/documents/ontology.yaml"
    session = _GraphSession([{"ontology_uri": ontology_uri}])
    graph = object.__new__(GraphService)
    graph.session = lambda: _SessionContext(session)

    result = await graph.get_reindex_ontology_uri(_MEMORY_ID)

    assert result == ontology_uri
    query, params = session.calls[0]
    assert "MATCH (m:Memory {id: $memory_id})" in query
    assert params == {"memory_id": _MEMORY_ID}


async def test_constraint_migration_is_global_before_global_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)

    session = _GraphSession([])
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    await graph.initialize_document_schema()

    queries = [(" ".join(query.split()), params) for query, params in session.calls]
    assert queries == [
        (
            "CREATE CONSTRAINT hivemind_schema_migration_id_unique "
            "IF NOT EXISTS FOR (m:HivemindSchemaMigration) "
            "REQUIRE m.id IS UNIQUE",
            {},
        ),
        (
            "SHOW CONSTRAINTS YIELD name, type, entityType, labelsOrTypes, "
            "properties WHERE entityType = 'NODE' AND type IN "
            "['UNIQUENESS', 'NODE_PROPERTY_UNIQUENESS'] AND labelsOrTypes = "
            "$labels_or_types AND properties = $constraint_properties "
            "RETURN name, type, entityType, labelsOrTypes, properties LIMIT 2",
            {
                "labels_or_types": ["HivemindSchemaMigration"],
                "constraint_properties": ["id"],
            },
        ),
        (
            "MATCH (m:HivemindSchemaMigration {id: $migration_id}) "
            "RETURN properties(m) AS marker LIMIT 2",
            {"migration_id": "document-source-path-empty-to-null-v1"},
        ),
        (
            "MATCH (d:Document) WHERE d.source_path = '' "
            "WITH d LIMIT $batch_size SET d.source_path = null "
            "RETURN count(d) AS normalized",
            {"batch_size": 1000},
        ),
        (
            "CREATE CONSTRAINT document_source_path_unique IF NOT EXISTS "
            "FOR (d:Document) REQUIRE (d.memory_id, d.source_path) IS UNIQUE",
            {},
        ),
        (
            "SHOW CONSTRAINTS YIELD name, type, entityType, labelsOrTypes, "
            "properties WHERE entityType = 'NODE' AND type IN "
            "['UNIQUENESS', 'NODE_PROPERTY_UNIQUENESS'] AND labelsOrTypes = "
            "$labels_or_types AND properties = $constraint_properties "
            "RETURN name, type, entityType, labelsOrTypes, properties LIMIT 2",
            {
                "labels_or_types": ["Document"],
                "constraint_properties": ["memory_id", "source_path"],
            },
        ),
        (
            "MERGE (m:HivemindSchemaMigration {id: $migration_id}) "
            "ON CREATE SET m.version = $migration_version "
            "RETURN m.id AS migration_id, m.version AS version",
            {
                "migration_id": "document-source-path-empty-to-null-v1",
                "migration_version": 1,
            },
        ),
    ]
    assert graph._doc_source_normalization_done is True
    assert graph._doc_constraints_ready is True
    assert graph._doc_migration_marker_ready is True
    assert session.schema_state.marker_version == 1
    assert len(session.consumed_queries) == 7
    assert all(getattr(query, "timeout", None) == 30 for query, _ in session.calls)


async def test_constraint_deferred_commit_failure_never_publishes_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model Neo4j returning RUN success before PULL/commit reports duplicates."""

    GraphService = _graph_service_class(monkeypatch)
    session = _GraphSession([], deferred_constraint_failures=1)
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    with pytest.raises(RuntimeError, match="document schema initialization"):
        await graph.initialize_document_schema()

    assert graph._doc_source_normalization_done is True
    assert graph._doc_constraints_ready is False
    assert graph._doc_migration_marker_ready is False
    await graph.initialize_document_schema()
    assert graph._doc_constraints_ready is True
    assert graph._doc_migration_marker_ready is True
    assert sum(
        "CREATE CONSTRAINT document_source_path_unique" in query
        for query in session.consumed_queries
    ) == 2


async def test_normalization_deferred_commit_failure_never_publishes_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swallowed session-close error must be observed by result consumption."""

    GraphService = _graph_service_class(monkeypatch)
    session = _GraphSession([], deferred_normalization_failures=1)
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    with pytest.raises(RuntimeError, match="document schema initialization"):
        await graph.initialize_document_schema()

    assert graph._doc_source_normalization_done is False
    assert graph._doc_constraints_ready is False
    assert graph._doc_migration_marker_ready is False
    await graph.initialize_document_schema()
    assert graph._doc_source_normalization_done is True
    assert graph._doc_constraints_ready is True
    assert graph._doc_migration_marker_ready is True


async def test_normalization_uses_consumed_bounded_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    session = _GraphSession([], normalization_batches=[1000, 3, 0])
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    await graph.initialize_document_schema()

    normalization_calls = [
        (query, params)
        for query, params in session.calls
        if "SET d.source_path = null" in query
    ]
    assert len(normalization_calls) == 3
    assert all("LIMIT $batch_size" in query for query, _params in normalization_calls)
    assert all(params == {"batch_size": 1000} for _query, params in normalization_calls)
    assert all(
        query in session.consumed_queries for query, _params in normalization_calls
    )


async def test_durable_migration_marker_skips_the_global_scan_in_a_new_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    schema_state = _GraphSchemaState()
    first_session = _GraphSession(
        [],
        normalization_batches=[2, 0],
        schema_state=schema_state,
    )
    first = object.__new__(GraphService)
    first._doc_constraints_ready = False
    first._doc_source_normalization_done = False
    first._doc_migration_marker_ready = False
    first._doc_constraints_lock = asyncio.Lock()
    first.session = lambda: _SessionContext(first_session)

    await first.initialize_document_schema()

    assert schema_state.marker_constraint_ready is True
    assert schema_state.document_constraint_ready is True
    assert schema_state.marker_version == 1

    second_session = _GraphSession(
        [],
        normalization_batches=[999],
        schema_state=schema_state,
    )
    second = object.__new__(GraphService)
    second._doc_constraints_ready = False
    second._doc_source_normalization_done = False
    second._doc_migration_marker_ready = False
    second._doc_constraints_lock = asyncio.Lock()
    second.session = lambda: _SessionContext(second_session)

    await second.initialize_document_schema()

    assert not any(
        "SET d.source_path = null" in query for query, _params in second_session.calls
    )
    assert second.document_schema_status() == {"status": "ok", "ready": True}


async def test_durable_marker_never_hides_a_missing_document_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    schema_state = _GraphSchemaState(marker_version=1)
    session = _GraphSession(
        [],
        deferred_constraint_failures=1,
        normalization_batches=[999],
        schema_state=schema_state,
    )
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_migration_marker_ready = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    with pytest.raises(RuntimeError, match="document schema initialization"):
        await graph.initialize_document_schema()

    assert not any(
        "SET d.source_path = null" in query for query, _params in session.calls
    )
    assert graph._doc_source_normalization_done is True
    assert graph._doc_migration_marker_ready is True
    assert graph._doc_constraints_ready is False
    assert graph.document_schema_status() == {"status": "error", "ready": False}


async def test_invalid_durable_migration_marker_fails_closed_before_data_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    session = _GraphSession(
        [],
        normalization_batches=[999],
        schema_state=_GraphSchemaState(marker_version=2),
    )
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_migration_marker_ready = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    with pytest.raises(RuntimeError, match="document schema initialization"):
        await graph.initialize_document_schema()

    assert not any(
        "SET d.source_path = null" in query for query, _params in session.calls
    )
    assert graph.document_schema_status() == {"status": "error", "ready": False}


async def test_homonymous_marker_constraint_cannot_publish_schema_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    wrong_constraint = copy.deepcopy(_MARKER_CONSTRAINT)
    wrong_constraint["labelsOrTypes"] = ["UnrelatedMigration"]
    session = _GraphSession(
        [],
        normalization_batches=[999],
        schema_state=_GraphSchemaState(marker_constraint=wrong_constraint),
    )
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_migration_marker_ready = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    with pytest.raises(RuntimeError, match="document schema initialization"):
        await graph.initialize_document_schema()

    assert not any(
        "SET d.source_path = null" in query for query, _params in session.calls
    )
    assert session.schema_state.marker_constraint_ready is False
    assert graph.document_schema_status() == {"status": "error", "ready": False}


async def test_homonymous_document_constraint_cannot_publish_durable_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    wrong_constraint = copy.deepcopy(_DOCUMENT_CONSTRAINT)
    wrong_constraint["properties"] = ["memory_id", "id"]
    state = _GraphSchemaState(document_constraint=wrong_constraint)
    session = _GraphSession([], schema_state=state)
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_migration_marker_ready = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    with pytest.raises(RuntimeError, match="document schema initialization"):
        await graph.initialize_document_schema()

    assert graph._doc_source_normalization_done is True
    assert graph._doc_constraints_ready is False
    assert graph._doc_migration_marker_ready is False
    assert state.marker_version is None
    assert not any(
        "MERGE (m:HivemindSchemaMigration" in query
        for query, _params in session.calls
    )


@pytest.mark.parametrize(
    ("catalog_argument", "catalog_rows", "normalization_completed"),
    (
        (
            "marker_catalog_rows",
            [
                {
                    **_MARKER_CONSTRAINT,
                    "labelsOrTypes": "HivemindSchemaMigration",
                }
            ],
            False,
        ),
        (
            "document_catalog_rows",
            [_DOCUMENT_CONSTRAINT, _DOCUMENT_CONSTRAINT],
            True,
        ),
    ),
    ids=("malformed-marker-catalog-row", "duplicate-document-catalog-rows"),
)
async def test_malformed_or_duplicate_constraint_catalog_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    catalog_argument: str,
    catalog_rows: list[dict],
    normalization_completed: bool,
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    session = _GraphSession([], **{catalog_argument: catalog_rows})
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_migration_marker_ready = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    with pytest.raises(RuntimeError, match="document schema initialization"):
        await graph.initialize_document_schema()

    assert graph._doc_source_normalization_done is normalization_completed
    assert graph._doc_constraints_ready is False
    assert graph._doc_migration_marker_ready is False
    assert session.schema_state.marker_version is None


async def test_exact_legacy_named_constraints_remain_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    marker_constraint = copy.deepcopy(_MARKER_CONSTRAINT)
    marker_constraint["name"] = "legacy_marker_uniqueness"
    document_constraint = copy.deepcopy(_DOCUMENT_CONSTRAINT)
    document_constraint["name"] = "legacy_document_uniqueness"
    state = _GraphSchemaState(
        marker_constraint=marker_constraint,
        document_constraint=document_constraint,
    )
    session = _GraphSession([], schema_state=state)
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_migration_marker_ready = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    await graph.initialize_document_schema()

    assert graph.document_schema_status() == {"status": "ok", "ready": True}
    assert state.marker_version == 1


@pytest.mark.parametrize(
    ("failure_count", "normalization_completed"),
    (
        ("deferred_marker_catalog_failures", False),
        ("deferred_document_catalog_failures", True),
    ),
)
async def test_deferred_constraint_catalog_failure_never_publishes_readiness(
    monkeypatch: pytest.MonkeyPatch,
    failure_count: str,
    normalization_completed: bool,
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    session = _GraphSession([], **{failure_count: 1})
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_migration_marker_ready = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    with pytest.raises(RuntimeError, match="document schema initialization"):
        await graph.initialize_document_schema()

    assert graph._doc_source_normalization_done is normalization_completed
    assert graph._doc_constraints_ready is False
    assert graph._doc_migration_marker_ready is False
    assert session.schema_state.marker_version is None


@pytest.mark.parametrize(
    "failure_count",
    (
        "deferred_marker_constraint_failures",
        "deferred_marker_read_failures",
    ),
)
async def test_deferred_marker_setup_failure_blocks_before_data_scan(
    monkeypatch: pytest.MonkeyPatch,
    failure_count: str,
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    session = _GraphSession([], **{failure_count: 1})
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_migration_marker_ready = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    with pytest.raises(RuntimeError, match="document schema initialization"):
        await graph.initialize_document_schema()

    assert not any(
        "SET d.source_path = null" in query for query, _params in session.calls
    )
    assert graph.document_schema_status() == {"status": "error", "ready": False}


async def test_deferred_marker_write_failure_never_publishes_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    session = _GraphSession([], deferred_marker_write_failures=1)
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_migration_marker_ready = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    with pytest.raises(RuntimeError, match="document schema initialization"):
        await graph.initialize_document_schema()

    assert graph._doc_source_normalization_done is True
    assert graph._doc_constraints_ready is True
    assert graph._doc_migration_marker_ready is False
    assert session.schema_state.marker_version is None

    await graph.initialize_document_schema()

    assert sum(
        "SET d.source_path = null" in query for query, _params in session.calls
    ) == 1
    assert graph.document_schema_status() == {"status": "ok", "ready": True}


async def test_committed_constraint_with_lost_ack_reconciles_in_same_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    session = _GraphSession([], ambiguous_constraint_failures=1)
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_migration_marker_ready = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    with pytest.raises(RuntimeError, match="document schema initialization"):
        await graph.initialize_document_schema()

    assert session.schema_state.document_constraint == _DOCUMENT_CONSTRAINT
    assert session.schema_state.marker_version is None
    assert graph._doc_source_normalization_done is True
    assert graph._doc_constraints_ready is False
    assert graph._doc_migration_marker_ready is False
    assert graph.document_schema_status() == {"status": "error", "ready": False}
    assert sum(
        "SET d.source_path = null" in query for query, _params in session.calls
    ) == 1

    await graph.initialize_document_schema()

    assert sum(
        "SET d.source_path = null" in query for query, _params in session.calls
    ) == 1
    assert graph.document_schema_status() == {"status": "ok", "ready": True}
    assert session.schema_state.marker_version == 1


async def test_committed_marker_with_lost_ack_reconciles_after_process_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    state = _GraphSchemaState()
    first_session = _GraphSession(
        [],
        ambiguous_marker_write_failures=1,
        schema_state=state,
    )
    first = object.__new__(GraphService)
    first._doc_constraints_ready = False
    first._doc_source_normalization_done = False
    first._doc_migration_marker_ready = False
    first._doc_constraints_lock = asyncio.Lock()
    first.session = lambda: _SessionContext(first_session)

    with pytest.raises(RuntimeError, match="document schema initialization"):
        await first.initialize_document_schema()

    assert state.document_constraint == _DOCUMENT_CONSTRAINT
    assert state.marker_version == 1
    assert first._doc_constraints_ready is True
    assert first._doc_migration_marker_ready is False
    assert first.document_schema_status() == {"status": "error", "ready": False}

    second_session = _GraphSession(
        [],
        normalization_batches=[999],
        schema_state=state,
    )
    second = object.__new__(GraphService)
    second._doc_constraints_ready = False
    second._doc_source_normalization_done = False
    second._doc_migration_marker_ready = False
    second._doc_constraints_lock = asyncio.Lock()
    second.session = lambda: _SessionContext(second_session)

    await second.initialize_document_schema()

    assert not any(
        "SET d.source_path = null" in query
        for query, _params in second_session.calls
    )
    assert not any(
        "MERGE (m:HivemindSchemaMigration" in query
        for query, _params in second_session.calls
    )
    assert second.document_schema_status() == {"status": "ok", "ready": True}
    assert state.marker_version == 1


async def test_graph_backup_import_cannot_reintroduce_empty_source_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    session = _GraphSession([])
    graph = object.__new__(GraphService)
    graph.session = lambda: _SessionContext(session)

    async def missing_memory(_memory_id: str):
        return None

    graph.get_memory = missing_memory
    data = {
        "memory": {"id": _MEMORY_ID},
        "documents": [
            {"id": "legacy-empty", "source_path": ""},
            {"id": "normalized", "source_path": " /repo/source.md "},
        ],
    }

    result = await GraphService.import_memory_data.__wrapped__(graph, data)

    document_params = [
        params
        for query, params in session.calls
        if "CREATE (d:Document" in query
    ]
    assert result["documents"] == 2
    assert [params["source_path"] for params in document_params] == [
        None,
        "repo/source.md",
    ]


async def test_best_effort_index_deferred_failures_are_consumed_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    session = _GraphSession([], deferred_lookup_failures=1)
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    await graph.ensure_document_lookup_index()
    await graph.initialize_document_schema()

    stderr = capsys.readouterr().err
    assert graph._doc_constraints_ready is True
    assert any("CREATE INDEX document_memory_id_id" in query for query in session.consumed_queries)
    assert "Index (memory_id, id) non créé" in stderr
    assert "planted deferred lookup backend detail" not in stderr


async def test_best_effort_lookup_timeout_preserves_the_critical_schema_budget(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    session = _GraphSession([], block_pattern="CREATE INDEX document_memory_id_id")
    graph = object.__new__(GraphService)
    graph._schema_query_timeout_seconds = 1
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_migration_marker_ready = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    async with asyncio.timeout(0.5):
        await graph.ensure_document_lookup_index()
        await graph.initialize_document_schema()

    assert "Index (memory_id, id) non créé" in capsys.readouterr().err
    assert graph.document_schema_status() == {"status": "ok", "ready": True}


async def test_fulltext_readiness_never_publishes_after_deferred_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    session = _GraphSession([], deferred_fulltext_failures=1)
    graph = object.__new__(GraphService)
    graph._fulltext_index_ready = False
    graph.session = lambda: _SessionContext(session)

    await graph.ensure_fulltext_index()

    stderr = capsys.readouterr().err
    assert graph._fulltext_index_ready is False
    assert getattr(session.calls[0][0], "timeout", None) == 30
    assert "Impossible de créer l'index fulltext" in stderr
    assert "planted deferred fulltext backend detail" not in stderr


def test_graph_has_no_unconsumed_fire_and_forget_session_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    module = sys.modules[GraphService.__module__]
    source = inspect.getsource(module)
    assert re.search(r"(?m)^\s*await session\.run\(", source) is None


def test_document_schema_timeout_configuration_must_be_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _graph_service_class(monkeypatch)
    from mcp_memory.config import Settings

    with pytest.raises(ValueError, match="NEO4J_QUERY_TIMEOUT_SECONDS must be positive"):
        Settings(neo4j_query_timeout_seconds=0)
    assert Settings(neo4j_query_timeout_seconds=1).neo4j_query_timeout_seconds == 1


def test_process_admission_health_snapshots_are_value_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    graph = object.__new__(GraphService)
    graph._doc_source_normalization_done = False
    graph._doc_constraints_ready = False
    graph._doc_migration_marker_ready = False
    assert graph.document_schema_status() == {"status": "error", "ready": False}
    graph._doc_source_normalization_done = True
    graph._doc_constraints_ready = True
    assert graph.document_schema_status() == {"status": "error", "ready": False}
    graph._doc_migration_marker_ready = True
    assert graph.document_schema_status() == {"status": "ok", "ready": True}

    from mcp_memory.core.maintenance import MaintenanceCoordinator

    coordinator = MaintenanceCoordinator()
    assert coordinator.health_status() == {
        "status": "ok",
        "admissions_available": True,
    }
    coordinator._corrupted = True
    assert coordinator.health_status() == {
        "status": "error",
        "admissions_available": False,
    }


async def test_constraint_initialization_is_single_flight_across_namespaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)

    session = _GraphSession([], block_pattern="SET d.source_path = null")
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    startup = asyncio.create_task(graph.initialize_document_schema())
    await session.block_entered.wait()
    admission = asyncio.create_task(graph.ensure_document_constraints())
    await asyncio.sleep(0)

    assert admission.done() is False
    assert sum(
        "SET d.source_path = null" in query for query, _params in session.calls
    ) == 1
    assert not any(
        "CREATE CONSTRAINT document_source_path_unique" in query
        for query, _params in session.calls
    )

    session.block_release.set()
    await asyncio.gather(startup, admission)

    queries = [query for query, _params in session.calls]
    assert sum("SET d.source_path = null" in query for query in queries) == 1
    assert sum(
        "CREATE CONSTRAINT document_source_path_unique" in query
        for query in queries
    ) == 1
    assert graph._doc_constraints_ready is True


async def test_constraint_failure_remains_not_ready_and_retries_full_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)

    session = _GraphSession([], constraint_failures=1)
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    with pytest.raises(RuntimeError, match="document schema initialization"):
        await graph.initialize_document_schema()
    assert graph._doc_source_normalization_done is True
    assert graph._doc_constraints_ready is False
    assert graph._doc_migration_marker_ready is False

    await graph.initialize_document_schema()
    assert graph._doc_constraints_ready is True
    assert graph._doc_migration_marker_ready is True

    queries = [query for query, _params in session.calls]
    assert sum("SET d.source_path = null" in query for query in queries) == 1
    assert sum(
        "CREATE CONSTRAINT document_source_path_unique" in query
        for query in queries
    ) == 2


async def test_normalization_failure_blocks_schema_and_can_retry_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)

    session = _GraphSession([], normalization_failures=1)
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    with pytest.raises(RuntimeError, match="document schema initialization"):
        await graph.initialize_document_schema()
    assert graph._doc_source_normalization_done is False
    assert graph._doc_constraints_ready is False
    assert graph._doc_migration_marker_ready is False

    await graph.initialize_document_schema()
    assert graph._doc_source_normalization_done is True
    assert graph._doc_constraints_ready is True
    assert graph._doc_migration_marker_ready is True
    queries = [query for query, _params in session.calls]
    assert sum("SET d.source_path = null" in query for query in queries) == 2
    assert sum(
        "CREATE CONSTRAINT document_source_path_unique" in query
        for query in queries
    ) == 1


async def test_admission_constraint_retry_never_runs_the_global_data_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)

    session = _GraphSession([])
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = False
    graph._doc_source_normalization_done = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    with pytest.raises(RuntimeError, match="document schema initialization"):
        await graph.ensure_document_constraints()

    assert session.calls == []
    assert graph._doc_source_normalization_done is False
    assert graph._doc_constraints_ready is False


async def test_admission_cannot_bypass_an_unpublished_migration_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GraphService = _graph_service_class(monkeypatch)
    session = _GraphSession([])
    graph = object.__new__(GraphService)
    graph._doc_constraints_ready = True
    graph._doc_source_normalization_done = True
    graph._doc_migration_marker_ready = False
    graph._doc_constraints_lock = asyncio.Lock()
    graph.session = lambda: _SessionContext(session)

    with pytest.raises(RuntimeError, match="document schema initialization"):
        await graph.ensure_document_constraints()

    assert session.calls == []
    assert graph.document_schema_status() == {"status": "error", "ready": False}
