# -*- coding: utf-8 -*-
"""Integration locks for the shared P13 per-memory maintenance boundary.

These tests exercise the real public mutation wrappers.  Backend doubles record
the first reachable I/O seam so a maintenance refusal proves that admission
happened before any backend contact.  Events make every concurrency ordering
explicit; the suite intentionally contains no timing sleeps.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import tarfile
import types
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Awaitable, Callable

import pytest

from mcp_memory.core.maintenance import (
    MaintenanceAdmissionError,
    MaintenanceRejectionReason,
    get_maintenance_coordinator,
    reset_maintenance_coordinator_for_tests,
)


MEMORY_A = "memory-a"
MEMORY_B = "memory-b"


@pytest.fixture(autouse=True)
def _fresh_shared_coordinator(monkeypatch):
    from tests.fakes.inference_fakes import apply_graph_memory_baseline_env

    # Graph Memory constructs Settings at module import time.
    apply_graph_memory_baseline_env(monkeypatch)
    reset_maintenance_coordinator_for_tests()
    try:
        yield
    finally:
        reset_maintenance_coordinator_for_tests()


async def _exercise_active_maintenance(
    *,
    owner_call: Callable[[], Awaitable[object]],
    rejected_call: Callable[[], Awaitable[object]],
    other_call: Callable[[], Awaitable[object]],
    effects: list[object],
    assert_rejected: Callable[[object], None] | None = None,
) -> tuple[object, object]:
    """Prove owner nesting, same-memory refusal, and other-memory progress."""

    coordinator = get_maintenance_coordinator()
    owner_finished = asyncio.Event()
    release_owner = asyncio.Event()
    owner_results: list[object] = []
    owner_errors: list[BaseException] = []

    async def hold_maintenance() -> None:
        async with coordinator.maintenance(MEMORY_A):
            try:
                owner_results.append(await owner_call())
            except BaseException as error:  # surface failures without deadlock
                owner_errors.append(error)
            finally:
                owner_finished.set()
            if not owner_errors:
                await release_owner.wait()

    holder = asyncio.create_task(hold_maintenance())
    await owner_finished.wait()
    if owner_errors:
        await holder
        raise owner_errors[0]

    assert effects, "the maintenance owner must reach the fake backend"
    effects_after_owner = list(effects)
    try:
        if assert_rejected is None:
            with pytest.raises(MaintenanceAdmissionError) as exc_info:
                await rejected_call()
            assert (
                exc_info.value.reason
                == MaintenanceRejectionReason.MAINTENANCE_ACTIVE
            )
        else:
            assert_rejected(await rejected_call())

        # The rejected path must stop before the first fake backend call.
        assert effects == effects_after_owner

        before_other = len(effects)
        other_result = await other_call()
        assert len(effects) > before_other
    finally:
        release_owner.set()
        await holder

    return owner_results[0], other_result


async def test_run_ingest_pipeline_is_guarded_before_its_first_backend_read(
    monkeypatch,
) -> None:
    from mcp_memory.core import ingest_pipeline

    effects: list[object] = []

    class Graph:
        async def get_memory(self, memory_id: str):
            effects.append(("graph.get_memory", memory_id))
            return None

    monkeypatch.setattr(ingest_pipeline, "_graph", lambda: Graph())
    monkeypatch.setattr(
        ingest_pipeline,
        "get_settings",
        lambda: SimpleNamespace(),
    )

    async def call(memory_id: str):
        return await ingest_pipeline.run_ingest_pipeline(
            memory_id=memory_id,
            content=b"document",
            filename="document.txt",
            doc_hash="sha256",
        )

    owner, other = await _exercise_active_maintenance(
        owner_call=lambda: call(MEMORY_A),
        rejected_call=lambda: call(MEMORY_A),
        other_call=lambda: call(MEMORY_B),
        effects=effects,
    )

    assert owner["status"] == "error"
    assert other["status"] == "error"
    assert effects == [
        ("graph.get_memory", MEMORY_A),
        ("graph.get_memory", MEMORY_B),
    ]


async def test_delete_document_everywhere_is_guarded_before_qdrant(
    monkeypatch,
) -> None:
    from mcp_memory.core import ingest_pipeline

    effects: list[object] = []

    class Vectors:
        async def delete_document_chunks(self, memory_id: str, doc_id: str):
            effects.append(("qdrant.delete", memory_id, doc_id))
            return 2

    class Graph:
        async def delete_document(self, memory_id: str, doc_id: str):
            effects.append(("neo4j.delete", memory_id, doc_id))
            return {
                "deleted": True,
                "entities_deleted": 1,
                "relations_deleted": 3,
            }

    class Storage:
        async def delete_document(self, memory_id: str, uri: str):
            effects.append(("s3.delete", memory_id, uri))
            return True

    monkeypatch.setattr(ingest_pipeline, "_vector_store", lambda: Vectors())
    monkeypatch.setattr(ingest_pipeline, "_graph", lambda: Graph())
    monkeypatch.setattr(ingest_pipeline, "_storage", lambda: Storage())

    async def call(memory_id: str):
        return await ingest_pipeline.delete_document_everywhere(
            memory_id,
            "doc-1",
            uri=f"s3://bucket/{memory_id}/doc-1",
        )

    owner, other = await _exercise_active_maintenance(
        owner_call=lambda: call(MEMORY_A),
        rejected_call=lambda: call(MEMORY_A),
        other_call=lambda: call(MEMORY_B),
        effects=effects,
    )

    assert owner["qdrant_chunks_deleted"] == 2
    assert owner["neo4j_deleted"] is True
    assert other["s3_deleted"] is True
    assert [effect[1] for effect in effects] == [
        MEMORY_A,
        MEMORY_A,
        MEMORY_A,
        MEMORY_B,
        MEMORY_B,
        MEMORY_B,
    ]


async def test_queue_admission_closes_while_idle_check_is_pending_and_tracks_idle(
    monkeypatch,
) -> None:
    from mcp_memory.core import ingest_queue

    monkeypatch.setattr(
        ingest_queue,
        "get_settings",
        lambda: SimpleNamespace(
            ingest_max_history=100,
            ingest_max_queued_per_memory=10,
            ingest_max_queued_bytes=10_000,
        ),
    )
    queue = ingest_queue.IngestQueueService()
    monkeypatch.setattr(queue, "_ensure_worker_locked", lambda _memory_id: None)

    resolver_calls: list[str] = []

    async def resolve(
        memory_id: str,
        source_path: str | None,
        _sha256: str,
        _replace_existing: bool,
    ) -> dict:
        resolver_calls.append(memory_id)
        return {
            "action": "ingest",
            "existing": None,
            "norm_source_path": source_path or "document.txt",
        }

    monkeypatch.setattr(ingest_queue, "resolve_ingestion", resolve)

    async def submit(memory_id: str, suffix: str) -> dict:
        return await queue.submit(
            memory_id=memory_id,
            content=b"payload",
            filename=f"{suffix}.txt",
            sha256=f"sha-{suffix}",
            source_path=f"{suffix}.txt",
            replace_existing=False,
            job_id=f"job-{suffix}",
        )

    assert await queue.is_idle_for_memory(MEMORY_A) is True
    assert await queue.is_idle_for_memory(MEMORY_B) is True

    coordinator = get_maintenance_coordinator()
    idle_entered = asyncio.Event()
    release_idle = asyncio.Event()
    maintenance_active = asyncio.Event()
    release_maintenance = asyncio.Event()
    owner_results: list[dict] = []

    async def idle_check() -> bool:
        idle_entered.set()
        await release_idle.wait()
        return await queue.is_idle_for_memory(MEMORY_A)

    async def maintain_and_submit_as_owner() -> None:
        async with coordinator.maintenance(MEMORY_A, idle_check=idle_check):
            owner_results.append(await submit(MEMORY_A, "owner-a"))
            maintenance_active.set()
            await release_maintenance.wait()

    holder = asyncio.create_task(maintain_and_submit_as_owner())
    await idle_entered.wait()
    try:
        with pytest.raises(MaintenanceAdmissionError) as requested_exc:
            await submit(MEMORY_A, "rejected-requested-a")
        assert (
            requested_exc.value.reason
            == MaintenanceRejectionReason.MAINTENANCE_REQUESTED
        )
        assert resolver_calls == []

        # A distinct namespace remains admitted while A's idle check waits.
        other_requested = await submit(MEMORY_B, "other-b")
        assert other_requested["status"] == "running"
        assert resolver_calls == [MEMORY_B]

        release_idle.set()
        await maintenance_active.wait()
        assert owner_results[0]["status"] == "running"
        assert resolver_calls == [MEMORY_B, MEMORY_A]

        calls_before_rejection = list(resolver_calls)
        with pytest.raises(MaintenanceAdmissionError) as active_exc:
            await submit(MEMORY_A, "rejected-active-a")
        assert (
            active_exc.value.reason
            == MaintenanceRejectionReason.MAINTENANCE_ACTIVE
        )
        assert resolver_calls == calls_before_rejection

        # Unrelated admission also remains live during the active phase.
        other_active = await submit("memory-c", "other-c")
        assert other_active["status"] == "running"
        assert resolver_calls[-1] == "memory-c"

        assert await queue.is_idle_for_memory(MEMORY_A) is False
        assert await queue.is_idle_for_memory(MEMORY_B) is False
        assert await queue.is_idle_for_memory("memory-d") is True
    finally:
        release_idle.set()
        release_maintenance.set()
        await holder

    # Every worker-visible deque entry is non-idle, including a stale terminal
    # or orphan record. Corrupted queue bookkeeping must fail closed.
    owner_job_id = owner_results[0]["job_id"]
    async with queue._state_lock:
        queue._active_jobs.pop(MEMORY_A)
        queue._queues[MEMORY_A].append(owner_job_id)
        queue._jobs[owner_job_id].status = "queued"
    assert await queue.is_idle_for_memory(MEMORY_A) is False
    async with queue._state_lock:
        queue._jobs[owner_job_id].status = "succeeded"
    assert await queue.is_idle_for_memory(MEMORY_A) is False
    async with queue._state_lock:
        queue._jobs.pop(owner_job_id)
    assert await queue.is_idle_for_memory(MEMORY_A) is False
    async with queue._state_lock:
        queue._queues[MEMORY_A].clear()
    assert await queue.is_idle_for_memory(MEMORY_A) is True


async def test_production_reindex_factory_wires_queue_idle_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting the production queue wiring must expose backend contact."""
    from mcp_memory.core import chunker as chunker_module
    from mcp_memory.core import embedder as embedder_module
    from mcp_memory.core import ingest_queue as queue_module
    from mcp_memory.core import reindex as reindex_module
    from mcp_memory.core import storage as storage_module
    from mcp_memory.core import vector_store as vector_module

    graph_module = types.ModuleType("mcp_memory.core.graph")
    graph_module.get_graph_service = object
    server_module = types.ModuleType("mcp_memory.server")
    server_module._extract_text = lambda content, _filename: content.decode("utf-8")
    monkeypatch.setitem(sys.modules, "mcp_memory.core.graph", graph_module)
    monkeypatch.setitem(sys.modules, "mcp_memory.server", server_module)

    events: list[str] = []

    class Queue:
        async def is_idle_for_memory(self, memory_id: str) -> bool:
            events.append(f"idle:{memory_id}")
            return False

    class Vectors:
        async def inspect_reindex_state(self, memory_id: str):
            events.append(f"backend:{memory_id}")
            raise AssertionError("queue admission was bypassed")

    monkeypatch.setattr(queue_module, "get_ingest_queue", lambda: Queue())
    monkeypatch.setattr(storage_module, "get_storage_service", object)
    monkeypatch.setattr(chunker_module, "get_chunker", object)
    monkeypatch.setattr(embedder_module, "get_embedding_service", object)
    monkeypatch.setattr(vector_module, "get_vector_store", Vectors)
    reindex_module.reset_reindex_service_for_tests()
    try:
        result = await reindex_module.get_reindex_service().reindex(MEMORY_A)
    finally:
        reindex_module.reset_reindex_service_for_tests()

    assert result["status"] == "error"
    assert result["phase"] == "admission"
    assert result["reason"] == "namespace_busy"
    assert result["activated"] is False
    assert events == [f"idle:{MEMORY_A}"]


@pytest.mark.parametrize(
    "operation",
    [
        "store_chunks",
        "delete_collection",
        "delete_document_chunks",
        "import_collection",
    ],
)
async def test_vector_mutations_share_one_gate_across_service_instances(
    operation: str,
) -> None:
    from hivemind_inference import EmbeddingResult
    from hivemind_inference.collection_identity import (
        build_embedding_collection_identity,
    )
    from mcp_memory.core.models import Chunk
    from mcp_memory.core.vector_store import VectorStoreService, _ResolvedCollection
    from tests.fakes.inference_fakes import make_embedding_profile

    effects: list[object] = []
    profile = make_embedding_profile(expected_dimensions=3)
    embedding_result = EmbeddingResult(
        vectors=((1.0, 0.0, 0.0),),
        configured_model=profile.configured_model,
        resolved_model="provider-model",
        model_evidence="provider_reported",
        effective_dimensions=3,
    )

    class Client:
        def __init__(self, label: str):
            self.label = label

        def upsert(self, *, collection_name, points, wait):
            effects.append((self.label, "upsert", collection_name, len(points), wait))

        def count(self, *, collection_name, count_filter, exact):
            effects.append((self.label, "count", collection_name, exact))
            return SimpleNamespace(count=1)

        def delete(self, *, collection_name, points_selector, wait):
            effects.append((self.label, "delete", collection_name, wait))

        def delete_collection(self, *, collection_name):
            effects.append((self.label, "delete_collection", collection_name))

    def make_store(label: str) -> VectorStoreService:
        service = VectorStoreService(
            client=Client(label),
            profile=profile,
            legacy_prefix="memory_",
        )

        def resolve(memory_id: str, *, result=None, create_identity=None):
            identity = create_identity or build_embedding_collection_identity(
                memory_id,
                profile,
                result or embedding_result,
            )
            return _ResolvedCollection(
                name=f"collection-{memory_id}",
                identity=identity,
                points_count=0,
            )

        service._resolve_collection = resolve
        service._validate_owner = lambda *_args, **_kwargs: None
        return service

    first = make_store("first")
    second = make_store("second")

    async def invoke(service: VectorStoreService, memory_id: str):
        if operation == "store_chunks":
            return await service.store_chunks(
                memory_id,
                "doc-1",
                "document.txt",
                [
                    Chunk(
                        text="document",
                        index=0,
                        total_chunks=1,
                        char_count=8,
                        token_estimate=1,
                    )
                ],
                embedding_result=embedding_result,
            )
        if operation == "delete_collection":
            return await service.delete_collection(memory_id)
        if operation == "delete_document_chunks":
            return await service.delete_document_chunks(memory_id, "doc-1")
        identity = build_embedding_collection_identity(
            memory_id,
            profile,
            embedding_result,
        )
        return await service.import_collection(
            memory_id,
            [
                {
                    "id": 1,
                    "vector": [1.0, 0.0, 0.0],
                    "payload": {"memory_id": memory_id, "doc_id": "doc-1"},
                }
            ],
            identity=identity.to_mapping(),
        )

    owner, other = await _exercise_active_maintenance(
        owner_call=lambda: invoke(first, MEMORY_A),
        # A different instance must still observe A's shared maintenance gate.
        rejected_call=lambda: invoke(second, MEMORY_A),
        other_call=lambda: invoke(second, MEMORY_B),
        effects=effects,
    )

    assert owner in (1, True)
    assert other in (1, True)
    assert effects[0][0] == "first"
    assert effects[-1][0] == "second"
    assert not any(
        effect[0] == "second" and MEMORY_A in str(effect)
        for effect in effects
    )
    assert any(
        effect[0] == "second" and MEMORY_B in str(effect)
        for effect in effects
    )


class _BackendReached(Exception):
    """Stop a real method exactly when its first backend seam is entered."""


@pytest.mark.parametrize(
    "method_name",
    [
        "create_memory",
        "update_memory",
        "delete_memory",
        "add_document",
        "update_document_ingestion",
        "delete_document",
        "add_entities_and_relations",
        "import_memory_data",
    ],
)
async def test_every_graph_mutation_wrapper_gates_before_opening_neo4j(
    method_name: str,
    monkeypatch,
) -> None:
    # Neo4j is a service-runtime dependency, not a root Hivemind test
    # dependency.  A minimal import shim keeps this an I/O-free wrapper test.
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
    neo4j_exceptions.ServiceUnavailable = type("ServiceUnavailable", (Exception,), {})
    neo4j_exceptions.AuthError = type("AuthError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "neo4j", neo4j)
    monkeypatch.setitem(sys.modules, "neo4j.exceptions", neo4j_exceptions)

    from mcp_memory.core.graph import GraphService

    effects: list[object] = []
    service = object.__new__(GraphService)
    service._doc_constraints_ready = True
    service._doc_source_normalization_done = True
    service._doc_migration_marker_ready = True
    current_memory = {"id": ""}

    @asynccontextmanager
    async def session():
        effects.append((method_name, "session", current_memory["id"]))
        raise _BackendReached
        yield None  # pragma: no cover - keeps this an async context manager

    async def get_memory(memory_id: str):
        effects.append((method_name, "get_memory", memory_id))
        raise _BackendReached

    service.session = session
    service.get_memory = get_memory

    async def invoke(memory_id: str):
        current_memory["id"] = memory_id
        try:
            if method_name == "create_memory":
                await service.create_memory(memory_id, "Name")
            elif method_name == "update_memory":
                await service.update_memory(memory_id, name="Updated")
            elif method_name == "delete_memory":
                await service.delete_memory(memory_id)
            elif method_name == "add_document":
                await service.add_document(
                    memory_id,
                    "doc-1",
                    "s3://bucket/doc-1",
                    "document.txt",
                    "sha256",
                )
            elif method_name == "update_document_ingestion":
                await service.update_document_ingestion(
                    memory_id,
                    "doc-1",
                    "succeeded",
                )
            elif method_name == "delete_document":
                await service.delete_document(memory_id, "doc-1")
            elif method_name == "add_entities_and_relations":
                await service.add_entities_and_relations(
                    memory_id,
                    "doc-1",
                    SimpleNamespace(entities=[], relations=[]),
                )
            else:
                await service.import_memory_data({"memory": {"id": memory_id}})
        except _BackendReached:
            return "backend-reached"
        raise AssertionError("the fake Neo4j boundary was not reached")

    owner, other = await _exercise_active_maintenance(
        owner_call=lambda: invoke(MEMORY_A),
        rejected_call=lambda: invoke(MEMORY_A),
        other_call=lambda: invoke(MEMORY_B),
        effects=effects,
    )

    assert owner == "backend-reached"
    assert other == "backend-reached"
    assert effects == [
        (method_name, "get_memory" if method_name == "import_memory_data" else "session", MEMORY_A),
        (method_name, "get_memory" if method_name == "import_memory_data" else "session", MEMORY_B),
    ]


def _archive_for(memory_id: str) -> bytes:
    manifest = json.dumps(
        {
            "version": "1.0",
            "memory_id": memory_id,
            "stats": {"entities": 0, "qdrant_vectors": 0},
        }
    ).encode("utf-8")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("backup/manifest.json")
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
    return buffer.getvalue()


@pytest.mark.parametrize("method_name", ["restore_backup", "restore_from_archive"])
async def test_backup_restore_wrappers_gate_before_first_backend_effect(
    method_name: str,
) -> None:
    from mcp_memory.core.backup import BackupService

    effects: list[object] = []
    service = object.__new__(BackupService)
    service._prefix = "_backups"

    class StorageClient:
        def get_object(self, *, Bucket, Key):
            del Bucket
            memory_id = Key.split("/", 2)[1]
            effects.append(("restore_backup", memory_id))
            raise _BackendReached

    class Graph:
        async def get_memory(self, memory_id: str):
            effects.append(("restore_from_archive", memory_id))
            raise _BackendReached

    service._storage = SimpleNamespace(
        _bucket="bucket",
        _client=StorageClient(),
    )
    service._graph = Graph()

    async def invoke(memory_id: str):
        try:
            if method_name == "restore_backup":
                await service.restore_backup(f"{memory_id}/2026-08-02")
            else:
                await service.restore_from_archive(_archive_for(memory_id))
        except (FileNotFoundError, _BackendReached):
            return {"status": "restored", "memory_id": memory_id}
        raise AssertionError("the fake backup backend boundary was not reached")

    owner, other = await _exercise_active_maintenance(
        owner_call=lambda: invoke(MEMORY_A),
        rejected_call=lambda: invoke(MEMORY_A),
        other_call=lambda: invoke(MEMORY_B),
        effects=effects,
    )

    assert owner == {"status": "restored", "memory_id": MEMORY_A}
    assert other == {"status": "restored", "memory_id": MEMORY_B}
    assert [effect[1] for effect in effects] == [MEMORY_A, MEMORY_B]


@pytest.mark.parametrize(
    "tool_name",
    ["memory_create", "memory_update", "memory_delete"],
)
async def test_server_memory_tools_hold_the_outer_multi_backend_gate(
    tool_name: str,
    monkeypatch,
) -> None:
    from tests.fakes.inference_fakes import apply_graph_memory_baseline_env

    apply_graph_memory_baseline_env(monkeypatch)
    from mcp_memory import server
    from mcp_memory.core import ontology as ontology_module

    effects: list[object] = []

    monkeypatch.setattr(server, "check_memory_access", lambda _memory_id: None)
    monkeypatch.setattr(server, "check_write_permission", lambda: None)

    class Ontologies:
        def get_ontology(self, name: str):
            return {"name": name, "entity_types": []}

        def list_ontologies(self):
            return [{"name": "test"}]

    class Storage:
        async def upload_document(self, *, memory_id, filename, content, metadata):
            effects.append(("s3.upload", memory_id, filename))
            return {
                "uri": f"s3://bucket/{memory_id}/{filename}",
                "size_bytes": len(content),
            }

        async def delete_prefix(self, prefix: str):
            assert prefix.endswith("/")
            memory_id = prefix.removesuffix("/")
            effects.append(("s3.delete_prefix", memory_id))
            return {"deleted_count": 1, "error_count": 0}

    class Graph:
        async def create_memory(self, *, memory_id, name, description, ontology, ontology_uri):
            effects.append(("neo4j.create_memory", memory_id))
            return SimpleNamespace(
                id=memory_id,
                name=name,
                description=description,
                ontology=ontology,
            )

        async def delete_memory(self, memory_id: str):
            effects.append(("neo4j.delete_memory", memory_id))
            return True

        async def update_memory(self, memory_id: str, *, name, description):
            effects.append(("neo4j.update_memory", memory_id))
            return SimpleNamespace(
                id=memory_id,
                name=name,
                description=description,
                ontology="test",
            )

    class Vectors:
        async def delete_collection(self, memory_id: str):
            effects.append(("qdrant.delete_collection", memory_id))
            return True

    monkeypatch.setattr(ontology_module, "get_ontology_manager", lambda: Ontologies())
    monkeypatch.setattr(server, "get_storage", lambda: Storage())
    monkeypatch.setattr(server, "get_graph", lambda: Graph())
    monkeypatch.setattr(server, "get_vector_store", lambda: Vectors())

    async def invoke(memory_id: str):
        if tool_name == "memory_create":
            return await server.memory_create(
                memory_id,
                name=f"Name {memory_id}",
                ontology="test",
            )
        if tool_name == "memory_update":
            return await server.memory_update(
                memory_id,
                name=f"Name {memory_id}",
            )
        return await server.memory_delete(memory_id)

    def assert_rejected(result: object) -> None:
        assert result == {
            "status": "error",
            "message": "Namespace maintenance is active",
        }

    auth_token = server.current_auth.set(None)
    try:
        owner, other = await _exercise_active_maintenance(
            owner_call=lambda: invoke(MEMORY_A),
            rejected_call=lambda: invoke(MEMORY_A),
            other_call=lambda: invoke(MEMORY_B),
            effects=effects,
            assert_rejected=assert_rejected,
        )
    finally:
        server.current_auth.reset(auth_token)

    expected_status = {
        "memory_create": "created",
        "memory_update": "ok",
        "memory_delete": "deleted",
    }[tool_name]
    assert owner["status"] == expected_status
    assert other["status"] == expected_status
    assert all(effect[1] in {MEMORY_A, MEMORY_B} for effect in effects)
    assert [effect[1] for effect in effects].count(MEMORY_A) == len(effects) // 2
    assert [effect[1] for effect in effects].count(MEMORY_B) == len(effects) // 2


async def test_memory_delete_rejects_reserved_namespace_before_backend_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_memory import server

    def unexpected_backend():
        raise AssertionError("backend resolved for an invalid memory_id")

    monkeypatch.setattr(server, "check_memory_access", unexpected_backend)
    monkeypatch.setattr(server, "get_vector_store", unexpected_backend)
    monkeypatch.setattr(server, "get_storage", unexpected_backend)
    monkeypatch.setattr(server, "get_graph", unexpected_backend)

    result = await server.memory_delete("_system")

    assert result == {"status": "error", "message": "Invalid memory_id"}


async def test_memory_delete_fails_closed_before_s3_or_graph_for_active_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_memory import server
    from mcp_memory.core.vector_store import EmbeddingCollectionUnavailable

    effects: list[str] = []

    monkeypatch.setattr(server, "check_memory_access", lambda _memory_id: None)
    monkeypatch.setattr(server, "check_write_permission", lambda: None)

    class Vectors:
        async def delete_collection(self, memory_id: str) -> bool:
            assert memory_id == MEMORY_A
            effects.append("qdrant.delete_collection")
            raise EmbeddingCollectionUnavailable(
                "active_alias_delete_unsupported"
            )

    def unexpected_storage():
        effects.append("s3.resolve")
        raise AssertionError("S3 must remain untouched after vector refusal")

    def unexpected_graph():
        effects.append("neo4j.resolve")
        raise AssertionError("Neo4j must remain untouched after vector refusal")

    monkeypatch.setattr(server, "get_vector_store", lambda: Vectors())
    monkeypatch.setattr(server, "get_storage", unexpected_storage)
    monkeypatch.setattr(server, "get_graph", unexpected_graph)

    auth_token = server.current_auth.set(None)
    try:
        result = await server.memory_delete(MEMORY_A)
    finally:
        server.current_auth.reset(auth_token)

    assert result["status"] == "error"
    assert "active_alias_delete_unsupported" in result["message"]
    assert effects == ["qdrant.delete_collection"]


@pytest.mark.parametrize("denied_gate", [None, "access", "write"])
async def test_internal_memory_reindex_authorizes_before_service_resolution(
    denied_gate: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_memory import server
    from mcp_memory.core import reindex as reindex_module

    events: list[str] = []
    denied = {
        "status": "error",
        "message": "denied secret=https://internal.invalid/token",
    }
    admission_denied = {
        "status": "error",
        "phase": "admission",
        "reason": "maintenance_unavailable",
        "operation_id": None,
        "source_documents": 0,
        "source_chunks": 0,
        "vectors_written": 0,
        "activated": False,
        "active_state": "unavailable",
    }
    expected = {
        "status": "ok",
        "phase": "verified",
        "reason": None,
        "operation_id": "a" * 32,
        "source_documents": 1,
        "source_chunks": 1,
        "vectors_written": 1,
        "activated": True,
        "active_state": "ready",
    }

    def validate(memory_id: str) -> None:
        events.append(f"validate:{memory_id}")

    def access(memory_id: str):
        events.append(f"access:{memory_id}")
        return denied if denied_gate == "access" else None

    def write():
        events.append("write")
        return denied if denied_gate == "write" else None

    class Service:
        async def reindex(self, memory_id: str):
            events.append(f"reindex:{memory_id}")
            return expected

    def service():
        events.append("service")
        return Service()

    monkeypatch.setattr(server, "validate_memory_id", validate)
    monkeypatch.setattr(server, "check_memory_access", access)
    monkeypatch.setattr(server, "check_write_permission", write)
    monkeypatch.setattr(reindex_module, "get_reindex_service", service)

    result = await server.memory_reindex(MEMORY_A)

    if denied_gate is None:
        assert result == expected
        assert events == [
            f"validate:{MEMORY_A}",
            f"access:{MEMORY_A}",
            "write",
            "service",
            f"reindex:{MEMORY_A}",
        ]
    elif denied_gate == "access":
        assert result == admission_denied
        assert set(result) == set(admission_denied)
        assert "internal.invalid" not in json.dumps(result)
        assert events == [f"validate:{MEMORY_A}", f"access:{MEMORY_A}"]
    else:
        assert result == admission_denied
        assert set(result) == set(admission_denied)
        assert "internal.invalid" not in json.dumps(result)
        assert events == [
            f"validate:{MEMORY_A}",
            f"access:{MEMORY_A}",
            "write",
        ]


async def test_internal_memory_reindex_normalizes_service_resolution_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_memory import server
    from mcp_memory.core import reindex as reindex_module

    expected = {
        "status": "error",
        "phase": "admission",
        "reason": "maintenance_unavailable",
        "operation_id": None,
        "source_documents": 0,
        "source_chunks": 0,
        "vectors_written": 0,
        "activated": False,
        "active_state": "unavailable",
    }

    monkeypatch.setattr(server, "validate_memory_id", lambda _memory_id: None)
    monkeypatch.setattr(server, "check_memory_access", lambda _memory_id: None)
    monkeypatch.setattr(server, "check_write_permission", lambda: None)

    def fail_resolution():
        raise RuntimeError("secret=https://internal.invalid/token=planted")

    monkeypatch.setattr(reindex_module, "get_reindex_service", fail_resolution)

    result = await server.memory_reindex(MEMORY_A)

    assert result == expected
    assert set(result) == set(expected)
    assert "internal.invalid" not in json.dumps(result)
    assert "planted" not in json.dumps(result)


async def test_archive_restore_auth_uses_nested_manifest_before_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_memory import server

    events: list[str] = []
    denied = {"status": "error", "message": "denied"}
    archive = _archive_for(MEMORY_A)

    monkeypatch.setattr(server, "check_write_permission", lambda: None)

    def access(memory_id: str):
        events.append(f"access:{memory_id}")
        return denied

    def unexpected_backup():
        raise AssertionError("restore service resolved before archive access")

    monkeypatch.setattr(server, "check_memory_access", access)
    monkeypatch.setattr(server, "get_backup", unexpected_backup)

    result = await server.backup_restore_archive(
        base64.b64encode(archive).decode("ascii")
    )

    assert result == denied
    assert events == [f"access:{MEMORY_A}"]


async def test_storage_cleanup_obeys_per_memory_maintenance_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_memory import server

    orphan_key = f"{MEMORY_A}/documents/orphan.txt"
    deleted: list[str] = []

    async def storage_check():
        return {
            "status": "ok",
            "s3_orphans": {
                "files": [
                    {
                        "key": orphan_key,
                        "uri": f"s3://bucket/{orphan_key}",
                        "size": 1,
                    }
                ],
                "total_size": "1.0 B",
            },
        }

    class Storage:
        async def delete_objects(self, keys: list[str]):
            deleted.extend(keys)
            return {"deleted_count": len(keys), "error_count": 0}

    monkeypatch.setattr(server, "check_admin_permission", lambda: None)
    monkeypatch.setattr(server, "storage_check", storage_check)
    monkeypatch.setattr(server, "get_storage", lambda: Storage())

    coordinator = get_maintenance_coordinator()
    async with coordinator.maintenance(MEMORY_A):
        blocked = await asyncio.create_task(
            server.storage_cleanup(dry_run=False)
        )

    assert blocked == {
        "status": "error",
        "message": "Namespace maintenance is active",
    }
    assert deleted == []

    result = await server.storage_cleanup(dry_run=False)

    assert result["status"] == "ok"
    assert result["deleted"] == 1
    assert deleted == [orphan_key]
