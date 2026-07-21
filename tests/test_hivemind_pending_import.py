# -*- coding: utf-8 -*-
"""Project Mesh pending-aware bootstrap import (P10-3, issue #191).

The target is admitted PENDING at e+1, so it must import a snapshot in which it
is the PENDING self-member and stay node_status UNSAFE (route REFUSE) until its
own proven e+2 self-activation flips it HEALTHY.
"""

from __future__ import annotations

import json

import pytest

from live_mem.core.hivemind import (
    BankCommit,
    BankVersionPointer,
    BootstrapError,
    BootstrapService,
    HiveNodeStatus,
    HivemindStateStore,
    Member,
    MemberStatus,
    MembershipService,
    MembershipView,
    NodeIdentity,
    generate_peer_keypair,
)
from live_mem.core.hivemind.lifecycle import (
    WriteRoute,
    resolve_hive_context,
    route_for_context,
)
from tests.test_hivemind_state import FakeStorage

SOURCE = "source-space"
TARGET = "target-space"
SRC_ID = "sourcenode00000000000000000000aa"
TGT_ID = "peernode0000000000000000000000bb"


async def _seed_source_with_pending_peer(storage: FakeStorage):
    """Source at e+1: SRC ACTIVE, target PENDING (post admit_pending), bv=1."""
    await storage.put(f"{SOURCE}/_meta.json", json.dumps({"space_id": SOURCE, "version": 1}))
    await storage.put(f"{SOURCE}/_rules.md", "# Rules")
    await storage.put(f"{SOURCE}/bank/.keep", "")
    await storage.put(f"{SOURCE}/bank/activeContext.md", "# Active")
    await storage.put(f"{SOURCE}/live/.keep", "")

    store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]
    src_keys = generate_peer_keypair()
    tgt_keys = generate_peer_keypair()
    await store.set_node_identity(NodeIdentity(node_id=SRC_ID, public_key=src_keys.public_key))
    await store.set_membership(
        MembershipView(
            epoch=2,
            members=[
                Member(node_id=SRC_ID, public_key=src_keys.public_key),
                Member(node_id=TGT_ID, public_key=tgt_keys.public_key, status=MemberStatus.PENDING),
            ],
        )
    )
    await store.bump_term(2, updated_by_node_id=SRC_ID)
    await store.append_commit(
        BankCommit(bank_version=1, parent_bank_version=0, term=2, commit_id="c1", committed_by_node_id=SRC_ID)
    )
    await store.set_bank_version_pointer(BankVersionPointer(bank_version=1, commit_id="c1"))
    return src_keys, tgt_keys


async def _seed_blank_target(storage: FakeStorage) -> None:
    await storage.put(f"{TARGET}/_meta.json", json.dumps({"space_id": TARGET, "version": 1}))
    await storage.put(f"{TARGET}/_rules.md", "")
    await storage.put(f"{TARGET}/live/.keep", "")
    await storage.put(f"{TARGET}/bank/.keep", "")


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


async def test_pending_import_adopts_pending_self_and_stays_unsafe(storage) -> None:
    _src, tgt_keys = await _seed_source_with_pending_peer(storage)
    await _seed_blank_target(storage)
    svc = BootstrapService(storage)  # type: ignore[arg-type]

    snapshot = await svc.export_snapshot(SOURCE)
    assert snapshot.manifest.membership_epoch == 2

    result = await svc.import_pending_snapshot(TARGET, snapshot, tgt_keys)
    # Adopted the PENDING self-member's node_id, NOT the active source.
    assert result.local_node_id == TGT_ID
    assert result.membership_epoch == 2
    # Stays UNSAFE (never HEALTHY) so it routes REFUSE as defense-in-depth.
    assert result.node_status == HiveNodeStatus.UNSAFE

    tstore = HivemindStateStore(storage=storage, space_id=TARGET)  # type: ignore[arg-type]
    health = await tstore.get_node_status()
    assert HiveNodeStatus(health.status) == HiveNodeStatus.UNSAFE
    node = await tstore.get_node_identity()
    assert node.node_id == TGT_ID
    membership = await tstore.get_membership()
    tgt_member = next(m for m in membership.members if m.node_id == TGT_ID)
    assert tgt_member.status == "pending"

    # A pending/unsafe target routes REFUSE for ordinary writes.
    ctx = await resolve_hive_context(storage, TARGET)  # type: ignore[arg-type]
    assert route_for_context(ctx) == WriteRoute.REFUSE


async def test_pending_import_then_self_activation_reaches_active_healthy(storage) -> None:
    _src, tgt_keys = await _seed_source_with_pending_peer(storage)
    await _seed_blank_target(storage)
    svc = BootstrapService(storage)  # type: ignore[arg-type]
    snapshot = await svc.export_snapshot(SOURCE)
    await svc.import_pending_snapshot(TARGET, snapshot, tgt_keys)

    tstore = HivemindStateStore(storage=storage, space_id=TARGET)  # type: ignore[arg-type]
    membership_svc = MembershipService(tstore)
    # Self-activation advances e+1 -> e+2 despite UNSAFE, promoting self ACTIVE.
    view = await membership_svc.apply_self_activation(expected_epoch=2)
    assert view.epoch == 3
    assert next(m for m in view.members if m.node_id == TGT_ID).status == "active"
    # The caller (pairing service) flips HEALTHY last; simulate it here and
    # verify routing becomes non-REFUSE.
    from live_mem.core.hivemind import NodeHealth

    await tstore.set_node_status(NodeHealth(status=HiveNodeStatus.HEALTHY, reason="mesh_active"))
    ctx = await resolve_hive_context(storage, TARGET)  # type: ignore[arg-type]
    assert route_for_context(ctx) != WriteRoute.REFUSE


async def test_pending_import_refuses_active_key_and_wrong_key(storage) -> None:
    src_keys, _tgt = await _seed_source_with_pending_peer(storage)
    await _seed_blank_target(storage)
    svc = BootstrapService(storage)  # type: ignore[arg-type]
    snapshot = await svc.export_snapshot(SOURCE)

    # A key matching the ACTIVE source must be refused (no adopting an active
    # identity), and a random key (no pending match) must be refused.
    with pytest.raises(BootstrapError):
        await svc.import_pending_snapshot(TARGET, snapshot, src_keys)
    with pytest.raises(BootstrapError):
        await svc.import_pending_snapshot(TARGET, snapshot, generate_peer_keypair())
