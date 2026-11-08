"""#1420: staged selective admission for the 1.6 source (Core side).

Existing-equivalent search (before adding): tests/test_p13_maintenance_gate.py
covers the per-memory Graph coordinator only; nothing covers a staged,
deployment-wide, durable barrier or the tool-level read/short/mutation split.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from live_mem.core import selective_maintenance as sm
from live_mem.core.selective_maintenance import (
    MaintenanceRefused,
    SelectiveController,
)

OP = str(uuid.uuid4())


def make(tmp_path):
    return SelectiveController(tmp_path / "state")


def test_background_closes_mutation_but_not_short_or_read(tmp_path):
    c = make(tmp_path)
    c.pause(OP, ["*"], "background")
    with c.operation("short", "s1"), c.operation("read", "s1"):
        pass
    with pytest.raises(MaintenanceRefused):
        c.admit("mutation", "s1")


def test_all_closes_short_and_cutover_closes_reads(tmp_path):
    c = make(tmp_path)
    c.pause(OP, ["*"], "all")
    with pytest.raises(MaintenanceRefused):
        c.admit("short", "s1")
    c.admit("read", "s1")
    c.pause(OP, ["*"], "cutover")
    with pytest.raises(MaintenanceRefused):
        c.admit("read", "s1")


def test_scope_isolation(tmp_path):
    c = make(tmp_path)
    c.pause(OP, ["spaceA"], "all")
    c.admit("short", "spaceB")
    with pytest.raises(MaintenanceRefused):
        c.admit("short", "spaceA")


def test_admitted_work_drains_before_read_only_and_resume(tmp_path):
    c = make(tmp_path)
    ticket = c.admit("short", "s1")
    st = c.pause(OP, ["*"], "all")
    assert st["state"] == "all_write_draining" and not st["capture_ready"]
    with pytest.raises(MaintenanceRefused):
        c.resume(OP)
    c.finish(ticket)
    st = c.status(OP)
    assert st["state"] == "read_only" and st["capture_ready"] and st["reads_available"]
    assert c.resume(OP)["state"] == "normal"


def test_retained_ticket_keeps_ownership_until_last_ref(tmp_path):
    c = make(tmp_path)
    ticket = c.admit("mutation", "s1")
    c.retain(ticket)  # e.g. a queued job
    c.pause(OP, ["*"], "background")
    c.finish(ticket)
    assert c.status(OP)["pending"] == 1
    c.finish(ticket)
    assert c.status(OP)["state"] == "background_paused"


def test_cancelled_owner_is_durable_unknown(tmp_path):
    c = make(tmp_path)
    with pytest.raises(asyncio.CancelledError):
        with c.operation("mutation", "s1"):
            raise asyncio.CancelledError()
    assert c.status()["state"] == "recovery_required"
    # New pause over the uncertain scope refuses BEFORE closing anything.
    with pytest.raises(MaintenanceRefused):
        c.pause(OP, ["s1"], "all")
    c.admit("short", "s1")
    # Survives a restart.
    again = make(tmp_path)
    assert again.status()["unknown"] >= 1


def test_ordinary_exception_is_settled_not_unknown(tmp_path):
    c = make(tmp_path)
    with pytest.raises(ValueError):
        with c.operation("mutation", "s1"):
            raise ValueError
    assert c.status()["unknown"] == 0


def test_pause_survives_restart_and_never_auto_resumes(tmp_path):
    c = make(tmp_path)
    c.pause(OP, ["*"], "all")
    c.release()
    again = make(tmp_path)
    assert again.status(OP)["state"] == "read_only"
    with pytest.raises(MaintenanceRefused):
        again.admit("short", "s1")


def test_unclean_restart_marks_unknown_and_keeps_pause(tmp_path):
    c = make(tmp_path)
    c.pause(OP, ["*"], "all")
    # No release(): the process "crashed".
    again = make(tmp_path)
    st = again.status(OP)
    assert st["state"] == "recovery_required" and not st["capture_ready"]
    with pytest.raises(MaintenanceRefused):
        again.resume(OP)


def test_pending_ticket_blocks_clean_release_marker(tmp_path):
    c = make(tmp_path)
    c.admit("mutation", "s1")
    c.release()
    assert make(tmp_path).status()["unknown"] == 1


def test_same_uuid_cannot_change_scopes_or_downgrade(tmp_path):
    c = make(tmp_path)
    c.pause(OP, ["a"], "all")
    with pytest.raises(MaintenanceRefused):
        c.pause(OP, ["b"], "all")
    with pytest.raises(MaintenanceRefused):
        c.pause(OP, ["a"], "background")
    assert c.pause(OP, ["a"], "cutover")["mode"] == "cutover"


def test_overlapping_controller_refused(tmp_path):
    c = make(tmp_path)
    c.pause(OP, ["a"], "all")
    with pytest.raises(MaintenanceRefused):
        c.pause(str(uuid.uuid4()), ["*"], "all")


def test_invalid_inputs_refused(tmp_path):
    c = make(tmp_path)
    for args in (("not-a-uuid", ["*"], "all"), (OP, [], "all"), (OP, ["*"], "x"),
                 (OP, ["../x"], "all"), (OP, [1], "all")):
        with pytest.raises(MaintenanceRefused):
            c.pause(*args)


def test_corrupt_state_fails_closed(tmp_path):
    c = make(tmp_path)
    c.pause(OP, ["*"], "all")
    (tmp_path / "state" / sm.STATE_FILE).write_text("{not json")
    with pytest.raises(MaintenanceRefused):
        make(tmp_path)


def test_state_file_is_private_and_holds_no_secret(tmp_path):
    c = make(tmp_path)
    c.pause(OP, ["a"], "all")
    path = tmp_path / "state" / sm.STATE_FILE
    assert path.stat().st_mode & 0o777 == 0o600
    assert set(json.loads(path.read_text())) == {
        "version", "process", "controllers", "retired_unknown"}


def test_nested_effect_inherits_root_and_is_not_readmitted(tmp_path):
    c = make(tmp_path)
    with c.operation("mutation", "s1"):
        c.pause(OP, ["*"], "all")  # closes while the root is in flight
        with c.operation("mutation", "s1"):  # nested SDK/child effect
            c.check_physical("s1")
    assert c.status(OP)["state"] == "read_only"


def test_stray_physical_write_refused_when_closed(tmp_path):
    c = make(tmp_path)
    c.pause(OP, ["*"], "background")
    with pytest.raises(MaintenanceRefused):
        c.check_physical("s1")


# ─── Core tool gate: every registered handler, MCP and /api/tool alike ────────

from live_mem.tools import call_tool_direct, register_all_tools  # noqa: E402
from live_mem.tools.exposure import (  # noqa: E402
    TOOL_EXPOSURES,
    HivemindMCPServer,
    ToolOperation,
)
from live_mem.tools.maintenance_gate import REFUSAL, SHORT_WRITE_TOOLS  # noqa: E402


@pytest.fixture
def gated(tmp_path):
    controller = SelectiveController(tmp_path / "state")
    sm.reset_controller_for_tests(controller)
    mcp = HivemindMCPServer("selective-gate")
    register_all_tools(mcp)
    yield mcp, controller
    sm.reset_controller_for_tests(None)


def _names(operation):
    return [n for e in TOOL_EXPOSURES if e.operation is operation
            for n in (e.canonical_name, *e.aliases)]


def test_every_registered_handler_is_gated(gated):
    mcp, _ = gated
    assert all(getattr(t.fn, "_selective_gate", False)
               for t in mcp._tool_manager._tools.values())


@pytest.mark.asyncio
async def test_all_mutation_names_refused_at_background(gated):
    _, controller = gated
    controller.pause(OP, ["*"], "background")
    closed = [n for n in _names(ToolOperation.MUTATION)
              if n not in SHORT_WRITE_TOOLS and n not in _short_aliases()]
    assert closed
    for name in closed:
        assert await call_tool_direct(name, {}) == REFUSAL, name


def _short_aliases():
    return {a for e in TOOL_EXPOSURES if e.canonical_name in SHORT_WRITE_TOOLS
            for a in e.aliases}


@pytest.mark.asyncio
async def test_short_write_names_admitted_at_background_refused_at_all(gated):
    _, controller = gated
    shorts = sorted(SHORT_WRITE_TOOLS | _short_aliases())
    controller.pause(OP, ["*"], "background")
    for name in shorts:
        assert await call_tool_direct(name, {}) != REFUSAL, name
    controller.pause(OP, ["*"], "all")
    for name in shorts:
        assert await call_tool_direct(name, {}) == REFUSAL, name


@pytest.mark.asyncio
async def test_reads_stay_admitted_until_cutover(gated):
    _, controller = gated
    reads = _names(ToolOperation.READ)
    controller.pause(OP, ["*"], "all")
    for name in reads:
        assert await call_tool_direct(name, {}) != REFUSAL, name
    controller.pause(OP, ["*"], "cutover")
    for name in reads:
        assert await call_tool_direct(name, {}) == REFUSAL, name


def test_short_write_set_names_registered_canonical_tools():
    # Drift guard: a renamed SHORT writer must not silently become a
    # "mutation" (closed too early) or leave this set dangling.
    canonical = {e.canonical_name for e in TOOL_EXPOSURES
                 if e.operation is ToolOperation.MUTATION}
    assert SHORT_WRITE_TOOLS <= canonical


# ─── /api/maintenance control plane ───────────────────────────────────────────

from live_mem.auth.context import current_token_info  # noqa: E402
from live_mem.auth.middleware import StaticFilesMiddleware  # noqa: E402


async def _control(body, *, bearer=True, permissions=("admin",)):
    sent = []
    messages = [{"type": "http.request", "body": json.dumps(body).encode()}]

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/api/maintenance",
             "headers": [(b"authorization", b"Bearer x")] if bearer else []}
    token = current_token_info.set({"permissions": list(permissions), "client_name": "t"})
    try:
        await StaticFilesMiddleware(None)._api_maintenance(scope, receive, send)
    finally:
        current_token_info.reset(token)
    status = sent[0]["status"]
    return status, json.loads(sent[1]["body"])


@pytest.mark.asyncio
async def test_control_plane_pause_status_resume_and_stays_usable_in_cutover(gated):
    status, body = await _control({"action": "pause", "operation_id": OP,
                                   "scopes": ["*"], "mode": "cutover"})
    assert status == 200 and body["state"] == "exclusive_cutover"
    # Reads are closed at cutover, the control plane is not.
    status, body = await _control({"action": "status", "operation_id": OP})
    assert status == 200 and body["capture_ready"] is False
    status, body = await _control({"action": "resume", "operation_id": OP})
    assert status == 200 and body["state"] == "normal"


@pytest.mark.asyncio
async def test_control_plane_requires_admin_and_explicit_bearer(gated):
    req = {"action": "pause", "operation_id": OP, "scopes": ["*"], "mode": "all"}
    assert (await _control(req, permissions=("read", "write", "manage")))[0] == 403
    assert (await _control(req, bearer=False))[0] == 401  # ambient cookie refused
    assert gated[1].status()["state"] == "normal"


@pytest.mark.asyncio
async def test_control_plane_refusals_are_fixed_codes(gated):
    status, body = await _control({"action": "pause", "operation_id": "x",
                                   "scopes": ["*"], "mode": "all"})
    assert (status, body) == (409, {"status": "error", "error": "maintenance_input"})


# ─── physical storage gate ────────────────────────────────────────────────────

from live_mem.core.storage import StorageService  # noqa: E402


def _storage():
    service = StorageService.__new__(StorageService)
    service.bucket = "b"
    service._sse = None
    service._sse_kms_key_id = None
    calls = []

    class Client:
        def put_object(self, **kw): calls.append(("put", kw["Key"]))
        def delete_object(self, **kw): calls.append(("delete", kw["Key"]))
        def copy_object(self, **kw): calls.append(("copy", kw["Key"]))

    service._client_data = Client()
    return service, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("key,scope_paused,refused", [
    ("spaceA/live/n.md", "spaceA", True),
    ("spaceB/live/n.md", "spaceA", False),
    ("_backups/spaceA/ts/x", "spaceA", True),
    ("_system/tokens.json", "spaceA", True),     # deployment-wide root
    ("_health_check/probe", "spaceA", False),    # exact diagnostic exemption
])
@pytest.mark.parametrize("read_origin", [False, True])
async def test_physical_put_and_delete_gate(gated, key, scope_paused, refused, read_origin):
    _, controller = gated
    controller.pause(OP, [scope_paused], "background")
    storage, calls = _storage()
    from contextlib import nullcontext
    with controller.operation("read", "*") if read_origin else nullcontext():
        for op in (storage.put(key, "x"), storage.delete(key)):
            if refused:
                with pytest.raises(MaintenanceRefused):
                    await op
            else:
                await op
    assert bool(calls) is (not refused)


@pytest.mark.asyncio
async def test_delete_many_and_copy_are_gated_not_swallowed(gated):
    _, controller = gated
    controller.pause(OP, ["spaceA"], "background")
    storage, calls = _storage()
    with pytest.raises(MaintenanceRefused):  # delete_many swallows per-key errors
        await storage.delete_many(["spaceA/a", "spaceA/b"])
    with pytest.raises(MaintenanceRefused):
        await storage.copy_object("spaceB/x", "spaceA/y")
    assert calls == []


@pytest.mark.asyncio
async def test_owned_work_writes_through_a_closed_barrier(gated):
    _, controller = gated
    storage, calls = _storage()
    with controller.operation("short", "spaceA"):
        controller.pause(OP, ["spaceA"], "all")  # closes while owned work runs
        await storage.put("spaceA/live/n.md", "x")
    assert calls == [("put", "spaceA/live/n.md")]


# ─── queued consolidation: accepted work drains through a closing barrier ─────

from unittest.mock import patch  # noqa: E402

from live_mem.core.consolidation_queue import (  # noqa: E402
    ConsolidationQueueService,
)
from tests.test_consolidation_queue import (  # noqa: E402
    FakeConsolidator,
    _NonHiveStorageGate,
)


@pytest.mark.asyncio
async def test_accepted_queue_job_finishes_after_all_closes_and_settles(gated):
    _, controller = gated
    fake = FakeConsolidator()
    queue = ConsolidationQueueService()
    with _NonHiveStorageGate(), patch(
            "live_mem.core.consolidation_queue.get_consolidator", return_value=fake):
        # The tool call that accepts the job owns a root ticket.
        with controller.operation("mutation", "proj"):
            payload = await queue.enqueue("proj", "agent", "tester")
        await asyncio.wait_for(fake.started.wait(), 5)
        st = controller.pause(OP, ["*"], "all")
        assert st["state"] == "all_write_draining" and st["pending"] == 1
        with pytest.raises(MaintenanceRefused):  # new work is refused ...
            await queue.enqueue("proj", "other-agent", "tester")
        fake.release.set()  # ... the accepted job still completes
        for _ in range(100):
            if controller.status(OP)["state"] == "read_only":
                break
            await asyncio.sleep(0.02)
    assert controller.status(OP)["state"] == "read_only"
    assert (await queue.get_job(payload["job_id"]))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_closed_barrier_refuses_enqueue_without_leaving_a_job(gated):
    _, controller = gated
    controller.pause(OP, ["*"], "background")
    queue = ConsolidationQueueService()
    with _NonHiveStorageGate():
        with pytest.raises(MaintenanceRefused):
            await queue.enqueue("proj", "agent", "tester")
    assert queue._jobs == {} and controller.status()["unknown"] == 0


@pytest.mark.asyncio
async def test_malformed_space_id_keeps_the_handlers_own_validation_error(gated):
    # The gate must not turn bad input into a maintenance exception.
    out = await call_tool_direct("space_info", {"space_id": "../etc"})
    assert out != REFUSAL and "maintenance" not in json.dumps(out)


# ─── Mesh: peer routes and admin mutations are owned work ─────────────────────

from unittest.mock import AsyncMock as _AsyncMock  # noqa: E402

from live_mem.mesh.mesh_admin import MeshAdminMiddleware  # noqa: E402
from live_mem.mesh.router import MeshNamespaceRouter  # noqa: E402


def _router():
    router = MeshNamespaceRouter.__new__(MeshNamespaceRouter)
    router.is_mesh_namespace = lambda scope: True
    router._process_lock = type("L", (), {"acquired": True})()
    router._handle = _AsyncMock()
    router._send_unsigned = _AsyncMock()
    router._fallback = _AsyncMock()
    return router


@pytest.mark.asyncio
async def test_mesh_peer_request_refused_with_retryable_503_when_closed(gated):
    _, controller = gated
    router = _router()
    controller.pause(OP, ["*"], "background")
    await router({"type": "http"}, None, None)
    router._send_unsigned.assert_awaited_once_with(None, 503)
    router._handle.assert_not_awaited()
    controller.resume(OP)
    await router({"type": "http"}, None, None)
    router._handle.assert_awaited_once()


@pytest.mark.asyncio
async def test_mesh_admin_mutation_refused_but_read_admitted(gated):
    _, controller = gated
    served = []
    admin = MeshAdminMiddleware.__new__(MeshAdminMiddleware)
    admin._serve = lambda scope, receive, send: _record(served, scope)
    sent = []
    admin._json = lambda send, body, status=200: _record(sent, (body, status))
    controller.pause(OP, ["*"], "background")
    base = {"type": "http", "path": "/api/admin/mesh/pair"}
    await admin({**base, "method": "POST"}, None, None)
    assert sent == [({"status": "error", "error": "maintenance_admission_closed"}, 503)]
    await admin({**base, "method": "GET"}, None, None)
    assert len(served) == 1 and served[0]["method"] == "GET"
    controller.pause(OP, ["*"], "cutover")  # reads are owned work too (F4)
    await admin({**base, "method": "GET"}, None, None)
    assert len(served) == 1 and len(sent) == 2


async def _record(bucket, item):
    bucket.append(item)


def test_aliases_keep_sharing_their_canonical_handler_object(gated):
    mcp, _ = gated
    tools = mcp._tool_manager._tools
    assert tools["short_note"].fn is tools["live_note"].fn


# ─── Round-1 review ledger (PR #1479) ─────────────────────────────────────────

def test_f1_default_state_dir_is_outside_the_secrets_volume_and_created_by_the_image():
    # The one-shot initializer accepts an exact grammar under /data/secrets and
    # would refuse the next start if Core created a sibling there.
    from pathlib import Path
    from live_mem.config import Settings

    default = Settings.model_fields["maintenance_state_dir"].default
    assert default and not default.startswith("/data/secrets")
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    assert f"install -d -o mcp -g mcp -m 0700 {default}" in dockerfile


@pytest.mark.asyncio
async def test_f2_cancelled_automatic_compaction_settles_as_unknown(gated):
    _, controller = gated
    fake = FakeConsolidator()
    fake.release.set()
    queue = ConsolidationQueueService()

    async def interrupted(space_id):
        error = asyncio.CancelledError()
        error.mid_auto_compaction_result = {"status": "cancelled", "recovery_required": True}
        raise error

    with _NonHiveStorageGate(), patch(
            "live_mem.core.consolidation_queue.get_consolidator", return_value=fake), patch(
            "live_mem.core.mid_automation.compact_after_consolidation", interrupted):
        with controller.operation("mutation", "proj"):
            await queue.enqueue("proj", "agent", "tester")
        for _ in range(100):
            if controller.status()["unknown"]:
                break
            await asyncio.sleep(0.02)
    # Drained but NOT certified: the cancelled compaction left unknown effects.
    st = controller.pause(str(uuid.uuid4()), ["other"], "all")
    assert controller.status()["unknown"] == 1 and st["capture_ready"] is True
    with pytest.raises(MaintenanceRefused):
        controller.pause(OP, ["proj"], "all")


def test_f3_failed_resume_persistence_keeps_admission_closed(tmp_path, monkeypatch):
    c = make(tmp_path)
    c.pause(OP, ["*"], "all")
    monkeypatch.setattr(c, "_persist", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(MaintenanceRefused) as refusal:
        c.resume(OP)
    assert refusal.value.code == "maintenance_recovery_required"
    with pytest.raises(MaintenanceRefused):
        c.admit("short", "s1")  # still closed in memory, matching the disk


def test_f3_failed_pause_persistence_still_closes_and_reports_it(tmp_path, monkeypatch):
    c = make(tmp_path)
    monkeypatch.setattr(c, "_persist", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(MaintenanceRefused) as refusal:
        c.pause(OP, ["*"], "all")
    assert refusal.value.code == "maintenance_recovery_required"
    with pytest.raises(MaintenanceRefused):
        c.admit("short", "s1")  # ambiguous disk: closed, never a false "open"


def test_f3_unknown_persistence_failure_does_not_mask_the_handler(tmp_path, monkeypatch):
    c = make(tmp_path)
    monkeypatch.setattr(c, "_persist", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(asyncio.CancelledError):
        with c.operation("mutation", "s1"):
            raise asyncio.CancelledError()
    assert c.status()["unknown"] == 1  # kept in memory; disk receipt stays "active"


@pytest.mark.asyncio
async def test_f4_functional_rest_reads_are_refused_and_drained_at_cutover(gated):
    _, controller = gated
    served = []
    mw = StaticFilesMiddleware.__new__(StaticFilesMiddleware)
    mw._serve = lambda scope, receive, send: _record(served, scope["path"])
    sent = []

    async def send(message):
        sent.append(message)

    mw._send_json = lambda s, body, status=200: _record(sent, (body, status))
    controller.pause(OP, ["*"], "all")
    for path in ("/api/spaces", "/api/space/p", "/api/live/p", "/api/bank/p/f"):
        await mw({"type": "http", "method": "GET", "path": path}, None, send)
    assert len(served) == 4  # reads stay admitted before cutover
    controller.pause(OP, ["*"], "cutover")
    for path in ("/api/spaces", "/api/live/p"):
        await mw({"type": "http", "method": "GET", "path": path}, None, send)
    assert len(served) == 4
    assert sent.count(({"status": "error", "error": "maintenance_admission_closed"}, 503)) == 2
    # Exempt on purpose: health, control plane, login, static, tool proxy.
    for path, method in (("/health", "GET"), ("/api/maintenance", "POST"),
                         ("/api/login", "POST"), ("/static/x.js", "GET"), ("/api/tool", "POST")):
        await mw({"type": "http", "method": method, "path": path}, None, send)
    assert len(served) == 9


@pytest.mark.asyncio
async def test_f4_a_read_in_flight_holds_cutover_until_it_finishes(gated):
    _, controller = gated
    mw = StaticFilesMiddleware.__new__(StaticFilesMiddleware)
    gate, started = asyncio.Event(), asyncio.Event()

    async def slow(scope, receive, send):
        started.set()
        await gate.wait()

    mw._serve = slow
    task = asyncio.create_task(mw({"type": "http", "method": "GET", "path": "/api/spaces"}, None, None))
    await started.wait()
    st = controller.pause(OP, ["*"], "cutover")
    assert st["state"] == "cutover_draining" and st["pending"] == 1
    gate.set()
    await task
    assert controller.status(OP)["state"] == "exclusive_cutover"


# #1523: missing physical lifetime/binding conditions; equivalent search and
# rationale are recorded in PLAN-1523-read-physical-ownership.md.
@pytest.fixture(params=["core", "graph"])
def physical_controller(request, tmp_path):
    from mcp_memory.core import selective_maintenance as graph_sm

    module = sm if request.param == "core" else graph_sm
    c = module.SelectiveController(tmp_path / request.param)
    module.reset_controller_for_tests(c)
    yield module, c
    module.reset_controller_for_tests(None)


@pytest.mark.parametrize("mode", ["background", "all", "cutover"])
def test_read_origin_cannot_write_after_pause(physical_controller, mode):
    module, c = physical_controller
    with c.operation("read", "s1"):
        c.pause(OP, ["s1"], mode)
        with pytest.raises(module.MaintenanceRefused, match="maintenance_admission_closed"):
            c.check_physical("s1")
    assert c.status(OP)["pending"] == 0


def test_nested_owner_keeps_a_reference(physical_controller):
    module, c = physical_controller
    parent = c.admit("mutation", "s1")
    with c.rebind(parent), c.operation("mutation", "s1"):
        c.finish(parent)  # request completes while retained nested work remains
        assert c.pause(OP, ["s1"], "all")["pending"] == 1
        c.check_physical("s1")
    assert c.status(OP)["capture_ready"]


@pytest.mark.parametrize("binding", ["completed", "copy", "foreign", "scope", "global"])
def test_physical_binding_never_bypasses_admission(physical_controller, binding):
    import copy

    module, c = physical_controller
    owner = c.admit("mutation", "s1")
    supplied = owner
    destination = "s1"
    code = "maintenance_binding"
    if binding == "completed":
        c.finish(owner)
    elif binding == "copy":
        supplied = copy.copy(owner)
    elif binding == "foreign":
        supplied = module.SelectiveController(None).admit("mutation", "s1")
    elif binding == "scope":
        destination = "s2"
        code = "maintenance_admission_closed"
    elif binding == "global":
        destination = "*"
        code = "maintenance_admission_closed"
    c.pause(OP, ["s2" if binding in ("scope", "global") else "s1"], "all")
    with c.rebind(supplied):
        with pytest.raises(module.MaintenanceRefused, match=code):
            c.check_physical(destination)
    if binding != "completed":
        c.finish(owner)


@pytest.mark.asyncio
async def test_read_origin_core_sdk_drains_after_caller_cancellation(gated):
    import threading

    _, c = gated
    storage, calls = _storage()
    started, complete = threading.Event(), threading.Event()
    original = storage._client_data.put_object

    def delayed(**kw):
        started.set()
        assert complete.wait(5)
        original(**kw)

    storage._client_data.put_object = delayed
    async def read_then_repair():
        with c.operation("read", "s1"):
            await storage.put("s1/_meta.json", "repair")
    task = asyncio.create_task(read_then_repair())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        st = c.pause(OP, ["s1"], "all")
        assert st["pending"] == 1 and not st["capture_ready"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert c.status(OP)["pending"] == 1
        c.release()
        # A restart while the SDK is running inherits uncertainty.
        assert SelectiveController(c._dir).status(OP)["state"] == "recovery_required"
    finally:
        complete.set()
        if not task.done():
            await task
        for _ in range(200):
            if c.status(OP)["pending"] == 0:
                break
            await asyncio.sleep(.005)
    assert calls == [("put", "s1/_meta.json")]
    assert c.status(OP)["capture_ready"]


@pytest.mark.asyncio
async def test_read_origin_core_lost_response_is_durable_unknown(gated):
    _, c = gated
    storage, calls = _storage()
    def uncertain(**kw):
        calls.append(("applied", kw["Key"]))
        raise TimeoutError("response lost")
    storage._client_data.put_object = uncertain
    with c.operation("read", "s1"):
        with pytest.raises(TimeoutError):
            await storage.put("s1/_meta.json", "repair")
    assert calls == [("applied", "s1/_meta.json")]
    assert c.status()["unknown"] == 1
    c.release()
    assert SelectiveController(c._dir).status()["unknown"] == 1
