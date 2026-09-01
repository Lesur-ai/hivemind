# -*- coding: utf-8 -*-
"""
Tests for P3-3 (issue #52) — WriteSink boundary + DirectLocalWriteSink (default)
+ StagedHivemindWriteSink fail-closed stub.

Deterministic and offline: backed by the in-memory ``FakeStorage`` from
``tests.test_hivemind_state`` (established import pattern, e.g.
``tests/test_hive_status_label.py``). No real S3 / network / LLM.

Two surfaces verified:
- DirectLocalWriteSink delegates VERBATIM to StorageService — same stored
  objects / same forwarded args as a direct storage call (byte-for-byte parity).
- StagedHivemindWriteSink is fail-closed — every write op raises the typed
  exception and never touches storage.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from live_mem.core.hivemind import (
    BankVersionPointer,
    CommitRuntime,
    HivemindStateStore,
    LeaseRuntime,
    Member,
    MembershipView,
    NodeIdentity,
    QueueRuntime,
    TermState,
    TokenLeaseState,
    TokenState,
    generate_peer_keypair,
)
from live_mem.core.write_sink import (
    DirectLocalWriteSink,
    StagedHivemindWriteSink,
    StagedWriteNotImplemented,
    WriteSink,
)
from tests.test_hivemind_state import FakeStorage


# =============================================================================
# Staged-sink harness — build a real StagedHivemindWriteSink over a HELD-token
# hive (P5-8 #16 capstone). The sink drives a real CommitRuntime; the only
# authorization is assert_commit_allowed.
# =============================================================================


_FROZEN = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


def _frozen_clock() -> datetime:
    return _FROZEN


async def _seed_held_hive(storage: FakeStorage, space_id: str, node_id: str) -> None:
    """Seed a HELD token + term + bank pointer (-1) so the staged sink can commit
    (assert_commit_allowed G0 passes for ``node_id`` as holder at term 1).

    Also seeds node identity + an ACTIVE membership at epoch 1 (matching the
    token's ``membership_epoch``): the sink re-validates LIVE at commit time that
    the local node is still an ACTIVE member at the current epoch (the TOCTOU
    close), so a healthy-hive fixture must carry identity + membership exactly as
    a ``resolve_sink``-produced sink would have observed them."""
    store = HivemindStateStore(storage=storage, space_id=space_id)  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(
        NodeIdentity(node_id=node_id, public_key=keys.public_key)
    )
    await store.set_membership(
        MembershipView(
            epoch=1,
            members=[Member(node_id=node_id, public_key=keys.public_key)],
        )
    )
    await store.bump_term(1, updated_by_node_id=node_id)
    until = (_FROZEN + timedelta(seconds=300)).isoformat()
    await store.set_token(
        TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id=node_id,
            term=1,
            fencing_token=1,
            granted_at=_FROZEN.isoformat(),
            lease_until=until,
            membership_epoch=1,
            event_id="evt-seed",
        )
    )
    await store.set_bank_version_pointer(BankVersionPointer(bank_version=-1))


def _staged_sink(
    storage: FakeStorage, space_id: str = "space-x", node_id: str = "n1"
) -> StagedHivemindWriteSink:
    """Build the real staged sink over ``storage`` (HELD holder == ``node_id``)."""
    store = HivemindStateStore(storage=storage, space_id=space_id)  # type: ignore[arg-type]
    queue = QueueRuntime(store, space_id)
    lease = LeaseRuntime(store, space_id, queue, clock=_frozen_clock)
    crt = CommitRuntime(
        store, storage, space_id, lease, clock=_frozen_clock  # type: ignore[arg-type]
    )
    return StagedHivemindWriteSink(
        space_id,
        storage,  # type: ignore[arg-type]
        store=store,
        commit_runtime=crt,
        lease=lease,
        local_node_id=node_id,
        fencing_token=1,
        membership_epoch=1,
        clock=_frozen_clock,
    )


# =============================================================================
# Local fakes — extend the shared FakeStorage WITHOUT mutating it.
# (Idiom: CopyFakeStorage(FakeStorage) in test_backup_restore_hivemind_guard.py.)
# =============================================================================


class WriteSinkFakeStorage(FakeStorage):
    """``FakeStorage`` + ``delete_many`` (absent from the shared class).

    The shared ``FakeStorage`` implements put/put_json/get/get_json/delete/
    list_objects/exists but NOT ``delete_many``; ``DirectLocalWriteSink``
    requires it. Added here ONLY (the shared surface is pinned by other suites).
    Mirrors ``StorageService.delete_many``: deletes one-by-one and counts only
    the deletes that did not raise; empty list returns 0.
    """

    async def delete_many(self, keys: list[str]) -> int:
        if not keys:
            return 0
        deleted = 0
        for key in keys:
            try:
                await self.delete(key)
                deleted += 1
            except Exception:  # pragma: no cover - parity safety, fakes don't raise
                pass
        return deleted

    async def copy_object(self, source_key: str, dest_key: str) -> None:
        """Mirror the backup primitive without broadening ``FakeStorage``."""

        content = await self.get(source_key)
        if content is None:
            raise FileNotFoundError(source_key)
        await self.put(dest_key, content)


class RecordingFakeStorage(WriteSinkFakeStorage):
    """Captures the exact args forwarded to ``put`` so the test can assert the
    sink forwards the StorageService default content_type — NOT what the fake
    stores (the shared FakeStorage stores under a bare 'text/plain' default,
    so asserting the stored side would test the fake, not the sink)."""

    def __init__(self) -> None:
        super().__init__()
        self.put_args: list[tuple[str, str, str]] = []

    async def put(
        self, key: str, content: str, content_type: str = "text/plain"
    ) -> None:
        self.put_args.append((key, content, content_type))
        await super().put(key, content, content_type)


# =============================================================================
# WriteSink boundary shape
# =============================================================================


def test_writesink_defines_required_async_methods() -> None:
    """WriteSink is abstract; the four durable-write ops are abstract and
    instantiating WriteSink directly raises TypeError."""
    import inspect

    with pytest.raises(TypeError):
        WriteSink()  # type: ignore[abstract]

    assert WriteSink.__abstractmethods__ == frozenset(
        {"put", "put_json", "delete", "delete_many"}
    )

    # The concrete methods are coroutine functions on the subclasses.
    for name in ("put", "put_json", "delete", "delete_many"):
        assert inspect.iscoroutinefunction(getattr(DirectLocalWriteSink, name))
        assert inspect.iscoroutinefunction(getattr(StagedHivemindWriteSink, name))


def test_staged_is_a_writesink_subclass() -> None:
    """Both concrete sinks satisfy the WriteSink boundary type (P3-7 routing)."""
    assert isinstance(DirectLocalWriteSink(storage=WriteSinkFakeStorage()), WriteSink)
    assert isinstance(_staged_sink(WriteSinkFakeStorage()), WriteSink)


# =============================================================================
# DirectLocalWriteSink — byte-for-byte parity with direct storage
# =============================================================================


@pytest.mark.asyncio
async def test_direct_local_put_matches_direct_storage() -> None:
    """put() through the sink stores the identical object as a direct
    storage.put on a parallel FakeStorage."""
    sink_storage = WriteSinkFakeStorage()
    direct_storage = WriteSinkFakeStorage()
    sink = DirectLocalWriteSink(storage=sink_storage)

    await sink.put("k", "content")
    await direct_storage.put("k", "content")

    assert sink_storage.objects == direct_storage.objects
    assert sink_storage.objects["k"] == "content"


@pytest.mark.asyncio
async def test_direct_local_put_passes_default_content_type() -> None:
    """The sink forwards the StorageService default content_type
    'text/plain; charset=utf-8' (NOT a hardcoded bare 'text/plain'), and an
    explicit content_type is passed through unchanged."""
    storage = RecordingFakeStorage()
    sink = DirectLocalWriteSink(storage=storage)

    await sink.put("k1", "c1")  # no content_type -> sink's default forwarded
    await sink.put("k2", "c2", "application/json")  # explicit -> forwarded verbatim

    assert storage.put_args[0] == ("k1", "c1", "text/plain; charset=utf-8")
    assert storage.put_args[1] == ("k2", "c2", "application/json")


@pytest.mark.asyncio
async def test_direct_local_put_json_delegates_not_reserialize() -> None:
    """put_json through the sink produces identical stored bytes as
    storage.put_json (json.dumps(data, indent=2, ensure_ascii=False)) — proves
    the sink delegates and does not re-serialize differently."""
    sink_storage = WriteSinkFakeStorage()
    direct_storage = WriteSinkFakeStorage()
    sink = DirectLocalWriteSink(storage=sink_storage)

    data = {"b": "é", "a": 1}  # non-ASCII + key order to catch re-serialization
    await sink.put_json("k.json", data)
    await direct_storage.put_json("k.json", data)

    assert sink_storage.objects == direct_storage.objects
    # And the bytes match the StorageService serialization contract exactly.
    assert sink_storage.objects["k.json"] == json.dumps(
        data, indent=2, ensure_ascii=False
    )


@pytest.mark.asyncio
async def test_direct_local_delete_matches_direct_storage() -> None:
    """delete() through the sink removes the object identically to a direct
    storage.delete, and is a no-op on a missing key (matching StorageService)."""
    sink_storage = WriteSinkFakeStorage()
    sink_storage.objects["k"] = "v"
    sink = DirectLocalWriteSink(storage=sink_storage)

    await sink.delete("k")
    assert "k" not in sink_storage.objects

    # No-op on a missing key (must not raise).
    await sink.delete("does-not-exist")
    assert "does-not-exist" not in sink_storage.objects


@pytest.mark.asyncio
async def test_direct_local_delete_many_returns_count() -> None:
    """delete_many(['a','b','c']) through the sink returns int 3 and removes
    exactly those keys; delete_many([]) returns 0 (mirrors StorageService)."""
    sink_storage = WriteSinkFakeStorage()
    for k in ("a", "b", "c", "keep"):
        sink_storage.objects[k] = "v"
    sink = DirectLocalWriteSink(storage=sink_storage)

    count = await sink.delete_many(["a", "b", "c"])
    assert count == 3
    assert isinstance(count, int)
    assert set(sink_storage.objects) == {"keep"}

    assert await sink.delete_many([]) == 0


# =============================================================================
# StagedHivemindWriteSink — P5-8 (#16) capstone: put buffers + atomic commit;
# delete fails closed (apply_commit is put-only).
# =============================================================================


@pytest.mark.asyncio
async def test_staged_put_buffers_and_never_writes_before_commit() -> None:
    """put() BUFFERS — zero storage write until commit() drives the atomic op.
    (Post-P5-8 the staged sink no longer fail-closes on put; it stages.)"""
    storage = WriteSinkFakeStorage()
    await _seed_held_hive(storage, "space-x", "n1")
    sink = _staged_sink(storage, "space-x", "n1")

    before = storage.snapshot()
    await sink.put("space-x/bank/k.md", "content")  # buffers, no write

    # No bank/ object materialized yet; only the seeded _hivemind/ state exists.
    assert storage.objects == before
    assert "space-x/bank/k.md" not in storage.objects


@pytest.mark.asyncio
async def test_staged_put_json_buffers_and_never_writes_before_commit() -> None:
    storage = WriteSinkFakeStorage()
    await _seed_held_hive(storage, "space-x", "n1")
    sink = _staged_sink(storage, "space-x", "n1")

    before = storage.snapshot()
    await sink.put_json("space-x/bank/k.json", {"a": 1})  # buffers, no write

    assert storage.objects == before
    assert "space-x/bank/k.json" not in storage.objects


@pytest.mark.asyncio
async def test_staged_delete_raises_and_never_writes() -> None:
    """delete() FAILS CLOSED: apply_commit is put-only (no live-bank deletion),
    so a delete cannot be a forward commit. Refuses; storage untouched."""
    storage = WriteSinkFakeStorage()
    storage.objects["space-x/bank/k.md"] = "v"
    await _seed_held_hive(storage, "space-x", "n1")
    sink = _staged_sink(storage, "space-x", "n1")

    before = storage.snapshot()
    with pytest.raises(StagedWriteNotImplemented):
        await sink.delete("space-x/bank/k.md")

    assert storage.objects == before
    assert storage.delete_calls == 0


@pytest.mark.asyncio
async def test_staged_delete_many_raises_and_never_writes() -> None:
    storage = WriteSinkFakeStorage()
    for k in ("space-x/bank/a.md", "space-x/bank/b.md"):
        storage.objects[k] = "v"
    await _seed_held_hive(storage, "space-x", "n1")
    sink = _staged_sink(storage, "space-x", "n1")

    before = storage.snapshot()
    with pytest.raises(StagedWriteNotImplemented):
        await sink.delete_many(["space-x/bank/a.md", "space-x/bank/b.md"])

    assert storage.objects == before
    assert storage.delete_calls == 0


@pytest.mark.asyncio
async def test_staged_put_then_commit_promotes_bank_file() -> None:
    """put() + commit() drives ONE atomic CommitRuntime commit: the bank file
    lands at its live key, a BankCommit appears in commits/, and the pointer
    advances to bank_version 0."""
    storage = WriteSinkFakeStorage()
    await _seed_held_hive(storage, "space-x", "n1")
    sink = _staged_sink(storage, "space-x", "n1")

    await sink.put("space-x/bank/k.md", "content")
    pointer = await sink.commit(reason="bank_write")

    from live_mem.core.hivemind import layout

    assert pointer is not None
    assert pointer.bank_version == 0
    assert storage.objects["space-x/bank/k.md"] == "content"
    assert layout.commit_key("space-x", 0) in storage.objects


def test_staged_exception_message_references_gating_issue() -> None:
    """The deferred-leg fail-closed message references the gating protocol issues
    (#8/#9) per the P3-3 acceptance criterion."""
    exc = StagedWriteNotImplemented(op="delete", key="k")
    msg = str(exc)
    assert "#8" in msg
    assert "#9" in msg
