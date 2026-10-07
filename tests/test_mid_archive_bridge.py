"""Archive routing through the real bridge; fake storage/transport, no inference."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from live_mem.core.engines.long_engine import LongEngine
from live_mem.core.graph_bridge import GraphBridgeService
from tests.fakes import GraphLongFakeStorage
from tests.test_long_ingest_async import unbound_ingest_bridge  # noqa: F401


async def storage_pin(storage, memory_id, space_id="space-a"):
    return await storage.get_json(f"{space_id}/_mid_archive_bindings/{memory_id}.json")


async def read_archive_pin(case):
    meta = await case.storage.get_json("space-a/_meta.json")
    return await case.storage.get_json(case.bridge._archive_binding_key("space-a", meta))


async def local_archive_case(case, monkeypatch, binding="explicit"):
    from live_mem.core.models import EMBEDDED_TOKEN_SENTINEL

    monkeypatch.setattr("live_mem.core.graph_bridge.get_settings", lambda: SimpleNamespace(
        long_embedded_url="https://GRAPH.EXAMPLE.COM/sse/",
    ))
    monkeypatch.setattr("live_mem.core.graph_bridge.resolve_embedded_token", lambda *a, **kw: "test-secret")
    meta = await case.storage.get_json("space-a/_meta.json")
    meta["graph_memory"]["binding"] = binding
    if binding == "embedded":
        meta["graph_memory"]["token"] = EMBEDDED_TOKEN_SENTINEL
    await case.storage.put_json("space-a/_meta.json", meta)
    return case.bridge._archive_memory_id("space-a", meta)


@pytest.fixture
async def archive_case(monkeypatch):
    storage = GraphLongFakeStorage()
    await storage.put_json("space-a/_meta.json", {
        "space_id": "space-a", "created_at": "2026-09-16T08:00:00+00:00",
        "graph_memory": {
            "binding": "explicit", "url": "https://graph.example.com/mcp",
            "token": "test-secret", "memory_id": "documentary", "ontology": "cloud",
        },
    })
    monkeypatch.setattr("live_mem.core.graph_bridge.get_storage", lambda: storage)
    remote = {"documentary": {
        "id": "documentary", "created_at": "2026-09-15T08:00:00", "ontology": "cloud",
    }}
    calls = []

    async def call_tool(name, arguments):
        calls.append((name, deepcopy(arguments)))
        if name == "system_health":
            return {"status": "healthy"}
        if name == "memory_list":
            return {"status": "ok", "memories": list(deepcopy(remote).values())}
        if name == "memory_create":
            marker = (await storage.get_json(f"space-a/_mid_archive_bindings/{arguments['memory_id']}.json"))
            assert marker["memory_id"] == arguments["memory_id"]
            assert marker["initial_preimage_id"]
            remote[arguments["memory_id"]] = {
                "id": arguments["memory_id"], "ontology": arguments["ontology"],
                "created_at": "2026-09-16T09:00:00.123456",
            }
            return {"status": "created"}
        if name == "memory_ingest_batch_async":
            if arguments["memory_id"] != "documentary":
                marker = (await storage.get_json(f"space-a/_mid_archive_bindings/{arguments['memory_id']}.json"))
                assert marker["remote_created_at"] == remote[arguments["memory_id"]]["created_at"]
            return {"status": "ok", "batch_id": "batch-1", "items": [{"status": "queued"}]}
        if name == "ingest_job_status":
            return {"status": "ok", "job": {"job_id": arguments["job_id"], "status": "completed"}}
        if name == "ingest_job_list":
            return {"status": "ok", "memory_id": arguments["memory_id"],
                    "total": 1, "jobs": [{"job_id": "job-1", "status": "running",
                                           "current_step": "extracting", "progress_percent": 40}]}
        if name == "document_get":
            return {"status": "ok", "document": {"id": "doc-1", "source_path": arguments["source_path"], "sha256": "sha"}}
        if name == "memory_stats":
            return {"status": "ok", "embedding_collection": {"state": "missing"},
                    "embedding_identity": {"configured": {"provider": "openai-compatible", "model": "qwen-test", "dimensions": 1024}, "persisted": None}}
        raise AssertionError(name)

    client = SimpleNamespace(call_tool=AsyncMock(side_effect=call_tool))
    bridge = GraphBridgeService(client_factory=lambda *a, **kw: client, url_validator=lambda *a, **kw: None)
    return SimpleNamespace(storage=storage, remote=remote, calls=calls, client=client,
                           bridge=bridge, engine=LongEngine(bridge=bridge))


async def test_archive_first_capture_pins_before_egress_and_resumes_after_restart(archive_case):
    c = archive_case
    assert await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1") == {"status": "ok", "ontology_mode": "auto"}
    meta = await c.storage.get_json("space-a/_meta.json")
    marker = await read_archive_pin(c)
    assert marker["memory_id"] != "documentary"
    assert marker["memory_id"] == "mid_904d2560db94ee25fc24001f872f82d29ec2f87d"
    assert len(marker["memory_id"]) <= 64
    assert set(marker) == {"memory_id", "initial_preimage_id", "remote_created_at"}
    assert "mid_archive" not in meta["graph_memory"]
    assert "test-secret" not in str(marker)
    assert meta["graph_memory"]["ontology"] == "cloud"
    assert meta["graph_memory"]["token"] == "test-secret"
    restarted = GraphBridgeService(client_factory=lambda *a, **kw: c.client, url_validator=lambda *a, **kw: None)
    assert await restarted.prepare_archive_ingest("space-a", preimage_id="capture-1") == {"status": "ok", "ontology_mode": "auto"}
    pending = await restarted.prepare_archive_ingest("space-a", preimage_id="capture-2")
    assert pending["reason"] == "initial_capture_pending"
    assert len([call for call in c.calls if call[0] == "memory_create"]) == 1
    result = await c.engine.ingest_archive("space-a", documents=[{"filename": "old.md"}], automatic=True)
    assert result["batch_id"] == "batch-1"
    args = c.calls[-1][1]
    assert args["memory_id"] == marker["memory_id"]
    assert args["ontology"] == "auto" and args["replace_existing"] is False


@pytest.mark.parametrize("binding", ["embedded", "explicit"])
@pytest.mark.parametrize("sentinel", ["_meta.json", "bank/.keep", "_hivemind/members.json", "unavailable"])
async def test_archive_provisioning_refuses_hivemind_owned_prefix(archive_case, monkeypatch, binding, sentinel):
    c = archive_case
    archive_id = await local_archive_case(c, monkeypatch, binding)
    if sentinel == "unavailable":
        original_get = c.storage.get
        async def failed_get(key):
            if key.startswith(archive_id + "/"):
                raise OSError("storage unavailable")
            return await original_get(key)
        monkeypatch.setattr(c.storage, "get", failed_get)
    else:
        await c.storage.put(f"{archive_id}/{sentinel}", "historical authority")
    before = c.storage.snapshot()
    result = await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1")
    assert result["status"] == "error" and result.get("recovery_required") is True
    assert c.storage.snapshot() == before and c.calls == []


@pytest.mark.parametrize("binding", ["embedded", "explicit"])
@pytest.mark.parametrize("operation", ["prepare", "document", "ingest", "status"])
async def test_existing_archive_pin_refuses_historical_collision(archive_case, monkeypatch, binding, operation):
    c = archive_case
    archive_id = await local_archive_case(c, monkeypatch, binding)
    assert (await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1"))["status"] == "ok"
    c.remote[archive_id]["ontology"] = "auto_0123456789abcdef"
    await c.storage.put(f"{archive_id}/_rules.md", "historical authority")
    before = c.storage.snapshot()
    c.calls.clear()
    calls = {
        "prepare": lambda: c.engine.prepare_archive_ingest("space-a", preimage_id="capture-2"),
        "document": lambda: c.engine.get_archive_document("space-a", source_path="old.md"),
        "ingest": lambda: c.engine.ingest_archive("space-a", documents=[{}], automatic=False),
        "status": lambda: c.engine.archive_ingest_status("space-a", "job-1"),
    }
    result = await calls[operation]()
    assert result["status"] == "error" and result.get("recovery_required") is True
    assert c.storage.snapshot() == before and c.calls == []


async def test_remote_archive_does_not_claim_local_storage(archive_case, monkeypatch):
    c = archive_case
    monkeypatch.setattr("live_mem.core.graph_bridge.get_settings", lambda: SimpleNamespace(
        long_embedded_url="http://graph-memory:8002",
    ))
    meta = await c.storage.get_json("space-a/_meta.json")
    archive_id = c.bridge._archive_memory_id("space-a", meta)
    await c.storage.put(f"{archive_id}/_meta.json", "unrelated local authority")
    assert (await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1"))["status"] == "ok"
    assert (await c.engine.get_archive_document("space-a", source_path="old.md"))["status"] == "ok"
    assert await c.storage.get(f"{archive_id}/_meta.json") == "unrelated local authority"


@pytest.mark.parametrize("operation", ["query", "document"])
async def test_reserved_archive_id_is_not_a_public_long_space(archive_case, operation):
    c = archive_case
    meta = await c.storage.get_json("space-a/_meta.json")
    archive_id = c.bridge._archive_memory_id("space-a", meta)
    await c.storage.put_json(f"{archive_id}/_meta.json", {**meta, "space_id": archive_id})
    before = c.storage.snapshot()
    if operation == "query":
        result = await c.engine.query(archive_id, "historical fact")
    else:
        result = await c.engine.get_document(archive_id, source_path="old.md")
    assert result["status"] == "error" and result.get("recovery_required") is True
    assert c.storage.snapshot() == before and c.calls == []


async def test_archive_frozen_catalogue_and_documentary_choices_remain_independent(archive_case):
    c = archive_case
    await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1")
    marker = (await read_archive_pin(c))
    refused = await c.engine.ingest_archive("space-a", documents=[{}], automatic=False)
    assert refused["reason"] == "initial_capture_pending"
    c.remote[marker["memory_id"]]["ontology"] = "auto_0123456789abcdef"
    assert await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-2") == {"status": "ok", "ontology_mode": "frozen"}
    await c.engine.ingest_archive("space-a", documents=[{"filename": "old.md"}], automatic=False)
    archive_args = c.calls[-1][1]
    assert archive_args["memory_id"] == marker["memory_id"] and "ontology" not in archive_args
    await c.engine.ingest_async("space-a", documents=[{"filename": "new.md"}], options={"ontology": "legal"})
    assert c.calls[-1][1]["memory_id"] == "documentary"
    assert c.calls[-1][1]["ontology"] == "legal"
    assert (await c.engine.archive_ingest_status("space-a", "job-1"))["job"]["status"] == "completed"
    assert c.calls[-1][1]["expected_memory_id"] == marker["memory_id"]
    assert (await c.engine.get_archive_document("space-a", source_path="archive/old.md"))["document"]["document_id"] == "doc-1"
    assert c.calls[-1][1]["memory_id"] == marker["memory_id"]


async def test_archive_job_list_reads_only_pinned_archive_without_provisioning(archive_case):
    c = archive_case
    missing = await c.engine.ingest_list("space-a", archive=True, status="running", limit=10)
    assert missing["reason"] == "archive_not_configured"
    assert not c.calls

    assert (await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1"))["status"] == "ok"
    marker = await read_archive_pin(c)
    c.calls.clear()
    before = c.storage.snapshot()
    archive = await c.engine.ingest_list("space-a", archive=True, status="running", limit=10)
    assert archive["status"] == "ok" and archive["memory_id"] == marker["memory_id"]
    assert archive["jobs"][0]["current_step"] == "extracting"
    assert c.calls[-1] == ("ingest_job_list", {"memory_id": marker["memory_id"],
                                               "limit": 10, "offset": 0, "status": "running"})
    assert not any(name == "memory_create" for name, _ in c.calls)
    assert c.storage.snapshot() == before

    primary = await c.engine.ingest_list("space-a", status="running", limit=10)
    assert primary["memory_id"] == "documentary"
    assert c.calls[-1][1]["memory_id"] == "documentary"


async def test_archive_model_status_reads_pinned_namespace_and_refuses_changed_incarnation(archive_case):
    c = archive_case
    assert (await c.bridge.archive_index_status("space-a"))["reason"] == "archive_not_configured"
    assert not c.calls
    await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1")
    marker = await read_archive_pin(c)
    c.calls.clear()
    before = c.storage.snapshot()
    result = await c.bridge.archive_index_status("space-a")
    assert result["embedding_identity"]["configured"]["model"] == "qwen-test"
    assert c.calls[-1] == ("memory_stats", {"memory_id": marker["memory_id"]})
    assert c.storage.snapshot() == before
    assert not any(name == "memory_create" for name, _ in c.calls)
    c.remote[marker["memory_id"]]["created_at"] = "replacement"
    c.calls.clear()
    refused = await c.bridge.archive_index_status("space-a")
    assert refused["reason"] == "archive_destination_changed"
    assert not any(name == "memory_stats" for name, _ in c.calls)


async def test_archive_job_list_refuses_changed_archive_incarnation(archive_case):
    c = archive_case
    await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1")
    marker = await read_archive_pin(c)
    c.remote[marker["memory_id"]]["created_at"] = "replacement-incarnation"
    c.calls.clear()
    result = await c.engine.ingest_list("space-a", archive=True)
    assert result["status"] == "error" and result["reason"] == "archive_destination_changed"
    assert not any(name == "ingest_job_list" for name, _ in c.calls)


async def test_archive_job_list_reports_graph_read_failure_without_primary_fallback(archive_case):
    c = archive_case
    await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1")
    original = c.client.call_tool.side_effect

    async def fail_list(name, arguments):
        if name == "ingest_job_list":
            raise ConnectionError("synthetic archive outage")
        return await original(name, arguments)

    c.client.call_tool.side_effect = fail_list
    c.calls.clear()
    result = await c.engine.ingest_list("space-a", archive=True, status="running", limit=10)
    assert result["status"] == "error" and result["reason"] == "archive_unavailable"
    assert not any(args.get("memory_id") == "documentary" for name, args in c.calls
                   if name == "ingest_job_list")


@pytest.mark.parametrize("change", ["parent", "url", "space", "remote", "missing", "ontology"])
async def test_archive_refuses_changed_destination_before_content(archive_case, change):
    c = archive_case
    await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1")
    meta = await c.storage.get_json("space-a/_meta.json")
    archive_id = (await read_archive_pin(c))["memory_id"]
    if change == "parent":
        meta["graph_memory"]["memory_id"] = "other-parent"
    elif change == "url":
        meta["graph_memory"]["url"] = "https://other.example.com/mcp"
    elif change == "space":
        meta["created_at"] = "2026-09-17T08:00:00+00:00"
    elif change == "remote":
        c.remote[archive_id]["created_at"] = "2026-09-17T09:00:00"
    elif change == "missing":
        del c.remote[archive_id]
    else:
        c.remote[archive_id]["ontology"] = "auto_cloud"
    await c.storage.put_json("space-a/_meta.json", meta)
    c.calls.clear()
    if change in {"remote", "missing", "ontology"}:
        result = await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1")
        assert result["reason"] == "archive_destination_changed"
    else:
        # A different scope has no pin yet. Existing pending records reject
        # this change via their own binding hash; a read must not create it.
        result = await c.engine.get_archive_document("space-a", source_path="old.md")
        assert result["reason"] == "archive_not_configured"
    result = await c.engine.ingest_archive("space-a", documents=[{}], automatic=True)
    assert result["status"] == "error"
    assert not any(name in {"memory_create", "memory_ingest_batch_async"} for name, _ in c.calls)


async def test_archive_read_without_marker_never_provisions(archive_case):
    c = archive_case
    before = c.storage.snapshot()
    for response in (await c.engine.get_archive_document("space-a", source_path="archive/old.md"),
                     await c.engine.archive_ingest_status("space-a", "job-1")):
        assert response["status"] == "error"
        assert response["reason"] == "archive_not_configured"
    assert c.calls == [] and c.storage.snapshot() == before


async def test_reconnect_same_parent_preserves_archive_marker(archive_case):
    c = archive_case
    await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1")
    before = (await read_archive_pin(c))
    result = await c.bridge._connect_locked("space-a", "https://graph.example.com/mcp", "rotated", "documentary", "legal")
    assert result["status"] == "connected"
    after = (await c.storage.get_json("space-a/_meta.json"))["graph_memory"]
    assert await read_archive_pin(c) == before
    assert "mid_archive" not in after
    assert after["token"] == "rotated" and after["ontology"] == "legal"


async def test_archive_refuses_unverified_marker_write(archive_case, monkeypatch):
    c = archive_case
    monkeypatch.setattr(c.storage, "put_json", AsyncMock())
    result = await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1")
    assert result["status"] == "error"
    assert not any(name in {"memory_create", "memory_ingest_batch_async"} for name, _ in c.calls)


async def test_archive_reuses_shell_after_remote_pin_write_failure(archive_case, monkeypatch):
    c = archive_case
    real_put = c.storage.put_json

    async def fail_remote_pin(key, value):
        if "/_mid_archive_bindings/" in key and value.get("remote_created_at"):
            raise OSError("storage unavailable")
        await real_put(key, value)

    monkeypatch.setattr(c.storage, "put_json", fail_remote_pin)
    assert (await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1"))["status"] == "error"
    assert (await c.engine.ingest_archive("space-a", documents=[{}], automatic=True))["status"] == "error"
    assert not any(name == "memory_ingest_batch_async" for name, _ in c.calls)
    monkeypatch.setattr(c.storage, "put_json", real_put)
    assert await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1") == {"status": "ok", "ontology_mode": "auto"}
    assert len([call for call in c.calls if call[0] == "memory_create"]) == 1


async def test_archive_bootstraps_embedded_parent_without_duplicating_secret(unbound_ingest_bridge):
    from live_mem.core.memory_id import derive_memory_id
    from live_mem.core.models import EMBEDDED_TOKEN_SENTINEL

    c = unbound_ingest_bridge
    remote = {}

    async def call_tool(name, arguments):
        if name == "system_health":
            return {"status": "healthy"}
        if name == "memory_list":
            return {"status": "ok", "memories": list(remote.values())}
        if name == "memory_create":
            if arguments["memory_id"] != derive_memory_id("test-space"):
                meta = await c.storage.get_json("test-space/_meta.json")
                assert (await storage_pin(c.storage, arguments["memory_id"], "test-space"))["memory_id"] == arguments["memory_id"]
            remote[arguments["memory_id"]] = {
                "id": arguments["memory_id"], "created_at": "2026-09-16T09:00:00", "ontology": "general",
            }
            return {"status": "created"}
        raise AssertionError(name)

    c.client.call_tool.side_effect = call_tool
    assert await c.bridge.prepare_archive_ingest("test-space", preimage_id="capture-1") == {"status": "ok", "ontology_mode": "auto"}
    meta = await c.storage.get_json("test-space/_meta.json")
    assert len(remote) == 2
    assert meta["graph_memory"]["memory_id"] == derive_memory_id("test-space")
    assert meta["graph_memory"]["token"] == EMBEDDED_TOKEN_SENTINEL
    assert "live-test-secret" not in c.storage.objects["test-space/_meta.json"]


async def test_archive_failed_listing_never_creates_a_namespace(archive_case):
    c = archive_case
    c.client.call_tool.side_effect = None
    c.client.call_tool.return_value = {"status": "error", "message": "forbidden"}
    assert (await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1"))["reason"] == "archive_unavailable"
    assert [call.args[0] for call in c.client.call_tool.await_args_list] == ["memory_list"]


@pytest.mark.parametrize("loss", ["disconnect_reconnect", "restore"])
async def test_archive_readopts_frozen_namespace_after_marker_loss(archive_case, loss):
    c = archive_case
    before_archive = await c.storage.get_json("space-a/_meta.json")
    await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1")
    original = (await read_archive_pin(c))
    c.remote[original["memory_id"]]["ontology"] = "auto_0123456789abcdef"
    if loss == "disconnect_reconnect":
        assert (await c.bridge.disconnect("space-a"))["status"] == "disconnected"
        assert (await c.bridge._connect_locked("space-a", "https://graph.example.com/mcp",
                                               "rotated", "documentary", "cloud"))["status"] == "connected"
    else:
        await c.storage.put_json("space-a/_meta.json", before_archive)
    # Unlike ordinary disconnect/metadata restore, actual loss of derived
    # local state requires re-adoption of the surviving frozen namespace.
    c.storage.objects.pop(f"space-a/_mid_archive_bindings/{original['memory_id']}.json")
    c.calls.clear()
    assert await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-2") == {
        "status": "ok", "ontology_mode": "frozen"}
    marker = (await read_archive_pin(c))
    assert marker["memory_id"] == original["memory_id"]
    assert marker["remote_created_at"] == original["remote_created_at"]
    assert (await c.engine.get_archive_document("space-a", source_path="old.md"))["status"] == "ok"
    assert not any(name in {"memory_create", "memory_ingest_batch_async"} for name, _ in c.calls)


@pytest.mark.parametrize("ontology", ["general", "cloud", "auto_cloud"])
async def test_archive_unknown_existing_namespace_never_leaves_half_marker(archive_case, ontology):
    c = archive_case
    before = await c.storage.get_json("space-a/_meta.json")
    await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1")
    marker = (await read_archive_pin(c))
    c.remote[marker["memory_id"]]["ontology"] = ontology
    await c.storage.put_json("space-a/_meta.json", before)
    c.storage.objects.pop(f"space-a/_mid_archive_bindings/{marker['memory_id']}.json")
    c.calls.clear()
    result = await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-2")
    assert result["reason"] == "archive_destination_changed"
    assert await c.storage.get_json("space-a/_meta.json") == before
    assert await read_archive_pin(c) is None
    assert not any(name in {"memory_create", "memory_ingest_batch_async"} for name, _ in c.calls)


async def test_archive_counter_updates_during_meta_reads_do_not_change_destination(archive_case, monkeypatch):
    c = archive_case
    get_json = c.storage.get_json

    async def concurrent_push_count(key):
        value = await get_json(key)
        if key == "space-a/_meta.json":
            value["graph_memory"]["push_count"] = value["graph_memory"].get("push_count", 0) + 1
            await c.storage.put_json(key, value)
        return value

    monkeypatch.setattr(c.storage, "get_json", concurrent_push_count)
    assert await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1") == {"status": "ok", "ontology_mode": "auto"}
    meta = await get_json("space-a/_meta.json")
    assert meta["graph_memory"]["push_count"] > 1
    assert (await read_archive_pin(c))["remote_created_at"]
    assert (await c.engine.get_archive_document("space-a", source_path="old.md"))["status"] == "ok"


@pytest.mark.parametrize("change", ["memory_id", "url", "created_at"])
async def test_archive_identity_change_between_context_reads_is_still_refused(archive_case, monkeypatch, change):
    c = archive_case
    await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1")
    get_json, reads = c.storage.get_json, 0

    async def change_destination(key):
        nonlocal reads
        value = await get_json(key)
        if key == "space-a/_meta.json":
            reads += 1
            if reads == 2:
                if change == "created_at":
                    value[change] = "2026-09-17T08:00:00+00:00"
                else:
                    value["graph_memory"][change] = {"memory_id": "changed", "url": "https://other.example.com/mcp"}[change]
                await c.storage.put_json(key, value)
        return value

    c.calls.clear()
    monkeypatch.setattr(c.storage, "get_json", change_destination)
    assert (await c.engine.get_archive_document("space-a", source_path="old.md"))["status"] == "error"
    assert c.calls == []


@pytest.mark.parametrize("name", [None, 42, {}, "", "general", "auto_cloud", "auto_0123456789abcdef",
                                  "auto_0123456789abcde", "auto_0123456789abcdef0", "auto_0123456789ABCDEF",
                                  "auto_0123456789abcdef\n"])
def test_archive_frozen_name_agrees_with_graph_catalogue_contract(name):
    from mcp_memory.core.ontology import is_automatic_ontology_name

    assert GraphBridgeService._archive_frozen({"ontology": name}) == is_automatic_ontology_name(name)


@pytest.mark.parametrize("frozen", [False, True])
async def test_real_push_cannot_erase_archive_prepared_after_its_metadata_read(archive_case, frozen):
    c = archive_case
    await c.storage.put("space-a/bank/systemPatterns.md", "Documentary patterns")
    original_call = c.client.call_tool.side_effect
    pinned = None

    async def call_tool(name, arguments):
        if name == "document_list":
            return {"status": "ok", "documents": []}
        return await original_call(name, arguments)

    async def batch(calls):
        nonlocal pinned
        assert calls and calls[0][0] == "memory_ingest"
        assert await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1") == {
            "status": "ok", "ontology_mode": "auto"}
        _, _, pinned, err = await c.bridge._archive_context("space-a")
        assert err is None
        if frozen:
            c.remote[pinned["memory_id"]]["ontology"] = "auto_0123456789abcdef"
        return [{"status": "ok"} for _ in calls]

    c.client.call_tool.side_effect = call_tool
    c.client.call_tools_batch = AsyncMock(side_effect=batch)
    result = await c.engine.push("space-a")
    assert result["status"] == "ok" and result["pushed"] == 1
    assert await storage_pin(c.storage, pinned["memory_id"]) == pinned
    assert (await c.engine.get_archive_document("space-a", source_path="old.md"))["status"] == "ok"
    expected = "frozen" if frozen else "auto"
    assert await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1") == {"status": "ok", "ontology_mode": expected}


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("operation", ["reconnect", "restore_meta"])
async def test_archive_pin_survives_lifecycle_and_is_readable_before_worker(archive_case, frozen, operation):
    c = archive_case
    original_meta = await c.storage.get_json("space-a/_meta.json")
    await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1")
    _, _, original_pin, err = await c.bridge._archive_context("space-a")
    assert err is None
    if frozen:
        c.remote[original_pin["memory_id"]]["ontology"] = "auto_0123456789abcdef"
    if operation == "reconnect":
        assert (await c.bridge.disconnect("space-a"))["status"] == "disconnected"
        assert (await c.bridge._connect_locked("space-a", "https://graph.example.com/mcp",
                                               "rotated", "documentary", "cloud"))["status"] == "connected"
    else:
        await c.storage.put_json("space-a/_meta.json", original_meta)
    c.calls.clear()
    client, archive_id, err = await c.bridge._optional_archive_reader("space-a")
    assert err is None and client is not None and archive_id == original_pin["memory_id"]
    original_call = c.client.call_tool.side_effect

    async def list_documents(name, args):
        if name == "document_list":
            c.calls.append((name, deepcopy(args)))
            return {"status": "ok", "documents": [], "total_count": 0}
        return await original_call(name, args)

    c.client.call_tool.side_effect = list_documents
    assert (await c.engine.list_documents("space-a"))["status"] == "ok"
    assert {args["memory_id"] for name, args in c.calls if name == "document_list"} == {
        "documentary", original_pin["memory_id"]}
    assert (await c.engine.get_archive_document("space-a", source_path="old.md"))["status"] == "ok"
    assert await storage_pin(c.storage, original_pin["memory_id"]) == original_pin
    assert not any(name in {"memory_create", "memory_ingest_batch_async"} for name, _ in c.calls)
    if not frozen:
        assert (await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-2"))["reason"] == "initial_capture_pending"
    assert await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1") == {
        "status": "ok", "ontology_mode": "frozen" if frozen else "auto"}


async def test_new_parent_has_its_own_pin_and_return_to_old_parent_resumes_it(archive_case):
    c = archive_case
    await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1")
    first = await read_archive_pin(c)
    c.remote["another-parent"] = {"id": "another-parent", "ontology": "cloud", "created_at": "2026-09-17T09:00:00"}
    assert (await c.bridge._connect_locked("space-a", "https://graph.example.com/mcp",
                                           "rotated", "another-parent", "cloud"))["status"] == "connected"
    assert await c.bridge._optional_archive_reader("space-a") == (None, None, None)
    assert await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-2") == {"status": "ok", "ontology_mode": "auto"}
    second = await read_archive_pin(c)
    assert second["memory_id"] != first["memory_id"] and second["initial_preimage_id"] == "capture-2"
    assert (await c.bridge._connect_locked("space-a", "https://graph.example.com/mcp",
                                           "rotated", "documentary", "cloud"))["status"] == "connected"
    assert await read_archive_pin(c) == first
    assert (await c.bridge._optional_archive_reader("space-a"))[1] == first["memory_id"]
    assert await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1") == {"status": "ok", "ontology_mode": "auto"}
    assert await storage_pin(c.storage, second["memory_id"]) == second


async def test_archive_pin_write_refuses_a_concurrently_changed_pin(archive_case):
    c = archive_case
    original_call = c.client.call_tool.side_effect
    meta = await c.storage.get_json("space-a/_meta.json")
    key = c.bridge._archive_binding_key("space-a", meta)
    conflicting = {"memory_id": c.bridge._archive_memory_id("space-a", meta), "initial_preimage_id": "other-capture"}

    async def change_pin(name, args):
        if name == "memory_list":
            await c.storage.put_json(key, conflicting)
        return await original_call(name, args)

    c.client.call_tool.side_effect = change_pin
    assert (await c.engine.prepare_archive_ingest("space-a", preimage_id="capture-1"))["status"] == "error"
    assert await c.storage.get_json(key) == conflicting
    assert not any(name in {"memory_create", "memory_ingest_batch_async"} for name, _ in c.calls)
