"""#1420: staged selective admission, Graph side, and Core/Graph parity.

Existing-equivalent search: tests/test_p13_maintenance_gate.py covers the
per-memory coordinator only; tests/test_automatic_ontology_ingestion.py is
reused for its queue fixture. Heavy Graph modules are imported with the same
env scaffold those tests use (no Neo4j/Qdrant/S3 connection is made).
"""

from __future__ import annotations

import ast
import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from live_mem.core import selective_maintenance as core_sm
from mcp_memory.core import maintenance_gate as gate
from mcp_memory.core import selective_maintenance as graph_sm

ROOT = Path(__file__).resolve().parents[1]
GRAPH_SERVER = ROOT / "services/graph-memory/src/mcp_memory/server.py"
OP = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def graph_env(monkeypatch):
    for name, value in {
        "S3_ENDPOINT_URL": "http://127.0.0.1:9000",
        "S3_ACCESS_KEY_ID": "test", "S3_SECRET_ACCESS_KEY": "test",
        "NEO4J_PASSWORD": "test",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def controller():
    c = graph_sm.SelectiveController(None)
    graph_sm.reset_controller_for_tests(c)
    yield c
    graph_sm.reset_controller_for_tests(None)


def _registered_tool_names() -> set[str]:
    tree = ast.parse(GRAPH_SERVER.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            for deco in node.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                if isinstance(target, ast.Attribute) and target.attr == "tool":
                    names.add(node.name)
    return names


def test_every_graph_tool_is_explicitly_classified():
    # Drift guard: an unlisted tool would silently default to "mutation".
    tools = _registered_tool_names()
    assert len(tools) >= 30
    assert tools <= gate.READ_TOOLS | gate.MUTATION_TOOLS
    assert not gate.READ_TOOLS & gate.MUTATION_TOOLS
    # No stale names either: every classified name is a real tool.
    assert (gate.READ_TOOLS | gate.MUTATION_TOOLS) <= tools
    assert gate.kind_of("unlisted_future_tool") == "mutation"  # fail closed


@pytest.mark.asyncio
async def test_installed_gate_refuses_mutations_and_keeps_reads(controller):
    calls = []

    async def handler(**kw):
        calls.append(kw)
        return {"status": "ok"}

    tools = {n: SimpleNamespace(fn=handler)
             for n in ("memory_create", "memory_search", "unlisted_future_tool")}
    gate.install(SimpleNamespace(_tool_manager=SimpleNamespace(_tools=tools)))
    controller.pause(OP, ["*"], "all")
    assert await tools["memory_create"].fn(memory_id="m1") == gate.REFUSAL
    assert await tools["unlisted_future_tool"].fn() == gate.REFUSAL
    assert await tools["memory_search"].fn(memory_id="m1") == {"status": "ok"}
    assert [c for c in calls] == [{"memory_id": "m1"}]
    controller.pause(OP, ["*"], "cutover")
    assert await tools["memory_search"].fn(memory_id="m1") == gate.REFUSAL


@pytest.mark.asyncio
async def test_malformed_memory_id_reaches_the_handlers_own_validation(controller):
    async def handler(memory_id=""):
        return {"status": "error", "message": "invalid memory_id"}

    tools = {"memory_get_context": SimpleNamespace(fn=handler)}
    gate.install(SimpleNamespace(_tool_manager=SimpleNamespace(_tools=tools)))
    assert (await tools["memory_get_context"].fn(memory_id="../x"))["message"] == "invalid memory_id"


def test_graph_storage_physical_gate(controller):
    from mcp_memory.core.storage import StorageService

    controller.pause(OP, ["m1"], "background")
    with pytest.raises(graph_sm.MaintenanceRefused):
        StorageService._admit_write("m1/documents/a")
    StorageService._admit_write("m2/documents/a")
    StorageService._admit_write("_health_check/test.txt")
    with controller.operation("mutation", "m2"):
        pass


# ─── ingest queue: tickets from acceptance to settlement ──────────────────────

@pytest.fixture
def queue(monkeypatch, controller):
    from mcp_memory.core import ingest_queue as module

    seen = set()

    async def resolve(memory_id, source_path, sha256, replace_existing):
        # Submission sees a new document; the worker's anti-TOCTOU re-resolution
        # sees it already ingested, which ends the job without any pipeline.
        if sha256 not in seen:
            seen.add(sha256)
            return {"action": "ingest", "norm_source_path": source_path, "reason": "new"}
        return {"action": "skip", "existing": {"id": "d1"}, "reason": "same"}

    monkeypatch.setattr(module, "resolve_ingestion", resolve)
    return module, module.IngestQueueService()


async def _submit(q, n="a", **kw):
    return await q.submit(
        memory_id="m1", content=b"x", filename=f"{n}.md", sha256=n * 64,
        source_path=f"p/{n}.md", replace_existing=False, **kw)


@pytest.mark.asyncio
async def test_queued_cancel_settles_ticket_and_blocks_nothing(queue, controller, monkeypatch):
    module, q = queue
    monkeypatch.setattr(q, "_ensure_worker_locked", lambda m: None)  # keep jobs queued
    with controller.operation("mutation", "m1"):
        first = await _submit(q, "a")
        second = await _submit(q, "b")
    controller.pause(OP, ["*"], "background")
    # One root ticket, retained once per accepted job: it drains only when the
    # LAST owner settles (pending counts owned operations, not jobs).
    assert controller.status(OP)["pending"] == 1
    assert (await q.cancel(second["job_id"]))["status"] == "cancelled"
    assert controller.status(OP)["state"] == "background_draining"
    # The first job is the active one: its worker (disabled here) would end it.
    async with q._state_lock:
        q._finish_active_locked("m1", first["job_id"])
    assert controller.status(OP)["state"] == "background_paused"


@pytest.mark.asyncio
async def test_accepted_job_runs_to_settlement_through_a_closed_barrier(queue, controller):
    module, q = queue
    with controller.operation("mutation", "m1"):
        job = await _submit(q, "a", _defer_worker=True)
    st = controller.pause(OP, ["*"], "background")
    assert st["state"] == "background_draining" and st["pending"] == 1
    with pytest.raises(graph_sm.MaintenanceRefused):  # new work refused
        await _submit(q, "c")
    async with q._state_lock:
        q._ensure_worker_locked("m1")
    for _ in range(100):
        if controller.status(OP)["state"] == "background_paused":
            break
        await asyncio.sleep(0.02)
    assert controller.status(OP)["state"] == "background_paused"
    assert (await q.get_job(job["job_id"]))["status"] == "skipped"


@pytest.mark.asyncio
async def test_refused_submit_leaves_no_job(queue, controller):
    module, q = queue
    controller.pause(OP, ["*"], "background")
    with pytest.raises(graph_sm.MaintenanceRefused):
        await _submit(q, "a")
    assert q._jobs == {} and q._queued_bytes == 0


# ─── Core/Graph parity: one vocabulary, one set of transitions ────────────────

def _script(sm):
    c = sm.SelectiveController(None)
    trace = [c.status()]
    ticket = c.admit("short", "s1")
    trace.append(c.pause(OP, ["*"], "all"))
    trace.append(c.status(OP))
    c.finish(ticket)
    trace.append(c.status(OP))
    trace.append(c.pause(OP, ["*"], "cutover"))
    trace.append(c.resume(OP))
    return trace


def test_core_and_graph_controllers_are_behaviourally_identical():
    assert _script(core_sm) == _script(graph_sm)
    for name in ("MODES", "_CLOSES_AT", "MAX_CONTROLLERS", "MAX_SCOPES", "STATE_FILE"):
        assert getattr(core_sm, name) == getattr(graph_sm, name), name
    # Same implementation, not just same behaviour: only the state-dir source
    # and the docstring header may differ.
    core = (ROOT / "src/live_mem/core/selective_maintenance.py").read_text()
    graph = (ROOT / "services/graph-memory/src/mcp_memory/core/selective_maintenance.py").read_text()
    body = lambda t: t[t.index("from __future__"):t.index("def get_controller")]
    assert body(core) == body(graph)


# ─── Round-1 review ledger (PR #1479) ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_f5_cancelled_graph_worker_settles_unknown_and_keeps_siblings(queue, controller, monkeypatch):
    module, q = queue

    calls = []

    async def cancelled_pipeline(**kwargs):
        calls.append(kwargs["filename"])
        if len(calls) == 1:
            raise asyncio.CancelledError()  # the first job's worker is cancelled
        return {"status": "ok", "document_id": "d2", "entities": 0, "relations": 0}

    monkeypatch.setattr(module, "run_ingest_pipeline", cancelled_pipeline)

    async def fresh(memory_id, source_path, sha256, replace_existing):
        return {"action": "ingest", "norm_source_path": source_path, "reason": "new"}

    monkeypatch.setattr(module, "resolve_ingestion", fresh)
    with controller.operation("mutation", "m1"):
        first = await _submit(q, "a", _defer_worker=True)
        sibling = await _submit(q, "b", _defer_worker=True)
    controller.pause(OP, ["*"], "background")
    worker = None
    async with q._state_lock:
        q._ensure_worker_locked("m1")
        worker = q._workers["m1"]
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert (await q.get_job(first["job_id"]))["status"] == "failed"
    assert controller.status()["unknown"] == 1  # durable, blocks capture/resume
    assert controller.status(OP)["state"] == "recovery_required"
    # The accepted sibling is not stranded: a replacement worker runs it under
    # its own ticket, and the drain completes except for the unknown effect.
    for _ in range(100):
        if (await q.get_job(sibling["job_id"]))["status"] not in ("queued", "running"):
            break
        await asyncio.sleep(0.02)
    assert calls == ["a.md", "b.md"]
    assert (await q.get_job(sibling["job_id"]))["status"] == "succeeded"
    assert q._jobs[sibling["job_id"]]._ticket is None
    assert controller.status()["unknown"] == 1


@pytest.mark.asyncio
async def test_f4_graph_rest_reads_are_gated_at_cutover(controller):
    from mcp_memory.auth.middleware import StaticFilesMiddleware

    served, sent = [], []

    async def serve(scope, receive, send):
        served.append(scope["path"])

    async def send_json(send, body, status=200):
        sent.append((body, status))

    mw = StaticFilesMiddleware.__new__(StaticFilesMiddleware)
    mw._serve, mw._send_json = serve, send_json
    reads = (("GET", "/api/memories"), ("GET", "/api/graph/m1"),
             ("POST", "/api/ask"), ("POST", "/api/query"))
    controller.pause(OP, ["*"], "all")
    for method, path in reads:
        await mw({"type": "http", "method": method, "path": path}, None, None)
    assert len(served) == 4
    controller.pause(OP, ["*"], "cutover")
    for method, path in reads:
        await mw({"type": "http", "method": method, "path": path}, None, None)
    assert len(served) == 4 and len(sent) == 4
    await mw({"type": "http", "method": "POST", "path": "/api/maintenance"}, None, None)
    assert len(served) == 5


# ─── Round-2 review ledger (PR #1479): F3 status must not certify an undurable pause ──

@pytest.mark.parametrize("sm", [core_sm, graph_sm], ids=["core", "graph"])
def test_f3_status_never_certifies_after_a_failed_persist(sm, tmp_path, monkeypatch):
    c = sm.SelectiveController(tmp_path / "state")
    real = c._persist

    def broken(*args, **kwargs):
        c._durable = False  # what the real _persist records before re-raising
        raise OSError("disk")

    # 1. failed INITIAL pause
    monkeypatch.setattr(c, "_persist", broken)
    with pytest.raises(sm.MaintenanceRefused) as refusal:
        c.pause(OP, ["*"], "all")
    assert refusal.value.code == "maintenance_recovery_required"
    status = c.status(OP)
    assert status["state"] == "recovery_required" and status["capture_ready"] is False
    assert c.status()["state"] == "recovery_required"
    with pytest.raises(sm.MaintenanceRefused):
        c.admit("short", "s1")  # admission itself stays closed

    # 2. durability re-established by the same-UUID retry: only then certified
    monkeypatch.setattr(c, "_persist", real)
    assert c.pause(OP, ["*"], "all")["capture_ready"] is True

    # 3. failed mode ESCALATION is not certified either (and is not a downgrade)
    monkeypatch.setattr(c, "_persist", broken)
    with pytest.raises(sm.MaintenanceRefused):
        c.pause(OP, ["*"], "cutover")
    status = c.status(OP)
    assert status["mode"] == "cutover" and status["capture_ready"] is False
    monkeypatch.setattr(c, "_persist", real)
    assert c.pause(OP, ["*"], "cutover")["state"] == "exclusive_cutover"

    # 4. failed RESUME keeps the pause and reports recovery_required
    monkeypatch.setattr(c, "_persist", broken)
    with pytest.raises(sm.MaintenanceRefused):
        c.resume(OP)
    assert c.status(OP)["state"] == "recovery_required"
    monkeypatch.setattr(c, "_persist", real)
    assert c.resume(OP)["state"] == "normal"
    assert c.status()["state"] == "normal"


@pytest.mark.parametrize("sm", [core_sm, graph_sm], ids=["core", "graph"])
def test_f3_real_persist_failure_marks_the_state_undurable(sm, tmp_path, monkeypatch):
    # Not a stub of _persist: break the real write path (os.replace) and prove
    # the controller itself records the uncertainty.
    c = sm.SelectiveController(tmp_path / "state")
    monkeypatch.setattr(sm.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("replace")))
    with pytest.raises(sm.MaintenanceRefused):
        c.pause(OP, ["*"], "all")
    assert c.status(OP)["capture_ready"] is False
    monkeypatch.undo()
    assert c.pause(OP, ["*"], "all")["capture_ready"] is True


@pytest.mark.parametrize("sm", [core_sm, graph_sm], ids=["core", "graph"])
@pytest.mark.parametrize("op", ["pause", "resume"])
def test_f3_round3_failed_state_dir_creation_marks_the_state_undurable(sm, op, tmp_path, monkeypatch):
    # Round 3: mkdir used to sit outside the OSError guard, so a vanished or
    # unwritable state dir left _durable True and status certified the pause.
    c = sm.SelectiveController(tmp_path / "state")
    if op == "resume":
        c.pause(OP, ["*"], "all")
    monkeypatch.setattr(sm.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("mkdir")))
    with pytest.raises(sm.MaintenanceRefused) as refusal:
        getattr(c, op)(*((OP, ["*"], "all") if op == "pause" else (OP,)))
    assert refusal.value.code == "maintenance_recovery_required"
    status = c.status(OP)
    assert status["state"] == "recovery_required" and status["capture_ready"] is False
    assert c.status()["state"] == "recovery_required"
    with pytest.raises(sm.MaintenanceRefused):
        c.admit("short", "s1")  # admission stays closed while the disk is unknown
    monkeypatch.undo()
    # Same-UUID retry re-establishes durability and only then certifies.
    if op == "pause":
        assert c.pause(OP, ["*"], "all")["capture_ready"] is True
    else:
        assert c.resume(OP)["state"] == "normal"


@pytest.mark.parametrize("sm", [core_sm, graph_sm], ids=["core", "graph"])
def test_f3_round4_short_writes_are_completed_not_certified_truncated(sm, tmp_path, monkeypatch):
    # Round 4: os.write may return fewer bytes than requested. The old code
    # ignored the count, replaced the receipt with truncated JSON and certified.
    real_write = sm.os.write
    monkeypatch.setattr(sm.os, "write", lambda fd, data: real_write(fd, bytes(data)[:5]))
    c = sm.SelectiveController(tmp_path / "state")
    assert c.pause(OP, ["*"], "all")["capture_ready"] is True
    monkeypatch.undo()
    # The file on disk is complete: a fresh process reloads the pause intact.
    again = sm.SelectiveController(tmp_path / "state")
    assert again.status(OP)["state"] in ("read_only", "recovery_required")
    assert again.status(OP)["mode"] == "all"


@pytest.mark.parametrize("sm", [core_sm, graph_sm], ids=["core", "graph"])
@pytest.mark.parametrize("failure", ["zero_progress", "short_then_error"])
def test_f3_round4_incomplete_write_marks_the_state_undurable(sm, failure, tmp_path, monkeypatch):
    c = sm.SelectiveController(tmp_path / "state")
    real_write = sm.os.write
    calls = []

    def flaky(fd, data):
        calls.append(1)
        if failure == "zero_progress":
            return 0
        if len(calls) == 1:
            return real_write(fd, bytes(data)[:5])
        raise OSError("disk full")

    monkeypatch.setattr(sm.os, "write", flaky)
    with pytest.raises(sm.MaintenanceRefused) as refusal:
        c.pause(OP, ["*"], "all")
    assert refusal.value.code == "maintenance_recovery_required"
    assert c.status(OP)["capture_ready"] is False
    assert c.status()["state"] == "recovery_required"
    monkeypatch.undo()
    # The truncated temp file never replaced the receipt; a retry certifies.
    assert c.pause(OP, ["*"], "all")["capture_ready"] is True


@pytest.mark.parametrize("sm", [core_sm, graph_sm], ids=["core", "graph"])
def test_failed_clean_receipt_does_not_fail_shutdown_and_is_inherited(sm, tmp_path, monkeypatch):
    c = sm.SelectiveController(tmp_path / "state")
    c.pause(OP, ["*"], "all")
    monkeypatch.setattr(sm.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("replace")))
    c.release()  # must not raise
    monkeypatch.undo()
    again = sm.SelectiveController(tmp_path / "state")
    assert again.status(OP)["state"] == "recovery_required"  # unclean => unknown


# #1523: exercise the real Graph adapter, not just controller preflight.
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["background", "all"])
async def test_graph_read_origin_s3_refuses_before_dispatch(controller, mode):
    from mcp_memory.core.storage import StorageService
    storage = StorageService.__new__(StorageService)
    calls = []
    storage._bucket = "b"
    storage._client = SimpleNamespace(put_object=lambda **kw: calls.append(kw))
    with controller.operation("read", "m1"):
        controller.pause(OP, ["m1"], mode)
        with pytest.raises(graph_sm.MaintenanceRefused, match="maintenance_admission_closed"):
            await storage.upload_document("m1", "a.md", b"a")
    assert calls == []


@pytest.mark.asyncio
async def test_graph_read_origin_s3_lost_response_is_unknown(tmp_path, controller):
    from mcp_memory.core.storage import StorageService
    c = graph_sm.SelectiveController(tmp_path / "state")
    graph_sm.reset_controller_for_tests(c)
    storage = StorageService.__new__(StorageService)
    storage._bucket = "b"
    calls = []
    def uncertain(**kw):
        calls.append(kw["Key"])
        raise TimeoutError("response lost after application")
    storage._client = SimpleNamespace(put_object=uncertain)
    with c.operation("read", "m1"):
        with pytest.raises(TimeoutError):
            await storage.upload_document("m1", "a.md", b"a")
    assert len(calls) == 1
    assert c.status()["unknown"] == 1
    c.release()
    assert graph_sm.SelectiveController(c._dir).status()["unknown"] == 1


@pytest.mark.asyncio
async def test_graph_s3_sdk_drains_after_read_cancellation(controller):
    import threading
    from mcp_memory.core.storage import StorageService

    storage = StorageService.__new__(StorageService)
    storage._bucket = "b"
    started, complete = threading.Event(), threading.Event()
    calls = []
    def delayed(**kw):
        started.set()
        assert complete.wait(5)
        calls.append(kw["Key"])
    storage._client = SimpleNamespace(put_object=delayed)
    async def read_then_repair():
        with controller.operation("read", "m1"):
            # The real synchronous Graph SDK adapter can run in an SDK/task
            # worker; cancellation of the request never certifies it stopped.
            await asyncio.to_thread(lambda: asyncio.run(storage.upload_document("m1", "a.md", b"a")))
    task = asyncio.create_task(read_then_repair())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        st = controller.pause(OP, ["m1"], "all")
        assert st["pending"] == 1 and not st["capture_ready"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert controller.status(OP)["pending"] == 1
        with pytest.raises(graph_sm.MaintenanceRefused, match="maintenance_unclean"):
            controller.resume(OP)
    finally:
        complete.set()
        if not task.done():
            await task
        for _ in range(200):
            if controller.status(OP)["pending"] == 0:
                break
            await asyncio.sleep(.005)
    assert len(calls) == 1
    assert controller.status(OP)["capture_ready"]


@pytest.mark.asyncio
async def test_graph_neo4j_physical_owner_includes_session_close(controller):
    from contextlib import asynccontextmanager
    from mcp_memory.core.graph import GraphService

    service = object.__new__(GraphService)
    closing, complete = asyncio.Event(), asyncio.Event()
    @asynccontextmanager
    async def session():
        yield SimpleNamespace()
        closing.set()
        await complete.wait()  # auto-commit/PULL can finish on session close
    service.session = session
    async def read_then_repair():
        with controller.operation("read", "m1"):
            async with service.mutation_session("m1"):
                pass
    task = asyncio.create_task(read_then_repair())
    await closing.wait()
    assert controller.pause(OP, ["m1"], "all")["pending"] == 1
    complete.set()
    await task
    assert controller.status(OP)["capture_ready"]


@pytest.mark.asyncio
async def test_graph_neo4j_cancelled_commit_stays_unknown(controller):
    from contextlib import asynccontextmanager
    from mcp_memory.core.graph import GraphService

    service = object.__new__(GraphService)
    dispatched = asyncio.Event()
    @asynccontextmanager
    async def session():
        dispatched.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover
    service.session = session
    async def repair():
        with controller.operation("read", "m1"):
            async with service.mutation_session("m1"):
                pass
    task = asyncio.create_task(repair())
    await dispatched.wait()
    controller.pause(OP, ["m1"], "all")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert controller.status(OP)["state"] == "recovery_required"
    assert not controller.status(OP)["capture_ready"]


@pytest.mark.parametrize("outcome", ["completed", "acknowledged", "timeout"])
def test_graph_qdrant_read_origin_physical_outcome(controller, outcome):
    from mcp_memory.core.vector_store import VectorStoreService
    store = object.__new__(VectorStoreService)
    calls = []
    def upsert():
        calls.append("dispatched")
        assert controller.pause(OP, ["m1"], "all")["pending"] == 1
        if outcome == "timeout":
            raise TimeoutError("lost response")
        return SimpleNamespace(status=outcome)
    with controller.operation("read", "m1"):
        if outcome == "completed":
            store._write_call("m1", upsert)
        else:
            with pytest.raises(Exception):
                store._write_call("m1", upsert)
    assert calls == ["dispatched"]
    status = controller.status(OP)
    assert status["capture_ready"] is (outcome == "completed")
    assert status["unknown"] == (0 if outcome == "completed" else 1)


@pytest.mark.parametrize("prefix", ["_backups", "custom/backups"])
def test_graph_backup_effect_uses_memory_scope(controller, monkeypatch, prefix):
    from mcp_memory.core import storage as module
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(s3_backup_prefix=prefix))
    service = object.__new__(module.StorageService)
    effects = []
    controller.pause(OP, ["m1"], "all")
    with controller.operation("read", "m2"):
        with pytest.raises(graph_sm.MaintenanceRefused, match="maintenance_admission_closed"):
            service._write_call(lambda **kw: effects.append(kw), Key=f"{prefix}/m1/t/manifest.json")
    assert effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["background", "all"])
async def test_true_core_and_graph_storage_reads_remain_available(controller, monkeypatch, mode):
    import io
    from live_mem.core.storage import StorageService as CoreStorage
    from mcp_memory.core.storage import StorageService as GraphStorage

    c = core_sm.SelectiveController(None)
    core_sm.reset_controller_for_tests(c)
    core = object.__new__(CoreStorage)
    core.bucket = "b"
    graph = object.__new__(GraphStorage)
    graph._bucket = "b"
    calls = []
    def get(**kw):
        calls.append(kw["Key"])
        return {"Body": io.BytesIO(b"safe read")}
    core._client_data = graph._client = SimpleNamespace(get_object=get)
    c.pause(OP, ["*"], mode)
    controller.pause(OP, ["*"], mode)
    with c.operation("read", "m1"):
        assert await core.get("m1/live/a.md") == "safe read"
        assert await core.get("m1/bank/activeContext.md") == "safe read"
    with controller.operation("read", "m1"):
        assert await graph.download_document("m1", "m1/documents/a.md") == b"safe read"
    assert calls == ["m1/live/a.md", "m1/bank/activeContext.md", "m1/documents/a.md"]
    assert c.status(OP)["pending"] == controller.status(OP)["pending"] == 0


@pytest.fixture
def authorized_external_storage(request, monkeypatch, tmp_path):
    """Opt-in #1523 fixture: explicit bucket+prefix authorization, never prod.

    Config is a private dotenv file using the regular S3_* operator variables.
    Neither credentials nor a developer config file is committed or uploaded.
    Only exact objects in this fixture's unique namespace are cleaned up.
    """
    import os
    from dotenv import dotenv_values

    config_path = os.environ.get("HIVEMIND_1523_S3_CONFIG")
    if not config_path:
        pytest.skip("#1523 authorized external S3 fixture not supplied")
    path = Path(config_path)
    if path.stat().st_mode & 0o077:
        pytest.fail("external S3 config must be private (0600)")
    config = dotenv_values(path)
    bucket = os.environ.get("HIVEMIND_1523_S3_AUTHORIZED_BUCKET")
    prefix = os.environ.get("HIVEMIND_1523_S3_AUTHORIZED_PREFIX")
    if not bucket or bucket != config.get("S3_BUCKET_NAME") or not prefix:
        pytest.fail("explicit #1523 bucket/prefix authorization is required")
    scope = prefix + uuid.uuid4().hex[:12]
    graph_sm.validate_scope(scope)
    for name in ("S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY",
                 "S3_BUCKET_NAME", "S3_REGION_NAME", "S3_SIGNATURE_MODE"):
        if not config.get(name):
            pytest.fail("external S3 config is missing a required S3 variable")
        monkeypatch.setenv(name, config[name])
    monkeypatch.setenv("PROXY_URL", config.get("PROXY_URL") or "")
    monkeypatch.setenv("NEO4J_PASSWORD", "unused-external-s3-fixture")
    from live_mem.config import get_settings as core_settings
    from mcp_memory.config import get_settings as graph_settings
    from live_mem.core.storage import StorageService as CoreStorage
    from mcp_memory.core.storage import StorageService as GraphStorage

    core_settings.cache_clear()
    graph_settings.cache_clear()
    try:
        module = core_sm if request.param == "core" else graph_sm
        controller = module.SelectiveController(tmp_path / request.param)
        module.reset_controller_for_tests(controller)
        service = CoreStorage() if request.param == "core" else GraphStorage()
        client = service._client_data if request.param == "core" else service._client
        # Cleanup targets are populated only from run-owned, constructed keys.
        owned = []
        yield request.param, module, controller, service, client, scope, owned
    finally:
        # Raw cleanup of the fixture's exact keys does not attest settlement or
        # clear the deliberately durable unknown in its maintenance directory.
        if "client" in locals():
            for key in owned:
                client.delete_object(Bucket=bucket, Key=key)
            client.close()
        core_settings.cache_clear()
        graph_settings.cache_clear()


@pytest.mark.optional
@pytest.mark.parametrize("authorized_external_storage", ["core", "graph"], indirect=True)
@pytest.mark.parametrize("lost_response", [False, True])
async def test_authorized_external_s3_read_effect_lifetime(authorized_external_storage, lost_response):
    import threading

    kind, module, c, storage, client, scope, owned = authorized_external_storage
    content = b"identical bytes do not prove an in-flight write settled"
    if kind == "core":
        key = f"{scope}/bank/activeContext.md"
        async def put():
            await storage.put(key, content.decode())
        async def read():
            return (await storage.get(key)).encode()
    else:
        key = storage._get_key(scope, "source.md", storage.compute_hash(content))
        async def put():
            await storage.upload_document(scope, "source.md", content)
        async def read():
            return await storage.download_document(scope, key)
    owned.append(key)
    await put()  # initial real adapter request; future write has identical bytes
    started, complete = threading.Event(), threading.Event()
    original = client.put_object
    dispatched = []
    def delayed(**kwargs):
        started.set()
        if not complete.wait(10):
            raise TimeoutError("fixture effect was not released")
        result = original(**kwargs)  # real external storage request
        dispatched.append(kwargs["Key"])
        if lost_response:
            raise TimeoutError("injected loss after external S3 application")
        return result
    client.put_object = delayed
    async def request():
        with c.operation("read", scope):
            if kind == "core":
                await put()
            else:
                await asyncio.to_thread(lambda: asyncio.run(put()))
    task = asyncio.create_task(request())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        assert c.pause(OP, [scope], "all")["pending"] == 1
        assert await read() == content  # unchanged inventory is not settlement
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not c.status(OP)["capture_ready"]
        with c.operation("read", scope):
            with pytest.raises(module.MaintenanceRefused, match="maintenance_admission_closed"):
                await put()
        assert dispatched == []
    finally:
        complete.set()
        for _ in range(1000):
            if c.status(OP)["pending"] == 0:
                break
            await asyncio.sleep(.01)
        client.put_object = original
    assert dispatched == [key]
    status = c.status(OP)
    assert status["capture_ready"] is (not lost_response)
    assert status["unknown"] == int(lost_response)
    c.release()
    again = module.SelectiveController(c._dir)
    assert again.status(OP)["capture_ready"] is (not lost_response)
    if lost_response:
        with pytest.raises(module.MaintenanceRefused, match="maintenance_unclean"):
            again.resume(OP)


@pytest.mark.asyncio
@pytest.mark.parametrize("service_kind", ["core", "graph"])
@pytest.mark.parametrize("binding", ["completed", "copy", "foreign", "scope", "global", "multiple"])
async def test_actual_storage_rejects_invalid_mutation_owner(controller, service_kind, binding):
    import copy
    from live_mem.core.storage import StorageService as CoreStorage
    from mcp_memory.core.storage import StorageService

    module = core_sm if service_kind == "core" else graph_sm
    c = module.SelectiveController(None)
    module.reset_controller_for_tests(c)
    calls = []
    if service_kind == "core":
        storage = object.__new__(CoreStorage)
        storage.bucket = "b"
        storage._sse = storage._sse_kms_key_id = None
        storage._client_data = SimpleNamespace(put_object=lambda **kw: calls.append(kw))
        async def put(scope):
            await storage.put(("_system" if scope == "*" else scope) + "/x", "x")
    else:
        storage = object.__new__(StorageService)
        storage._bucket = "b"
        storage._client = SimpleNamespace(put_object=lambda **kw: calls.append(kw))
        async def put(scope):
            # Backup entry point also accounts for global/private roots.
            storage._write_call(storage._client.put_object, Key=("_system" if scope == "*" else scope) + "/x")
    owner = c.admit("mutation", "m1")
    supplied, destination = owner, "m1"
    code = "maintenance_binding"
    if binding == "completed":
        c.finish(owner)
    elif binding == "copy":
        supplied = copy.copy(owner)
    elif binding == "foreign":
        supplied = module.SelectiveController(None).admit("mutation", "m1")
    else:
        destination = "m2" if binding == "scope" else "*"
        code = "maintenance_admission_closed"
    if binding == "multiple":
        c.pause(str(uuid.uuid4()), ["m1"], "all")
    c.pause(OP, ["m2" if binding in ("scope", "global", "multiple") else "m1"], "all")
    with c.rebind(supplied):
        with pytest.raises(module.MaintenanceRefused, match=code):
            await put(destination)
    assert calls == []
    if binding != "completed":
        c.finish(owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create_token", "revoke_token", "update_token_memories",
                                       "update_token_permissions", "update_token_email"])
async def test_graph_token_writes_require_global_physical_ownership(controller, operation):
    from contextlib import asynccontextmanager
    from mcp_memory.core.graph import GraphService
    from mcp_memory.auth.token_manager import TokenManager

    graph = object.__new__(GraphService)
    reached = []
    @asynccontextmanager
    async def session():
        reached.append("session")
        yield SimpleNamespace()
    graph.session = session
    manager = TokenManager(graph_service=graph)
    controller.pause(OP, ["m2"], "all")
    arguments = {
        "create_token": {"client_name": "fixture"},
        "revoke_token": {"token_hash": "h"},
        "update_token_memories": {"token_hash": "h", "set_memories": ["m1"]},
        "update_token_permissions": {"token_hash": "h", "permissions": ["read"]},
        "update_token_email": {"token_hash": "h", "email": "test@example.invalid"},
    }
    with controller.operation("read", "m1"):
        with pytest.raises(graph_sm.MaintenanceRefused, match="maintenance_admission_closed"):
            await getattr(manager, operation)(**arguments[operation])
    assert reached == []
    assert controller.status(OP)["capture_ready"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["ontology_import", "ontology_update", "ontology_delete"])
async def test_graph_ontology_files_refuse_before_effect(controller, monkeypatch, tmp_path, operation):
    from mcp_memory import server

    path = tmp_path / "fixture.yaml"
    original = "name: fixture\n"
    path.write_text(original)
    monkeypatch.setattr(server, "check_admin_permission", lambda: None)
    monkeypatch.setattr(server, "_ontology_dir", lambda: str(tmp_path))
    monkeypatch.setattr(server, "_ontology_file_for_name", lambda name: str(path))
    monkeypatch.setattr(server, "_load_ontology_yaml", lambda content: {"name": "fixture"})
    monkeypatch.setattr(server, "get_graph", lambda: SimpleNamespace(list_memories=AsyncMock(return_value=[])))
    controller.pause(OP, ["m1"], "all")
    with controller.operation("read", "*"):
        if operation == "ontology_import":
            result = await server.ontology_import("name: fixture\nchanged: true", overwrite=True)
        elif operation == "ontology_update":
            result = await server.ontology_update("fixture", "name: fixture\nchanged: true")
        else:
            result = await server.ontology_delete("fixture")
    assert result["status"] == "error"
    assert result["message"] == "maintenance_admission_closed"
    assert path.read_text() == original
    assert controller.status(OP)["capture_ready"]


def test_graph_raw_sdk_mutations_have_no_unguarded_sibling():
    # Runtime adapter/commit tests above prove behavior; this inventory guard
    # prevents a future raw SDK sibling from silently escaping that boundary.
    raw = []
    for filename in ("core/storage.py", "core/backup.py", "core/vector_store.py"):
        path = ROOT / "services/graph-memory/src/mcp_memory" / filename
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"put_object", "delete_object", "copy_object", "upsert",
                                      "create_collection", "create_payload_index", "delete_collection",
                                      "delete", "update_collection_aliases"}:
                    raw.append((filename, ast.unparse(node.func)))
    assert raw == [("core/storage.py", "self._client_v2.put_object"),
                   ("core/storage.py", "self._client_v2.delete_object")]


@pytest.mark.asyncio
@pytest.mark.parametrize("service_kind", ["core", "graph"])
@pytest.mark.parametrize("status", [403, 412, 408, 503, "validation", "credentials"])
async def test_f1_s3_rejections_and_uncertainty_have_distinct_outcomes(service_kind, status, tmp_path):
    from botocore.exceptions import ClientError, ParamValidationError, NoCredentialsError
    from live_mem.core.storage import StorageService as CoreStorage
    from mcp_memory.core.storage import StorageService as GraphStorage

    module = core_sm if service_kind == "core" else graph_sm
    c = module.SelectiveController(tmp_path / service_kind)
    module.reset_controller_for_tests(c)
    codes = {403: "AccessDenied", 412: "PreconditionFailed", 408: "RequestTimeout", 503: "ServiceUnavailable"}
    if status == "validation":
        error = ParamValidationError(report="invalid SDK request")
    elif status == "credentials":
        error = NoCredentialsError()
    else:
        error = ClientError({"Error": {"Code": codes[status]},
                             "ResponseMetadata": {"HTTPStatusCode": status, "RetryAttempts": 0}}, "PutObject")
    calls = []
    def reject(**kwargs):
        calls.append(kwargs["Key"])
        raise error
    if service_kind == "core":
        storage = object.__new__(CoreStorage)
        storage.bucket = "b"
        storage._sse = storage._sse_kms_key_id = None
        storage._client_data = SimpleNamespace(put_object=reject)
        async def put():
            await storage.put("m1/_meta.json", "x")
    else:
        storage = object.__new__(GraphStorage)
        storage._bucket = "b"
        storage._client = SimpleNamespace(put_object=reject)
        async def put():
            await storage.upload_document("m1", "a.md", b"x")
    with c.operation("read", "m1"):
        with pytest.raises(type(error)) as raised:
            await put()
        assert raised.value is error
    assert len(calls) == 1
    unknown = status in (408, 503)
    assert c.status()["unknown"] == int(unknown)
    if not unknown:
        assert c.pause(OP, ["*"], "all")["capture_ready"]
    c.release()
    assert module.SelectiveController(c._dir).status()["unknown"] == int(unknown)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["duplicate", "ontology"])
async def test_f1_neo4j_logical_refusal_before_write_stays_settled(controller, action, tmp_path):
    from contextlib import asynccontextmanager
    from mcp_memory.core.graph import GraphService
    from mcp_memory.core.maintenance import reset_maintenance_coordinator_for_tests

    reset_maintenance_coordinator_for_tests()
    c = graph_sm.SelectiveController(tmp_path / "logical")
    graph_sm.reset_controller_for_tests(c)
    graph = object.__new__(GraphService)
    calls = []
    @asynccontextmanager
    async def session():
        async def run(query, **kwargs):
            calls.append(str(query))
            record = {"m": {"id": "m1"}} if action == "duplicate" else None
            return SimpleNamespace(single=AsyncMock(return_value=record))
        yield SimpleNamespace(run=run)
    graph.session = session
    with c.operation("read", "m1"):
        with pytest.raises(ValueError):
            if action == "duplicate":
                await graph.create_memory("m1", "duplicate")
            else:
                await graph.save_automatic_ontology("m1", expected_created_at="missing",
                                                    batch_fingerprint="a" * 64, checkpoint={})
    assert len(calls) == 1 and calls[0].strip().startswith("MATCH")
    assert c.status()["unknown"] == 0
    assert c.pause(OP, ["*"], "background")["state"] == "background_paused"
    c.release()
    assert graph_sm.SelectiveController(c._dir).status(OP)["state"] == "background_paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("service_kind", ["core", "graph"])
@pytest.mark.parametrize("kind,mode,admitted", [("mutation", "background", True),
                                              ("mutation", "all", True),
                                              ("short", "background", False),
                                              ("short", "all", True)])
async def test_f2_admitted_scoped_work_drains_global_effect(service_kind, kind, mode, admitted):
    from contextlib import asynccontextmanager
    from live_mem.core.storage import StorageService as CoreStorage
    from mcp_memory.core.graph import GraphService
    from mcp_memory.auth.token_manager import TokenManager

    module = core_sm if service_kind == "core" else graph_sm
    c = module.SelectiveController(None)
    module.reset_controller_for_tests(c)
    effects = []
    def observe():
        # A separate, wider child must remain visible to every relevant pause.
        assert c.current().scope == "*"
        assert c.status(OP)["pending"] >= 1
        effects.append("global write")
    if service_kind == "core":
        storage = object.__new__(CoreStorage)
        storage.bucket = "b"
        storage._sse = storage._sse_kms_key_id = None
        def put(**kwargs):
            observe()
            return {}
        storage._client_data = SimpleNamespace(put_object=put)
        async def write():
            await storage.put_json("_system/tokens.json", {"version": 2})
    else:
        graph = object.__new__(GraphService)
        @asynccontextmanager
        async def session():
            async def run(*args, **kwargs):
                observe()
                return SimpleNamespace(consume=AsyncMock())
            yield SimpleNamespace(run=run)
        graph.session = session
        manager = TokenManager(graph_service=graph)
        async def write():
            await manager.create_token("fixture")
    with c.operation(kind, "m1"):
        c.pause(OP, ["*"], mode)
        if admitted:
            await write()
        else:
            with pytest.raises(module.MaintenanceRefused, match="maintenance_admission_closed"):
                await write()
    assert effects == (["global write"] if admitted else [])
    assert c.status()["unknown"] == 0
    assert c.pause(OP, ["*"], "all")["capture_ready"]


@pytest.mark.asyncio
async def test_f1_logical_body_error_does_not_hide_failed_neo4j_close(controller):
    from contextlib import asynccontextmanager
    from mcp_memory.core.graph import GraphService

    service = object.__new__(GraphService)
    @asynccontextmanager
    async def session():
        try:
            yield SimpleNamespace()
        finally:
            raise ValueError("driver close failed")
    service.session = session
    with controller.operation("read", "m1"):
        with pytest.raises(ValueError, match="driver close failed"):
            async with service.mutation_session("m1"):
                raise ValueError("logical refusal")
    assert controller.status()["unknown"] == 1
    with pytest.raises(graph_sm.MaintenanceRefused, match="maintenance_unclean"):
        controller.pause(OP, ["*"], "all")


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", ["ClientError", "ConstraintError"])
@pytest.mark.parametrize("location", ["body", "close"])
async def test_f1_definitive_neo4j_rejection_is_settled(controller, rejection, location):
    from contextlib import asynccontextmanager
    from neo4j import exceptions
    from mcp_memory.core.graph import GraphService

    graph = object.__new__(GraphService)
    error = getattr(exceptions, rejection)("definitive rejection")
    @asynccontextmanager
    async def session():
        yield SimpleNamespace()
        if location == "close":
            raise error
    graph.session = session
    with controller.operation("read", "m1"):
        with pytest.raises(type(error)):
            async with graph.mutation_session("m1"):
                if location == "body":
                    raise error
    assert controller.status()["unknown"] == 0
    assert controller.pause(OP, ["*"], "all")["capture_ready"]


@pytest.mark.parametrize("status", [400, 408, 503, 429])
def test_f1_qdrant_rejection_or_transport_failure(controller, status):
    import httpx
    from qdrant_client.http.exceptions import UnexpectedResponse
    from mcp_memory.core.vector_store import VectorStoreService

    from qdrant_client.common.client_exceptions import ResourceExhaustedResponse
    graph = object.__new__(VectorStoreService)
    error = (ResourceExhaustedResponse("rejected", 1) if status == 429 else
             UnexpectedResponse(status, "rejected", b"", httpx.Headers()))
    def fail():
        raise error
    with controller.operation("read", "m1"):
        with pytest.raises(type(error)):
            graph._write_call("m1", fail)
    assert controller.status()["unknown"] == int(status not in (400, 429))
    if status in (400, 429):
        assert controller.pause(OP, ["*"], "all")["capture_ready"]


@pytest.mark.asyncio
@pytest.mark.parametrize("service_kind", ["core", "graph"])
@pytest.mark.parametrize("final_response", ["success", "4xx"])
async def test_f1_later_s3_response_cannot_settle_an_uncertain_retry(service_kind, final_response):
    from botocore.exceptions import ClientError
    from live_mem.core.storage import StorageService as CoreStorage
    from mcp_memory.core.storage import StorageService as GraphStorage

    module = core_sm if service_kind == "core" else graph_sm
    c = module.SelectiveController(None)
    module.reset_controller_for_tests(c)
    def retried(**kwargs):
        response = {"ResponseMetadata": {"HTTPStatusCode": 200, "RetryAttempts": 1}}
        if final_response == "4xx":
            response.update(Error={"Code": "AccessDenied"})
            response["ResponseMetadata"]["HTTPStatusCode"] = 403
            raise ClientError(response, "PutObject")
        return response
    if service_kind == "core":
        service = object.__new__(CoreStorage)
        service.bucket = "b"
        service._sse = service._sse_kms_key_id = None
        service._client_data = SimpleNamespace(put_object=retried)
        async def write():
            await service.put("m1/_meta.json", "x")
    else:
        service = object.__new__(GraphStorage)
        service._bucket = "b"
        service._client = SimpleNamespace(put_object=retried)
        async def write():
            await service.upload_document("m1", "a.md", b"x")
    with c.operation("read", "m1"):
        if final_response == "success":
            await write()  # applied result remains caller-visible success
        else:
            with pytest.raises(ClientError):
                await write()
    assert c.status()["unknown"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("service_kind", ["core", "graph"])
async def test_f2_wider_child_is_counted_by_a_later_unrelated_pause(service_kind):
    from live_mem.core.storage import StorageService as CoreStorage
    from mcp_memory.core.storage import StorageService as GraphStorage

    module = core_sm if service_kind == "core" else graph_sm
    c = module.SelectiveController(None)
    module.reset_controller_for_tests(c)
    calls = []
    def put(**kwargs):
        calls.append(kwargs["Key"])
        assert c.current().scope == "*"
        # m2 does not overlap the m1 root, but must wait for its global child.
        assert c.pause(OP, ["m2"], "all")["pending"] == 1
        return {}
    if service_kind == "core":
        service = object.__new__(CoreStorage)
        service.bucket = "b"
        service._sse = service._sse_kms_key_id = None
        service._client_data = SimpleNamespace(put_object=put)
        async def write():
            await service.put("_system/tokens.json", "x")
    else:
        service = object.__new__(GraphStorage)
        service._bucket = "b"
        service._client = SimpleNamespace(put_object=put)
        async def write():
            service._write_call(service._client.put_object, Key="_system/tokens.json")
    with c.operation("mutation", "m1"):
        await write()
    assert calls == ["_system/tokens.json"]
    assert c.status(OP)["capture_ready"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["logical", "logical_group", "transport", "transport_group"])
async def test_f1_neo4j_body_logic_and_driver_failures_stay_distinct(controller, failure):
    from contextlib import asynccontextmanager
    from neo4j.exceptions import ServiceUnavailable
    from mcp_memory.core.graph import GraphService

    service = object.__new__(GraphService)
    errors = {
        "logical": RuntimeError("logical refusal"),
        "logical_group": ExceptionGroup("logical refusal", [RuntimeError("logical refusal")]),
        "transport": ServiceUnavailable("driver connection lost"),
        "transport_group": ExceptionGroup("driver connection lost", [ServiceUnavailable("driver connection lost")]),
    }
    @asynccontextmanager
    async def session():
        async def run(*args, **kwargs):
            raise errors[failure]
        yield SimpleNamespace(run=run)
    service.session = session
    with controller.operation("read", "m1"):
        with pytest.raises(type(errors[failure])):
            async with service.mutation_session("m1") as owned:
                if failure.startswith("transport"):
                    await owned.run("CREATE (n:Memory)")
                else:
                    raise errors[failure]
    assert controller.status()["unknown"] == int(failure.startswith("transport"))
    if failure.startswith("logical"):
        assert controller.pause(OP, ["*"], "all")["capture_ready"]


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["run", "single", "consume", "iterate"])
async def test_f1_neo4j_sdk_uncertainty_survives_a_handled_error(controller, point):
    from contextlib import asynccontextmanager
    from mcp_memory.core.graph import GraphService

    service = object.__new__(GraphService)
    class Result:
        async def single(self):
            raise RuntimeError("uncertain SDK result")
        async def consume(self):
            raise RuntimeError("uncertain SDK result")
        def __aiter__(self):
            async def rows():
                raise RuntimeError("uncertain SDK result")
                yield  # pragma: no cover
            return rows()
    @asynccontextmanager
    async def session():
        async def run(*args, **kwargs):
            if point == "run":
                raise RuntimeError("uncertain SDK dispatch")
            return Result()
        yield SimpleNamespace(run=run)
    service.session = session
    with controller.operation("read", "m1"):
        async with service.mutation_session("m1") as owned:
            try:
                result = await owned.run("CREATE (n:Memory)")
                if point == "single":
                    await result.single()
                elif point == "consume":
                    await result.consume()
                elif point == "iterate":
                    async for _ in result:
                        pass
            except RuntimeError:
                pass  # existing caller error translation must not clear unknown
    assert controller.status()["unknown"] == 1
    with pytest.raises(graph_sm.MaintenanceRefused, match="maintenance_unclean"):
        controller.pause(OP, ["*"], "all")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["create_token", "update_token_memories", "update_token_permissions", "update_token_email"])
@pytest.mark.parametrize("outcome", ["success", "transport", "rejected"])
async def test_r2_token_commit_is_observed_before_real_driver_close(controller, method, outcome):
    from neo4j._async.work.session import AsyncSession
    from neo4j._conf import SessionConfig
    from neo4j.exceptions import ClientError, ServiceUnavailable
    from mcp_memory.core.graph import GraphService
    from mcp_memory.auth.token_manager import TokenManager

    error = {"success": None, "transport": ServiceUnavailable("commit response lost"),
             "rejected": ClientError("definitively rejected")}[outcome]
    raw = AsyncSession(SimpleNamespace(), SessionConfig())
    raw._connection = SimpleNamespace(send_all=AsyncMock(), fetch_all=AsyncMock())
    raw._disconnect = AsyncMock()
    pending = SimpleNamespace(consume=AsyncMock(side_effect=error))
    async def run(query, **kwargs):
        if "RETURN t" in str(query):
            return SimpleNamespace(single=AsyncMock(return_value={"t": {
                "client_name": "fixture", "memory_ids": [], "permissions": [], "email": "old"}}))
        raw._auto_result = pending
        return pending
    raw.run = run
    graph = object.__new__(GraphService)
    graph._driver = SimpleNamespace(session=lambda **kwargs: raw)
    graph._database = "neo4j"
    manager = TokenManager(graph_service=graph)
    arguments = {
        "create_token": {"client_name": "fixture"},
        "update_token_memories": {"token_hash": "h", "add_memories": ["m1"]},
        "update_token_permissions": {"token_hash": "h", "permissions": ["read"]},
        "update_token_email": {"token_hash": "h", "email": "new"},
    }
    with controller.operation("read", "m1"):
        if error is not None:
            with pytest.raises(type(error)) as raised:
                await getattr(manager, method)(**arguments[method])
            assert raised.value is error
        else:
            assert await getattr(manager, method)(**arguments[method]) is not None
    assert raw._closed and pending.consume.await_count >= 1
    assert controller.status()["unknown"] == int(outcome == "transport")
    if outcome == "transport":
        with pytest.raises(graph_sm.MaintenanceRefused, match="maintenance_unclean"):
            controller.pause(OP, ["*"], "all")
    else:
        assert controller.pause(OP, ["*"], "all")["capture_ready"]
