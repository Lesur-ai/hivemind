# -*- coding: utf-8 -*-
"""Focused local-Qdrant proof for bounded P13 maintenance reindex."""

from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import inspect
import json
import os
import textwrap
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import quote as url_quote
import uuid

import pytest
from qdrant_client import QdrantClient, models as qmodels

from hivemind_inference import (
    EmbeddingResult,
    build_embedding_collection_identity,
    canonical_qdrant_collection_name,
)
from mcp_memory.core.maintenance import (
    MaintenanceAdmissionError,
    MaintenanceCoordinatorCorrupted,
    get_maintenance_coordinator,
    reset_maintenance_coordinator_for_tests,
)
from mcp_memory.core.models import Chunk
from mcp_memory.core import reindex as reindex_module
from mcp_memory.core.reindex import ReindexService
from mcp_memory.core.vector_store import (
    EmbeddingCollectionReindexRequired,
    EmbeddingCollectionUnavailable,
    VectorStoreService,
)
from tests.fakes.inference_fakes import make_embedding_profile


_MEMORY_ID = "memory-a"
_OPERATION_ID = "a" * 32
_RESULT_FIELDS = {
    "status",
    "phase",
    "reason",
    "operation_id",
    "source_documents",
    "source_chunks",
    "vectors_written",
    "activated",
    "active_state",
}


@pytest.mark.parametrize("worker_fails", [False, True])
async def test_cancelled_activation_drains_worker_before_releasing_maintenance(
    worker_fails: bool,
    local_qdrant,
    monkeypatch: pytest.MonkeyPatch,
    profile,
) -> None:
    coordinator = get_maintenance_coordinator()
    store = VectorStoreService(
        client=local_qdrant,
        profile=profile,
        legacy_prefix="memory_",
    )
    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocking_activation(*_args, **_kwargs) -> int:
        worker_started.set()
        release_worker.wait(timeout=5)
        if worker_fails:
            raise RuntimeError("worker failed after cancellation")
        return 0

    monkeypatch.setattr(store, "_activate_reindex_shadow", blocking_activation)

    async def activate_under_maintenance() -> int:
        async with coordinator.maintenance(_MEMORY_ID):
            return await store.activate_reindex_shadow(
                _MEMORY_ID,
                _OPERATION_ID,
                identity=None,
                expected_chunks={},
                expected_target=None,
            )

    holder = asyncio.create_task(activate_under_maintenance())
    assert await asyncio.to_thread(worker_started.wait, 1)
    holder.cancel()
    await asyncio.sleep(0)
    holder.cancel()
    await asyncio.sleep(0)

    try:
        assert not holder.done()
        with pytest.raises(MaintenanceAdmissionError):
            async with coordinator.ordinary(_MEMORY_ID):
                pass
    finally:
        release_worker.set()

    with pytest.raises(asyncio.CancelledError):
        await holder

    async with coordinator.ordinary(_MEMORY_ID):
        pass


@pytest.fixture(autouse=True)
def _reset_maintenance_singleton():
    reset_maintenance_coordinator_for_tests()
    yield
    reset_maintenance_coordinator_for_tests()


@pytest.fixture
def profile():
    return make_embedding_profile(expected_dimensions=3)


@pytest.fixture
def local_qdrant(tmp_path):
    client = QdrantClient(path=str(tmp_path / "qdrant"))
    try:
        yield client
    finally:
        client.close()


def _embedding_result(texts: list[str], *, resolved_model: str = "resolved-v1"):
    vectors = []
    for text in texts:
        checksum = sum(text.encode("utf-8"))
        vectors.append((1.0, (checksum % 17 + 1) / 20.0, 0.25))
    return EmbeddingResult(
        vectors=vectors,
        configured_model="test-embedding-model",
        resolved_model=resolved_model,
        model_evidence="provider_reported",
        effective_dimensions=3,
    )


async def _invoke_namespace_read(
    store: VectorStoreService,
    profile,
    read_path: str,
) -> None:
    if read_path == "ensure_collection":
        await store.ensure_collection(_MEMORY_ID)
        return
    if read_path == "search":
        await store.search(
            _MEMORY_ID,
            embedding_result=_embedding_result(["query"]),
        )
        return
    if read_path == "count_document_chunks":
        await store.count_document_chunks(_MEMORY_ID, "doc-a")
        return
    if read_path == "list_doc_ids":
        await store.list_doc_ids(_MEMORY_ID)
        return
    if read_path == "preflight_import":
        identity = build_embedding_collection_identity(
            _MEMORY_ID,
            profile,
            _embedding_result(["source"]),
        )
        await store.preflight_import(
            _MEMORY_ID,
            identity.to_mapping(),
            [],
        )
        return
    if read_path == "export_collection":
        await store.export_collection(_MEMORY_ID)
        return
    if read_path == "get_collection_info":
        await store.get_collection_info(_MEMORY_ID)
        return
    raise AssertionError(f"unknown read path: {read_path}")


@pytest.mark.parametrize(
    "read_path",
    [
        "ensure_collection",
        "search",
        "count_document_chunks",
        "list_doc_ids",
        "preflight_import",
        "export_collection",
        "get_collection_info",
    ],
)
async def test_namespace_reads_wait_for_memory_lock_off_event_loop(
    read_path: str,
    monkeypatch: pytest.MonkeyPatch,
    profile,
) -> None:
    store = VectorStoreService(
        client=SimpleNamespace(),
        profile=profile,
        legacy_prefix="memory_",
    )
    worker_started = threading.Event()
    unrelated_resolver_reached = threading.Event()
    worker_observation: dict[str, bool] = {}

    def resolve(memory_id: str, *_args, **_kwargs):
        if memory_id == "memory-b":
            unrelated_resolver_reached.set()
        return None

    monkeypatch.setattr(store, "_resolve_collection", resolve)

    def blocking_inspection(memory_id: str):
        with store._memory_lock(memory_id):
            worker_started.set()
            worker_observation["unrelated_resolver_reached"] = (
                unrelated_resolver_reached.wait(timeout=5)
            )
            return ({"state": "missing"}, None)

    monkeypatch.setattr(store, "_inspect_reindex_state", blocking_inspection)
    maintenance_worker = asyncio.create_task(
        store.inspect_reindex_state(_MEMORY_ID)
    )
    assert await asyncio.to_thread(worker_started.wait, 1)

    blocked_read = asyncio.create_task(
        _invoke_namespace_read(store, profile, read_path)
    )
    unrelated_read = asyncio.create_task(store.get_collection_info("memory-b"))

    try:
        await asyncio.gather(blocked_read, unrelated_read)
    finally:
        unrelated_resolver_reached.set()
        await maintenance_worker

    assert worker_observation == {"unrelated_resolver_reached": True}


def test_every_locking_vector_entrypoint_uses_the_async_front_gate() -> None:
    required_existing = {
        "ensure_collection",
        "delete_collection",
        "store_chunks",
        "search",
        "delete_document_chunks",
        "count_document_chunks",
        "list_doc_ids",
        "preflight_import",
        "export_collection",
        "import_collection",
        "inspect_reindex_state",
        "create_reindex_shadow",
        "store_reindex_chunks",
        "validate_reindex_shadow",
        "activate_reindex_shadow",
        "get_collection_info",
    }
    mutation_entrypoints = {
        "delete_collection",
        "store_chunks",
        "delete_document_chunks",
        "import_collection",
        "create_reindex_shadow",
        "store_reindex_chunks",
        "activate_reindex_shadow",
    }
    tree = ast.parse(textwrap.dedent(inspect.getsource(VectorStoreService)))
    class_node = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    methods = {
        node.name: node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    # Derive the front-gate obligation from the implementation call graph,
    # rather than only comparing against a frozen list. This catches a future
    # public async entrypoint that directly takes ``_memory_lock`` or delegates
    # to a synchronous helper that does, even when nobody remembered to extend
    # this test's historical inventory.
    references: dict[str, set[str]] = {}
    for name, method in methods.items():
        references[name] = {
            node.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr in methods
        }

    gated = set()
    for name, method in methods.items():
        if not isinstance(method, ast.AsyncFunctionDef):
            continue
        decorators = [
            decorator.id
            for decorator in method.decorator_list
            if isinstance(decorator, ast.Name)
        ]
        if "_guard_memory_operation" in decorators:
            gated.add(name)
        if name in mutation_entrypoints:
            assert decorators.index("_guard_namespace_mutation") < decorators.index(
                "_guard_memory_operation"
            )

    def has_unguarded_path_to_memory_lock(
        name: str,
        seen: frozenset[str] = frozenset(),
    ) -> bool:
        """Stop at a nested gate: an outer wrapper is already protected there."""

        if name == "_memory_lock":
            return True
        if name in seen:
            return False
        return any(
            reference == "_memory_lock"
            or (
                reference not in gated
                and has_unguarded_path_to_memory_lock(reference, seen | {name})
            )
            for reference in references[name]
        )

    unprotected_entrypoints = {
        name
        for name, method in methods.items()
        if isinstance(method, ast.AsyncFunctionDef)
        and not name.startswith("_")
        and name not in gated
        and has_unguarded_path_to_memory_lock(name)
    }

    assert required_existing <= gated
    assert not unprotected_entrypoints


def _source_bundle(memory_id: str = _MEMORY_ID):
    definitions = (
        (
            "doc-a",
            "alpha note.txt",
            b"fresh-alpha-0|fresh-alpha-1",
        ),
        (
            "doc-b",
            "béta.txt",
            b"fresh-beta-0|fresh-beta-1|fresh-beta-2|fresh-beta-3|"
            b"fresh-beta-4|fresh-beta-5",
        ),
    )
    documents = []
    objects = []
    content_by_key = {}
    for document_id, filename, content in definitions:
        digest = hashlib.sha256(content).hexdigest()
        key = f"{memory_id}/documents/{digest[:8]}_{filename}"
        uri = f"s3://test-bucket/{key}"
        chunk_count = len(content.decode("utf-8").split("|"))
        documents.append(
            {
                "memory_id": memory_id,
                "document_id": document_id,
                "filename": filename,
                "uri": uri,
                "sha256": digest,
                "size_bytes": len(content),
                "status": "succeeded",
                "chunk_count": chunk_count,
            }
        )
        objects.append(
            {
                "key": key,
                "uri": uri,
                "size_bytes": len(content),
                "metadata": {
                    "memory_id": memory_id,
                    "original_filename": (
                        filename
                        if filename.isascii()
                        else url_quote(filename, safe="")
                    ),
                    "doc_hash": digest,
                },
            }
        )
        content_by_key[key] = content
    # Deliberately unordered: the service must create a deterministic snapshot.
    return list(reversed(documents)), list(reversed(objects)), content_by_key


def _add_ontology_config(objects: list[dict], content_by_key: dict[str, bytes]):
    content = b"name: general\n"
    digest = hashlib.sha256(content).hexdigest()
    filename = "_ontology_general.yaml"
    key = f"{_MEMORY_ID}/documents/{digest[:8]}_{filename}"
    objects.append(
        {
            "key": key,
            "uri": f"s3://test-bucket/{key}",
            "size_bytes": len(content),
            "metadata": {
                "memory_id": _MEMORY_ID,
                "original_filename": filename,
                "doc_hash": digest,
                "type": "ontology",
                "ontology_name": "general",
            },
        }
    )
    content_by_key[key] = content
    return key


class _FakeGraph:
    def __init__(
        self,
        snapshots,
        *,
        hooks=None,
        memory_id: str = _MEMORY_ID,
        ontology_uri: str | None = None,
    ):
        if snapshots and isinstance(snapshots[0], dict):
            snapshots = [snapshots]
        self.snapshots = [copy.deepcopy(snapshot) for snapshot in snapshots]
        self.hooks = dict(hooks or {})
        self.memory_id = memory_id
        self.ontology_uri = ontology_uri
        self.calls = 0

    async def list_reindex_documents(self, memory_id: str):
        assert memory_id == self.memory_id
        self.calls += 1
        hook = self.hooks.get(self.calls)
        if hook is not None:
            outcome = hook()
            if inspect.isawaitable(outcome):
                await outcome
        index = min(self.calls - 1, len(self.snapshots) - 1)
        return copy.deepcopy(self.snapshots[index])

    async def get_reindex_ontology_uri(self, memory_id: str):
        assert memory_id == self.memory_id
        return self.ontology_uri


class _FailingGraph:
    def __init__(self, message: str):
        self.message = message

    async def list_reindex_documents(self, memory_id: str):
        raise RuntimeError(self.message)


class _FakeStorage:
    def __init__(
        self,
        objects,
        content_by_key,
        *,
        memory_id: str = _MEMORY_ID,
    ):
        self.objects = copy.deepcopy(objects)
        self.content_by_key = dict(content_by_key)
        self.memory_id = memory_id
        self.list_calls = 0
        self.read_calls: list[str] = []

    async def list_reindex_objects(self, memory_id: str):
        assert memory_id == self.memory_id
        self.list_calls += 1
        return copy.deepcopy(self.objects)

    async def read_reindex_object(
        self,
        memory_id: str,
        key: str,
        expected_size: int,
    ):
        assert memory_id == self.memory_id
        assert expected_size == len(self.content_by_key[key])
        self.read_calls.append(key)
        return self.content_by_key[key]


class _FakeChunker:
    def __init__(self, *, signatures=((128, 16),)):
        self.signatures = tuple(signatures)
        self.signature_calls = 0
        self.inputs: list[tuple[str, str]] = []

    def configuration_signature(self):
        index = min(self.signature_calls, len(self.signatures) - 1)
        self.signature_calls += 1
        return self.signatures[index]

    def chunk_document(self, text: str, filename: str):
        self.inputs.append((text, filename))
        parts = text.split("|")
        return [
            Chunk(
                text=part,
                index=index,
                total_chunks=len(parts),
                filename=filename,
                char_count=len(part),
                token_estimate=max(1, len(part) // 4),
            )
            for index, part in enumerate(parts)
        ]


class _FakeEmbedder:
    def __init__(self, *, resolved_models=("resolved-v1",)):
        self.resolved_models = tuple(resolved_models)
        self.calls: list[list[str]] = []

    async def embed_texts_result(self, texts: list[str]):
        self.calls.append(list(texts))
        index = min(len(self.calls) - 1, len(self.resolved_models) - 1)
        return _embedding_result(
            texts,
            resolved_model=self.resolved_models[index],
        )


class _RecordingClient:
    """Transparent Qdrant client that records only state-changing calls."""

    def __init__(self, client):
        self._client = client
        self.mutations: list[tuple[str, str | None]] = []
        self.alias_batches: list[list[object]] = []

    def __getattr__(self, name):
        return getattr(self._client, name)

    @staticmethod
    def _collection(args, kwargs):
        if "collection_name" in kwargs:
            return kwargs["collection_name"]
        return args[0] if args else None

    def create_collection(self, *args, **kwargs):
        self.mutations.append(("create_collection", self._collection(args, kwargs)))
        return self._client.create_collection(*args, **kwargs)

    def create_payload_index(self, *args, **kwargs):
        self.mutations.append(
            ("create_payload_index", self._collection(args, kwargs))
        )
        return self._client.create_payload_index(*args, **kwargs)

    def upsert(self, *args, **kwargs):
        self.mutations.append(("upsert", self._collection(args, kwargs)))
        return self._client.upsert(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self.mutations.append(("delete", self._collection(args, kwargs)))
        return self._client.delete(*args, **kwargs)

    def delete_collection(self, *args, **kwargs):
        self.mutations.append(("delete_collection", self._collection(args, kwargs)))
        return self._client.delete_collection(*args, **kwargs)

    def update_collection_aliases(self, operations, *args, **kwargs):
        self.mutations.append(("update_collection_aliases", None))
        self.alias_batches.append(list(operations))
        return self._client.update_collection_aliases(operations, *args, **kwargs)


class _NoLegacyVectorReadClient(_RecordingClient):
    """Abort if reindex ever tries to export vectors from the old target."""

    def __init__(self, client, legacy_name: str):
        super().__init__(client)
        self._legacy_name = legacy_name

    def scroll(self, *args, **kwargs):
        name = self._collection(args, kwargs)
        if name == self._legacy_name:
            raise AssertionError("reindex must never scroll the legacy target")
        return self._client.scroll(*args, **kwargs)


class _InvalidAliasRepairClient(_RecordingClient):
    """Expose one corrupt alias record until its atomic replacement."""

    def __init__(self, client, alias_name: str, canonical_name: str):
        super().__init__(client)
        self._alias_name = alias_name
        self._canonical_name = canonical_name
        self._activated = False

    def get_aliases(self, *args, **kwargs):
        if not self._activated:
            return SimpleNamespace(
                aliases=[
                    SimpleNamespace(
                        alias_name=self._alias_name,
                        collection_name="",
                    )
                ]
            )
        return self._client.get_aliases(*args, **kwargs)

    def collection_exists(self, collection_name: str, *args, **kwargs):
        if not self._activated and collection_name == self._canonical_name:
            raise AssertionError("canonical fallback probed despite alias presence")
        return self._client.collection_exists(collection_name, *args, **kwargs)

    def update_collection_aliases(self, operations, *args, **kwargs):
        batch = list(operations)
        self.mutations.append(("update_collection_aliases", None))
        self.alias_batches.append(batch)
        assert len(batch) == 2
        assert isinstance(batch[0], qmodels.DeleteAliasOperation)
        assert isinstance(batch[1], qmodels.CreateAliasOperation)

        # Local Qdrant cannot contain the deliberately malformed alias record.
        # Apply only the create half while retaining the production batch above
        # as the assertion surface.
        result = self._client.update_collection_aliases(
            [batch[1]],
            *args,
            **kwargs,
        )
        self._activated = True
        return result


class _InvalidAliasRaceClient(_RecordingClient):
    """Change one invalid alias record only at the activation reread."""

    def __init__(self, client, alias_name: str):
        super().__init__(client)
        self._alias_name = alias_name
        self.alias_reads = 0

    def get_aliases(self, *args, **kwargs):
        del args, kwargs
        self.alias_reads += 1
        target = "" if self.alias_reads <= 2 else self._alias_name
        return SimpleNamespace(
            aliases=[
                SimpleNamespace(
                    alias_name=self._alias_name,
                    collection_name=target,
                )
            ]
        )


class _PostSwitchUnreadableClient(_RecordingClient):
    def __init__(self, client):
        super().__init__(client)
        self._fail_next_alias_read = False

    def update_collection_aliases(self, operations, *args, **kwargs):
        result = super().update_collection_aliases(operations, *args, **kwargs)
        self._fail_next_alias_read = True
        return result

    def get_aliases(self, *args, **kwargs):
        if self._fail_next_alias_read:
            self._fail_next_alias_read = False
            raise RuntimeError("post-switch secret must not escape")
        return self._client.get_aliases(*args, **kwargs)


class _AliasUpdateFailureClient(_RecordingClient):
    def __init__(self, client, *, raises: bool, commits_before_raise: bool = False):
        super().__init__(client)
        self._raises = raises
        self._commits_before_raise = commits_before_raise

    def update_collection_aliases(self, operations, *args, **kwargs):
        batch = list(operations)
        self.mutations.append(("update_collection_aliases", None))
        self.alias_batches.append(batch)
        if self._raises:
            if self._commits_before_raise:
                self._client.update_collection_aliases(batch, *args, **kwargs)
            raise RuntimeError("alias update outcome is private")
        return False


class _LegacyProbeBombClient:
    def __init__(self, client, legacy_name: str):
        self._client = client
        self._legacy_name = legacy_name

    def __getattr__(self, name):
        return getattr(self._client, name)

    def count(self, *args, **kwargs):
        name = kwargs.get("collection_name", args[0] if args else None)
        if name == self._legacy_name:
            raise AssertionError("legacy probe ran despite an active alias")
        return self._client.count(*args, **kwargs)


class _FalsePayloadIndexClient:
    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    def create_payload_index(self, *args, **kwargs):
        del args, kwargs
        return False


class _EmptyNonterminalScrollClient:
    def __init__(self, client, shadow_name: str):
        self._client = client
        self._shadow_name = shadow_name

    def __getattr__(self, name):
        return getattr(self._client, name)

    def scroll(self, *args, **kwargs):
        name = kwargs.get("collection_name", args[0] if args else None)
        if name == self._shadow_name:
            return [], "same-offset"
        return self._client.scroll(*args, **kwargs)


class _ProfileRaceStore(VectorStoreService):
    def __init__(self, *args, raced_profile, **kwargs):
        super().__init__(*args, **kwargs)
        self._raced_profile = raced_profile
        self._profile_reads = 0

    def reindex_profile(self):
        self._profile_reads += 1
        if self._profile_reads == 1:
            return super().reindex_profile()
        return self._raced_profile


class _TamperedScrollClient:
    def __init__(self, client, shadow_name: str, tamper):
        self._client = client
        self._shadow_name = shadow_name
        self._tamper = tamper

    def __getattr__(self, name):
        return getattr(self._client, name)

    def scroll(self, *args, **kwargs):
        points, next_offset = self._client.scroll(*args, **kwargs)
        name = kwargs.get("collection_name", args[0] if args else None)
        if name == self._shadow_name and kwargs.get("with_vectors") is True:
            points = [point.model_copy(deep=True) for point in points]
            if points:
                points[0] = self._tamper(points[0])
        return points, next_offset


def _seed_legacy(client, memory_id: str = _MEMORY_ID):
    name = "memory_" + "".join(
        character if character.isalnum() else "_" for character in memory_id
    )
    client.create_collection(
        collection_name=name,
        vectors_config=qmodels.VectorParams(
            size=3,
            distance=qmodels.Distance.COSINE,
        ),
    )
    client.upsert(
        collection_name=name,
        points=[
            qmodels.PointStruct(
                id=1,
                vector=[0.0, 1.0, 0.0],
                payload={
                    "memory_id": memory_id,
                    "doc_id": "old-doc",
                    "text": "OLD_VECTOR_ONLY_SECRET",
                },
            )
        ],
        wait=True,
    )
    return name


def _alias_map(client):
    return {
        alias.alias_name: alias.collection_name
        for alias in client.get_aliases().aliases
    }


def _shadow_names(client, store: VectorStoreService):
    prefix = f"{canonical_qdrant_collection_name(_MEMORY_ID)}__shadow_v1_"
    return sorted(
        collection.name
        for collection in client.get_collections().collections
        if collection.name.startswith(prefix)
    )


def _service(
    *,
    graph,
    storage,
    chunker,
    embedder,
    vectors,
    text_extractor=lambda content, _filename: content.decode("utf-8"),
    coordinator=None,
):
    return ReindexService(
        graph=graph,
        storage=storage,
        chunker=chunker,
        embedder=embedder,
        vectors=vectors,
        text_extractor=text_extractor,
        coordinator=(
            get_maintenance_coordinator()
            if coordinator is None
            else coordinator
        ),
    )


def _fix_operation_id(monkeypatch, operation_id: str = _OPERATION_ID):
    monkeypatch.setattr(
        reindex_module,
        "uuid4",
        lambda: SimpleNamespace(hex=operation_id),
    )


def _assert_no_active_alias(client, store: VectorStoreService):
    assert store._active_alias_name(_MEMORY_ID) not in _alias_map(client)


async def _create_complete_shadow(client, profile, operation_id="b" * 32):
    store = VectorStoreService(
        client=client,
        profile=profile,
        legacy_prefix="memory_",
    )
    texts = ["chunk-0", "chunk-1"]
    embedding = _embedding_result(texts)
    identity = await store.create_reindex_shadow(
        _MEMORY_ID,
        operation_id,
        embedding_result=embedding,
    )
    chunks = [
        Chunk(text=text, index=index, total_chunks=2)
        for index, text in enumerate(texts)
    ]
    assert await store.store_reindex_chunks(
        _MEMORY_ID,
        operation_id,
        "doc-a",
        "alpha.txt",
        chunks,
        embedding_result=embedding,
        identity=identity,
    ) == 2
    return store, identity, store._shadow_name(_MEMORY_ID, operation_id)


async def test_success_uses_verified_sources_and_alias_wins_after_restart(
    tmp_path,
    monkeypatch,
    profile,
) -> None:
    _fix_operation_id(monkeypatch)
    path = tmp_path / "restart"
    raw = QdrantClient(path=str(path))
    legacy_name = _seed_legacy(raw)
    tracker = _NoLegacyVectorReadClient(raw, legacy_name)
    store = VectorStoreService(
        client=tracker,
        profile=profile,
        legacy_prefix="memory_",
    )
    documents, objects, content_by_key = _source_bundle()
    graph = _FakeGraph(documents)
    storage = _FakeStorage(objects, content_by_key)
    chunker = _FakeChunker()
    embedder = _FakeEmbedder()
    service = _service(
        graph=graph,
        storage=storage,
        chunker=chunker,
        embedder=embedder,
        vectors=store,
    )

    result = await service.reindex(_MEMORY_ID)

    assert result == {
        "status": "ok",
        "phase": "verified",
        "reason": None,
        "operation_id": _OPERATION_ID,
        "source_documents": 2,
        "source_chunks": 8,
        "vectors_written": 8,
        "activated": True,
        "active_state": "ready",
    }
    assert graph.calls == 2
    assert storage.list_calls == 2
    assert sorted(storage.read_calls) == sorted(list(content_by_key) * 2)
    embedded_inputs = [text for batch in embedder.calls for text in batch]
    assert embedded_inputs == [
        "fresh-alpha-0",
        "fresh-alpha-1",
        "fresh-beta-0",
        "fresh-beta-1",
        "fresh-beta-2",
        "fresh-beta-3",
        "fresh-beta-4",
        "fresh-beta-5",
    ]
    assert all("OLD_VECTOR_ONLY_SECRET" not in text for text in embedded_inputs)

    shadow_name = store._shadow_name(_MEMORY_ID, _OPERATION_ID)
    alias_name = store._active_alias_name(_MEMORY_ID)
    assert _alias_map(raw) == {alias_name: shadow_name}
    assert raw.count(collection_name=shadow_name, exact=True).count == 8
    assert raw.collection_exists(legacy_name)
    assert raw.count(collection_name=legacy_name, exact=True).count == 1
    assert [name for name, _target in tracker.mutations].count(
        "update_collection_aliases"
    ) == 1
    assert tracker.mutations[-1] == ("update_collection_aliases", None)
    assert len(tracker.alias_batches) == 1
    assert len(tracker.alias_batches[0]) == 1

    serialized = json.dumps(result, sort_keys=True)
    assert profile.endpoint not in serialized
    assert legacy_name not in serialized
    assert shadow_name not in serialized
    assert "OLD_VECTOR_ONLY_SECRET" not in serialized

    raw.close()
    reopened = QdrantClient(path=str(path))
    try:
        restarted_store = VectorStoreService(
            client=_LegacyProbeBombClient(reopened, legacy_name),
            profile=profile,
            legacy_prefix="memory_",
        )
        expected_identity = build_embedding_collection_identity(
            _MEMORY_ID,
            profile,
            _embedding_result(["fingerprint-only"]),
        )
        assert await restarted_store.get_collection_info(_MEMORY_ID) == {
            "state": "ready",
            "profile_fingerprint": expected_identity.profile_fingerprint,
            "points_count": 8,
        }
        assert await restarted_store.list_doc_ids(_MEMORY_ID) == {"doc-a", "doc-b"}
        assert _alias_map(reopened) == {alias_name: shadow_name}
        assert reopened.collection_exists(legacy_name)
        assert reopened.count(collection_name=legacy_name, exact=True).count == 1
    finally:
        reopened.close()


async def test_delete_collection_refuses_active_alias_without_cleanup(
    local_qdrant,
    profile,
) -> None:
    legacy_name = _seed_legacy(local_qdrant)
    store, identity, shadow_name = await _create_complete_shadow(
        local_qdrant,
        profile,
    )
    _state, target = await store.inspect_reindex_state(_MEMORY_ID)
    assert await store.activate_reindex_shadow(
        _MEMORY_ID,
        "b" * 32,
        identity=identity,
        expected_chunks={"doc-a": 2},
        expected_target=target,
    ) == 2
    alias_name = store._active_alias_name(_MEMORY_ID)
    before = sorted(
        collection.name
        for collection in local_qdrant.get_collections().collections
    )

    with pytest.raises(EmbeddingCollectionUnavailable) as refusal:
        await store.delete_collection(_MEMORY_ID)

    assert refusal.value.reason == "active_alias_delete_unsupported"
    assert _alias_map(local_qdrant) == {alias_name: shadow_name}
    assert sorted(
        collection.name
        for collection in local_qdrant.get_collections().collections
    ) == before == sorted([legacy_name, shadow_name])


async def test_published_invalid_alias_is_repaired_without_canonical_fallback(
    local_qdrant,
    monkeypatch,
    profile,
) -> None:
    _fix_operation_id(monkeypatch)
    canonical_name = canonical_qdrant_collection_name(_MEMORY_ID)
    alias_name = f"{canonical_name}__active_v1"
    tracker = _InvalidAliasRepairClient(
        local_qdrant,
        alias_name,
        canonical_name,
    )
    store = VectorStoreService(
        client=tracker,
        profile=profile,
        legacy_prefix="memory_",
    )

    assert await store.get_collection_info(_MEMORY_ID) == {
        "state": "reindex_required",
        "reason": "active_alias_invalid",
    }

    documents, objects, content_by_key = _source_bundle()
    result = await _service(
        graph=_FakeGraph(documents),
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
    ).reindex(_MEMORY_ID)

    assert result == {
        "status": "ok",
        "phase": "verified",
        "reason": None,
        "operation_id": _OPERATION_ID,
        "source_documents": 2,
        "source_chunks": 8,
        "vectors_written": 8,
        "activated": True,
        "active_state": "ready",
    }
    shadow_name = store._shadow_name(_MEMORY_ID, _OPERATION_ID)
    assert _alias_map(local_qdrant) == {alias_name: shadow_name}
    assert not local_qdrant.collection_exists(canonical_name)
    assert len(tracker.alias_batches) == 1
    batch = tracker.alias_batches[0]
    assert len(batch) == 2
    assert batch[0].delete_alias.alias_name == alias_name
    assert batch[1].create_alias.alias_name == alias_name
    assert batch[1].create_alias.collection_name == shadow_name

    expected_identity = build_embedding_collection_identity(
        _MEMORY_ID,
        profile,
        _embedding_result(["fingerprint-only"]),
    )
    assert await store.get_collection_info(_MEMORY_ID) == {
        "state": "ready",
        "profile_fingerprint": expected_identity.profile_fingerprint,
        "points_count": 8,
    }


async def test_invalid_alias_change_at_activation_reread_aborts_cutover(
    local_qdrant,
    monkeypatch,
    profile,
) -> None:
    _fix_operation_id(monkeypatch)
    alias_name = f"{canonical_qdrant_collection_name(_MEMORY_ID)}__active_v1"
    tracker = _InvalidAliasRaceClient(local_qdrant, alias_name)
    store = VectorStoreService(
        client=tracker,
        profile=profile,
        legacy_prefix="memory_",
    )
    documents, objects, content_by_key = _source_bundle()

    result = await _service(
        graph=_FakeGraph(documents),
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
    ).reindex(_MEMORY_ID)

    assert result["status"] == "error"
    assert result["phase"] == "pre_switch"
    assert result["reason"] == "active_target_changed"
    assert result["activated"] is False
    assert tracker.alias_reads == 3
    assert tracker.alias_batches == []
    assert _alias_map(local_qdrant) == {}


async def test_pinned_qdrant_server_executes_atomic_alias_create_and_replace(
    profile,
) -> None:
    url = os.environ.get("HIVEMIND_QDRANT_TEST_URL")
    if not url:
        pytest.skip("set HIVEMIND_QDRANT_TEST_URL for Qdrant 1.16 server proof")

    memory_id = f"P13_reindex_{uuid.uuid4().hex}"
    client = QdrantClient(url=url, timeout=10)
    legacy_name = _seed_legacy(client, memory_id)
    store = VectorStoreService(
        client=client,
        profile=profile,
        legacy_prefix="memory_",
    )
    documents, objects, content_by_key = _source_bundle(memory_id)
    service = _service(
        graph=_FakeGraph(documents, memory_id=memory_id),
        storage=_FakeStorage(objects, content_by_key, memory_id=memory_id),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
    )
    alias_name = store._active_alias_name(memory_id)
    created_names: set[str] = {legacy_name}

    try:
        result = await service.reindex(memory_id)

        assert result["status"] == "ok"
        assert result["phase"] == "verified"
        assert result["activated"] is True
        shadow_name = _alias_map(client)[alias_name]
        created_names.add(shadow_name)
        assert client.count(collection_name=shadow_name, exact=True).count == 8
        assert client.count(collection_name=legacy_name, exact=True).count == 1
        assert await store.list_doc_ids(memory_id) == {"doc-a", "doc-b"}
        assert client.collection_exists(legacy_name)

        replacement_profile = make_embedding_profile(
            expected_dimensions=3,
            endpoint="https://replacement-embedding.test/v1",
        )
        replacement_store = VectorStoreService(
            client=client,
            profile=replacement_profile,
            legacy_prefix="memory_",
        )
        assert (await replacement_store.get_collection_info(memory_id))["state"] == (
            "reindex_required"
        )
        replacement = await _service(
            graph=_FakeGraph(documents, memory_id=memory_id),
            storage=_FakeStorage(objects, content_by_key, memory_id=memory_id),
            chunker=_FakeChunker(),
            embedder=_FakeEmbedder(),
            vectors=replacement_store,
        ).reindex(memory_id)

        assert replacement["status"] == "ok"
        replacement_shadow = _alias_map(client)[alias_name]
        created_names.add(replacement_shadow)
        assert replacement_shadow != shadow_name
        assert client.collection_exists(shadow_name)
        assert client.count(collection_name=shadow_name, exact=True).count == 8
        assert client.count(collection_name=replacement_shadow, exact=True).count == 8
        assert client.collection_exists(legacy_name)
    finally:
        aliases = _alias_map(client)
        if alias_name in aliases:
            client.update_collection_aliases(
                [
                    qmodels.DeleteAliasOperation(
                        delete_alias=qmodels.DeleteAlias(alias_name=alias_name)
                    )
                ]
            )
        for name in created_names:
            if client.collection_exists(name):
                client.delete_collection(name)
        client.close()


@pytest.mark.parametrize(
    ("case", "phase", "reason"),
    [
        ("status", "snapshot", "source_status_invalid"),
        ("hash", "snapshot", "source_hash_mismatch"),
        ("document-duplicate", "snapshot", "source_document_duplicate"),
        ("object-duplicate", "snapshot", "source_object_duplicate"),
        ("ownership", "snapshot", "source_ownership_invalid"),
        ("size", "snapshot", "source_size_mismatch"),
        ("metadata", "snapshot", "source_metadata_mismatch"),
        ("extraction", "snapshot", "source_extraction_failed"),
        ("chunks", "snapshot", "source_chunk_accounting_mismatch"),
        ("embedding-cardinality", "rebuild", "embedding_invalid"),
    ],
)
async def test_invalid_retained_source_fails_before_shadow(
    local_qdrant,
    monkeypatch,
    profile,
    case,
    phase,
    reason,
) -> None:
    _fix_operation_id(monkeypatch)
    legacy_name = _seed_legacy(local_qdrant)
    documents, objects, content_by_key = _source_bundle()
    text_extractor = lambda content, _filename: content.decode("utf-8")
    embedder = _FakeEmbedder()
    if case == "status":
        documents[0]["status"] = "running"
    elif case == "hash":
        key = next(iter(content_by_key))
        original = content_by_key[key]
        content_by_key[key] = b"x" * len(original)
    elif case == "document-duplicate":
        documents[1]["document_id"] = documents[0]["document_id"]
    elif case == "object-duplicate":
        objects.append(copy.deepcopy(objects[0]))
    elif case == "ownership":
        documents[0]["memory_id"] = "other-memory"
    elif case == "size":
        documents[0]["size_bytes"] += 1
    elif case == "metadata":
        obj = next(item for item in objects if item["uri"] == documents[0]["uri"])
        obj["metadata"]["original_filename"] = "other.txt"
    elif case == "extraction":
        text_extractor = lambda _content, _filename: ""
    elif case == "chunks":
        documents[0]["chunk_count"] += 1
    elif case == "embedding-cardinality":
        class InvalidCardinalityEmbedder:
            async def embed_texts_result(self, texts: list[str]):
                valid = _embedding_result(texts)
                return EmbeddingResult(
                    vectors=valid.vectors[:-1],
                    configured_model=valid.configured_model,
                    resolved_model=valid.resolved_model,
                    model_evidence=valid.model_evidence,
                    effective_dimensions=valid.effective_dimensions,
                )

        embedder = InvalidCardinalityEmbedder()
    store = VectorStoreService(
        client=local_qdrant,
        profile=profile,
        legacy_prefix="memory_",
    )
    result = await _service(
        graph=_FakeGraph(documents),
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=embedder,
        vectors=store,
        text_extractor=text_extractor,
    ).reindex(_MEMORY_ID)

    assert result["status"] == "error"
    assert result["phase"] == phase
    assert result["reason"] == reason
    assert result["activated"] is False
    assert set(result) == _RESULT_FIELDS
    _assert_no_active_alias(local_qdrant, store)
    assert _shadow_names(local_qdrant, store) == []
    assert local_qdrant.count(collection_name=legacy_name, exact=True).count == 1


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("embedding-identity", "embedding_identity_changed"),
        ("write-count", "shadow_write_failed"),
    ],
)
async def test_rebuild_refusals_leave_attributable_shadow_without_activation(
    case: str,
    reason: str,
    local_qdrant,
    monkeypatch: pytest.MonkeyPatch,
    profile,
) -> None:
    _fix_operation_id(monkeypatch)
    legacy_name = _seed_legacy(local_qdrant)
    documents, objects, content_by_key = _source_bundle()
    store = VectorStoreService(
        client=local_qdrant,
        profile=profile,
        legacy_prefix="memory_",
    )
    embedder = _FakeEmbedder(
        resolved_models=("resolved-v1", "resolved-v2")
        if case == "embedding-identity"
        else ("resolved-v1",)
    )
    if case == "write-count":
        async def incomplete_write(*_args, **_kwargs) -> int:
            return 0

        monkeypatch.setattr(store, "store_reindex_chunks", incomplete_write)

    result = await _service(
        graph=_FakeGraph(documents),
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=embedder,
        vectors=store,
    ).reindex(_MEMORY_ID)

    assert result["status"] == "error"
    assert result["phase"] == "rebuild"
    assert result["reason"] == reason
    assert result["activated"] is False
    _assert_no_active_alias(local_qdrant, store)
    assert _shadow_names(local_qdrant, store) == [
        store._shadow_name(_MEMORY_ID, _OPERATION_ID)
    ]
    assert local_qdrant.count(collection_name=legacy_name, exact=True).count == 1


async def test_aggregate_source_chunk_cap_fails_before_extraction_or_shadow(
    local_qdrant,
    monkeypatch: pytest.MonkeyPatch,
    profile,
) -> None:
    _fix_operation_id(monkeypatch)
    _seed_legacy(local_qdrant)
    documents, objects, content_by_key = _source_bundle()
    monkeypatch.setattr(reindex_module, "_MAX_REINDEX_CHUNKS", 7)
    extraction_calls: list[str] = []
    store = VectorStoreService(
        client=local_qdrant,
        profile=profile,
        legacy_prefix="memory_",
    )

    result = await _service(
        graph=_FakeGraph(documents),
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
        text_extractor=lambda _content, filename: extraction_calls.append(filename),
    ).reindex(_MEMORY_ID)

    assert result["status"] == "error"
    assert result["phase"] == "snapshot"
    assert result["reason"] == "source_size_limit_exceeded"
    assert result["activated"] is False
    assert extraction_calls == []
    assert _shadow_names(local_qdrant, store) == []


@pytest.mark.parametrize("valid_config", [True, False])
async def test_only_exact_unreferenced_ontology_config_is_excluded(
    local_qdrant,
    monkeypatch,
    profile,
    valid_config,
) -> None:
    _fix_operation_id(monkeypatch)
    _seed_legacy(local_qdrant)
    documents, objects, content_by_key = _source_bundle()
    config_key = _add_ontology_config(objects, content_by_key)
    if not valid_config:
        objects[-1]["metadata"]["ontology_name"] = "other"
    storage = _FakeStorage(objects, content_by_key)
    store = VectorStoreService(
        client=local_qdrant,
        profile=profile,
        legacy_prefix="memory_",
    )

    result = await _service(
        graph=_FakeGraph(
            documents,
            ontology_uri=f"s3://test-bucket/{config_key}",
        ),
        storage=storage,
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
    ).reindex(_MEMORY_ID)

    if valid_config:
        assert result["status"] == "ok"
        assert result["reason"] is None
        assert config_key not in storage.read_calls
    else:
        assert result["status"] == "error"
        assert result["reason"] == "source_object_mismatch"
        assert _shadow_names(local_qdrant, store) == []


async def test_shadow_creation_rejects_false_payload_index_acknowledgement(
    local_qdrant,
    monkeypatch,
    profile,
) -> None:
    _fix_operation_id(monkeypatch)
    _seed_legacy(local_qdrant)
    documents, objects, content_by_key = _source_bundle()
    store = VectorStoreService(
        client=_FalsePayloadIndexClient(local_qdrant),
        profile=profile,
        legacy_prefix="memory_",
    )

    result = await _service(
        graph=_FakeGraph(documents),
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
    ).reindex(_MEMORY_ID)

    assert result["status"] == "error"
    assert result["phase"] == "rebuild"
    assert result["reason"] == "shadow_creation_failed"
    _assert_no_active_alias(local_qdrant, store)


async def test_shadow_validation_rejects_empty_nonterminal_scroll_page(
    local_qdrant,
    monkeypatch,
    profile,
) -> None:
    _fix_operation_id(monkeypatch)
    _seed_legacy(local_qdrant)
    documents, objects, content_by_key = _source_bundle()
    shadow_name = (
        f"{canonical_qdrant_collection_name(_MEMORY_ID)}"
        f"__shadow_v1_{_OPERATION_ID}"
    )
    store = VectorStoreService(
        client=_EmptyNonterminalScrollClient(local_qdrant, shadow_name),
        profile=profile,
        legacy_prefix="memory_",
    )

    result = await _service(
        graph=_FakeGraph(documents),
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
    ).reindex(_MEMORY_ID)

    assert result["status"] == "error"
    assert result["phase"] == "validate"
    assert result["reason"] == "shadow_invalid"
    _assert_no_active_alias(local_qdrant, store)


async def test_shared_retained_source_is_downloaded_once_per_snapshot(
    local_qdrant,
    monkeypatch,
    profile,
) -> None:
    _fix_operation_id(monkeypatch)
    _seed_legacy(local_qdrant)
    documents, objects, content_by_key = _source_bundle()
    shared = copy.deepcopy(documents[0])
    shared["document_id"] = "doc-shared-second"
    documents = [documents[0], shared]
    objects = [objects[0]]
    key = objects[0]["key"]
    content_by_key = {key: content_by_key[key]}
    storage = _FakeStorage(objects, content_by_key)
    store = VectorStoreService(
        client=local_qdrant,
        profile=profile,
        legacy_prefix="memory_",
    )

    result = await _service(
        graph=_FakeGraph(documents),
        storage=storage,
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
    ).reindex(_MEMORY_ID)

    assert result["status"] == "ok"
    assert result["source_documents"] == 2
    assert storage.read_calls == [key, key]


async def test_source_work_cap_refuses_duplicate_reference_amplification(
    local_qdrant,
    monkeypatch,
    profile,
) -> None:
    _fix_operation_id(monkeypatch)
    _seed_legacy(local_qdrant)
    documents, objects, content_by_key = _source_bundle()
    documents[0]["size_bytes"] = 200 * 1024 * 1024
    documents[1]["size_bytes"] = 200 * 1024 * 1024
    store = VectorStoreService(
        client=local_qdrant,
        profile=profile,
        legacy_prefix="memory_",
    )

    result = await _service(
        graph=_FakeGraph(documents),
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
    ).reindex(_MEMORY_ID)

    assert result["status"] == "error"
    assert result["phase"] == "snapshot"
    assert result["reason"] == "source_size_limit_exceeded"
    _assert_no_active_alias(local_qdrant, store)


async def test_source_document_count_cap_uses_stable_size_limit_reason(
    local_qdrant,
    monkeypatch,
    profile,
) -> None:
    _fix_operation_id(monkeypatch)
    _seed_legacy(local_qdrant)
    documents, objects, content_by_key = _source_bundle()
    monkeypatch.setattr(reindex_module, "MAX_REINDEX_SOURCE_DOCUMENTS", 1)
    store = VectorStoreService(
        client=local_qdrant,
        profile=profile,
        legacy_prefix="memory_",
    )

    result = await _service(
        graph=_FakeGraph(documents),
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
    ).reindex(_MEMORY_ID)

    assert result["status"] == "error"
    assert result["phase"] == "snapshot"
    assert result["reason"] == "source_size_limit_exceeded"
    _assert_no_active_alias(local_qdrant, store)


async def test_storage_inventory_cap_uses_stable_size_limit_reason(
    local_qdrant,
    monkeypatch,
    profile,
) -> None:
    from mcp_memory.core.maintenance import ReindexSourceLimitExceeded

    _fix_operation_id(monkeypatch)
    _seed_legacy(local_qdrant)
    documents, _objects, _content_by_key = _source_bundle()

    class Storage:
        async def list_reindex_objects(self, _memory_id: str):
            raise ReindexSourceLimitExceeded("private storage detail")

    store = VectorStoreService(
        client=local_qdrant,
        profile=profile,
        legacy_prefix="memory_",
    )

    result = await _service(
        graph=_FakeGraph(documents),
        storage=Storage(),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
    ).reindex(_MEMORY_ID)

    assert result["status"] == "error"
    assert result["phase"] == "snapshot"
    assert result["reason"] == "source_size_limit_exceeded"
    assert "private storage detail" not in json.dumps(result)
    _assert_no_active_alias(local_qdrant, store)


async def test_inventory_failure_cancels_and_drains_sibling_reads(
    local_qdrant,
    monkeypatch,
    profile,
) -> None:
    _fix_operation_id(monkeypatch)
    _seed_legacy(local_qdrant)
    storage_started = asyncio.Event()
    storage_cancelled = asyncio.Event()

    class Graph:
        async def list_reindex_documents(self, _memory_id: str):
            await storage_started.wait()
            raise RuntimeError("inventory failed")

        async def get_reindex_ontology_uri(self, _memory_id: str):
            return None

    class Storage:
        async def list_reindex_objects(self, _memory_id: str):
            storage_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                storage_cancelled.set()

    store = VectorStoreService(
        client=local_qdrant,
        profile=profile,
        legacy_prefix="memory_",
    )

    result = await _service(
        graph=Graph(),
        storage=Storage(),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
    ).reindex(_MEMORY_ID)

    assert result["status"] == "error"
    assert result["reason"] == "source_inventory_unavailable"
    assert storage_cancelled.is_set()


@pytest.mark.parametrize("race", ["source", "chunking"])
async def test_source_and_chunking_races_keep_the_validated_shadow_unactivated(
    local_qdrant,
    monkeypatch,
    profile,
    race,
) -> None:
    _fix_operation_id(monkeypatch)
    legacy_name = _seed_legacy(local_qdrant)
    documents, objects, content_by_key = _source_bundle()
    changed = copy.deepcopy(documents)
    changed[0]["chunk_count"] += 1
    graph = _FakeGraph([documents, changed] if race == "source" else documents)
    chunker = _FakeChunker(
        signatures=((128, 16), (129, 16))
        if race == "chunking"
        else ((128, 16),)
    )
    store = VectorStoreService(
        client=local_qdrant,
        profile=profile,
        legacy_prefix="memory_",
    )
    result = await _service(
        graph=graph,
        storage=_FakeStorage(objects, content_by_key),
        chunker=chunker,
        embedder=_FakeEmbedder(),
        vectors=store,
    ).reindex(_MEMORY_ID)

    assert result["status"] == "error"
    assert result["phase"] == "pre_switch"
    assert result["reason"] == (
        "source_changed" if race == "source" else "chunking_config_changed"
    )
    assert result["vectors_written"] == 8
    assert result["activated"] is False
    _assert_no_active_alias(local_qdrant, store)
    assert _shadow_names(local_qdrant, store) == [
        store._shadow_name(_MEMORY_ID, _OPERATION_ID)
    ]
    assert local_qdrant.count(collection_name=legacy_name, exact=True).count == 1


async def test_embedding_profile_race_keeps_shadow_and_old_target(
    local_qdrant,
    monkeypatch,
    profile,
) -> None:
    _fix_operation_id(monkeypatch)
    legacy_name = _seed_legacy(local_qdrant)
    documents, objects, content_by_key = _source_bundle()
    raced_profile = make_embedding_profile(
        expected_dimensions=3,
        configured_model="raced-model",
    )
    store = _ProfileRaceStore(
        client=local_qdrant,
        profile=profile,
        legacy_prefix="memory_",
        raced_profile=raced_profile,
    )
    result = await _service(
        graph=_FakeGraph(documents),
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
    ).reindex(_MEMORY_ID)

    assert result["reason"] == "embedding_profile_changed"
    assert result["phase"] == "pre_switch"
    assert result["activated"] is False
    assert result["vectors_written"] == 8
    _assert_no_active_alias(local_qdrant, store)
    assert len(_shadow_names(local_qdrant, store)) == 1
    assert local_qdrant.count(collection_name=legacy_name, exact=True).count == 1


async def test_active_target_race_is_detected_before_alias_switch(
    local_qdrant,
    monkeypatch,
    profile,
) -> None:
    _fix_operation_id(monkeypatch)
    legacy_name = _seed_legacy(local_qdrant)
    documents, objects, content_by_key = _source_bundle()

    def create_racing_canonical():
        local_qdrant.create_collection(
            collection_name=canonical_qdrant_collection_name(_MEMORY_ID),
            vectors_config=qmodels.VectorParams(
                size=3,
                distance=qmodels.Distance.COSINE,
            ),
            metadata=build_embedding_collection_identity(
                _MEMORY_ID,
                profile,
                _embedding_result(["race"]),
            ).to_mapping(),
        )

    graph = _FakeGraph(documents, hooks={2: create_racing_canonical})
    store = VectorStoreService(
        client=local_qdrant,
        profile=profile,
        legacy_prefix="memory_",
    )
    result = await _service(
        graph=graph,
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
    ).reindex(_MEMORY_ID)

    assert result["reason"] == "active_target_changed"
    assert result["phase"] == "pre_switch"
    assert result["activated"] is False
    _assert_no_active_alias(local_qdrant, store)
    assert len(_shadow_names(local_qdrant, store)) == 1
    assert local_qdrant.count(collection_name=legacy_name, exact=True).count == 1


@pytest.mark.parametrize(
    ("tamper", "expected_reason"),
    [
        ("missing", "shadow_invalid"),
        ("unexpected_document", "shadow_invalid"),
        ("duplicate_index", "shadow_invalid"),
        ("wrong_owner", "payload_ownership_mismatch"),
    ],
)
async def test_shadow_validation_rejects_incomplete_or_wrong_accounting(
    local_qdrant,
    profile,
    tamper,
    expected_reason,
) -> None:
    store, identity, shadow_name = await _create_complete_shadow(
        local_qdrant,
        profile,
    )
    points, _ = local_qdrant.scroll(
        collection_name=shadow_name,
        with_payload=True,
        with_vectors=True,
        limit=10,
    )
    if tamper == "missing":
        local_qdrant.delete(
            collection_name=shadow_name,
            points_selector=[points[0].id],
            wait=True,
        )
    elif tamper == "unexpected_document":
        local_qdrant.upsert(
            collection_name=shadow_name,
            points=[
                qmodels.PointStruct(
                    id=99,
                    vector=[1.0, 0.5, 0.25],
                    payload={
                        "memory_id": _MEMORY_ID,
                        "doc_id": "unexpected-doc",
                        "chunk_index": 0,
                        "total_chunks": 1,
                    },
                )
            ],
            wait=True,
        )
    elif tamper == "duplicate_index":
        second_chunk = next(
            point for point in points if point.payload.get("chunk_index") == 1
        )
        local_qdrant.set_payload(
            collection_name=shadow_name,
            payload={"chunk_index": 0},
            points=[second_chunk.id],
            wait=True,
        )
    else:
        local_qdrant.set_payload(
            collection_name=shadow_name,
            payload={"memory_id": "foreign-memory"},
            points=[points[0].id],
            wait=True,
        )

    with pytest.raises(EmbeddingCollectionReindexRequired) as exc_info:
        await store.validate_reindex_shadow(
            _MEMORY_ID,
            "b" * 32,
            identity=identity,
            expected_chunks={"doc-a": 2},
        )
    assert exc_info.value.reason == expected_reason
    _assert_no_active_alias(local_qdrant, store)


@pytest.mark.parametrize("tamper", ["nonfinite_vector", "wrong_dimension", "point_id"])
async def test_shadow_validation_rechecks_vectors_and_point_ids(
    local_qdrant,
    profile,
    tamper,
) -> None:
    base_store, identity, shadow_name = await _create_complete_shadow(
        local_qdrant,
        profile,
    )

    def alter(point):
        if tamper == "nonfinite_vector":
            return point.model_copy(update={"vector": [1.0, float("nan"), 0.25]})
        if tamper == "wrong_dimension":
            return point.model_copy(update={"vector": [1.0, 0.25]})
        return point.model_copy(update={"id": "not-a-uuid"})

    validating_store = VectorStoreService(
        client=_TamperedScrollClient(local_qdrant, shadow_name, alter),
        profile=profile,
        legacy_prefix="memory_",
    )
    with pytest.raises(EmbeddingCollectionReindexRequired) as exc_info:
        await validating_store.validate_reindex_shadow(
            _MEMORY_ID,
            "b" * 32,
            identity=identity,
            expected_chunks={"doc-a": 2},
        )
    assert exc_info.value.reason == "backup_point_invalid"
    _assert_no_active_alias(local_qdrant, base_store)


@pytest.mark.parametrize(
    ("invalid_target", "expected_reason"),
    [
        ("distance", "vector_config_mismatch"),
        ("identity", "memory_namespace_mismatch"),
    ],
)
async def test_shadow_validation_rechecks_distance_and_exact_identity(
    local_qdrant,
    profile,
    invalid_target,
    expected_reason,
) -> None:
    store = VectorStoreService(
        client=local_qdrant,
        profile=profile,
        legacy_prefix="memory_",
    )
    operation_id = "c" * 32
    name = store._shadow_name(_MEMORY_ID, operation_id)
    expected_identity = build_embedding_collection_identity(
        _MEMORY_ID,
        profile,
        _embedding_result(["expected"]),
    )
    stored_identity = (
        build_embedding_collection_identity(
            "other-memory",
            profile,
            _embedding_result(["expected"]),
        )
        if invalid_target == "identity"
        else expected_identity
    )
    local_qdrant.create_collection(
        collection_name=name,
        vectors_config=qmodels.VectorParams(
            size=3,
            distance=(
                qmodels.Distance.DOT
                if invalid_target == "distance"
                else qmodels.Distance.COSINE
            ),
        ),
        metadata=stored_identity.to_mapping(),
    )

    with pytest.raises(EmbeddingCollectionReindexRequired) as exc_info:
        await store.validate_reindex_shadow(
            _MEMORY_ID,
            operation_id,
            identity=expected_identity,
            expected_chunks={"doc-a": 1},
        )
    assert exc_info.value.reason == expected_reason
    _assert_no_active_alias(local_qdrant, store)


async def test_abandoned_shadow_collision_is_never_adopted_or_deleted(
    local_qdrant,
    monkeypatch,
    profile,
) -> None:
    _fix_operation_id(monkeypatch)
    legacy_name = _seed_legacy(local_qdrant)
    documents, objects, content_by_key = _source_bundle()
    identity = build_embedding_collection_identity(
        _MEMORY_ID,
        profile,
        _embedding_result(["abandoned"]),
    )
    base_store = VectorStoreService(
        client=local_qdrant,
        profile=profile,
        legacy_prefix="memory_",
    )
    shadow_name = base_store._shadow_name(_MEMORY_ID, _OPERATION_ID)
    local_qdrant.create_collection(
        collection_name=shadow_name,
        vectors_config=qmodels.VectorParams(
            size=3,
            distance=qmodels.Distance.COSINE,
        ),
        metadata=identity.to_mapping(),
    )
    local_qdrant.upsert(
        collection_name=shadow_name,
        points=[
            qmodels.PointStruct(
                id=7,
                vector=[1.0, 0.5, 0.25],
                payload={
                    "memory_id": _MEMORY_ID,
                    "doc_id": "abandoned-doc",
                    "chunk_index": 0,
                    "total_chunks": 1,
                    "text": "ABANDONED_SHADOW_SECRET",
                },
            )
        ],
        wait=True,
    )
    tracker = _RecordingClient(local_qdrant)
    store = VectorStoreService(
        client=tracker,
        profile=profile,
        legacy_prefix="memory_",
    )
    result = await _service(
        graph=_FakeGraph(documents),
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
    ).reindex(_MEMORY_ID)

    assert result["reason"] == "shadow_collision"
    assert result["phase"] == "rebuild"
    assert result["vectors_written"] == 0
    assert result["activated"] is False
    assert tracker.mutations == []
    assert local_qdrant.count(collection_name=shadow_name, exact=True).count == 1
    assert local_qdrant.count(collection_name=legacy_name, exact=True).count == 1
    _assert_no_active_alias(local_qdrant, store)
    serialized = json.dumps(result, sort_keys=True)
    assert shadow_name not in serialized
    assert "ABANDONED_SHADOW_SECRET" not in serialized


async def test_post_switch_read_failure_never_rolls_back_or_deletes_targets(
    local_qdrant,
    monkeypatch,
    profile,
) -> None:
    _fix_operation_id(monkeypatch)
    legacy_name = _seed_legacy(local_qdrant)
    documents, objects, content_by_key = _source_bundle()
    tracker = _PostSwitchUnreadableClient(local_qdrant)
    store = VectorStoreService(
        client=tracker,
        profile=profile,
        legacy_prefix="memory_",
    )
    result = await _service(
        graph=_FakeGraph(documents),
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
    ).reindex(_MEMORY_ID)

    shadow_name = store._shadow_name(_MEMORY_ID, _OPERATION_ID)
    alias_name = store._active_alias_name(_MEMORY_ID)
    assert result == {
        "status": "error",
        "phase": "activated",
        "reason": "post_switch_unverified",
        "operation_id": _OPERATION_ID,
        "source_documents": 2,
        "source_chunks": 8,
        "vectors_written": 8,
        "activated": True,
        "active_state": "unavailable",
    }
    assert _alias_map(local_qdrant) == {alias_name: shadow_name}
    assert local_qdrant.count(collection_name=shadow_name, exact=True).count == 8
    assert local_qdrant.count(collection_name=legacy_name, exact=True).count == 1
    mutation_names = [name for name, _target in tracker.mutations]
    assert mutation_names.count("update_collection_aliases") == 1
    assert tracker.mutations[-1] == ("update_collection_aliases", None)
    assert "delete_collection" not in mutation_names
    assert len(tracker.alias_batches) == 1


async def test_post_switch_coordinator_cleanup_failure_cannot_preserve_success(
    local_qdrant,
    monkeypatch: pytest.MonkeyPatch,
    profile,
) -> None:
    """A poisoned release after activation remains explicit and retry-unsafe."""

    class _FailingExitCoordinator:
        @asynccontextmanager
        async def maintenance(self, _memory_id, *, idle_check=None):
            if idle_check is not None:
                assert await idle_check() is True
            yield
            raise MaintenanceCoordinatorCorrupted()

    _fix_operation_id(monkeypatch)
    legacy_name = _seed_legacy(local_qdrant)
    documents, objects, content_by_key = _source_bundle()
    tracker = _RecordingClient(local_qdrant)
    store = VectorStoreService(
        client=tracker,
        profile=profile,
        legacy_prefix="memory_",
    )

    result = await _service(
        graph=_FakeGraph(documents),
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
        coordinator=_FailingExitCoordinator(),
    ).reindex(_MEMORY_ID)

    shadow_name = store._shadow_name(_MEMORY_ID, _OPERATION_ID)
    alias_name = store._active_alias_name(_MEMORY_ID)
    assert result == {
        "status": "error",
        "phase": "activated",
        "reason": "post_switch_unverified",
        "operation_id": _OPERATION_ID,
        "source_documents": 2,
        "source_chunks": 8,
        "vectors_written": 8,
        "activated": True,
        "active_state": "unavailable",
    }
    assert _alias_map(local_qdrant) == {alias_name: shadow_name}
    assert local_qdrant.count(collection_name=shadow_name, exact=True).count == 8
    assert local_qdrant.count(collection_name=legacy_name, exact=True).count == 1
    mutation_names = [name for name, _target in tracker.mutations]
    assert mutation_names.count("update_collection_aliases") == 1
    assert "delete_collection" not in mutation_names
    assert "maintenance coordinator" not in json.dumps(result, sort_keys=True)


@pytest.mark.parametrize("raises", [False, True])
async def test_unverified_alias_update_is_retry_unsafe_and_never_rolls_back(
    raises: bool,
    local_qdrant,
    monkeypatch: pytest.MonkeyPatch,
    profile,
) -> None:
    _fix_operation_id(monkeypatch)
    legacy_name = _seed_legacy(local_qdrant)
    tracker = _AliasUpdateFailureClient(local_qdrant, raises=raises)
    store = VectorStoreService(
        client=tracker,
        profile=profile,
        legacy_prefix="memory_",
    )
    documents, objects, content_by_key = _source_bundle()

    result = await _service(
        graph=_FakeGraph(documents),
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
    ).reindex(_MEMORY_ID)

    assert result["status"] == "error"
    assert result["phase"] == "activated"
    assert result["reason"] == "activation_unverified"
    assert result["activated"] is True
    assert result["active_state"] == "unavailable"
    assert len(tracker.alias_batches) == 1
    assert tracker.mutations[-1] == ("update_collection_aliases", None)
    assert not any(name == "delete_collection" for name, _ in tracker.mutations)
    _assert_no_active_alias(local_qdrant, store)
    assert _shadow_names(local_qdrant, store) == [
        store._shadow_name(_MEMORY_ID, _OPERATION_ID)
    ]
    assert local_qdrant.count(collection_name=legacy_name, exact=True).count == 1


async def test_coordinator_poison_cannot_hide_a_post_switch_driver_error(
    local_qdrant,
    monkeypatch: pytest.MonkeyPatch,
    profile,
) -> None:
    """A cleanup exception may replace the original post-switch exception."""

    class _ReplacingFailureCoordinator:
        @asynccontextmanager
        async def maintenance(self, _memory_id, *, idle_check=None):
            if idle_check is not None:
                assert await idle_check() is True
            try:
                yield
            finally:
                raise MaintenanceCoordinatorCorrupted()

    _fix_operation_id(monkeypatch)
    legacy_name = _seed_legacy(local_qdrant)
    documents, objects, content_by_key = _source_bundle()
    tracker = _PostSwitchUnreadableClient(local_qdrant)
    store = VectorStoreService(
        client=tracker,
        profile=profile,
        legacy_prefix="memory_",
    )

    result = await _service(
        graph=_FakeGraph(documents),
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
        coordinator=_ReplacingFailureCoordinator(),
    ).reindex(_MEMORY_ID)

    shadow_name = store._shadow_name(_MEMORY_ID, _OPERATION_ID)
    alias_name = store._active_alias_name(_MEMORY_ID)
    assert result == {
        "status": "error",
        "phase": "activated",
        "reason": "post_switch_unverified",
        "operation_id": _OPERATION_ID,
        "source_documents": 2,
        "source_chunks": 8,
        "vectors_written": 8,
        "activated": True,
        "active_state": "unavailable",
    }
    assert _alias_map(local_qdrant) == {alias_name: shadow_name}
    assert local_qdrant.count(collection_name=shadow_name, exact=True).count == 8
    assert local_qdrant.count(collection_name=legacy_name, exact=True).count == 1
    assert [name for name, _target in tracker.mutations].count(
        "update_collection_aliases"
    ) == 1
    assert not any(name == "delete_collection" for name, _ in tracker.mutations)


def test_post_switch_error_walker_handles_groups_cycles_and_a_fixed_bound() -> None:
    marker = reindex_module.ReindexPostSwitchError("post_switch_unverified")
    grouped = ExceptionGroup("cleanup", [RuntimeError("other"), marker])
    assert reindex_module._find_post_switch_error(grouped) is marker

    first = RuntimeError("first")
    second = RuntimeError("second")
    first.__cause__ = second
    second.__cause__ = first
    assert reindex_module._find_post_switch_error(first) is None

    oversized = ExceptionGroup(
        "bounded",
        [RuntimeError(str(index)) for index in range(32)],
    )
    assert reindex_module._find_post_switch_error(oversized) is None


async def test_inner_mutation_cleanup_cannot_hide_a_post_switch_driver_error(
    local_qdrant,
    monkeypatch: pytest.MonkeyPatch,
    profile,
) -> None:
    """The vector-store admission wrapper can replace its own body error."""

    from mcp_memory.core import maintenance as maintenance_module

    class _OuterCoordinator:
        @asynccontextmanager
        async def maintenance(self, _memory_id, *, idle_check=None):
            if idle_check is not None:
                assert await idle_check() is True
            yield

    class _ReplacingInnerCoordinator:
        @asynccontextmanager
        async def ordinary(self, _memory_id):
            try:
                yield
            except reindex_module.ReindexPostSwitchError:
                raise MaintenanceCoordinatorCorrupted()

    monkeypatch.setattr(
        maintenance_module,
        "get_maintenance_coordinator",
        lambda: _ReplacingInnerCoordinator(),
    )
    _fix_operation_id(monkeypatch)
    legacy_name = _seed_legacy(local_qdrant)
    documents, objects, content_by_key = _source_bundle()
    tracker = _PostSwitchUnreadableClient(local_qdrant)
    store = VectorStoreService(
        client=tracker,
        profile=profile,
        legacy_prefix="memory_",
    )

    result = await _service(
        graph=_FakeGraph(documents),
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
        coordinator=_OuterCoordinator(),
    ).reindex(_MEMORY_ID)

    shadow_name = store._shadow_name(_MEMORY_ID, _OPERATION_ID)
    alias_name = store._active_alias_name(_MEMORY_ID)
    assert result["status"] == "error"
    assert result["phase"] == "activated"
    assert result["reason"] == "post_switch_unverified"
    assert result["activated"] is True
    assert result["active_state"] == "unavailable"
    assert _alias_map(local_qdrant) == {alias_name: shadow_name}
    assert local_qdrant.count(collection_name=legacy_name, exact=True).count == 1


async def test_committed_but_unacknowledged_alias_update_stays_retry_unsafe(
    local_qdrant,
    monkeypatch: pytest.MonkeyPatch,
    profile,
) -> None:
    _fix_operation_id(monkeypatch)
    legacy_name = _seed_legacy(local_qdrant)
    tracker = _AliasUpdateFailureClient(
        local_qdrant,
        raises=True,
        commits_before_raise=True,
    )
    store = VectorStoreService(
        client=tracker,
        profile=profile,
        legacy_prefix="memory_",
    )
    documents, objects, content_by_key = _source_bundle()

    result = await _service(
        graph=_FakeGraph(documents),
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
    ).reindex(_MEMORY_ID)

    shadow_name = store._shadow_name(_MEMORY_ID, _OPERATION_ID)
    alias_name = store._active_alias_name(_MEMORY_ID)
    assert result["status"] == "error"
    assert result["phase"] == "activated"
    assert result["reason"] == "activation_unverified"
    assert result["activated"] is True
    assert result["active_state"] == "unavailable"
    assert _alias_map(local_qdrant) == {alias_name: shadow_name}
    assert local_qdrant.collection_exists(shadow_name)
    assert local_qdrant.collection_exists(legacy_name)
    mutation_names = [name for name, _target in tracker.mutations]
    assert mutation_names.count("update_collection_aliases") == 1
    assert tracker.mutations[-1] == ("update_collection_aliases", None)
    assert "delete_collection" not in mutation_names


async def test_backend_details_are_collapsed_to_stable_redacted_result(
    local_qdrant,
    monkeypatch,
    profile,
) -> None:
    _fix_operation_id(monkeypatch)
    legacy_name = _seed_legacy(local_qdrant)
    _documents, objects, content_by_key = _source_bundle()
    secret = "https://private.invalid token=do-not-leak"
    store = VectorStoreService(
        client=local_qdrant,
        profile=profile,
        legacy_prefix="memory_",
    )
    result = await _service(
        graph=_FailingGraph(secret),
        storage=_FakeStorage(objects, content_by_key),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=store,
    ).reindex(_MEMORY_ID)

    assert result == {
        "status": "error",
        "phase": "snapshot",
        "reason": "source_inventory_unavailable",
        "operation_id": _OPERATION_ID,
        "source_documents": 0,
        "source_chunks": 0,
        "vectors_written": 0,
        "activated": False,
        "active_state": "reindex_required",
    }
    serialized = json.dumps(result, sort_keys=True)
    assert secret not in serialized
    assert profile.endpoint not in serialized
    assert legacy_name not in serialized
    assert canonical_qdrant_collection_name(_MEMORY_ID) not in serialized


async def test_cloud_temple_named_profile_migration_reindexes_then_ingests_and_queries(
    local_qdrant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the operator journey from a generic legacy identity to readiness.

    The provider rename is deliberately the *only* static identity change.  The
    test drives the real vector store, reindex service, hidden ``long_reindex``
    façade, bridge, internal Graph Memory tool, ingestion pipeline, and
    ``long_query`` read path.  Only provider/graph/S3 transports are replaced by
    deterministic in-process doubles; no inference endpoint is contacted.
    """

    from mcp.server.fastmcp import FastMCP

    from live_mem.core.engines.long_engine import LongEngine
    from live_mem.core.graph_bridge import GraphBridgeService
    from live_mem.core.memory_id import derive_memory_id
    from live_mem.core.models import EMBEDDED_TOKEN_SENTINEL
    from live_mem.tools import graph as graph_tools
    from tests.fakes.inference_fakes import apply_graph_memory_baseline_env

    apply_graph_memory_baseline_env(monkeypatch)

    from mcp_memory import server as graph_server
    from mcp_memory.core import ingest_pipeline
    from tests.fakes import GraphLongFakeStorage

    space_id = "p13-cloud-temple-migration"
    memory_id = derive_memory_id(space_id)
    endpoint = "https://api.ai.cloud-temple.com/v1"
    legacy_profile = make_embedding_profile(
        provider_id="openai-compatible",
        adapter_id="openai-compatible",
        endpoint=endpoint,
        expected_dimensions=3,
    )
    named_profile = make_embedding_profile(
        provider_id="cloud-temple",
        adapter_id="openai-compatible",
        endpoint=endpoint,
        expected_dimensions=3,
    )
    identity_probe = _embedding_result(["identity-probe"])
    legacy_identity = build_embedding_collection_identity(
        memory_id,
        legacy_profile,
        identity_probe,
    )
    named_identity = build_embedding_collection_identity(
        memory_id,
        named_profile,
        identity_probe,
    )
    legacy_mapping = legacy_identity.to_mapping()
    named_mapping = named_identity.to_mapping()
    assert {
        key
        for key in legacy_mapping
        if legacy_mapping[key] != named_mapping[key]
    } == {"provider_id", "profile_fingerprint"}
    assert legacy_mapping["provider_id"] == "openai-compatible"
    assert named_mapping["provider_id"] == "cloud-temple"

    tracker = _RecordingClient(local_qdrant)
    legacy_store = VectorStoreService(
        client=tracker,
        profile=legacy_profile,
        legacy_prefix="memory_",
    )
    legacy_text = "legacy generic provider projection"
    assert await legacy_store.store_chunks(
        memory_id,
        "legacy-doc",
        "legacy.txt",
        [
            Chunk(
                text=legacy_text,
                index=0,
                total_chunks=1,
                filename="legacy.txt",
            )
        ],
        embedding_result=_embedding_result([legacy_text]),
    ) == 1
    assert await legacy_store.get_collection_info(memory_id) == {
        "state": "ready",
        "profile_fingerprint": legacy_identity.profile_fingerprint,
        "points_count": 1,
    }

    named_store = VectorStoreService(
        client=tracker,
        profile=named_profile,
        legacy_prefix="memory_",
    )
    canonical_name = canonical_qdrant_collection_name(memory_id)
    before_collections = sorted(
        collection.name
        for collection in local_qdrant.get_collections().collections
    )
    before_aliases = _alias_map(local_qdrant)
    before_points = local_qdrant.count(
        collection_name=canonical_name,
        exact=True,
    ).count
    tracker.mutations.clear()

    assert await named_store.get_collection_info(memory_id) == {
        "state": "reindex_required",
        "reason": "static_profile_mismatch",
    }
    with pytest.raises(EmbeddingCollectionReindexRequired) as drift:
        await named_store.store_chunks(
            memory_id,
            "must-not-write",
            "blocked.txt",
            [Chunk(text="blocked", index=0, total_chunks=1)],
            embedding_result=_embedding_result(["blocked"]),
        )
    assert drift.value.reason == "static_profile_mismatch"
    assert tracker.mutations == []
    assert _alias_map(local_qdrant) == before_aliases == {}
    assert sorted(
        collection.name
        for collection in local_qdrant.get_collections().collections
    ) == before_collections == [canonical_name]
    assert local_qdrant.count(
        collection_name=canonical_name,
        exact=True,
    ).count == before_points == 1

    documents, objects, content_by_key = _source_bundle(memory_id)
    reindex_service = _service(
        graph=_FakeGraph(documents, memory_id=memory_id),
        storage=_FakeStorage(
            objects,
            content_by_key,
            memory_id=memory_id,
        ),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vectors=named_store,
    )
    _fix_operation_id(monkeypatch)

    binding_storage = GraphLongFakeStorage()
    embedded_url = "http://graph-memory:8002"
    await binding_storage.put_json(
        f"{space_id}/_meta.json",
        {
            "space_id": space_id,
            "version": 1,
            "graph_memory": {
                "binding": "embedded",
                "url": embedded_url,
                "token": EMBEDDED_TOKEN_SENTINEL,
                "memory_id": memory_id,
                "ontology": "general",
            },
        },
    )

    class _JourneyGraph:
        def __init__(self) -> None:
            self.documents: dict[str, dict] = {}

        async def get_document_by_hash(self, _memory_id: str, _digest: str):
            return None

        async def get_memory(self, requested_memory_id: str):
            assert requested_memory_id == memory_id
            return SimpleNamespace(ontology="general")

        async def add_document(self, **fields) -> None:
            self.documents[fields["doc_id"]] = dict(fields)

        async def add_entities_and_relations(self, **_fields) -> dict:
            return {
                "entities_created": 0,
                "entities_merged": 0,
                "relations_created": 0,
                "relations_merged": 0,
            }

        async def update_document_ingestion(
            self,
            *,
            doc_id: str,
            **fields,
        ) -> None:
            self.documents[doc_id].update(fields)

        async def search_entities(self, _memory_id, search_query, limit):
            assert search_query
            assert limit == 1
            return []

        async def get_documents_meta(self, _memory_id, doc_ids):
            return {
                doc_id: self.documents[doc_id]
                for doc_id in doc_ids
                if doc_id in self.documents
            }

        async def get_memory_stats(self, requested_memory_id: str):
            assert requested_memory_id == memory_id
            return SimpleNamespace(
                document_count=len(self.documents),
                entity_count=0,
                relation_count=0,
                top_entities=[],
            )

        async def get_full_graph(self, requested_memory_id: str):
            assert requested_memory_id == memory_id
            return {"documents": list(self.documents.values())}

    class _JourneyStorage:
        @staticmethod
        def compute_hash(content: bytes) -> str:
            return hashlib.sha256(content).hexdigest()

        async def upload_document(self, **fields) -> dict:
            return {
                "uri": f"s3://test-bucket/{memory_id}/{fields['filename']}",
                "size_bytes": len(fields["content"]),
            }

        async def delete_document(self, _memory_id: str, _uri: str) -> bool:
            return True

    class _JourneyExtractor:
        async def extract_with_ontology_chunked(self, *_args, **_kwargs):
            return SimpleNamespace(
                entities=[],
                relations=[],
                summary="",
                key_topics=[],
            )

    class _JourneyEmbedder(_FakeEmbedder):
        async def embed_query_result(self, query: str):
            return _embedding_result([query])

    journey_graph = _JourneyGraph()
    journey_storage = _JourneyStorage()
    journey_embedder = _JourneyEmbedder()
    journey_chunker = _FakeChunker()
    internal_auth: list[tuple[str, str | None]] = []

    def _gm_access(requested_memory_id: str):
        internal_auth.append(("access", requested_memory_id))
        return None

    def _gm_write():
        internal_auth.append(("write", None))
        return None

    monkeypatch.setattr(graph_server, "check_memory_access", _gm_access)
    monkeypatch.setattr(graph_server, "check_write_permission", _gm_write)
    monkeypatch.setattr(graph_server, "get_storage", lambda: journey_storage)
    monkeypatch.setattr(graph_server, "get_graph", lambda: journey_graph)
    monkeypatch.setattr(graph_server, "get_embedder", lambda: journey_embedder)
    monkeypatch.setattr(graph_server, "_vector_store", named_store)
    monkeypatch.setattr(
        graph_server,
        "settings",
        SimpleNamespace(
            max_document_size_bytes=1_000_000,
            rag_score_threshold=-1.0,
            rag_chunk_limit=1,
        ),
    )
    monkeypatch.setattr(
        reindex_module,
        "get_reindex_service",
        lambda: reindex_service,
    )
    monkeypatch.setattr(ingest_pipeline, "_graph", lambda: journey_graph)
    monkeypatch.setattr(ingest_pipeline, "_storage", lambda: journey_storage)
    monkeypatch.setattr(
        ingest_pipeline,
        "_extractor",
        lambda: _JourneyExtractor(),
    )
    monkeypatch.setattr(ingest_pipeline, "_chunker", lambda: journey_chunker)
    monkeypatch.setattr(ingest_pipeline, "_embedder", lambda: journey_embedder)
    monkeypatch.setattr(ingest_pipeline, "_vector_store", lambda: named_store)
    monkeypatch.setattr(
        ingest_pipeline,
        "get_settings",
        lambda: SimpleNamespace(),
    )

    dispatches: list[tuple[str, dict]] = []

    class _InProcessGraphMemoryClient:
        def __init__(self, url: str, token: str, **_kwargs) -> None:
            assert url == embedded_url
            assert token == "internal-read-write-token"

        async def call_tool(self, tool_name: str, arguments: dict) -> dict:
            dispatches.append((tool_name, dict(arguments)))
            if tool_name == "memory_reindex":
                return await graph_server.memory_reindex(**arguments)
            if tool_name == "memory_ingest":
                return await graph_server.memory_ingest(**arguments)
            if tool_name == "memory_query":
                return await graph_server.memory_query(**arguments)
            raise AssertionError(f"unexpected in-process tool: {tool_name}")

        async def call_tools_batch(self, calls: list[tuple[str, dict]]) -> list[dict]:
            results = []
            for tool_name, arguments in calls:
                dispatches.append((tool_name, dict(arguments)))
                if tool_name == "memory_stats":
                    results.append(await graph_server.memory_stats(**arguments))
                elif tool_name == "document_list":
                    results.append(await graph_server.document_list(**arguments))
                else:
                    raise AssertionError(
                        f"unexpected in-process batch tool: {tool_name}"
                    )
            return results

    settings = SimpleNamespace(
        long_embedded_url=embedded_url,
        long_embedded_token="unused",
        long_embedded_token_file="/does/not/exist",
    )
    from live_mem.core import graph_bridge as graph_bridge_module

    monkeypatch.setattr(
        graph_bridge_module,
        "get_storage",
        lambda: binding_storage,
    )
    monkeypatch.setattr(graph_bridge_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        graph_bridge_module,
        "resolve_embedded_token",
        lambda *_args, **_kwargs: "internal-read-write-token",
    )
    bridge = GraphBridgeService(
        client_factory=_InProcessGraphMemoryClient,
        url_validator=lambda _url, **_kwargs: None,
    )
    engine = LongEngine(bridge=bridge)

    class _Registry:
        @staticmethod
        def long_engine():
            return engine

    outer_auth: list[tuple[str, str | None]] = []

    def _outer_access(requested_space_id: str):
        outer_auth.append(("access", requested_space_id))
        return None

    def _outer_manage():
        outer_auth.append(("manage", None))
        return None

    monkeypatch.setattr("live_mem.auth.context.check_access", _outer_access)
    monkeypatch.setattr(
        "live_mem.auth.context.check_manage_permission",
        _outer_manage,
    )
    monkeypatch.setattr(
        "live_mem.core.engines.get_engine_registry",
        lambda: _Registry(),
    )
    tools = FastMCP(name="p13-cloud-temple-migration")
    graph_tools.register(tools)

    status_before = await tools._tool_manager._tools["graph_status"].fn(
        space_id=space_id,
        include_graph=False,
    )
    assert status_before["status"] == "ok"
    assert status_before["reachable"] is True
    assert status_before["embedding_collection"] == {
        "state": "reindex_required",
        "reason": "static_profile_mismatch",
    }

    reindex_result = await tools._tool_manager._tools["long_reindex"].fn(
        space_id=space_id
    )

    assert reindex_result == {
        "status": "ok",
        "phase": "verified",
        "reason": None,
        "operation_id": _OPERATION_ID,
        "source_documents": 2,
        "source_chunks": 8,
        "vectors_written": 8,
        "activated": True,
        "active_state": "ready",
    }
    status_after = await tools._tool_manager._tools["graph_status"].fn(
        space_id=space_id,
        include_graph=False,
    )
    assert status_after["status"] == "ok"
    assert status_after["reachable"] is True
    assert status_after["embedding_collection"] == {
        "state": "ready",
        "profile_fingerprint": named_identity.profile_fingerprint,
        "points_count": 8,
    }
    assert await named_store.get_collection_info(memory_id) == {
        "state": "ready",
        "profile_fingerprint": named_identity.profile_fingerprint,
        "points_count": 8,
    }
    alias_name = named_store._active_alias_name(memory_id)
    active_target = _alias_map(local_qdrant)[alias_name]
    assert active_target == named_store._shadow_name(memory_id, _OPERATION_ID)
    assert local_qdrant.count(collection_name=canonical_name, exact=True).count == 1

    post_migration_text = "post-migration unique evidence"
    ingest_result = await engine.ingest(
        space_id,
        filename="post-migration.txt",
        content=post_migration_text,
        source_path="evidence/post-migration.txt",
    )
    assert ingest_result["status"] == "ok"
    assert ingest_result["chunks_stored"] == 1
    assert await named_store.get_collection_info(memory_id) == {
        "state": "ready",
        "profile_fingerprint": named_identity.profile_fingerprint,
        "points_count": 9,
    }
    assert local_qdrant.count(collection_name=active_target, exact=True).count == 9
    assert local_qdrant.count(collection_name=canonical_name, exact=True).count == 1
    assert graph_server.get_vector_store() is named_store
    assert _alias_map(local_qdrant) == {alias_name: active_target}
    query_text = "find post migration evidence m"
    direct_query = await named_store.search(
        memory_id,
        embedding_result=_embedding_result([query_text]),
        limit=1,
    )
    assert len(direct_query) == 1
    assert direct_query[0].chunk.text == post_migration_text

    query_result = await tools._tool_manager._tools["long_query"].fn(
        space_id=space_id,
        query=query_text,
        limit=1,
    )

    assert query_result["status"] == "ok", query_result
    assert query_result["retrieval_mode"] == "rag-only"
    assert query_result["stats"]["rag_chunks_retained"] == 1
    assert len(query_result["rag_chunks"]) == 1
    queried_chunk = query_result["rag_chunks"][0]
    assert queried_chunk["text"] == post_migration_text
    assert queried_chunk["doc_id"] == ingest_result["document_id"]
    assert queried_chunk["filename"] == "post-migration.txt"
    assert queried_chunk["source_path"] == "evidence/post-migration.txt"
    assert queried_chunk["repo_path"] is None
    assert 0.99 < queried_chunk["score"] <= 1.0
    assert [tool_name for tool_name, _arguments in dispatches] == [
        "memory_stats",
        "document_list",
        "memory_reindex",
        "memory_stats",
        "document_list",
        "memory_ingest",
        "memory_query",
    ]
    assert all(
        arguments["memory_id"] == memory_id
        for _tool_name, arguments in dispatches
    )
    assert outer_auth == [
        ("access", space_id),
        ("access", space_id),
        ("manage", None),
        ("access", space_id),
        ("access", space_id),
    ]
    assert internal_auth == [
        ("access", memory_id),
        ("access", memory_id),
        ("access", memory_id),
        ("write", None),
        ("access", memory_id),
        ("access", memory_id),
        ("access", memory_id),
        ("write", None),
        ("access", memory_id),
    ]
