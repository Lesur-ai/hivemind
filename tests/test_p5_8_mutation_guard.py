# -*- coding: utf-8 -*-
"""
P5-8 (issue #16) — the CAPSTONE mutation guard.

Proves the DoD at the mutation surface: EVERY shared-space bank mutation goes
through the single-writer commit path (assert_commit_allowed is the ONLY auth);
the legacy direct-write path is unreachable for Hivemind spaces; non-Hivemind
spaces stay byte-for-byte unchanged.

Scope WIRED (real staged commit via StagedHivemindWriteSink + CommitRuntime):
- bank_write (put -> buffer -> one atomic commit);
- the staged-sink put/commit primitives (buffer-no-write, single auth, atomic
  bank_version bump, _meta.json graph stripping, non-bank key fail-closed).

Scope FAIL-CLOSED + PINNED (what the current CommitRuntime cannot express; the
single-writer guarantee holds — no direct write — and the wiring is deferred):
- bank_delete / bank_repair deletes: apply_commit is put-only (no live-bank
  delete), so the sink's delete/delete_many fail closed;
- bank_consolidate / bank_compact (Class B): the entry-point route gate refuses
  before any worker runs;
- the no-HELD-token lease ceremony: resolve_sink fail-closes (RegistryRefused).

Deterministic, OFFLINE, FakeStorage-backed, injectable frozen clock. No real S3 /
boto3 / network / LLM.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from mcp.server.fastmcp import FastMCP

from live_mem.core.engines import EngineRegistry, RegistryRefused
from live_mem.core.hivemind import (
    BankVersionPointer,
    CommitNotAuthorized,
    CommitRuntime,
    HivemindStateStore,
    LeaseRuntime,
    Member,
    MemberStatus,
    MembershipView,
    NodeIdentity,
    QueueRuntime,
    TokenLeaseState,
    TokenState,
    generate_peer_keypair,
    layout,
)
from live_mem.core.live import LiveService
from live_mem.core.write_sink import (
    StagedHivemindWriteSink,
    StagedWriteNotImplemented,
)
from live_mem.tools.bank import register as register_bank_tools
from tests.test_write_sink import WriteSinkFakeStorage


# =============================================================================
# Determinism seams — frozen clock + fake storage with the reads the sink needs
# =============================================================================


_FROZEN = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


def _frozen_clock() -> datetime:
    return _FROZEN


class GuardFakeStorage(WriteSinkFakeStorage):
    """``WriteSinkFakeStorage`` (put/put_json/get/get_json/delete/delete_many/
    list_objects/exists/snapshot) + ``list_and_get`` (the read shape the bank
    tools and consolidator use). Mirrors ``StorageService.list_and_get``."""

    async def list_and_get(self, prefix: str, exclude_keep: bool = True) -> list[dict]:
        out: list[dict] = []
        for key in sorted(self.objects):
            if not key.startswith(prefix):
                continue
            if exclude_keep and key.endswith(".keep"):
                continue
            content = self.objects[key]
            out.append(
                {"key": key, "content": content, "size": len(content), "last_modified": ""}
            )
        return out


NODE_ID = "n1"


async def _seed_meta(storage: GuardFakeStorage, space_id: str) -> None:
    await storage.put(f"{space_id}/_meta.json", "{}")


async def _seed_held_hive(storage: GuardFakeStorage, space_id: str) -> None:
    """HEALTHY hive (node + ACTIVE member) + a HELD token + term + bank pointer
    so the staged sink can commit (assert_commit_allowed G0 passes for NODE_ID as
    holder at term 1). Also seeds _meta.json so tool existence checks pass."""
    await _seed_meta(storage, space_id)
    store = HivemindStateStore(storage=storage, space_id=space_id)  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(NodeIdentity(node_id=NODE_ID, public_key=keys.public_key))
    await store.set_membership(
        MembershipView(epoch=1, members=[Member(node_id=NODE_ID, public_key=keys.public_key)])
    )
    await store.bump_term(1, updated_by_node_id=NODE_ID)
    # lease_until is FAR in the future relative to REAL wall-clock so the token
    # stays unexpired whether assert_commit_allowed runs under the frozen sink
    # clock (unit harness) or the production registry's real clock (tool harness).
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


def _staged_sink(storage: GuardFakeStorage, space_id: str, *, spy_lease=None):
    """Build the real staged sink over ``storage`` (HELD holder == NODE_ID).
    ``spy_lease`` optionally wraps LeaseRuntime to count assert_commit_allowed."""
    store = HivemindStateStore(storage=storage, space_id=space_id)  # type: ignore[arg-type]
    queue = QueueRuntime(store, space_id)
    lease = LeaseRuntime(store, space_id, queue, clock=_frozen_clock)
    if spy_lease is not None:
        spy_lease(lease)
    crt = CommitRuntime(store, storage, space_id, lease, clock=_frozen_clock)  # type: ignore[arg-type]
    return StagedHivemindWriteSink(
        space_id,
        storage,  # type: ignore[arg-type]
        store=store,
        commit_runtime=crt,
        lease=lease,
        local_node_id=NODE_ID,
        fencing_token=1,
        membership_epoch=1,
        clock=_frozen_clock,
    )


# --- MCP tool harness ---------------------------------------------------------


def _admin_token() -> dict:
    return {
        "client_name": "admin",
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


class _ToolHive:
    """Patch the tool's registry + legacy get_storage seams at the SAME fake, and
    set an admin token. The registry STAGED branch builds the real sink over the
    fake; healthy-hive seeds carry a HELD token so commits succeed."""

    def __init__(self, storage: GuardFakeStorage) -> None:
        self.storage = storage
        self.registry = EngineRegistry(
            storage=storage,
            live=LiveService(),
            consolidator=object(),
            queue=object(),
            bridge=object(),
        )
        self._patches: list = []

    def __enter__(self):
        from live_mem.auth.context import current_token_info

        p = patch("live_mem.core.engines.get_engine_registry", return_value=self.registry)
        p.start()
        self._patches.append(p)
        for target in (
            "live_mem.core.storage.get_storage",
            "live_mem.tools.bank.get_storage",
        ):
            try:
                pp = patch(target, return_value=self.storage)
                pp.start()
                self._patches.append(pp)
            except (AttributeError, ModuleNotFoundError):
                pass
        self._token = current_token_info.set(_admin_token())
        return self

    def __exit__(self, *exc):
        from live_mem.auth.context import current_token_info

        current_token_info.reset(self._token)
        for p in reversed(self._patches):
            p.stop()
        return False


def _bank_keys(storage: GuardFakeStorage, space_id: str) -> set[str]:
    return {k for k in storage.objects if k.startswith(f"{space_id}/bank/")}


def _commit_count(storage: GuardFakeStorage, space_id: str) -> int:
    prefix = f"{space_id}/_hivemind/commits/"
    return sum(1 for k in storage.objects if k.startswith(prefix) and k.endswith(".json"))


# =============================================================================
# 1. Staged-sink primitives — buffer, single auth, atomic bump, meta-strip
# =============================================================================


@pytest.mark.asyncio
async def test_staged_sink_buffers_no_storage_write() -> None:
    """put/put_json BUFFER (zero storage write); delete/delete_many FAIL CLOSED
    (apply_commit is put-only) — never a direct write."""
    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-a")
    sink = _staged_sink(storage, "hive-a")

    before = storage.snapshot()
    await sink.put("hive-a/bank/a.md", "A")
    await sink.put_json("hive-a/bank/b.json", {"k": 1})
    assert storage.objects == before  # buffered, nothing written

    with pytest.raises(StagedWriteNotImplemented):
        await sink.delete("hive-a/bank/a.md")
    with pytest.raises(StagedWriteNotImplemented):
        await sink.delete_many(["hive-a/bank/a.md"])
    assert storage.objects == before


@pytest.mark.asyncio
async def test_staged_commit_authorizes_through_single_gate() -> None:
    """commit() routes authorization through the SINGLE gate
    (assert_commit_allowed) only — no parallel/alternative auth check.

    Post mutation-guard the gate is invoked TWICE through the SAME path: once as
    a READ-ONLY pre-stage prefilter (so an unauthorized caller writes no durable
    state) and once live inside apply_commit (the linearization gate, no TOCTOU).
    Both invocations carry the IDENTICAL CommitIntent — it is one authorization
    mechanism, not two competing ones."""
    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-a")

    calls: list[object] = []

    def _spy(lease: LeaseRuntime) -> None:
        original = lease.assert_commit_allowed

        async def _counting(intent):
            calls.append(intent)
            return await original(intent)

        lease.assert_commit_allowed = _counting  # type: ignore[assignment]

    sink = _staged_sink(storage, "hive-a", spy_lease=_spy)
    await sink.put("hive-a/bank/a.md", "A")
    await sink.commit(reason="bank_write")

    # Pre-stage prefilter + live apply gate = 2 calls through the ONE gate.
    assert len(calls) == 2
    # Both invocations are the SAME single-auth mechanism on the SAME intent
    # (no second/alternative authorization mechanism exists).
    assert calls[0] == calls[1]


@pytest.mark.asyncio
async def test_staged_commit_atomic_single_bank_version_bump() -> None:
    """Two buffered puts -> ONE commit; bank_version advances by exactly 1 and
    the manifest covers BOTH touched files (full-snapshot replay)."""
    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-a")
    sink = _staged_sink(storage, "hive-a")

    await sink.put("hive-a/bank/a.md", "A")
    await sink.put("hive-a/bank/b.md", "B")
    pointer = await sink.commit(reason="bank_write")

    assert pointer is not None
    assert pointer.bank_version == 0  # -1 -> 0, exactly one bump
    assert _commit_count(storage, "hive-a") == 1
    assert storage.objects["hive-a/bank/a.md"] == "A"
    assert storage.objects["hive-a/bank/b.md"] == "B"


@pytest.mark.asyncio
async def test_staged_sink_rejects_key_without_bank_prefix() -> None:
    """A buffered write to a key OUTSIDE {space}/bank/ FAILS CLOSED at commit()
    (apply_commit promotes only under bank/) — no write, no silent mislanding."""
    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-a")
    sink = _staged_sink(storage, "hive-a")

    await sink.put("hive-a/_synthesis.md", "syn")  # buffers
    before = storage.snapshot()
    with pytest.raises(StagedWriteNotImplemented):
        await sink.commit(reason="x")
    assert storage.objects == before
    assert "hive-a/_synthesis.md" not in storage.objects


@pytest.mark.asyncio
async def test_meta_json_graph_memory_excluded_from_staged_commit() -> None:
    """A buffered put_json of bank/_meta.json carrying a graph_memory block is
    projected via staged_meta_text; the committed bytes carry NO graph_memory
    (ADR-0012). apply_commit's assert_no_graph_memory_in_manifest would otherwise
    fire."""
    import json

    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-a")
    sink = _staged_sink(storage, "hive-a")

    await sink.put_json(
        "hive-a/bank/_meta.json",
        {"consolidation_count": 3, "graph_memory": {"token": "SECRET"}},
    )
    await sink.commit(reason="x")

    committed = json.loads(storage.objects["hive-a/bank/_meta.json"])
    assert "graph_memory" not in committed
    assert committed.get("consolidation_count") == 3


@pytest.mark.asyncio
async def test_staged_commit_empty_buffer_is_noop() -> None:
    """commit() with an empty buffer is a no-op (returns None, writes nothing) —
    a route-blind tool whose op was a no-op never bumps bank_version."""
    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-a")
    sink = _staged_sink(storage, "hive-a")

    before = storage.snapshot()
    result = await sink.commit(reason="x")
    assert result is None
    assert storage.objects == before


# =============================================================================
# 2. bank tools on a HIVEMIND space — staged (no direct-to-S3 write)
# =============================================================================


@pytest.mark.asyncio
async def test_bank_write_staged_hive_routes_through_commit() -> None:
    """bank_write on a healthy hive (HELD token) -> the file lands at its live
    bank key ONLY after a BankCommit + bumped pointer (staged), never via a
    direct storage.put."""
    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-a")

    with _ToolHive(storage):
        bank_write = _tool(register_bank_tools, "bank_write")
        res = await bank_write("hive-a", "activeContext.md", "# body\n")

    assert res["status"] == "ok"
    # Landed at the live bank key, AND a commit exists (staged path), pointer bumped.
    assert storage.objects["hive-a/bank/activeContext.md"] == "# body\n"
    assert _commit_count(storage, "hive-a") == 1
    pointer_raw = storage.objects.get(layout.bank_version_key("hive-a"))
    assert pointer_raw is not None and '"bank_version": 0' in pointer_raw


@pytest.mark.asyncio
async def test_bank_delete_staged_hive_fails_closed_no_write() -> None:
    """bank_delete on a healthy hive -> safe_error (the sink's delete_many fails
    closed: apply_commit is put-only). The target bank file SURVIVES; no commit."""
    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-a")
    storage.objects["hive-a/bank/keepme.md"] = "live"

    before = storage.snapshot()
    with _ToolHive(storage):
        bank_delete = _tool(register_bank_tools, "bank_delete")
        res = await bank_delete("hive-a", "keepme.md", confirm=True)

    assert res["status"] == "error"  # safe_error
    assert storage.objects == before  # nothing deleted, nothing committed
    assert "hive-a/bank/keepme.md" in storage.objects


@pytest.mark.asyncio
async def test_bank_repair_staged_hive_fails_closed_no_write() -> None:
    """bank_repair dry_run=False on a healthy hive -> safe_error (the repair's
    move/dup deletes fail closed) — no put/delete reaches storage."""
    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-a")
    # A file at a non-canonical key so repair would try to move+delete it.
    storage.objects["hive-a/bank/1.MEMORY_BANK/ctx.md"] = "body"

    before = storage.snapshot()
    with _ToolHive(storage):
        bank_repair = _tool(register_bank_tools, "bank_repair")
        res = await bank_repair("hive-a", dry_run=False)

    assert res["status"] == "error"  # safe_error
    assert storage.objects == before  # no move, no delete, no commit


# =============================================================================
# 3. THE GUARD — no direct bank write on a Hivemind space (ADR-0007 (d) / R12)
# =============================================================================


@pytest.mark.asyncio
async def test_guard_no_direct_bank_write_on_hivemind() -> None:
    """On a healthy hive, the ONLY writes to {space}/bank/* come from the staged
    commit's promote loop (each carries a matching BankCommit); there is NO path
    that lands a bank file via a direct storage.put outside the commit. We prove
    it by spying: across a bank_write, every {space}/bank/* PUT happens AFTER a
    commit was staged (a commit object exists)."""
    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-a")

    bank_writes_seen_at: list[bool] = []
    orig_put = storage.put

    async def _spy_put(key, content, content_type="text/plain"):
        if key.startswith("hive-a/bank/"):
            # At the moment a live bank key is written, a staged manifest for the
            # in-flight commit must already exist (promote happens inside
            # apply_commit, after stage_commit wrote the MANIFEST). Direct
            # tool-level puts would have NO staging tree at all.
            staging_present = any(
                k.startswith("hive-a/_hivemind/staging/") for k in storage.objects
            )
            bank_writes_seen_at.append(staging_present)
        await orig_put(key, content, content_type)

    storage.put = _spy_put  # type: ignore[assignment]

    with _ToolHive(storage):
        bank_write = _tool(register_bank_tools, "bank_write")
        res = await bank_write("hive-a", "x.md", "body")

    assert res["status"] == "ok"
    # At least one bank PUT happened, and EVERY one was through the staged path.
    assert bank_writes_seen_at
    assert all(bank_writes_seen_at)


# =============================================================================
# 4. Class B (consolidate/compact) — UNREACHABLE for a hive (entry-point gate)
# =============================================================================


@pytest.mark.asyncio
async def test_consolidate_hivemind_still_fail_closed_no_enqueue() -> None:
    """bank_consolidate on a healthy hive -> safe_error BEFORE enqueue (the
    entry-point route gate refuses). Pins the deferred Class B: no worker ever
    runs on a hive, so the legacy ConsolidatorService direct-write path is
    unreachable."""
    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-a")

    enqueued: list = []

    class _SpyQueue:
        async def enqueue(self, **kw):
            enqueued.append(kw)
            return {"status": "queued"}

    reg = EngineRegistry(
        storage=storage, live=LiveService(), consolidator=object(), queue=object(), bridge=object()
    )
    before = storage.snapshot()
    with patch("live_mem.core.engines.get_engine_registry", return_value=reg), patch(
        "live_mem.core.consolidation_queue.get_consolidation_queue", return_value=_SpyQueue()
    ):
        from live_mem.auth.context import current_token_info

        tok = current_token_info.set(_admin_token())
        try:
            bank_consolidate = _tool(register_bank_tools, "bank_consolidate")
            res = await bank_consolidate("hive-a", "")
        finally:
            current_token_info.reset(tok)

    assert res["status"] == "error"  # safe_error (StagedWriteNotImplemented)
    assert enqueued == []  # NO job queued
    assert storage.objects == before  # no worker write


@pytest.mark.asyncio
async def test_compact_hivemind_still_fail_closed() -> None:
    """bank_compact dry_run=False on a healthy hive -> safe_error before the
    consolidator runs (entry-point gate on the engine's resolved sink). Pins the
    deferred Class B."""
    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-a")

    compact_calls: list = []

    class _SpyConsolidator:
        async def compact_bank(self, space_id, dry_run=True):
            compact_calls.append((space_id, dry_run))
            return {"status": "ok"}

    reg = EngineRegistry(
        storage=storage,
        live=LiveService(),
        consolidator=_SpyConsolidator(),
        queue=object(),
        bridge=object(),
    )
    before = storage.snapshot()
    with patch("live_mem.core.engines.get_engine_registry", return_value=reg):
        from live_mem.auth.context import current_token_info

        tok = current_token_info.set(_admin_token())
        try:
            bank_compact = _tool(register_bank_tools, "bank_compact")
            res = await bank_compact("hive-a", dry_run=False)
        finally:
            current_token_info.reset(tok)

    assert res["status"] == "error"  # safe_error
    assert compact_calls == []  # consolidator never ran
    assert storage.objects == before


# =============================================================================
# 5. Lease ceremony deferral — no HELD token on a hive -> RegistryRefused
# =============================================================================


@pytest.mark.asyncio
async def test_bank_write_hive_no_held_token_fails_closed_no_write() -> None:
    """A healthy hive with NO HELD token -> bank_write safe_errors via
    RegistryRefused (the deferred lease-acquisition ceremony); zero bank writes.
    Pins the chosen fail-closed scope."""
    storage = GuardFakeStorage()
    # HEALTHY hive but NO token seeded.
    await _seed_meta(storage, "hive-nt")
    store = HivemindStateStore(storage=storage, space_id="hive-nt")  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(NodeIdentity(node_id=NODE_ID, public_key=keys.public_key))
    await store.set_membership(
        MembershipView(epoch=1, members=[Member(node_id=NODE_ID, public_key=keys.public_key)])
    )

    before = storage.snapshot()
    with _ToolHive(storage):
        bank_write = _tool(register_bank_tools, "bank_write")
        res = await bank_write("hive-nt", "x.md", "body")

    assert res["status"] == "error"  # safe_error (RegistryRefused)
    assert _bank_keys(storage, "hive-nt") == set()  # zero bank writes
    assert storage.objects == before


@pytest.mark.asyncio
async def test_resolve_sink_no_held_token_raises_registry_refused() -> None:
    """Direct seam pin: resolve_sink on a healthy hive with no HELD token raises
    RegistryRefused (never a sink, never a direct write)."""
    storage = GuardFakeStorage()
    await _seed_meta(storage, "hive-nt")
    store = HivemindStateStore(storage=storage, space_id="hive-nt")  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(NodeIdentity(node_id=NODE_ID, public_key=keys.public_key))
    await store.set_membership(
        MembershipView(epoch=1, members=[Member(node_id=NODE_ID, public_key=keys.public_key)])
    )
    reg = EngineRegistry(
        storage=storage, live=LiveService(), consolidator=object(), queue=object(), bridge=object()
    )
    with pytest.raises(RegistryRefused):
        await reg.resolve_sink("hive-nt")


@pytest.mark.asyncio
async def test_resolve_sink_token_held_by_another_node_refused() -> None:
    """FINDING (a): a HELD token whose holder is ANOTHER node (not the LOCAL node
    identity used to build the sink) must FAIL CLOSED at resolve_sink with
    RegistryRefused — never a sink bound to our identity that would only be
    rejected later (NOT_HOLDER) AFTER staging writes.

    RED without the fix: resolve_sink only checked ``state == HELD``, so it
    returned a StagedHivemindWriteSink for a token held by ``other-node``."""
    storage = GuardFakeStorage()
    await _seed_meta(storage, "hive-other")
    store = HivemindStateStore(storage=storage, space_id="hive-other")  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    # LOCAL node identity is NODE_ID ("n1").
    await store.set_node_identity(NodeIdentity(node_id=NODE_ID, public_key=keys.public_key))
    await store.set_membership(
        MembershipView(epoch=1, members=[Member(node_id=NODE_ID, public_key=keys.public_key)])
    )
    await store.bump_term(1, updated_by_node_id="other-node")
    # Token is HELD — but by ANOTHER node, not the local NODE_ID.
    far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    await store.set_token(
        TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id="other-node",
            term=1,
            fencing_token=1,
            granted_at=_FROZEN.isoformat(),
            lease_until=far_future.isoformat(),
            membership_epoch=1,
            event_id="evt-other",
        )
    )
    await store.set_bank_version_pointer(BankVersionPointer(bank_version=-1))

    reg = EngineRegistry(
        storage=storage, live=LiveService(), consolidator=object(), queue=object(), bridge=object()
    )
    with pytest.raises(RegistryRefused):
        await reg.resolve_sink("hive-other")


@pytest.mark.asyncio
async def test_resolve_sink_local_holder_not_active_member_refused() -> None:
    """FINDING (1): a local node that HOLDS the token but is NO LONGER an ACTIVE
    member of the current membership (e.g. evicted -> status EVICTED, or dropped
    from the member list) must FAIL CLOSED at resolve_sink with RegistryRefused.

    assert_commit_allowed is DELIBERATELY NOT a membership gate (it only checks
    token/term/pointer), so without this gate an evicted-but-still-holder node
    could commit (token/term/pointer all match) — violating the ACTIVE-membership
    / all-ACK model.

    RED without the fix: resolve_sink only required ``membership is not None`` and
    holder == local node, so it returned a StagedHivemindWriteSink for an evicted
    holder.

    NOTE: the membership keeps ANOTHER ACTIVE peer so the upstream
    ``resolve_write_route`` verdict is HEALTHY/STAGED (it only checks that SOME
    member is ACTIVE, not that the LOCAL node is) — the route therefore reaches
    the STAGED branch, and only the per-sink gate can catch the evicted LOCAL
    holder."""
    storage = GuardFakeStorage()
    await _seed_meta(storage, "hive-evicted")
    store = HivemindStateStore(storage=storage, space_id="hive-evicted")  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    peer_keys = generate_peer_keypair()
    await store.set_node_identity(NodeIdentity(node_id=NODE_ID, public_key=keys.public_key))
    # The local node HOLDS the token, but its membership entry is EVICTED (not
    # ACTIVE) at the current epoch — it must not be allowed to commit. A separate
    # ACTIVE peer keeps the hive HEALTHY upstream (route == STAGED).
    await store.set_membership(
        MembershipView(
            epoch=2,
            members=[
                Member(
                    node_id=NODE_ID,
                    public_key=keys.public_key,
                    status=MemberStatus.EVICTED,
                ),
                Member(
                    node_id="peer-active",
                    public_key=peer_keys.public_key,
                    status=MemberStatus.ACTIVE,
                ),
            ],
        )
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
            membership_epoch=2,  # matches current epoch — only status is stale
            event_id="evt-seed",
        )
    )
    await store.set_bank_version_pointer(BankVersionPointer(bank_version=-1))

    reg = EngineRegistry(
        storage=storage, live=LiveService(), consolidator=object(), queue=object(), bridge=object()
    )
    with pytest.raises(RegistryRefused):
        await reg.resolve_sink("hive-evicted")


@pytest.mark.asyncio
async def test_resolve_sink_token_membership_epoch_stale_refused() -> None:
    """FINDING (1): a local ACTIVE holder whose HELD token was granted under an
    OLDER membership epoch than the CURRENT membership must FAIL CLOSED at
    resolve_sink with RegistryRefused.

    The membership advanced (epoch bumped) after the grant; a token carrying a
    stale ``membership_epoch`` is no longer valid for committing at the current
    view, and assert_commit_allowed does not check epoch. Without this gate a
    stale-epoch holder could still commit.

    RED without the fix: resolve_sink never compared token.membership_epoch to the
    current membership.epoch, so it returned a sink for a stale-epoch token."""
    storage = GuardFakeStorage()
    await _seed_meta(storage, "hive-stale-epoch")
    store = HivemindStateStore(storage=storage, space_id="hive-stale-epoch")  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(NodeIdentity(node_id=NODE_ID, public_key=keys.public_key))
    # Local node IS an ACTIVE member at the CURRENT epoch (3) ...
    await store.set_membership(
        MembershipView(
            epoch=3, members=[Member(node_id=NODE_ID, public_key=keys.public_key)]
        )
    )
    await store.bump_term(1, updated_by_node_id=NODE_ID)
    # ... but the HELD token was granted under an OLDER membership epoch (1).
    far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    await store.set_token(
        TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id=NODE_ID,
            term=1,
            fencing_token=1,
            granted_at=_FROZEN.isoformat(),
            lease_until=far_future.isoformat(),
            membership_epoch=1,  # STALE vs current membership.epoch == 3
            event_id="evt-seed",
        )
    )
    await store.set_bank_version_pointer(BankVersionPointer(bank_version=-1))

    reg = EngineRegistry(
        storage=storage, live=LiveService(), consolidator=object(), queue=object(), bridge=object()
    )
    with pytest.raises(RegistryRefused):
        await reg.resolve_sink("hive-stale-epoch")


@pytest.mark.asyncio
async def test_resolve_sink_active_holder_current_epoch_succeeds() -> None:
    """GREEN counterpart to the membership gate: an ACTIVE local holder whose
    token epoch MATCHES the current membership epoch resolves a real
    StagedHivemindWriteSink (the gate does not over-refuse the healthy case)."""
    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-ok")  # ACTIVE member, token epoch == 1

    reg = EngineRegistry(
        storage=storage, live=LiveService(), consolidator=object(), queue=object(), bridge=object()
    )
    sink = await reg.resolve_sink("hive-ok")
    assert isinstance(sink, StagedHivemindWriteSink)


@pytest.mark.asyncio
async def test_unauthorized_commit_writes_no_staging_and_no_pointer() -> None:
    """FINDING (b): an UNAUTHORIZED commit() (here: a FENCED holder — expired
    lease) writes ZERO durable Hivemind state. assert_commit_allowed runs as a
    READ-ONLY prefilter BEFORE stage_commit, so the staging prefix stays EMPTY,
    no MANIFEST.json is published, and the bank_version pointer never advances.

    RED without the fix: commit() called stage_commit FIRST (writing
    staging/{commit_id}/<bank files> + MANIFEST.json) and only reached the auth
    gate inside apply_commit — leaving durable staging objects behind on refusal."""
    storage = GuardFakeStorage()
    await _seed_meta(storage, "hive-fenced")
    store = HivemindStateStore(storage=storage, space_id="hive-fenced")  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(NodeIdentity(node_id=NODE_ID, public_key=keys.public_key))
    await store.set_membership(
        MembershipView(epoch=1, members=[Member(node_id=NODE_ID, public_key=keys.public_key)])
    )
    await store.bump_term(1, updated_by_node_id=NODE_ID)
    # HELD by the LOCAL node at the current term — but the lease EXPIRED before
    # the frozen clock, so assert_commit_allowed fails closed (FENCED).
    expired = datetime(2026, 6, 19, 11, 0, 0, tzinfo=timezone.utc)  # 1h before _FROZEN
    await store.set_token(
        TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id=NODE_ID,
            term=1,
            fencing_token=1,
            granted_at=expired.isoformat(),
            lease_until=expired.isoformat(),
            membership_epoch=1,
            event_id="evt-seed",
        )
    )
    await store.set_bank_version_pointer(BankVersionPointer(bank_version=-1))

    sink = _staged_sink(storage, "hive-fenced")
    await sink.put("hive-fenced/bank/a.md", "A")

    before = storage.snapshot()
    staging_prefix = layout.staging_prefix("hive-fenced")
    pointer_key = layout.bank_version_key("hive-fenced")
    pointer_before = storage.objects.get(pointer_key)

    with pytest.raises(CommitNotAuthorized):
        await sink.commit(reason="bank_write")

    # ZERO durable Hivemind state: no staging objects, no commit, storage frozen.
    assert not any(k.startswith(staging_prefix) for k in storage.objects)
    assert _commit_count(storage, "hive-fenced") == 0
    assert storage.objects == before
    # The linearization pointer never advanced (still bank_version -1).
    assert storage.objects.get(pointer_key) == pointer_before


# =============================================================================
# 5b. TOCTOU close — membership change BETWEEN resolve and commit() fails closed
# =============================================================================
#
# The ACTIVE-member / token-epoch gate runs at resolve_sink, but the sink then
# captures fencing_token / membership_epoch and applies them later. Membership
# can change between resolve and commit() (evict / epoch bump) WITHOUT touching
# token/term/pointer, and assert_commit_allowed is NOT a membership gate. These
# tests build the sink (resolve), THEN mutate membership on the SAME store, THEN
# call commit() and assert it fails closed (RegistryRefused) with ZERO durable
# staging. RED without the commit-time _assert_local_membership_current() check:
# commit() would proceed (token/term/pointer still valid) and stage/apply a
# commit under a stale membership view.


async def _assert_zero_durable_staging(
    storage: GuardFakeStorage, space_id: str, before: dict, pointer_key: str
) -> None:
    """Shared assertion: a fail-closed commit() left ZERO durable Hivemind state
    — no staging objects, no BankCommit, pointer unmoved, storage frozen."""
    staging_prefix = layout.staging_prefix(space_id)
    assert not any(k.startswith(staging_prefix) for k in storage.objects)
    assert _commit_count(storage, space_id) == 0
    assert storage.objects == before
    assert storage.objects.get(pointer_key) == before.get(pointer_key)


@pytest.mark.asyncio
async def test_commit_fails_closed_when_local_node_evicted_after_resolve() -> None:
    """TOCTOU close: the local node is EVICTED (status flip + epoch bump) AFTER
    the sink is built but BEFORE commit(). commit() must fail closed
    (RegistryRefused) with ZERO durable staging — assert_commit_allowed still
    passes (token/term/pointer unchanged by an eviction), so only the commit-time
    membership re-validation catches it.

    RED without the fix: commit() proceeds and stages/applies a commit under the
    superseded membership, letting an evicted local holder mutate shared bank
    state (the verdict's core race)."""
    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-evict-race")  # ACTIVE n1, epoch 1, HELD token
    sink = _staged_sink(storage, "hive-evict-race")
    await sink.put("hive-evict-race/bank/a.md", "A")

    # Membership changes AFTER resolve: a peer is added ACTIVE (so the hive stays
    # HEALTHY upstream) and the LOCAL node is flipped to EVICTED at a bumped epoch.
    # Token/term/pointer are deliberately UNCHANGED — the eviction path
    # (lifecycle.evict_member) only rewrites membership.
    store = HivemindStateStore(storage=storage, space_id="hive-evict-race")  # type: ignore[arg-type]
    peer_keys = generate_peer_keypair()
    local_keys = generate_peer_keypair()
    await store.set_membership(
        MembershipView(
            epoch=2,
            members=[
                Member(node_id=NODE_ID, public_key=local_keys.public_key, status=MemberStatus.EVICTED),
                Member(node_id="peer-active", public_key=peer_keys.public_key, status=MemberStatus.ACTIVE),
            ],
        )
    )

    before = storage.snapshot()
    pointer_key = layout.bank_version_key("hive-evict-race")
    with pytest.raises(RegistryRefused):
        await sink.commit(reason="bank_write")
    await _assert_zero_durable_staging(storage, "hive-evict-race", before, pointer_key)


@pytest.mark.asyncio
async def test_commit_fails_closed_when_epoch_bumped_after_resolve() -> None:
    """TOCTOU close: the membership epoch advances PAST the held token's
    membership_epoch AFTER the sink is built but BEFORE commit() (the local node
    stays ACTIVE, but its token was granted under the now-superseded epoch).
    commit() must fail closed (RegistryRefused) with ZERO durable staging.

    RED without the fix: token/term/pointer are unchanged so assert_commit_allowed
    passes and commit() applies a commit carrying a stale membership_epoch."""
    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-epoch-race")  # ACTIVE n1, epoch 1, token epoch 1
    sink = _staged_sink(storage, "hive-epoch-race")
    await sink.put("hive-epoch-race/bank/a.md", "A")

    # Epoch bumps to 2 AFTER resolve (local node still ACTIVE); the HELD token is
    # left at membership_epoch 1 — now stale relative to the current view.
    store = HivemindStateStore(storage=storage, space_id="hive-epoch-race")  # type: ignore[arg-type]
    local_keys = generate_peer_keypair()
    await store.set_membership(
        MembershipView(
            epoch=2,
            members=[Member(node_id=NODE_ID, public_key=local_keys.public_key, status=MemberStatus.ACTIVE)],
        )
    )

    before = storage.snapshot()
    pointer_key = layout.bank_version_key("hive-epoch-race")
    with pytest.raises(RegistryRefused):
        await sink.commit(reason="bank_write")
    await _assert_zero_durable_staging(storage, "hive-epoch-race", before, pointer_key)


@pytest.mark.asyncio
async def test_commit_fails_closed_when_local_node_dropped_after_resolve() -> None:
    """TOCTOU close: the local node is DROPPED from the member list entirely
    (epoch bump) AFTER the sink is built but BEFORE commit(). commit() must fail
    closed (RegistryRefused) with ZERO durable staging — the local node is no
    longer ACTIVE in the current membership.

    RED without the fix: token/term/pointer remain valid so commit() proceeds."""
    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-drop-race")  # ACTIVE n1, epoch 1, HELD token
    sink = _staged_sink(storage, "hive-drop-race")
    await sink.put("hive-drop-race/bank/a.md", "A")

    # The local node disappears from the membership; a different ACTIVE peer keeps
    # the hive HEALTHY upstream. Token/term/pointer untouched.
    store = HivemindStateStore(storage=storage, space_id="hive-drop-race")  # type: ignore[arg-type]
    peer_keys = generate_peer_keypair()
    await store.set_membership(
        MembershipView(
            epoch=2,
            members=[Member(node_id="peer-active", public_key=peer_keys.public_key, status=MemberStatus.ACTIVE)],
        )
    )

    before = storage.snapshot()
    pointer_key = layout.bank_version_key("hive-drop-race")
    with pytest.raises(RegistryRefused):
        await sink.commit(reason="bank_write")
    await _assert_zero_durable_staging(storage, "hive-drop-race", before, pointer_key)


@pytest.mark.asyncio
async def test_commit_succeeds_when_membership_unchanged_after_resolve() -> None:
    """GREEN counterpart: when membership is STILL current at commit() (local node
    ACTIVE, token epoch matches), the commit-time re-validation does NOT
    over-refuse — the staged commit proceeds and bumps bank_version exactly once.
    Guards against the membership check rejecting the healthy path."""
    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-no-race")
    sink = _staged_sink(storage, "hive-no-race")

    await sink.put("hive-no-race/bank/a.md", "A")
    pointer = await sink.commit(reason="bank_write")

    assert pointer is not None
    assert pointer.bank_version == 0
    assert _commit_count(storage, "hive-no-race") == 1
    assert storage.objects["hive-no-race/bank/a.md"] == "A"


@pytest.mark.asyncio
async def test_commit_serializes_with_eviction_during_staging_window() -> None:
    """TOCTOU close — the verdict's UNCOVERED race: a membership change that lands
    DURING the commit's staging window (AFTER the pre-stage re-check would have
    passed, while stage_commit / apply_commit are in flight) must NOT let a stale
    commit linearize. commit() runs the membership re-check + stage + apply UNDER
    ``lifecycle._membership_lock`` — the SAME per-(loop, space) lock evict_member /
    add_member hold — so an eviction cannot interleave the staging window: it
    either completes BEFORE commit takes the lock (re-check fails closed) or WAITS
    behind commit until the flip is done.

    Driven deterministically: the test HOLDS the membership lock (standing in for
    an in-flight evict_member, which mutates membership under exactly this lock),
    starts commit() concurrently, proves commit BLOCKS before writing ANY staging
    (it is gated behind the lock, not merely pre-checked), then applies the
    eviction's effect and releases. commit() resumes, the now load-bearing
    re-check observes the bumped epoch / EVICTED status, and fails closed with
    ZERO durable staging.

    RED before this PR's serialization (membership re-check NOT under the lock):
    commit() would stage/apply while the lock was held elsewhere, linearizing a
    commit under a superseded membership view — an evicted local holder mutating
    shared bank state, the exact race a bare pre-stage check leaves open."""
    from live_mem.core.hivemind.lifecycle import _membership_lock

    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-evict-staging")  # ACTIVE n1, epoch 1, HELD
    sink = _staged_sink(storage, "hive-evict-staging")
    await sink.put("hive-evict-staging/bank/a.md", "A")

    store = HivemindStateStore(storage=storage, space_id="hive-evict-staging")  # type: ignore[arg-type]
    staging_prefix = layout.staging_prefix("hive-evict-staging")
    pointer_key = layout.bank_version_key("hive-evict-staging")

    lock = _membership_lock("hive-evict-staging")
    await lock.acquire()  # stands in for an in-flight evict_member holding the lock
    try:
        commit_task = asyncio.create_task(sink.commit(reason="bank_write"))
        # Let commit() run its READ-ONLY prelude (snapshot, parent/term,
        # assert_commit_allowed) until it parks on the membership lock we hold.
        for _ in range(20):
            await asyncio.sleep(0)
            if commit_task.done():
                break
        # SERIALIZATION PROOF: commit is blocked on the lock and has staged
        # NOTHING — stage_commit lives INSIDE the lock-guarded section, so an
        # eviction holding the lock fully precedes any staging write.
        assert not commit_task.done()
        assert not any(k.startswith(staging_prefix) for k in storage.objects)

        # The eviction's effect, applied under the lock exactly as evict_member
        # does: bump epoch, flip the local node to EVICTED (a peer stays ACTIVE so
        # the hive remains HEALTHY upstream). Token / term / pointer untouched.
        peer_keys = generate_peer_keypair()
        local_keys = generate_peer_keypair()
        await store.set_membership(
            MembershipView(
                epoch=2,
                members=[
                    Member(node_id=NODE_ID, public_key=local_keys.public_key, status=MemberStatus.EVICTED),
                    Member(node_id="peer-active", public_key=peer_keys.public_key, status=MemberStatus.ACTIVE),
                ],
            )
        )
        before = storage.snapshot()
    finally:
        lock.release()

    # commit() resumes, re-checks membership UNDER the lock, sees the bumped epoch
    # / EVICTED status, and fails closed — ZERO durable staging.
    with pytest.raises(RegistryRefused):
        await commit_task
    await _assert_zero_durable_staging(storage, "hive-evict-staging", before, pointer_key)


@pytest.mark.asyncio
async def test_eviction_blocks_until_commit_flips_pointer_mutation_proof() -> None:
    """Mutation-proof companion: prove the membership lock is held across the WHOLE
    stage->apply window, not just the 6b re-check. While commit() is INSIDE its
    critical section (past 6b, during stage_commit), a REAL concurrent
    MembershipService.evict_member() must NOT be able to complete — it blocks on
    the same _membership_lock until commit() flips the pointer and releases.

    This is the gap Codex flagged on the first variant: a weaker mutant that locked
    ONLY the 6b re-check and released BEFORE stage_commit would still pass the
    "evict-before-commit" interleaving. Here the eviction is launched FROM WITHIN
    the staging window (a spy on stage_commit), so such a mutant would let the
    eviction land mid-staging and trip the in-stage assertion below.

    commit() legitimately SUCCEEDS (the local node was ACTIVE for the entire
    critical section); the eviction lands only AFTER the pointer flip."""
    from live_mem.core.hivemind.lifecycle import MembershipService

    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-evict-blocks")  # n1 ACTIVE, epoch 1, HELD
    # A second ACTIVE peer so evicting n1 is allowed (evict_member refuses to evict
    # the LAST ACTIVE member). Epoch stays 1 so the held token (epoch 1) is current.
    store = HivemindStateStore(storage=storage, space_id="hive-evict-blocks")  # type: ignore[arg-type]
    local_keys = generate_peer_keypair()
    peer_keys = generate_peer_keypair()
    await store.set_membership(
        MembershipView(
            epoch=1,
            members=[
                Member(node_id=NODE_ID, public_key=local_keys.public_key, status=MemberStatus.ACTIVE),
                Member(node_id="peer-active", public_key=peer_keys.public_key, status=MemberStatus.ACTIVE),
            ],
        )
    )

    sink = _staged_sink(storage, "hive-evict-blocks")
    await sink.put("hive-evict-blocks/bank/a.md", "A")

    membership = MembershipService(store)
    observed: dict = {}
    real_stage = sink._crt.stage_commit

    async def spy_stage(*args, **kwargs):
        # We are INSIDE commit()'s critical section: 6b has passed and the
        # membership lock is held. Launch a REAL eviction of the local node and
        # prove it cannot complete while we hold the lock through stage + apply.
        evict_task = asyncio.create_task(
            membership.evict_member(NODE_ID, operator="op-test", confirm=True)
        )
        for _ in range(20):
            await asyncio.sleep(0)
            if evict_task.done():
                break
        observed["evict_done_during_stage"] = evict_task.done()
        observed["evict_task"] = evict_task
        return await real_stage(*args, **kwargs)

    sink._crt.stage_commit = spy_stage  # type: ignore[assignment]

    pointer = await sink.commit(reason="bank_write")

    # commit() SUCCEEDED — the local node was ACTIVE for the whole critical section.
    assert pointer is not None
    assert pointer.bank_version == 0
    assert _commit_count(storage, "hive-evict-blocks") == 1
    # KEY mutation-proof assertion: the concurrent eviction could NOT complete
    # during staging — it was blocked on the membership lock held by commit().
    assert observed["evict_done_during_stage"] is False

    # Once commit() released the lock (after the pointer flip) the eviction lands.
    await observed["evict_task"]
    view = await store.get_membership()
    assert view is not None
    assert view.epoch == 2
    assert any(
        m.node_id == NODE_ID and m.status == MemberStatus.EVICTED.value
        for m in view.members
    )


@pytest.mark.asyncio
async def test_concurrent_commit_loser_leaves_no_orphan_staging() -> None:
    """Two concurrent same-holder commits leave NO orphan staging tree: EXACTLY one
    lands, the other is refused with ZERO durable staging. Because the WHOLE commit
    body (snapshot + parent/term + auth prefilter + membership re-check + stage +
    apply) runs under _membership_lock, the loser re-reads the CURRENT token at the
    auth prefilter INSIDE the lock — after the winner released the token in
    apply_commit — and is refused there (CommitNotAuthorized) BEFORE any
    stage_commit write.

    Deterministic interleaving via a spy on sink_a's assert_commit_allowed: the
    FIRST time sink_a passes auth (token HELD) a concurrent sink_b commit is
    launched and pumped. Whole-body serialization makes sink_b block on the lock
    until sink_a finishes, so sink_a lands and sink_b is refused at its prefilter
    (token now FREE) with no staging.

    This is the concurrent-commit interleaving coverage Codex asked for. NOTE: the
    pre-existing 6b membership re-check (``_assert_local_membership_current``)
    already re-reads the token and refuses before staging, so this interleaving
    never left an orphan even WITHOUT the whole-body serialization (the loser was
    caught at 6b instead of the prefilter) — verified empirically. The whole-body
    serialization makes the auth prefilter robustly consistent with apply rather
    than relying on the 6b token re-check as the implicit backstop; this test pins
    the resulting behaviour (loser refused at the auth prefilter, zero orphan)."""
    storage = GuardFakeStorage()
    await _seed_held_hive(storage, "hive-concurrent")  # n1 ACTIVE, epoch 1, HELD, ptr -1
    sink_a = _staged_sink(storage, "hive-concurrent")
    sink_b = _staged_sink(storage, "hive-concurrent")
    await sink_a.put("hive-concurrent/bank/a.md", "A")
    await sink_b.put("hive-concurrent/bank/b.md", "B")

    real_assert = sink_a._lease.assert_commit_allowed
    state: dict = {"fired": False, "task": None}

    async def spy_assert(intent):
        # sink_a's lease is shared with its CommitRuntime, so apply_commit's G0 also
        # routes here — only act on the FIRST call (the pre-stage prefilter).
        await real_assert(intent)
        if state["fired"]:
            return
        state["fired"] = True
        # sink_a has now passed auth with the token HELD. Launch the concurrent
        # commit and pump the loop so it makes all the progress it can.
        state["task"] = asyncio.create_task(sink_b.commit(reason="bank_write"))
        for _ in range(200):
            await asyncio.sleep(0)
            if state["task"].done():
                break

    sink_a._lease.assert_commit_allowed = spy_assert  # type: ignore[assignment]

    results: list = []
    try:
        results.append(await sink_a.commit(reason="bank_write"))
    except Exception as e:  # noqa: BLE001 - capturing the refusal for assertion
        results.append(e)
    try:
        results.append(await state["task"])
    except Exception as e:  # noqa: BLE001
        results.append(e)

    # EXACTLY one commit landed (bank_version 0); the other was refused.
    pointers = [r for r in results if isinstance(r, BankVersionPointer)]
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(pointers) == 1, f"expected exactly one landed commit, got {results!r}"
    assert len(errors) == 1, f"expected exactly one refusal, got {results!r}"
    assert isinstance(errors[0], CommitNotAuthorized), f"unexpected error: {errors[0]!r}"
    assert pointers[0].bank_version == 0

    # The refused commit left ZERO durable staging — NO orphan staging tree.
    staging_prefix = layout.staging_prefix("hive-concurrent")
    assert not any(k.startswith(staging_prefix) for k in storage.objects), (
        "refused concurrent commit left an orphan staging tree"
    )
    # Exactly one commit journal exists.
    assert _commit_count(storage, "hive-concurrent") == 1


# =============================================================================
# 6. WriteSink is the ONLY local-vs-staged decision (no tool bypass)
# =============================================================================


@pytest.mark.asyncio
async def test_bank_write_non_hivemind_byte_identical_to_direct() -> None:
    """bank_write on a NON-Hivemind space writes the bank file byte-identically
    to a parallel direct storage.put — the DirectLocalWriteSink path is verbatim
    and commit() is a no-op (non-Hivemind byte-for-byte). Proves the tool stays
    route-blind: the SINK alone decided local-vs-staged."""
    storage = GuardFakeStorage()
    await _seed_meta(storage, "plain")  # no _hivemind/ -> DIRECT_LOCAL

    with _ToolHive(storage):
        bank_write = _tool(register_bank_tools, "bank_write")
        res = await bank_write("plain", "activeContext.md", "# body\n")

    assert res["status"] == "ok"
    assert storage.objects["plain/bank/activeContext.md"] == "# body\n"
    # No staging tree, no commit object — pure direct-local write.
    assert not any(k.startswith("plain/_hivemind/") for k in storage.objects)

    # Parallel direct path produces identical bytes for the bank file.
    direct = GuardFakeStorage()
    await direct.put("plain/bank/activeContext.md", "# body\n")
    assert (
        storage.objects["plain/bank/activeContext.md"]
        == direct.objects["plain/bank/activeContext.md"]
    )
