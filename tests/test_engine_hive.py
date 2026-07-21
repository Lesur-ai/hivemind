# -*- coding: utf-8 -*-
"""
Tests for P3-4 — HiveEngine read-only coordination facade over
``HivemindStateStore`` (+ optional ``HivemindPeerChannel``).

Asserts:
- DI construction from a store (+ optional peer);
- ``status()`` is sourced from the imported ``lifecycle.hive_status`` aggregate
  (full-dict equality + targeted field checks);
- every read delegator is a value-equal pass-through to the wrapped store;
- ``expected_ack_node_ids()`` delegates to the sync ``lifecycle`` helper
  (ACTIVE only, EVICTED/LEAVING excluded);
- ``CorruptedStateError`` propagates UNCHANGED from ``status()`` and from each
  read path (no catch-and-default) — including the ack quorum path;
- NO mutation/coordination primitives are exposed (wrap-don't-rewrite, AC4);
- the optional peer is HELD, never proxied as transport/coordination runtime.

Deterministic: an in-memory ``FakeStorage`` stand-in (copied from
``tests/test_hivemind_state.py``, which is not importable) — no real S3, no
network, no clock dependency.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from live_mem.core.hivemind import (
    Ack,
    BankCommit,
    BankVersionPointer,
    CorruptedStateError,
    EventEnvelope,
    EventType,
    HivemindPeerChannel,
    HivemindStateStore,
    Member,
    MemberStatus,
    MembershipView,
    NodeHealth,
    NodeIdentity,
    QueueEntry,
    TermState,
    TokenLeaseState,
    TokenState,
    Tombstone,
    Watermark,
    generate_peer_keypair,
    layout,
    lifecycle,
)

from live_mem.core.engines.hive import HiveEngine


# =============================================================================
# Fake storage — in-memory StorageService stand-in (no S3)
# =============================================================================


class FakeStorage:
    """Minimal in-memory implementation of the ``StorageService`` contract used
    by ``HivemindStateStore`` (put / put_json / get / get_json / delete /
    list_objects / exists). Copied from ``tests/test_hivemind_state.py`` because
    that class is module-local and not importable."""

    def __init__(self) -> None:
        self.objects: dict[str, str] = {}
        self.put_calls = 0
        self.get_calls = 0
        self.delete_calls = 0

    async def put(self, key: str, content: str, content_type: str = "text/plain") -> None:
        self.put_calls += 1
        self.objects[key] = content

    async def put_json(self, key: str, data: dict[str, Any]) -> None:
        await self.put(key, json.dumps(data, indent=2, ensure_ascii=False))

    async def get(self, key: str) -> str | None:
        self.get_calls += 1
        return self.objects.get(key)

    async def get_json(self, key: str) -> dict | None:
        raw = await self.get(key)
        return None if raw is None else json.loads(raw)

    async def delete(self, key: str) -> None:
        self.delete_calls += 1
        self.objects.pop(key, None)

    async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
        out: list[dict] = []
        for key in sorted(self.objects):
            if key.startswith(prefix):
                out.append({"Key": key, "Size": len(self.objects[key]), "LastModified": ""})
                if max_keys and len(out) >= max_keys:
                    break
        return out

    async def exists(self, key: str) -> bool:
        return key in self.objects

    def snapshot(self) -> dict[str, str]:
        return deepcopy(self.objects)


SPACE = "alpha"


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def store(storage: FakeStorage) -> HivemindStateStore:
    return HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]


@pytest.fixture
def engine(store: HivemindStateStore) -> HiveEngine:
    return HiveEngine(store)


async def _seed_hive(store: HivemindStateStore) -> None:
    """Seed a structurally-complete, HEALTHY-ish Hivemind state via the store's
    OWN setters (test-setup writes, NOT engine surface)."""
    await store.set_node_identity(
        NodeIdentity(node_id="n1", display_name="Node One", public_key="")
    )
    await store.set_membership(
        MembershipView(
            epoch=3,
            members=[
                Member(node_id="n1", display_name="Node One", status=MemberStatus.ACTIVE),
                Member(node_id="n2", display_name="Node Two", status=MemberStatus.ACTIVE),
            ],
        )
    )
    await store.bump_term(5, "n1")
    await store.set_token(
        TokenLeaseState(state=TokenState.FREE, term=5, fencing_token=5)
    )
    await store.set_bank_version_pointer(
        BankVersionPointer(bank_version=7, commit_id="commit-7")
    )
    await store.enqueue(
        QueueEntry(event_id="q-evt", sequence=0, requester_node_id="n2")
    )


# =============================================================================
# Construction (DI)
# =============================================================================


@pytest.mark.asyncio
async def test_construct_from_store_via_di(store: HivemindStateStore) -> None:
    engine = HiveEngine(store)
    assert engine.peer is None
    # space_id is read from the wrapped store (mono-tenant; no tenant param).
    assert engine.space_id == SPACE


@pytest.mark.asyncio
async def test_construct_with_optional_peer(store: HivemindStateStore) -> None:
    keypair = generate_peer_keypair()
    peer = HivemindPeerChannel(
        state=store,
        local_node_id="n1",
        private_key=keypair.private_key,
    )
    engine = HiveEngine(store, peer)
    assert engine.peer is peer


# =============================================================================
# status() — sourced from lifecycle.hive_status
# =============================================================================


@pytest.mark.asyncio
async def test_status_returns_snapshot_fields(
    store: HivemindStateStore, storage: FakeStorage, engine: HiveEngine
) -> None:
    await _seed_hive(store)
    result = await engine.status()
    # status() is the lifecycle aggregate verbatim (the EPIC-mandated source).
    assert result == await lifecycle.hive_status(storage, SPACE)  # type: ignore[arg-type]
    # The full contracted key set is present.
    assert set(result.keys()) == {
        "space_id",
        "hive_status",
        "is_hive",
        "protocol_version",
        "membership_epoch",
        "peers",
        "expected_ack_node_ids",
        "term",
        "bank_version",
        "commit_id",
        "node_status",
        "reason",
    }


@pytest.mark.asyncio
async def test_status_surfaces_epoch_term_bank_version(
    store: HivemindStateStore, storage: FakeStorage, engine: HiveEngine
) -> None:
    await _seed_hive(store)
    result = await engine.status()
    # Aggregate snapshot fields present and correct (AC3). NB: lifecycle.hive_status
    # emits NO 'token_holder'/'queue_head' key — assert only on real fields.
    assert result["is_hive"] is True
    assert result["membership_epoch"] == 3
    assert result["term"] == 5
    assert result["bank_version"] == 7
    assert result["commit_id"] == "commit-7"
    # peers reflect the membership composition.
    assert {p["node_id"] for p in result["peers"]} == {"n1", "n2"}
    assert result["expected_ack_node_ids"] == ["n1", "n2"]


# =============================================================================
# Read delegators — value-equal pass-through to the wrapped store
# =============================================================================


@pytest.mark.asyncio
async def test_read_methods_delegate_passthrough(
    store: HivemindStateStore, engine: HiveEngine
) -> None:
    await _seed_hive(store)
    # Seed extra read surfaces: a commit, a tombstone, a watermark, an ack,
    # and an event — all via the store's own setters.
    await store.append_commit(
        BankCommit(
            bank_version=7,
            parent_bank_version=6,
            term=5,
            commit_id="commit-7",
            committed_by_node_id="n1",
        )
    )
    await store.add_tombstone(Tombstone(note_id="note-1", deleted_by_node_id="n1"))
    await store.set_watermark(Watermark(node_id="n2", bank_version=6))
    await store.record_ack(Ack(event_id="evt-1", ack_by_node_id="n2"))
    await store.append_event(
        EventEnvelope(
            event_id="evt-1",
            type=EventType.BANK_COMMITTED,
            origin_node_id="n1",
        )
    )

    assert await engine.get_node_identity() == await store.get_node_identity()
    assert await engine.get_node_status() == await store.get_node_status()
    assert await engine.get_membership() == await store.get_membership()
    assert await engine.get_term() == await store.get_term()
    assert await engine.get_token() == await store.get_token()
    assert (
        await engine.get_bank_version_pointer()
        == await store.get_bank_version_pointer()
    )
    assert await engine.list_queue() == await store.list_queue()
    assert await engine.list_commits() == await store.list_commits()
    assert await engine.list_commits(since_bank_version=6) == await store.list_commits(
        since_bank_version=6
    )
    assert await engine.latest_commit() == await store.latest_commit()
    assert await engine.get_commit(7) == await store.get_commit(7)
    assert await engine.list_tombstones() == await store.list_tombstones()
    assert await engine.list_watermarks() == await store.list_watermarks()
    assert await engine.get_watermark("n2") == await store.get_watermark("n2")
    assert await engine.list_acks("evt-1") == await store.list_acks("evt-1")
    assert await engine.count_acks("evt-1") == await store.count_acks("evt-1")
    assert await engine.list_events() == await store.list_events()
    assert await engine.get_event("evt-1") == await store.get_event("evt-1")
    assert await engine.has_event("evt-1") == await store.has_event("evt-1")
    assert await engine.has_event("missing") is False


@pytest.mark.asyncio
async def test_list_queue_preserves_order(
    store: HivemindStateStore, engine: HiveEngine
) -> None:
    await store.enqueue(QueueEntry(event_id="e10", sequence=10, requester_node_id="n1"))
    await store.enqueue(QueueEntry(event_id="e1", sequence=1, requester_node_id="n1"))
    await store.enqueue(QueueEntry(event_id="e2", sequence=2, requester_node_id="n1"))
    via_engine = await engine.list_queue()
    assert [q.sequence for q in via_engine] == [1, 2, 10]
    assert via_engine == await store.list_queue()


@pytest.mark.asyncio
async def test_expected_ack_node_ids_delegates_to_lifecycle(
    store: HivemindStateStore, engine: HiveEngine
) -> None:
    membership = MembershipView(
        epoch=2,
        members=[
            Member(node_id="active1", status=MemberStatus.ACTIVE),
            Member(node_id="active2", status=MemberStatus.ACTIVE),
            Member(node_id="gone", status=MemberStatus.EVICTED),
            Member(node_id="leaving", status=MemberStatus.LEAVING),
        ],
    )
    await store.set_membership(membership)
    result = await engine.expected_ack_node_ids()
    # Only ACTIVE node_ids; EVICTED/LEAVING excluded — identical to the helper.
    assert result == lifecycle.expected_ack_node_ids(membership)
    assert result == ["active1", "active2"]


# =============================================================================
# CorruptedStateError propagation — never swallowed (AC2)
# =============================================================================


@pytest.mark.asyncio
async def test_corruption_propagates_unchanged_status(
    store: HivemindStateStore, storage: FakeStorage, engine: HiveEngine
) -> None:
    await _seed_hive(store)
    # Corrupt members.json so resolve_hive_context (inside hive_status) raises.
    storage.objects[layout.members_key(SPACE)] = "{ this is not valid json"
    with pytest.raises(CorruptedStateError):
        await engine.status()


@pytest.mark.asyncio
async def test_corruption_propagates_unchanged_read_methods(
    store: HivemindStateStore, storage: FakeStorage, engine: HiveEngine
) -> None:
    await _seed_hive(store)
    # Schema-invalid term.json (term must be int >= 0) -> ValidationError ->
    # CorruptedStateError, surfaced unchanged through the facade.
    storage.objects[layout.term_key(SPACE)] = json.dumps({"term": "not-an-int"})
    with pytest.raises(CorruptedStateError):
        await engine.get_term()

    # Broken JSON on members.json surfaces from get_membership AND load_snapshot.
    storage.objects[layout.members_key(SPACE)] = "}{ broken"
    with pytest.raises(CorruptedStateError):
        await engine.get_membership()
    with pytest.raises(CorruptedStateError):
        await engine.load_snapshot()


@pytest.mark.asyncio
async def test_corruption_in_acks_propagates_via_count_acks(
    store: HivemindStateStore, storage: FakeStorage, engine: HiveEngine
) -> None:
    await store.record_ack(Ack(event_id="evt-x", ack_by_node_id="n2"))
    # Corrupt the ack object: count_acks delegates to list_acks, so a corrupt
    # acks/{event_id}/{node}.json must surface rather than be silently counted.
    storage.objects[layout.ack_key(SPACE, "evt-x", "n2")] = "not json at all"
    with pytest.raises(CorruptedStateError):
        await engine.count_acks("evt-x")
    with pytest.raises(CorruptedStateError):
        await engine.list_acks("evt-x")


# =============================================================================
# No mutation / coordination runtime (wrap-don't-rewrite, AC4)
# =============================================================================


@pytest.mark.asyncio
async def test_no_mutation_or_coordination_primitives_exposed(
    engine: HiveEngine,
) -> None:
    forbidden = {
        # store mutation primitives
        "set_node_identity",
        "set_node_status",
        "set_membership",
        "bump_term",
        "set_token",
        "set_bank_version_pointer",
        "enqueue",
        "update_queue_entry_status",
        "remove_queue_entry",
        "record_ack",
        "append_commit",
        "append_event",
        "add_tombstone",
        "set_watermark",
        "rebuild_pointer_from_commits",
        "garbage_collect_tombstones",
        "compact_events_before",
        "initialize",
        # coordination runtime reserved for #6/#7/#8
        "assert_commit_allowed",
        # peer transport runtime
        "send",
        "receive",
        "sign_event",
    }
    for name in forbidden:
        assert not hasattr(engine, name), f"HiveEngine must not expose {name!r}"


@pytest.mark.asyncio
async def test_optional_peer_held_not_proxied(
    store: HivemindStateStore,
) -> None:
    keypair = generate_peer_keypair()
    peer = HivemindPeerChannel(
        state=store,
        local_node_id="n1",
        private_key=keypair.private_key,
    )
    engine = HiveEngine(store, peer)
    # The peer is held (read-only handoff for #6/#7)...
    assert engine.peer is peer
    # ...but never proxied: no transport/coordination delegators on the engine.
    assert not hasattr(engine, "send")
    assert not hasattr(engine, "receive")
    assert not hasattr(engine, "sign_event")
