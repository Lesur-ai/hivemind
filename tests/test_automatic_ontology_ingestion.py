"""Automatic batch admission uses the existing queue without foreground LLM work."""

import asyncio
import base64
import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def graph_env(monkeypatch):
    for name, value in {
        "S3_ENDPOINT_URL": "http://127.0.0.1:9000",
        "S3_ACCESS_KEY_ID": "test", "S3_SECRET_ACCESS_KEY": "test",
        "NEO4J_PASSWORD": "test",
    }.items():
        monkeypatch.setenv(name, value)


def document(index=0):
    content = f"Instrument {index} measures temperature.".encode()
    return {"filename": f"doc{index}.md", "source_path": f"manual/doc{index}.md",
            "content": content, "sha256": hashlib.sha256(content).hexdigest()}


@pytest.fixture
def queue_context(monkeypatch):
    from mcp_memory.core import ingest_queue as module
    from mcp_memory.core import graph as graph_module
    graph = SimpleNamespace(
        get_memory=AsyncMock(return_value=SimpleNamespace(
            created_at=datetime(2026, 9, 16, tzinfo=timezone.utc), ontology="general")),
        load_automatic_ontology=AsyncMock(return_value=None),
        get_memory_stats=AsyncMock(return_value=SimpleNamespace(
            document_count=0, entity_count=0, relation_count=0)),
    )
    monkeypatch.setattr(graph_module, "get_graph_service", lambda: graph)
    from mcp_memory.core import automatic_ontology
    monkeypatch.setattr(automatic_ontology, "get_graph_service", lambda: graph)
    async def resolve(memory_id, source_path, sha256, replace_existing):
        return {"action": "ingest", "norm_source_path": source_path, "reason": "new"}
    monkeypatch.setattr(module, "resolve_ingestion", resolve)
    queue = module.IngestQueueService()
    return module, queue, graph


@pytest.mark.asyncio
async def test_complete_batch_is_queued_before_worker_can_prepare(queue_context, monkeypatch):
    module, queue, _ = queue_context
    observed = []
    async def worker(memory_id):
        jobs = list(queue._jobs.values())
        observed.append((len(jobs), len(jobs[0]._automatic_ontology.documents)))
    monkeypatch.setattr(queue, "_worker", worker)
    result = await queue.submit_automatic_batch(
        memory_id="test", documents=[document(i) for i in range(3)],
        batch_id="batch_test", replace_existing=False, requested_by="tester")
    assert len(result) == 3
    await queue._workers["test"]
    assert observed == [(3, 3)]


@pytest.mark.asyncio
async def test_auto_capacity_rejection_admits_nothing(queue_context):
    from mcp_memory.core.ontology_construction import OntologyConstructionError
    _, queue, _ = queue_context
    queue._max_queued_bytes = 1
    with pytest.raises(OntologyConstructionError, match="^automatic_ontology_queue_full$"):
        await queue.submit_automatic_batch(
            memory_id="test", documents=[document()], batch_id="batch_test",
            replace_existing=False, requested_by="tester")
    assert not queue._jobs
    assert not queue._workers


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", ["bytes", "count"])
async def test_auto_server_capacity_refusal_never_claims_admission(queue_context, monkeypatch, capacity):
    from mcp_memory import server
    from mcp_memory.auth.context import current_auth
    module, queue, _ = queue_context
    if capacity == "bytes":
        queue._max_queued_bytes = 1
    else:
        queue._max_queued_per_memory = 0
    monkeypatch.setattr(module, "get_ingest_queue", lambda: queue)
    monkeypatch.setattr(server, "get_storage", lambda: SimpleNamespace(
        compute_hash=lambda content: hashlib.sha256(content).hexdigest()))
    docs = [document(i) for i in range(2)]
    for doc in docs:
        doc["content_base64"] = base64.b64encode(doc.pop("content")).decode()
    token = current_auth.set({"permissions": ["read", "write"], "memory_ids": ["test"]})
    try:
        response = await server.memory_ingest_batch_async("test", docs, ontology="auto")
    finally:
        current_auth.reset(token)
    assert response["status"] == "error"
    assert response["code"] == "automatic_batch_not_admitted"
    assert "queued" not in response["message"].lower()
    assert queue._jobs == queue._workers == queue._active_jobs == queue._queues == {}
    assert queue._queued_bytes == 0


@pytest.mark.asyncio
async def test_cancelled_batch_does_not_pin_unaccounted_corpus(queue_context, monkeypatch):
    _, queue, _ = queue_context
    # Delay the worker until cancellation is admitted for every job.
    real_start = queue._ensure_worker_locked
    monkeypatch.setattr(queue, "_ensure_worker_locked", lambda _: None)
    results = await queue.submit_automatic_batch(
        memory_id="test", documents=[document(i) for i in range(2)],
        batch_id="batch_test", replace_existing=False, requested_by="tester")
    batch = next(iter(queue._jobs.values()))._automatic_ontology
    for result in results:
        await queue.cancel(result["job_id"])
    # The still-running first job needs the original corpus until its cancel
    # boundary; removing only the queued second job must not destroy selection.
    assert len(batch.documents) == 2
    real_start("test")
    await queue._workers["test"]
    assert {job.status for job in queue._jobs.values()} == {"cancelled"}
    assert queue._queued_bytes == 0
    assert batch.documents == []


@pytest.mark.asyncio
async def test_delete_cannot_overlap_an_automatic_document(queue_context, monkeypatch):
    from mcp_memory.core.automatic_ontology import AutomaticOntologyBatch
    from mcp_memory.core.graph import GraphService
    from mcp_memory.core.maintenance import MaintenanceAdmissionError
    module, queue, _ = queue_context
    entered, finish = asyncio.Event(), asyncio.Event()
    async def prepare(self, _, **kwargs):
        return "name: catalogue"
    async def pipeline(**kwargs):
        entered.set()
        await finish.wait()
        return {"status": "ok"}
    monkeypatch.setattr(AutomaticOntologyBatch, "prepare", prepare)
    monkeypatch.setattr(module, "run_ingest_pipeline", pipeline)
    await queue.submit_automatic_batch(
        memory_id="test", documents=[document()], batch_id="batch_test",
        replace_existing=False, requested_by="tester")
    worker = queue._workers["test"]
    await asyncio.wait_for(entered.wait(), timeout=2)
    try:
        # Exercise the actual deletion entry guard. No Neo4j session should be
        # opened while a document is being prepared/written by the auto batch.
        graph = object.__new__(GraphService)
        with pytest.raises(MaintenanceAdmissionError):
            await graph.delete_memory("test")
    finally:
        finish.set()
        await worker


@pytest.mark.asyncio
async def test_cancel_at_admission_exit_still_starts_the_admitted_worker(queue_context, monkeypatch):
    from contextlib import asynccontextmanager
    from mcp_memory.core.automatic_ontology import AutomaticOntologyBatch
    from mcp_memory.core.maintenance import get_maintenance_coordinator
    module, queue, _ = queue_context
    coordinator = get_maintenance_coordinator()
    maintenance = coordinator.maintenance
    first = True
    @asynccontextmanager
    async def cancel_after_release(memory_id, **kwargs):
        nonlocal first
        cancel_here, first = first, False
        async with maintenance(memory_id, **kwargs):
            yield
        if cancel_here:
            raise asyncio.CancelledError()
    monkeypatch.setattr(coordinator, "maintenance", cancel_after_release)
    async def prepare(self, memory_id, **kwargs):
        return "name: catalogue"
    monkeypatch.setattr(AutomaticOntologyBatch, "prepare", prepare)
    monkeypatch.setattr(module, "run_ingest_pipeline", AsyncMock(return_value={"status": "ok"}))
    with pytest.raises(asyncio.CancelledError):
        await queue.submit_automatic_batch(
            memory_id="test", documents=[document()], batch_id="batch_test",
            replace_existing=False, requested_by="tester")
    assert "test" in queue._workers
    await queue._workers["test"]
    assert await queue.is_idle_for_memory("test")
    assert {job.status for job in queue._jobs.values()} == {"succeeded"}


@pytest.mark.asyncio
async def test_one_shared_preparation_and_yaml_for_every_document(queue_context, monkeypatch):
    from mcp_memory.core.automatic_ontology import AutomaticOntologyBatch
    module, queue, _ = queue_context
    prepares = []
    async def prepare(self, memory_id, **kwargs):
        if self.ontology_yaml is None:
            prepares.append(len(self.documents))
            self.ontology_yaml = "name: test-catalogue"
        return self.ontology_yaml
    monkeypatch.setattr(AutomaticOntologyBatch, "prepare", prepare)
    pipeline = AsyncMock(return_value={"status": "ok", "document_id": "doc"})
    monkeypatch.setattr(module, "run_ingest_pipeline", pipeline)
    await queue.submit_automatic_batch(
        memory_id="test", documents=[document(i) for i in range(3)],
        batch_id="batch_test", replace_existing=False, requested_by="tester")
    await queue._workers["test"]
    assert prepares == [3]
    assert pipeline.await_count == 3
    assert {call.kwargs["ontology"] for call in pipeline.await_args_list} == {"name: test-catalogue"}
    assert {job.status for job in queue._jobs.values()} == {"succeeded"}


@pytest.mark.asyncio
async def test_recreated_memory_rejects_old_batch_before_pipeline(queue_context, monkeypatch):
    from mcp_memory.core.automatic_ontology import AutomaticOntologyBatch
    module, queue, graph = queue_context
    async def prepare(self, memory_id, **kwargs):
        graph.get_memory.return_value.created_at = datetime(2026, 9, 17, tzinfo=timezone.utc)
        self.ontology_yaml = "name: catalogue"
        return self.ontology_yaml
    monkeypatch.setattr(AutomaticOntologyBatch, "prepare", prepare)
    pipeline = AsyncMock()
    monkeypatch.setattr(module, "run_ingest_pipeline", pipeline)
    await queue.submit_automatic_batch(
        memory_id="test", documents=[document()], batch_id="batch_test",
        replace_existing=False, requested_by="tester")
    await queue._workers["test"]
    pipeline.assert_not_awaited()
    assert next(iter(queue._jobs.values())).status == "failed"


@pytest.mark.asyncio
async def test_auto_server_validates_last_document_before_admitting_any(monkeypatch):
    from mcp_memory import server
    from mcp_memory.core import ingest_queue
    monkeypatch.setattr(server, "check_memory_access", lambda memory_id: None)
    monkeypatch.setattr(server, "check_write_permission", lambda: None)
    monkeypatch.setattr(server, "get_storage", lambda: SimpleNamespace(
        compute_hash=lambda content: hashlib.sha256(content).hexdigest()))
    queue = SimpleNamespace(submit_automatic_batch=AsyncMock())
    monkeypatch.setattr(ingest_queue, "get_ingest_queue", lambda: queue)
    docs = []
    for i in range(2):
        doc = document(i)
        doc["content_base64"] = base64.b64encode(doc.pop("content")).decode()
        docs.append(doc)
    docs[-1]["sha256"] = "0" * 64
    result = await server.memory_ingest_batch_async("test", docs, ontology="auto")
    assert result["status"] == "error"
    assert result.get("code") == "invalid_automatic_batch"
    queue.submit_automatic_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_admitted_exit_failure_preserves_results_and_runs_worker(queue_context, monkeypatch):
    from contextlib import asynccontextmanager
    from mcp_memory.core.automatic_ontology import AutomaticOntologyBatch
    from mcp_memory.core.maintenance import get_maintenance_coordinator
    module, queue, _ = queue_context
    coordinator = get_maintenance_coordinator()
    maintenance = coordinator.maintenance
    first = True
    @asynccontextmanager
    async def fail_after_release(memory_id, **kwargs):
        nonlocal first
        fail_here, first = first, False
        async with maintenance(memory_id, **kwargs):
            yield
        if fail_here:
            raise RuntimeError("synthetic infrastructure detail")
    monkeypatch.setattr(coordinator, "maintenance", fail_after_release)
    monkeypatch.setattr(AutomaticOntologyBatch, "prepare", AsyncMock(return_value="name: catalogue"))
    monkeypatch.setattr(module, "run_ingest_pipeline", AsyncMock(return_value={"status": "ok"}))
    with pytest.raises(RuntimeError) as caught:
        await queue.submit_automatic_batch(
            memory_id="test", documents=[document()], batch_id="batch_test",
            replace_existing=False, requested_by="tester")
    await queue._workers["test"]
    assert str(caught.value) == "automatic_batch_admitted_release_failed"
    assert caught.value.batch_id == "batch_test"
    assert {row["job_id"] for row in caught.value.results} == set(queue._jobs)
    assert {job.status for job in queue._jobs.values()} == {"succeeded"}
    assert await queue.is_idle_for_memory("test")


@pytest.mark.asyncio
async def test_pre_admission_infrastructure_failure_is_not_a_refusal(queue_context):
    _, queue, graph = queue_context
    error = RuntimeError("synthetic unavailable backend")
    graph.get_memory.side_effect = error
    with pytest.raises(RuntimeError) as caught:
        await queue.submit_automatic_batch(
            memory_id="test", documents=[document()], batch_id="batch_test",
            replace_existing=False, requested_by="tester")
    assert caught.value is error
    assert queue._jobs == queue._workers == {}


@pytest.mark.asyncio
async def test_failed_admission_removes_only_batch_owned_jobs(queue_context, monkeypatch):
    module, queue, _ = queue_context
    submit = queue.submit
    calls = 0
    foreign = [module.IngestJob(
        job_id=jid, memory_id="test", source_path=jid, sha256="a" * 64,
        filename=jid + ".md", batch_id="other", status=status,
        _content=b"retained", _content_size=8,
    ) for jid, status in (("foreign-active", "running"), ("foreign-queued", "queued"))]
    async def inject_foreign_then_fail(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic second-document admission failure")
        response = await submit(**kwargs)
        # Unexpected state must not be erased just because admission was idle
        # at entry. No foreign worker is started by this synthetic fault.
        async with queue._state_lock:
            queue._jobs.update((job.job_id, job) for job in foreign)
            queue._active_jobs["test"] = foreign[0].job_id
            queue._queues["test"].append(foreign[1].job_id)
            queue._queued_bytes += 16
        return response
    monkeypatch.setattr(queue, "submit", inject_foreign_then_fail)
    with pytest.raises(OSError):
        await queue.submit_automatic_batch(
            memory_id="test", documents=[document(0), document(1)], batch_id="batch_test",
            replace_existing=False, requested_by="tester")
    assert set(queue._jobs) == {job.job_id for job in foreign}
    assert queue._active_jobs == {"test": "foreign-active"}
    assert list(queue._queues["test"]) == ["foreign-queued"]
    assert queue._queued_bytes == 16
    assert all(job._content == b"retained" for job in foreign)
    assert queue._workers == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["refused", "unavailable", "corrupted", "admitted"])
async def test_server_distinguishes_refusal_infrastructure_and_admitted_jobs(monkeypatch, failure):
    from mcp_memory import server
    from mcp_memory.core import ingest_queue
    from mcp_memory.core.maintenance import MaintenanceCoordinatorCorrupted
    from mcp_memory.core.ontology_construction import OntologyConstructionError
    monkeypatch.setattr(server, "check_memory_access", lambda _: None)
    monkeypatch.setattr(server, "check_write_permission", lambda: None)
    monkeypatch.setattr(server, "get_storage", lambda: SimpleNamespace(
        compute_hash=lambda content: hashlib.sha256(content).hexdigest()))
    async def submit(**kwargs):
        if failure == "refused":
            raise OntologyConstructionError("automatic_ontology_requires_empty_memory")
        if failure == "corrupted":
            raise MaintenanceCoordinatorCorrupted()
        if failure == "unavailable":
            raise RuntimeError("private infrastructure response")
        raise ingest_queue.AutomaticBatchAdmittedError(
            kwargs["batch_id"], [{"job_id": "ing_admitted", "status": "running"}])
    monkeypatch.setattr(ingest_queue, "get_ingest_queue", lambda: SimpleNamespace(submit_automatic_batch=submit))
    doc = document()
    doc["content_base64"] = base64.b64encode(doc.pop("content")).decode()
    response = await server.memory_ingest_batch_async("test", [doc], ontology="auto")
    if failure == "admitted":
        assert response["status"] == "ok"
        assert response["warning"] == "automatic_batch_admitted_release_failed"
        assert response["items"][0]["job_id"] == "ing_admitted"
        assert response["counts"]["running"] == 1
    else:
        assert response["status"] == "error"
        assert response["code"] == ("automatic_batch_not_admitted" if failure == "refused"
                                     else "automatic_batch_infrastructure_failed")
    assert "private infrastructure response" not in str(response)
