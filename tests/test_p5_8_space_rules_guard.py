# -*- coding: utf-8 -*-
"""
P5-8 (issue #16) — Class C: ``space_update_rules`` (_rules.md) routing guard.

``SpaceService.update_rules`` now routes the ``_rules.md`` durable write through
the per-space WriteSink (route-first):

- NON-Hivemind  -> DirectLocalWriteSink: byte-identical legacy put + no-op
  commit (non-Hivemind byte-for-byte).
- Hivemind hive -> StagedHivemindWriteSink: ``_rules.md`` is OUTSIDE
  ``{space}/bank/``, which the current ``CommitRuntime.apply_commit``
  (promote-only-under-``bank/``) cannot express. So ``commit()`` FAILS CLOSED
  (``StagedWriteNotImplemented``) — NO direct write, the single-writer guarantee
  holds, the ``_rules.md`` staging mechanism is the documented deferral.
- UNSAFE/RESYNC -> RegistryRefused; corrupt -> CorruptedStateError. Both surface
  via ``tools/space.py::space_update_rules``'s try/except safe_error.

Deterministic, OFFLINE, FakeStorage-backed. No real S3 / network / LLM.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from live_mem.core.engines import EngineRegistry
from live_mem.core.hivemind import (
    BankVersionPointer,
    HivemindStateStore,
    Member,
    MembershipView,
    NodeIdentity,
    TokenLeaseState,
    TokenState,
    generate_peer_keypair,
)
from live_mem.core.live import LiveService
from live_mem.core.space import SpaceService
from tests.test_p5_8_mutation_guard import GuardFakeStorage


NODE_ID = "n1"
_FROZEN = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


async def _seed_meta(storage: GuardFakeStorage, space_id: str) -> None:
    await storage.put(f"{space_id}/_meta.json", "{}")


async def _seed_held_hive(storage: GuardFakeStorage, space_id: str) -> None:
    await _seed_meta(storage, space_id)
    store = HivemindStateStore(storage=storage, space_id=space_id)  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(NodeIdentity(node_id=NODE_ID, public_key=keys.public_key))
    await store.set_membership(
        MembershipView(epoch=1, members=[Member(node_id=NODE_ID, public_key=keys.public_key)])
    )
    await store.bump_term(1, updated_by_node_id=NODE_ID)
    far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    await store.set_token(
        TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id=NODE_ID,
            term=1,
            fencing_token=1,
            granted_at=_FROZEN.isoformat(),
            lease_until=far_future.isoformat(),
            membership_epoch=1,
            event_id="evt-seed",
        )
    )
    await store.set_bank_version_pointer(BankVersionPointer(bank_version=-1))


def _registry(storage: GuardFakeStorage) -> EngineRegistry:
    return EngineRegistry(
        storage=storage, live=LiveService(), consolidator=object(), queue=object(), bridge=object()
    )


class _Patched:
    """Point space.py's get_storage + the registry at the same fake."""

    def __init__(self, storage: GuardFakeStorage) -> None:
        self.storage = storage
        self.registry = _registry(storage)
        self._patches: list = []

    def __enter__(self):
        for target in ("live_mem.core.space.get_storage", "live_mem.core.storage.get_storage"):
            p = patch(target, return_value=self.storage)
            p.start()
            self._patches.append(p)
        p = patch("live_mem.core.engines.get_engine_registry", return_value=self.registry)
        p.start()
        self._patches.append(p)
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


# =============================================================================
# NON-Hivemind — byte-identical _rules.md put
# =============================================================================


@pytest.mark.asyncio
async def test_update_rules_non_hivemind_byte_identical() -> None:
    """Non-hive _rules.md bytes are identical to a parallel direct storage.put,
    and the DirectLocal path writes nothing under _hivemind/ (no staging)."""
    storage = GuardFakeStorage()
    await _seed_meta(storage, "plain")  # no _hivemind/ -> DIRECT_LOCAL

    rules = "# Rules\n\nBe concise.\n"
    with _Patched(storage):
        res = await SpaceService().update_rules("plain", rules)

    assert res["status"] == "ok"
    assert storage.objects["plain/_rules.md"] == rules
    assert not any(k.startswith("plain/_hivemind/") for k in storage.objects)

    # Parallel direct path -> identical bytes.
    direct = GuardFakeStorage()
    await direct.put("plain/_rules.md", rules)
    assert storage.objects["plain/_rules.md"] == direct.objects["plain/_rules.md"]


@pytest.mark.asyncio
async def test_update_rules_non_hivemind_no_explicit_content_type() -> None:
    """The DirectLocal put forwards no explicit content_type (StorageService
    default applies) — same call shape as the legacy bare storage.put."""
    storage = GuardFakeStorage()
    await _seed_meta(storage, "plain")

    recorded: list = []
    orig_put = storage.put

    async def _spy(key, content, content_type="text/plain"):
        if key.endswith("_rules.md"):
            recorded.append((key, content, content_type))
        await orig_put(key, content, content_type)

    storage.put = _spy  # type: ignore[assignment]
    with _Patched(storage):
        await SpaceService().update_rules("plain", "# R\n")

    assert recorded == [("plain/_rules.md", "# R\n", "text/plain; charset=utf-8")]


# =============================================================================
# Hivemind hive — _rules.md is outside bank/, commit() fails closed (deferred)
# =============================================================================


@pytest.mark.asyncio
async def test_update_rules_hivemind_fails_closed_no_write() -> None:
    """On a healthy hive, _rules.md routes through the sink but commit() FAILS
    CLOSED (the file is outside {space}/bank/, which apply_commit cannot promote)
    -> the service raises StagedWriteNotImplemented, NO direct write, _rules.md
    unchanged. Pins the documented _rules.md-staging deferral; the single-writer
    guarantee holds (no direct-to-S3 _rules.md write on a hive)."""
    from live_mem.core.write_sink import StagedWriteNotImplemented

    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-a")
    storage.objects["hive-a/_rules.md"] = "# old rules\n"

    before = storage.snapshot()
    with _Patched(storage):
        with pytest.raises(StagedWriteNotImplemented):
            await SpaceService().update_rules("hive-a", "# new rules\n")

    # _rules.md unchanged; no staging tree, no commit, no direct write.
    assert storage.objects == before
    assert storage.objects["hive-a/_rules.md"] == "# old rules\n"


@pytest.mark.asyncio
async def test_update_rules_hivemind_via_tool_surfaces_safe_error() -> None:
    """Through the MCP tool wrapper, the fail-closed StagedWriteNotImplemented is
    caught and returned as a safe_error (status=error) — not a crash."""
    from mcp.server.fastmcp import FastMCP

    from live_mem.auth.context import current_token_info
    from live_mem.tools.space import register as register_space_tools

    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-a")
    storage.objects["hive-a/_rules.md"] = "# old rules\n"

    def _tool(name):
        mcp = FastMCP(name="test")
        register_space_tools(mcp)
        t = mcp._tool_manager._tools[name]
        for attr in ("fn", "func", "handler", "_fn", "run", "callback"):
            fn = getattr(t, attr, None)
            if callable(fn):
                return fn
        raise AssertionError("no callable")

    before = storage.snapshot()
    with _Patched(storage):
        tok = current_token_info.set(
            {"client_name": "admin", "permissions": ["read", "write", "manage", "admin"], "allowed_resources": []}
        )
        try:
            space_update_rules = _tool("space_update_rules")
            res = await space_update_rules("hive-a", "# new rules\n")
        finally:
            current_token_info.reset(tok)

    assert res["status"] == "error"  # safe_error, not a propagated exception
    assert storage.objects == before  # nothing written


# =============================================================================
# space_update (_meta.json) routing guard — FINDING (2)
#
# ``_meta.json`` carries SHARED metadata (``description`` / ``owner`` are in
# core/models.SHARED_META_FIELDS); ADR-0007 puts the shared portion behind the
# WriteSink boundary. ``SpaceService.update`` must route through the same
# route-first sink as ``update_rules`` — NEVER a direct ``storage.put_json`` for a
# Hivemind space (single-writer bypass), while staying byte-for-byte on
# non-Hivemind.
# =============================================================================


@pytest.mark.asyncio
async def test_update_meta_non_hivemind_byte_identical() -> None:
    """Non-hive ``_meta.json`` bytes after an update are identical to a parallel
    direct read-mutate-``put_json`` (same json.dumps shape), and the DirectLocal
    path writes nothing under ``_hivemind/`` (no staging).

    RED without the fix is N/A here (non-hive stays direct); this PINS the
    byte-for-byte preservation the fix must not break."""
    storage = GuardFakeStorage()
    # Seed a realistic _meta.json with shared + local fields.
    await storage.put_json(
        "plain/_meta.json",
        {
            "space_id": "plain",
            "description": "old",
            "owner": "alice",
            "graph_memory": {"token": "SECRET"},
        },
    )

    with _Patched(storage):
        res = await SpaceService().update("plain", description="new", owner="bob")

    assert res["status"] == "ok"
    assert set(res["updated_fields"]) == {"description", "owner"}
    assert not any(k.startswith("plain/_hivemind/") for k in storage.objects)

    # Parallel direct read-mutate-put_json -> identical bytes (local fields kept).
    direct = GuardFakeStorage()
    await direct.put_json(
        "plain/_meta.json",
        {
            "space_id": "plain",
            "description": "old",
            "owner": "alice",
            "graph_memory": {"token": "SECRET"},
        },
    )
    meta = await direct.get_json("plain/_meta.json")
    meta["description"] = "new"
    meta["owner"] = "bob"
    await direct.put_json("plain/_meta.json", meta)
    assert storage.objects["plain/_meta.json"] == direct.objects["plain/_meta.json"]


@pytest.mark.asyncio
async def test_update_meta_hivemind_fails_closed_no_write() -> None:
    """On a healthy hive, ``space_update`` routes ``_meta.json`` through the sink
    but commit() FAILS CLOSED (the file is outside ``{space}/bank/``, which
    apply_commit cannot promote) -> the service raises StagedWriteNotImplemented,
    NO direct ``storage.put_json``, ``_meta.json`` unchanged. Closes the
    single-writer bypass: no direct shared-_meta write on a hive.

    RED without the fix: ``SpaceService.update`` called ``storage.put_json``
    directly, so the description/owner update landed in ``_meta.json`` with NO
    sink, NO commit — a single-writer bypass (storage.objects would differ)."""
    from live_mem.core.write_sink import StagedWriteNotImplemented

    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-a")
    # Replace the bare "{}" meta seed with a realistic shared-meta doc.
    await storage.put_json(
        "hive-a/_meta.json", {"space_id": "hive-a", "description": "old", "owner": "alice"}
    )

    before = storage.snapshot()
    with _Patched(storage):
        with pytest.raises(StagedWriteNotImplemented):
            await SpaceService().update("hive-a", description="new", owner="bob")

    # _meta.json unchanged; no staging tree, no commit, no direct write.
    assert storage.objects == before
    meta_after = await storage.get_json("hive-a/_meta.json")
    assert meta_after["description"] == "old"
    assert meta_after["owner"] == "alice"


@pytest.mark.asyncio
async def test_update_meta_hivemind_via_tool_surfaces_safe_error() -> None:
    """Through the MCP ``space_update`` tool wrapper, the fail-closed
    StagedWriteNotImplemented is caught and returned as a safe_error
    (status=error) — not a crash — and nothing is written."""
    from mcp.server.fastmcp import FastMCP

    from live_mem.auth.context import current_token_info
    from live_mem.tools.space import register as register_space_tools

    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-a")
    await storage.put_json(
        "hive-a/_meta.json", {"space_id": "hive-a", "description": "old", "owner": "alice"}
    )

    def _tool(name):
        mcp = FastMCP(name="test")
        register_space_tools(mcp)
        t = mcp._tool_manager._tools[name]
        for attr in ("fn", "func", "handler", "_fn", "run", "callback"):
            fn = getattr(t, attr, None)
            if callable(fn):
                return fn
        raise AssertionError("no callable")

    before = storage.snapshot()
    with _Patched(storage):
        tok = current_token_info.set(
            {"client_name": "admin", "permissions": ["read", "write", "manage", "admin"], "allowed_resources": []}
        )
        try:
            space_update = _tool("space_update")
            res = await space_update("hive-a", description="new", owner="bob")
        finally:
            current_token_info.reset(tok)

    assert res["status"] == "error"  # safe_error, not a propagated exception
    assert storage.objects == before  # nothing written
