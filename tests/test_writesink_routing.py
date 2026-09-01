# -*- coding: utf-8 -*-
"""
End-to-end tool-path WriteSink routing tests for P3-7 (issue #56).

Proves the route-first-then-delegate contract AT THE TOOL ENTRYPOINTS:

- NON-HIVEMIND: durable mutations are byte-for-byte identical to the legacy
  storage path (they route through DirectLocalWriteSink, which delegates verbatim
  to the same storage the tool would have used).
- HIVEMIND-HEALTHY (STAGED): every durable mutation fails closed
  (StagedWriteNotImplemented surfaced as a safe_error) and writes / deletes
  NOTHING — the staged sink refuses BEFORE any S3 write.
- CORRUPT critical _hivemind file: CorruptedStateError propagates to safe_error;
  the durable op never runs and NEVER reaches DirectLocalWriteSink.
- UNSAFE / RESYNC (REFUSE): RegistryRefused surfaced as safe_error; no write.

graph_* tools are downstream-derived (ADR-0010): they have NO resolve_sink gate
and the SSRF check stays in the tool layer — asserted here too.

Deterministic and offline: the tool's registry is patched to a DI-built
EngineRegistry over a WriteSinkFakeStorage (which has delete_many), and the
legacy get_storage seams the DIRECT_LOCAL path falls through to are patched to
the SAME fake. No real S3 / boto3 / network / LLM.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from unittest.mock import patch

import pytest
from mcp.server.fastmcp import FastMCP

from live_mem.auth.context import current_token_info
from live_mem.core.engines import EngineRegistry, RegistryRefused
from live_mem.core.hivemind import (
    HiveNodeStatus,
    HivemindStateStore,
    Member,
    MembershipView,
    NodeHealth,
    NodeIdentity,
    generate_peer_keypair,
    layout,
)
from live_mem.core.hivemind.lifecycle import WriteRoute
from live_mem.core.hivemind.models import CorruptedStateError
from live_mem.core.live import LiveService
from live_mem.core.write_sink import StagedWriteNotImplemented
from live_mem.tools.bank import register as register_bank_tools
from live_mem.tools.graph import register as register_graph_tools
from live_mem.tools.live import register as register_live_tools
from tests.test_write_sink import WriteSinkFakeStorage


# =============================================================================
# Helpers
# =============================================================================


class _RecordingConsolidator:
    """A consolidator whose compact_bank records calls — to PROVE the
    resolve_sink gate fires BEFORE the consolidator runs on a fail-closed
    space (compact_bank must NOT be called)."""

    def __init__(self) -> None:
        self.compact_calls: list[tuple[str, bool]] = []

    async def compact_bank(self, space_id: str, dry_run: bool = True) -> dict:
        self.compact_calls.append((space_id, dry_run))
        return {"status": "ok", "space_id": space_id, "compacted": []}


def _admin_token(name: str = "admin") -> dict:
    return {
        "client_name": name,
        "permissions": ["read", "write", "manage", "admin"],
        "allowed_resources": [],
    }


def _tool(register, name: str):
    mcp = FastMCP(name="test")
    register(mcp)
    tool = mcp._tool_manager._tools[name]
    for attr in ("fn", "func", "handler", "_fn", "run", "callback"):
        fn = getattr(tool, attr, None)
        if callable(fn):
            return fn
    raise AssertionError(f"Tool {name} has no callable")


def test_bank_compact_apply_is_not_idempotent() -> None:
    mcp = FastMCP(name="test")
    register_bank_tools(mcp)

    assert mcp._tool_manager._tools["bank_compact"].annotations.idempotentHint is False


async def _seed_meta(storage: WriteSinkFakeStorage, space_id: str) -> None:
    await storage.put(f"{space_id}/_meta.json", "{}")


async def _seed_healthy_hive(storage: WriteSinkFakeStorage, space_id: str) -> None:
    """node.json + 1 ACTIVE member with a real Ed25519 key -> HEALTHY hive
    (STAGED route). Also seeds _meta.json so the tool's existence checks pass."""
    await _seed_meta(storage, space_id)
    store = HivemindStateStore(storage=storage, space_id=space_id)  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(
        NodeIdentity(node_id="n1", public_key=keys.public_key)
    )
    await store.set_membership(
        MembershipView(epoch=1, members=[Member(node_id="n1", public_key=keys.public_key)])
    )


class _Patched:
    """Context manager that points BOTH the registry and the legacy get_storage
    seams (the DIRECT_LOCAL fall-through targets) at the same fake."""

    def __init__(self, storage: WriteSinkFakeStorage, *, consolidator=None) -> None:
        self.storage = storage
        self.registry = EngineRegistry(
            storage=storage,
            live=LiveService(),
            consolidator=consolidator,
            queue=object(),
            bridge=object(),
        )
        self._patches = []

    def __enter__(self):
        # Tools import get_engine_registry LOCALLY inside each function
        # (`from ..core.engines import get_engine_registry`), so patch the
        # source symbol, not a module-level tool attribute.
        p = patch(
            "live_mem.core.engines.get_engine_registry",
            return_value=self.registry,
        )
        p.start()
        self._patches.append(p)
        # Legacy get_storage seams the DIRECT_LOCAL path / LiveService fall
        # through to (byte-for-byte legacy path) + the DirectLocalWriteSink
        # default-resolution seam.
        for t in (
            "live_mem.core.storage.get_storage",
            "live_mem.core.live.get_storage",
            "live_mem.core.write_sink.get_storage",
        ):
            p = patch(t, return_value=self.storage)
            p.start()
            self._patches.append(p)
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


@pytest.fixture(autouse=True)
def _token():
    tok = current_token_info.set(_admin_token())
    yield
    current_token_info.reset(tok)


def _bank_keys(storage: WriteSinkFakeStorage, space_id: str) -> set[str]:
    return {k for k in storage.objects if k.startswith(f"{space_id}/bank/")}


def _live_keys(storage: WriteSinkFakeStorage, space_id: str) -> set[str]:
    return {k for k in storage.objects if k.startswith(f"{space_id}/live/")}


# =============================================================================
# live_note
# =============================================================================


async def test_live_note_non_hivemind_byte_identical() -> None:
    """live_note on a non-Hivemind space writes the SAME bytes as the legacy
    LiveService path (frozen ts/uuid/agent), via DirectLocalWriteSink."""
    fixed = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)

    class _Dt:
        @classmethod
        def now(cls, tz=None):
            return fixed

    class _Uuid:
        class _U:
            hex = "deadbeefcafef00d"

        @staticmethod
        def uuid4():
            return _Uuid._U()

    # Legacy reference bytes.
    legacy_storage = WriteSinkFakeStorage()
    with patch("live_mem.core.live.get_storage", return_value=legacy_storage), patch(
        "live_mem.core.live.datetime", _Dt
    ), patch("live_mem.core.live.uuid", _Uuid):
        await _seed_meta(legacy_storage, "space-a")
        legacy_res = await LiveService().write_note(
            "space-a", "observation", "hello", tags="a,b"
        )
    legacy_key = f"space-a/live/{legacy_res['filename']}"
    legacy_bytes = legacy_storage.objects[legacy_key]

    # Tool path through the registry (DIRECT_LOCAL).
    storage = WriteSinkFakeStorage()
    with _Patched(storage), patch("live_mem.core.live.datetime", _Dt), patch(
        "live_mem.core.live.uuid", _Uuid
    ):
        await _seed_meta(storage, "space-a")
        res = await _tool(register_live_tools, "live_note")(
            space_id="space-a", category="observation", content="hello", tags="a,b"
        )

    assert res["status"] == "created"
    key = f"space-a/live/{res['filename']}"
    assert key == legacy_key
    assert storage.objects[key] == legacy_bytes


async def test_live_note_healthy_hive_fails_closed_no_write() -> None:
    """live_note on a HEALTHY hive -> safe_error, and NO live/* object written
    (StagedWriteNotImplemented raised BEFORE LiveService runs)."""
    storage = WriteSinkFakeStorage()
    with _Patched(storage):
        await _seed_healthy_hive(storage, "hive-a")
        before = storage.snapshot()
        res = await _tool(register_live_tools, "live_note")(
            space_id="hive-a", category="observation", content="hello", tags=""
        )

    assert res["status"] == "error"
    assert _live_keys(storage, "hive-a") == set()
    assert storage.objects == before  # nothing written at all


async def test_live_note_corrupted_hive_safe_error_no_write() -> None:
    """Corrupt node.json -> safe_error, no live/* object, never DirectLocal."""
    storage = WriteSinkFakeStorage()
    with _Patched(storage):
        await _seed_meta(storage, "hive-c")
        storage.objects[layout.node_key("hive-c")] = "{not valid json"
        before = storage.snapshot()
        res = await _tool(register_live_tools, "live_note")(
            space_id="hive-c", category="observation", content="x", tags=""
        )

    assert res["status"] == "error"
    assert _live_keys(storage, "hive-c") == set()
    assert storage.objects == before


# =============================================================================
# bank_write
# =============================================================================


async def test_bank_write_non_hivemind_byte_identical() -> None:
    """bank_write on non-Hivemind produces the identical bank object as a direct
    storage.put (same key, same content)."""
    storage = WriteSinkFakeStorage()
    with _Patched(storage):
        await _seed_meta(storage, "space-a")
        res = await _tool(register_bank_tools, "bank_write")(
            space_id="space-a", filename="activeContext.md", content="# Hello\n"
        )

    assert res["status"] == "ok"
    assert storage.objects["space-a/bank/activeContext.md"] == "# Hello\n"


async def test_bank_write_healthy_hive_fails_closed_no_write() -> None:
    storage = WriteSinkFakeStorage()
    with _Patched(storage):
        await _seed_healthy_hive(storage, "hive-a")
        before = storage.snapshot()
        res = await _tool(register_bank_tools, "bank_write")(
            space_id="hive-a", filename="activeContext.md", content="# Hello\n"
        )

    assert res["status"] == "error"
    assert _bank_keys(storage, "hive-a") == set()
    assert storage.objects == before


# =============================================================================
# bank_delete (delete_many)
# =============================================================================


async def test_bank_delete_non_hivemind_routes_delete_many() -> None:
    """bank_delete on non-Hivemind deletes via sink.delete_many (count matches
    the keys removed). Requires WriteSinkFakeStorage.delete_many."""
    storage = WriteSinkFakeStorage()
    with _Patched(storage):
        await _seed_meta(storage, "space-a")
        await storage.put("space-a/bank/notes.md", "content")
        res = await _tool(register_bank_tools, "bank_delete")(
            space_id="space-a", filename="notes.md", confirm=True
        )

    assert res["status"] == "deleted"
    assert res["files_deleted"] == 1
    assert "space-a/bank/notes.md" not in storage.objects


async def test_bank_delete_healthy_hive_fails_closed_no_delete() -> None:
    storage = WriteSinkFakeStorage()
    with _Patched(storage):
        await _seed_healthy_hive(storage, "hive-a")
        await storage.put("hive-a/bank/notes.md", "content")
        before = storage.snapshot()
        res = await _tool(register_bank_tools, "bank_delete")(
            space_id="hive-a", filename="notes.md", confirm=True
        )

    assert res["status"] == "error"
    assert storage.objects["hive-a/bank/notes.md"] == "content"  # still present
    assert storage.objects == before


# =============================================================================
# bank_repair (apply branch)
# =============================================================================


async def test_bank_repair_apply_non_hivemind_routes_through_sink() -> None:
    """bank_repair dry_run=False on non-Hivemind moves a mis-pathed file to its
    canonical key via the resolved DirectLocalWriteSink (byte-identical)."""
    storage = WriteSinkFakeStorage()
    with _Patched(storage):
        await _seed_meta(storage, "space-a")
        # A duplicate-prefixed key that sanitizes to a canonical name.
        await storage.put("space-a/bank/MEMORY_BANK/activeContext.md", "body")
        res = await _tool(register_bank_tools, "bank_repair")(
            space_id="space-a", dry_run=False
        )

    assert res["status"] == "ok"
    # The canonical key now exists with the same content.
    assert storage.objects.get("space-a/bank/activeContext.md") == "body"


async def test_bank_repair_apply_healthy_hive_fails_closed() -> None:
    """bank_repair dry_run=False on a HEALTHY hive -> safe_error, no put/delete.
    The dry_run=True scan still works (read-only, ungated)."""
    storage = WriteSinkFakeStorage()
    with _Patched(storage):
        await _seed_healthy_hive(storage, "hive-a")
        await storage.put("hive-a/bank/MEMORY_BANK/activeContext.md", "body")
        before = storage.snapshot()

        # Apply branch fails closed.
        applied = await _tool(register_bank_tools, "bank_repair")(
            space_id="hive-a", dry_run=False
        )
        assert applied["status"] == "error"
        assert storage.objects == before  # no mutation

        # Dry-run scan is read-only and still works on a hive space.
        scan = await _tool(register_bank_tools, "bank_repair")(
            space_id="hive-a", dry_run=True
        )
        assert scan["status"] == "ok"
        assert scan["mode"] == "dry-run"
        assert storage.objects == before  # still no mutation


# =============================================================================
# bank_compact — lock stays in tool; gate fires before consolidator
# =============================================================================


async def test_bank_compact_apply_healthy_hive_fails_closed_before_consolidator() -> None:
    """bank_compact dry_run=False on a HEALTHY hive -> safe_error, and the
    consolidator.compact_bank is NEVER called (the mid_engine resolve_sink gate
    fires first). The consolidation lock + conflict-check stay in the tool."""
    storage = WriteSinkFakeStorage()
    rec = _RecordingConsolidator()
    with _Patched(storage, consolidator=rec):
        await _seed_healthy_hive(storage, "hive-a")
        before = storage.snapshot()
        res = await _tool(register_bank_tools, "bank_compact")(
            space_id="hive-a", dry_run=False
        )

    assert res["status"] == "error"
    assert rec.compact_calls == []  # gate fired BEFORE the consolidator ran
    assert storage.objects == before


async def test_bank_compact_staged_refusal_is_typed_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The shared route fence remains attributable without logging its raw exception."""

    caplog.set_level(logging.ERROR)

    class StagedEngine:
        write_sink = object()

    class StagedRegistry:
        async def mid_engine(self, space_id: str) -> StagedEngine:
            assert space_id == "hive-a"
            return StagedEngine()

    monkeypatch.setattr(
        "live_mem.core.engines.get_engine_registry", lambda: StagedRegistry()
    )
    result = await _tool(register_bank_tools, "bank_compact")(
        space_id="hive-a", dry_run=False
    )

    assert result["status"] == "error"
    assert result["failure_reason"] == "direct_local_route_required"
    assert result["failed_phase"] == "prepare"
    assert result["rollback_outcome"] == "not_needed"
    assert result["failures"] == [
        {"filename": "", "error": "direct_local_route_required"}
    ]
    assert "op=compact" not in str(result)
    assert "op=compact" not in caplog.text


async def test_bank_compact_terminal_exception_never_echoes_raw_content(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Even debug-mode terminal handling never publishes provider/input text."""

    marker = "secret-compaction-completion-9f8e7d6c"

    class ExplodingConsolidator:
        async def compact_bank(self, space_id: str, dry_run: bool = True) -> dict:
            assert (space_id, dry_run) == ("space-a", True)
            raise RuntimeError(marker)

    caplog.set_level(logging.ERROR)
    monkeypatch.setattr(
        "live_mem.core.consolidator.get_consolidator",
        lambda: ExplodingConsolidator(),
    )
    monkeypatch.setattr(
        "live_mem.config.get_settings",
        lambda: type("DebugSettings", (), {"mcp_server_debug": True})(),
    )

    result = await _tool(register_bank_tools, "bank_compact")(
        space_id="space-a", dry_run=True
    )

    assert result["status"] == "error"
    assert result["failure_reason"] == "compaction_tool_failure"
    assert result["failed_phase"] == "prepare"
    assert result["rollback_outcome"] == "not_needed"
    assert result["failures"] == [
        {"filename": "", "error": "compaction_tool_failure"}
    ]
    assert marker not in str(result)
    assert marker not in caplog.text


async def test_bank_compact_mutation_path_exception_is_partial_and_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unclassified post-route failure never claims the apply was harmless."""

    marker = "secret-compaction-source-after-apply-3c1a4d"

    class ExplodingConsolidator:
        calls = 0

        async def compact_bank(self, space_id: str, dry_run: bool = True) -> dict:
            self.calls += 1
            assert (space_id, dry_run) == ("space-a", False)
            raise RuntimeError(marker)

    storage = WriteSinkFakeStorage()
    consolidator = ExplodingConsolidator()
    caplog.set_level(logging.ERROR)
    with _Patched(storage, consolidator=consolidator):
        result = await _tool(register_bank_tools, "bank_compact")(
            space_id="space-a", dry_run=False
        )

    assert consolidator.calls == 1
    assert result["status"] == "partial"
    assert result["failure_reason"] == "compaction_tool_failure"
    # The engine can still be in preparation, preimage, apply, or rollback;
    # the tool wrapper must not invent a more precise phase.
    assert result["failed_phase"] == "unknown"
    assert result["rollback_outcome"] == "unknown"
    assert result["apply_may_have_mutated"] is True
    assert result["recovery_required"] is True
    assert result["total_size_after"] is None
    assert marker not in str(result)
    assert marker not in caplog.text
    # The stable event remains content-free, but recovery can be correlated to
    # the already-authorized space and whether the mutating boundary was entered.
    assert "space=space-a" in caplog.text
    assert "mutation_path_entered=True" in caplog.text


@pytest.mark.parametrize(
    ("exception_factory", "failure_reason"),
    (
        (
            lambda: StagedWriteNotImplemented(op="compact", key="space-a/bank/"),
            "direct_local_route_required",
        ),
        (
            lambda: RegistryRefused("space-a", WriteRoute.REFUSE),
            "direct_local_route_required",
        ),
        (lambda: CorruptedStateError("injected after mutation boundary"), "hivemind_state_corrupt"),
    ),
)
async def test_bank_compact_typed_terminal_exception_after_mutation_boundary_is_partial(
    exception_factory,
    failure_reason: str,
) -> None:
    """Typed exceptions must not later reintroduce a false ``not_needed`` claim."""

    class ExplodingConsolidator:
        async def compact_bank(self, space_id: str, dry_run: bool = True) -> dict:
            assert (space_id, dry_run) == ("space-a", False)
            raise exception_factory()

    storage = WriteSinkFakeStorage()
    with _Patched(storage, consolidator=ExplodingConsolidator()):
        result = await _tool(register_bank_tools, "bank_compact")(
            space_id="space-a", dry_run=False
        )

    assert result["status"] == "partial"
    assert result["failure_reason"] == failure_reason
    assert result["failed_phase"] == "unknown"
    assert result["rollback_outcome"] == "unknown"
    assert result["apply_may_have_mutated"] is True
    assert result["recovery_required"] is True
    assert result["total_size_after"] is None


@pytest.mark.parametrize("route_state", ["unsafe", "resync", "corrupt"])
async def test_bank_compact_apply_non_direct_routes_fail_before_provider_or_compactor(
    route_state: str,
) -> None:
    """REFUSE and corrupt routes never reach the compactor/provider boundary.

    The route resolver necessarily reads Hivemind state; the assertion concerns
    everything after that decision: no consolidator call, backup/staging/apply,
    or mutation is possible on a non-DirectLocal route.
    """
    storage = WriteSinkFakeStorage()
    rec = _RecordingConsolidator()
    with _Patched(storage, consolidator=rec):
        if route_state == "unsafe":
            await _seed_meta(storage, "hive-route")
            store = HivemindStateStore(storage=storage, space_id="hive-route")  # type: ignore[arg-type]
            await store.set_node_status(NodeHealth(status=HiveNodeStatus.UNSAFE))
        elif route_state == "resync":
            await _seed_meta(storage, "hive-route")
            store = HivemindStateStore(storage=storage, space_id="hive-route")  # type: ignore[arg-type]
            await store.set_node_status(
                NodeHealth(status=HiveNodeStatus.RESYNC_REQUIRED)
            )
        else:
            await _seed_meta(storage, "hive-route")
            storage.objects[layout.node_key("hive-route")] = "{not valid json"
        before = storage.snapshot()
        res = await _tool(register_bank_tools, "bank_compact")(
            space_id="hive-route", dry_run=False
        )

    assert res["status"] == "error"
    assert rec.compact_calls == []
    assert storage.objects == before


async def test_bank_compact_dry_run_is_read_only_ungated() -> None:
    """bank_compact dry_run=True is a read-only scan: it stays on
    get_consolidator() (NOT routed through resolve_sink), so it works even on a
    hive space and never raises RegistryRefused/Staged. Asserts the consolidator
    IS called with dry_run=True."""
    storage = WriteSinkFakeStorage()
    rec = _RecordingConsolidator()
    with _Patched(storage, consolidator=rec), patch(
        "live_mem.core.consolidator.get_consolidator", return_value=rec
    ):
        await _seed_healthy_hive(storage, "hive-a")
        res = await _tool(register_bank_tools, "bank_compact")(
            space_id="hive-a", dry_run=True
        )

    assert res["status"] == "ok"
    assert rec.compact_calls == [("hive-a", True)]


# =============================================================================
# bank_consolidate — route-first gate on the WORKER write path
#
# bank_consolidate enqueues a background-worker job whose durable bank writes
# (ConsolidatorService get_storage call sites) run OUTSIDE the MCP auth/route
# context — the WriteSink seam cannot intercept them later. P3-7 therefore gates
# the enqueue at the tool entrypoint: non-Hivemind -> enqueue proceeds (the
# worker's direct writes are the legacy path); Hivemind/unsafe/corrupt -> fail
# closed BEFORE the job is queued, so no worker ever runs and no S3 write occurs.
# =============================================================================


class _RecordingQueue:
    """Records enqueue calls to PROVE the gate fires before enqueue on a
    fail-closed space (enqueue must NOT be called)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def enqueue(self, *, space_id: str, agent: str, requested_by: str) -> dict:
        self.calls.append((space_id, agent, requested_by))
        return {"status": "running", "job_id": "j1"}


async def test_bank_consolidate_non_hivemind_enqueues() -> None:
    """bank_consolidate on a non-Hivemind space resolves DIRECT_LOCAL and the
    enqueue proceeds (the worker's eventual direct writes are the legacy path).
    The enqueue call is unchanged (same kwargs as before P3-7)."""
    storage = WriteSinkFakeStorage()
    rec = _RecordingQueue()
    with _Patched(storage), patch(
        "live_mem.core.consolidation_queue.get_consolidation_queue",
        return_value=rec,
    ):
        await _seed_meta(storage, "space-a")
        res = await _tool(register_bank_tools, "bank_consolidate")(
            space_id="space-a"
        )

    assert res["status"] == "running"
    # DIRECT_LOCAL: the gate let the enqueue through with the verbatim kwargs.
    # Omitted scope is caller-only for every role, including admin.  Global
    # scope now requires an explicit agent="" request.
    assert rec.calls == [("space-a", "admin", "admin")]


async def test_bank_consolidate_healthy_hive_fails_closed_no_enqueue() -> None:
    """bank_consolidate on a HEALTHY hive -> safe_error, and the worker job is
    NEVER enqueued (the route-first gate raises StagedWriteNotImplemented before
    enqueue). No worker => no S3 write on the shared space."""
    storage = WriteSinkFakeStorage()
    rec = _RecordingQueue()
    with _Patched(storage), patch(
        "live_mem.core.consolidation_queue.get_consolidation_queue",
        return_value=rec,
    ):
        await _seed_healthy_hive(storage, "hive-a")
        before = storage.snapshot()
        res = await _tool(register_bank_tools, "bank_consolidate")(
            space_id="hive-a"
        )

    assert res["status"] == "error"
    assert rec.calls == []  # gate fired BEFORE enqueue — no worker queued
    assert storage.objects == before  # nothing written


async def test_bank_consolidate_corrupted_hive_fails_closed_no_enqueue() -> None:
    """Corrupt node.json -> CorruptedStateError propagates to safe_error; the
    job is never enqueued and the path never resolves DIRECT_LOCAL."""
    storage = WriteSinkFakeStorage()
    rec = _RecordingQueue()
    with _Patched(storage), patch(
        "live_mem.core.consolidation_queue.get_consolidation_queue",
        return_value=rec,
    ):
        await _seed_meta(storage, "hive-c")
        storage.objects[layout.node_key("hive-c")] = "{not valid json"
        before = storage.snapshot()
        res = await _tool(register_bank_tools, "bank_consolidate")(
            space_id="hive-c"
        )

    assert res["status"] == "error"
    assert rec.calls == []
    assert storage.objects == before


async def test_bank_consolidate_unsafe_hive_refuses_no_enqueue() -> None:
    """bank_consolidate on an UNSAFE hive -> RegistryRefused surfaced as
    safe_error; never enqueues (REFUSE is non-serviceable, distinct from STAGED)."""
    storage = WriteSinkFakeStorage()
    rec = _RecordingQueue()
    with _Patched(storage), patch(
        "live_mem.core.consolidation_queue.get_consolidation_queue",
        return_value=rec,
    ):
        await _seed_meta(storage, "hive-u")
        store = HivemindStateStore(storage=storage, space_id="hive-u")  # type: ignore[arg-type]
        await store.set_node_status(NodeHealth(status=HiveNodeStatus.UNSAFE))
        before = storage.snapshot()
        res = await _tool(register_bank_tools, "bank_consolidate")(
            space_id="hive-u"
        )

    assert res["status"] == "error"
    assert rec.calls == []
    assert storage.objects == before


# =============================================================================
# graph tools — no resolve_sink gate, SSRF check stays in the tool
# =============================================================================


async def test_graph_connect_ssrf_check_rejects_private_url_no_gate() -> None:
    """graph_connect rejects a loopback/private URL via the tool-layer SSRF
    check (ADR-0010: graph_* is downstream-derived, no resolve_sink gate). The
    rejection happens BEFORE any bridge/engine call, so no _hivemind/ state is
    touched even on a hive space."""
    storage = WriteSinkFakeStorage()
    with patch(
        "live_mem.core.engines.get_engine_registry"
    ) as reg_factory:
        await _seed_healthy_hive(storage, "hive-a")
        before = storage.snapshot()
        res = await _tool(register_graph_tools, "graph_connect")(
            space_id="hive-a",
            url="http://127.0.0.1:8080/mcp",
            token="t",
            memory_id="m",
        )

    assert res["status"] == "error"
    assert "loopback" in res["message"].lower()
    # The SSRF check short-circuits before the engine is ever resolved.
    reg_factory.assert_not_called()
    assert storage.objects == before


# =============================================================================
# Initial tool-gate single-resolution guard (codex PR #64): the tool must not
# resolve once on its own and again while building its engine, because the old
# two-step code could observe STAGED second yet write through the first direct
# path. Gating on the engine's own resolved sink keeps the initial tool check
# coherent. #395 and #413 deliberately add separate service-owned final route
# fences immediately before their true durable mutation boundaries.
# =============================================================================


def _count_resolves(pp: "_Patched") -> list[str]:
    calls: list[str] = []
    orig = pp.registry.resolve_sink

    async def counting(space_id: str):
        calls.append(space_id)
        return await orig(space_id)

    pp.registry.resolve_sink = counting  # type: ignore[assignment]
    return calls


async def test_live_note_resolves_route_exactly_once() -> None:
    """live_note builds exactly one registry sink at its initial tool gate."""
    storage = WriteSinkFakeStorage()
    with _Patched(storage) as pp:
        calls = _count_resolves(pp)
        res = await _tool(register_live_tools, "live_note")(
            space_id="space-once",
            category="observation",
            content="x",
            tags="",
        )
    assert res.get("status") != "error", res
    assert len(calls) == 1, (
        f"registry sink resolved {len(calls)}x; the initial tool gate must build "
        "exactly one sink (the service-owned final route fence is separate)"
    )


async def test_bank_compact_tool_gate_resolves_route_exactly_once() -> None:
    """The tool's initial gate resolves once; compactor owns its final fence."""
    storage = WriteSinkFakeStorage()
    rec = _RecordingConsolidator()
    with _Patched(storage, consolidator=rec) as pp:
        calls = _count_resolves(pp)
        res = await _tool(register_bank_tools, "bank_compact")(
            space_id="space-once", dry_run=False
        )
    assert res.get("status") != "error", res
    assert rec.compact_calls == [("space-once", False)]
    assert len(calls) == 1, (
        f"tool gate resolved {len(calls)}x; must be exactly 1 before delegation"
    )
