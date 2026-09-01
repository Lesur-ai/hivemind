# -*- coding: utf-8 -*-
"""Project Mesh two-epoch enrolment membership primitives (P10-3, issue #191).

Covers the NEW ``MembershipService`` primitives that keep a candidate PENDING
across the two serialized fenced transitions (admit e->e+1, activate e+1->e+2)
instead of ``add_member``'s single absent->ACTIVE jump, plus the confined
target-side ``apply_self_activation``.
"""

from __future__ import annotations

import pytest

from live_mem.core.hivemind import (
    BootstrapError,
    EventType,
    HiveNodeStatus,
    HivemindStateStore,
    Member,
    MemberStatus,
    MembershipService,
    MembershipView,
    NodeHealth,
    NodeIdentity,
    active_members,
    expected_ack_node_ids,
    generate_peer_keypair,
)
from tests.test_hivemind_state import FakeStorage


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


async def _seed_source(storage: FakeStorage):
    """Source space: nodeA ACTIVE at epoch 1 (node_status absent == healthy)."""
    keys_a = generate_peer_keypair()
    store = HivemindStateStore(storage=storage, space_id="alpha")  # type: ignore[arg-type]
    await store.set_node_identity(
        NodeIdentity(node_id="nodeA", display_name="A", public_key=keys_a.public_key)
    )
    await store.set_membership(
        MembershipView(epoch=1, members=[Member(node_id="nodeA", public_key=keys_a.public_key)])
    )
    await store.bump_term(2, updated_by_node_id="nodeA")
    return store, keys_a


# ---------------------------------------------------------------------------
# admit_pending_candidate — Transition 1 (e -> e+1), status PENDING
# ---------------------------------------------------------------------------


async def test_admit_pending_adds_pending_member_and_bumps_epoch(storage) -> None:
    store, _ = await _seed_source(storage)
    keys_b = generate_peer_keypair()
    svc = MembershipService(store)

    view = await svc.admit_pending_candidate(
        Member(node_id="nodeB", public_key=keys_b.public_key, scopes=["read"])
    )

    assert view.epoch == 2
    member_b = next(m for m in view.members if m.node_id == "nodeB")
    assert member_b.status == MemberStatus.PENDING.value
    # PENDING is excluded from every ACTIVE roster (all-ACK / ordinary-write).
    assert "nodeB" not in [m.node_id for m in active_members(view)]
    assert "nodeB" not in expected_ack_node_ids(view)
    # Emitted as the generic MEMBERSHIP_UPDATED (not PEER_JOINED).
    events = await store.list_events()
    updated = [e for e in events if e.type == EventType.MEMBERSHIP_UPDATED.value]
    assert len(updated) == 1 and updated[0].payload["status"] == "pending"
    assert not [e for e in events if e.type == EventType.PEER_JOINED.value]


async def test_admit_pending_refuses_duplicate_and_bad_key(storage) -> None:
    store, keys_a = await _seed_source(storage)
    keys_b = generate_peer_keypair()
    svc = MembershipService(store)
    await svc.admit_pending_candidate(Member(node_id="nodeB", public_key=keys_b.public_key))
    # duplicate PENDING node_id
    with pytest.raises(BootstrapError):
        await svc.admit_pending_candidate(Member(node_id="nodeB", public_key=keys_b.public_key))
    # duplicate ACTIVE node_id
    with pytest.raises(BootstrapError):
        await svc.admit_pending_candidate(Member(node_id="nodeA", public_key=generate_peer_keypair().public_key))
    # key already used by an ACTIVE member
    with pytest.raises(BootstrapError):
        await svc.admit_pending_candidate(Member(node_id="nodeC", public_key=keys_a.public_key))
    # missing key
    with pytest.raises(BootstrapError):
        await svc.admit_pending_candidate(Member(node_id="nodeC"))


# ---------------------------------------------------------------------------
# promote_pending_to_active — Transition 2 (e+1 -> e+2), source side
# ---------------------------------------------------------------------------


async def test_two_transitions_never_collapse_into_one(storage) -> None:
    store, _ = await _seed_source(storage)
    keys_b = generate_peer_keypair()
    svc = MembershipService(store)

    v1 = await svc.admit_pending_candidate(Member(node_id="nodeB", public_key=keys_b.public_key))
    assert v1.epoch == 2
    assert next(m for m in v1.members if m.node_id == "nodeB").status == "pending"

    v2 = await svc.promote_pending_to_active("nodeB")
    assert v2.epoch == 3  # a SECOND fenced bump, never same-epoch
    assert next(m for m in v2.members if m.node_id == "nodeB").status == "active"
    assert "nodeB" in expected_ack_node_ids(v2)


async def test_promote_is_idempotent_and_refuses_non_pending(storage) -> None:
    store, _ = await _seed_source(storage)
    keys_b = generate_peer_keypair()
    svc = MembershipService(store)
    await svc.admit_pending_candidate(Member(node_id="nodeB", public_key=keys_b.public_key))
    v = await svc.promote_pending_to_active("nodeB")
    # idempotent: promoting an already-ACTIVE node is a no-op (no further bump)
    v2 = await svc.promote_pending_to_active("nodeB")
    assert v2.epoch == v.epoch
    # cannot promote an absent node
    with pytest.raises(BootstrapError):
        await svc.promote_pending_to_active("ghost")


async def test_add_member_is_not_the_pending_path(storage) -> None:
    # add_member forces ACTIVE in a single bump; it must NOT be used to admit a
    # candidate. Proven by contrast: add_member yields ACTIVE at e+1 directly.
    store, _ = await _seed_source(storage)
    keys_b = generate_peer_keypair()
    svc = MembershipService(store)
    v = await svc.add_member(Member(node_id="nodeB", public_key=keys_b.public_key))
    assert next(m for m in v.members if m.node_id == "nodeB").status == "active"
    assert v.epoch == 2  # single transition -> ACTIVE; the pending path uses TWO


# ---------------------------------------------------------------------------
# remove_pending_candidate — evict a PENDING candidate (evict_member no-ops)
# ---------------------------------------------------------------------------


async def test_remove_pending_candidate_evicts_via_epoch_bump(storage) -> None:
    store, _ = await _seed_source(storage)
    keys_b = generate_peer_keypair()
    svc = MembershipService(store)
    await svc.admit_pending_candidate(Member(node_id="nodeB", public_key=keys_b.public_key))
    # evict_member no-ops on a non-ACTIVE (PENDING) target -> needs the new path
    noop = await svc.evict_member("nodeB", operator="op", confirm=True)
    assert noop.epoch == 2
    assert next(m for m in noop.members if m.node_id == "nodeB").status == "pending"
    # remove_pending_candidate removes it via a real epoch-advancing PEER_EVICTED
    removed = await svc.remove_pending_candidate("nodeB", operator="op", confirm=True)
    assert removed.epoch == 3
    assert next(m for m in removed.members if m.node_id == "nodeB").status == "evicted"
    events = await store.list_events()
    assert any(e.type == EventType.PEER_EVICTED.value for e in events)


async def test_remove_pending_requires_confirm_and_operator(storage) -> None:
    store, _ = await _seed_source(storage)
    keys_b = generate_peer_keypair()
    svc = MembershipService(store)
    await svc.admit_pending_candidate(Member(node_id="nodeB", public_key=keys_b.public_key))
    with pytest.raises(BootstrapError):
        await svc.remove_pending_candidate("nodeB", operator="op", confirm=False)
    with pytest.raises(BootstrapError):
        await svc.remove_pending_candidate("nodeB", operator="", confirm=True)


# ---------------------------------------------------------------------------
# apply_self_activation — target self-promote (NOT health-gated, confined)
# ---------------------------------------------------------------------------


async def _seed_pending_target(storage: FakeStorage, *, unsafe: bool):
    """Target space as imported at e+1: local node nodeB is PENDING, nodeA ACTIVE."""
    keys_a = generate_peer_keypair()
    keys_b = generate_peer_keypair()
    store = HivemindStateStore(storage=storage, space_id="alpha")  # type: ignore[arg-type]
    await store.set_node_identity(
        NodeIdentity(node_id="nodeB", display_name="B", public_key=keys_b.public_key)
    )
    await store.set_membership(
        MembershipView(
            epoch=2,
            members=[
                Member(node_id="nodeA", public_key=keys_a.public_key),
                Member(node_id="nodeB", public_key=keys_b.public_key, status=MemberStatus.PENDING),
            ],
        )
    )
    await store.bump_term(2, updated_by_node_id="nodeA")
    if unsafe:
        await store.set_node_status(NodeHealth(status=HiveNodeStatus.UNSAFE, reason="mesh_pending"))
    return store, keys_a, keys_b


async def test_self_activation_promotes_self_despite_unsafe(storage) -> None:
    # The target is deliberately node_status UNSAFE while PENDING; self-activation
    # must still advance it e+1 -> e+2 (the health gate would otherwise refuse).
    store, _, _ = await _seed_pending_target(storage, unsafe=True)
    svc = MembershipService(store)
    view = await svc.apply_self_activation(expected_epoch=2)
    assert view.epoch == 3
    assert next(m for m in view.members if m.node_id == "nodeB").status == "active"


async def test_self_activation_is_idempotent(storage) -> None:
    store, _, _ = await _seed_pending_target(storage, unsafe=True)
    svc = MembershipService(store)
    first = await svc.apply_self_activation(expected_epoch=2)
    again = await svc.apply_self_activation(expected_epoch=2)
    assert again.epoch == first.epoch == 3


async def test_self_activation_only_promotes_a_pending_self(storage) -> None:
    # If the local node is ACTIVE (not pending), self-activation refuses — it can
    # never be used by an active node to re-bump, nor to promote a peer.
    store, _ = await _seed_source(storage)  # local node nodeA is ACTIVE
    keys_b = generate_peer_keypair()
    svc = MembershipService(store)
    await svc.admit_pending_candidate(Member(node_id="nodeB", public_key=keys_b.public_key))
    with pytest.raises(BootstrapError):
        await svc.apply_self_activation(expected_epoch=2)  # nodeA is ACTIVE, not PENDING


async def test_self_activation_refuses_wrong_expected_epoch(storage) -> None:
    store, _, _ = await _seed_pending_target(storage, unsafe=True)
    svc = MembershipService(store)
    with pytest.raises(BootstrapError):
        await svc.apply_self_activation(expected_epoch=5)  # local is at 2


async def test_evict_member_incarnation_compare_and_evict(storage) -> None:
    # Atomic compare-and-evict: evict_member removes the ACTIVE target only when its
    # incarnation matches. A wrong incarnation fails closed (no eviction), and an
    # unrelated rescope bumps the epoch but preserves the incarnation, so a later
    # force-eviction with the ORIGINAL incarnation still succeeds.
    from live_mem.core.hivemind import MembershipIncarnationError

    store = HivemindStateStore(storage=storage, space_id="alpha")  # type: ignore[arg-type]
    keys_a = generate_peer_keypair()
    keys_b = generate_peer_keypair()
    await store.set_node_identity(NodeIdentity(node_id="nodeA", public_key=keys_a.public_key))
    await store.set_membership(
        MembershipView(
            epoch=5,
            members=[
                Member(node_id="nodeA", public_key=keys_a.public_key),
                Member(node_id="nodeB", public_key=keys_b.public_key, incarnation="pairX"),
            ],
        )
    )
    svc = MembershipService(store)

    with pytest.raises(MembershipIncarnationError):
        await svc.evict_member("nodeB", operator="op", confirm=True, expected_incarnation="pairY")
    v = await store.get_membership()
    assert any(m.node_id == "nodeB" and m.status == MemberStatus.ACTIVE.value for m in v.members)
    assert v.epoch == 5  # fail closed: no eviction, no epoch bump

    await svc.update_member_scopes("nodeB", ["read"])  # unrelated mutation bumps epoch
    v2 = await store.get_membership()
    assert v2.epoch == 6
    assert next(m for m in v2.members if m.node_id == "nodeB").incarnation == "pairX"  # preserved

    await svc.evict_member("nodeB", operator="op", confirm=True, expected_incarnation="pairX")
    v3 = await store.get_membership()
    assert any(m.node_id == "nodeB" and m.status == MemberStatus.EVICTED.value for m in v3.members)
    assert v3.epoch == 7

    # A caller that supplies an expected incarnation requires an exact atomic
    # match.  ``None`` is not a legacy escape hatch: pairing bootstrap exports
    # intentionally strip source-local tags on peers, so accepting it would let
    # a stale retained pairing evict a re-enrolled node after a restore.
    keys_c = generate_peer_keypair()
    await store.set_membership(
        MembershipView(
            epoch=8,
            members=[
                Member(node_id="nodeA", public_key=keys_a.public_key),
                Member(node_id="nodeC", public_key=keys_c.public_key),  # incarnation absent
            ],
        )
    )
    with pytest.raises(MembershipIncarnationError):
        await svc.evict_member(
            "nodeC", operator="op", confirm=True, expected_incarnation="pairZ"
        )
    v4 = await store.get_membership()
    assert any(m.node_id == "nodeC" and m.status == MemberStatus.ACTIVE.value for m in v4.members)


async def test_remove_pending_candidate_requires_exact_expected_incarnation(storage) -> None:
    """Pairing give-up cannot remove a different PENDING incarnation."""

    from live_mem.core.hivemind import MembershipIncarnationError

    store, _ = await _seed_source(storage)
    svc = MembershipService(store)
    keys_b = generate_peer_keypair()
    await svc.admit_pending_candidate(
        Member(
            node_id="nodeB",
            public_key=keys_b.public_key,
            incarnation="pair_current",
        )
    )

    with pytest.raises(MembershipIncarnationError):
        await svc.remove_pending_candidate(
            "nodeB",
            operator="op",
            confirm=True,
            expected_incarnation="pair_stale",
        )
    view = await store.get_membership()
    assert view is not None
    member = next(item for item in view.members if item.node_id == "nodeB")
    assert member.status == MemberStatus.PENDING.value
    assert member.incarnation == "pair_current"


async def test_admit_and_promote_expected_epoch_compare(storage) -> None:
    # Compare-and-mutate: admit/promote at exactly expected_epoch under the
    # membership lock. A wrong expected_epoch (a concurrent mutation advanced it)
    # fails closed without mutating; the idempotent already-active promote is a
    # no-op regardless of expected_epoch.
    from live_mem.core.hivemind import MembershipEpochError

    store, keys_a = await _seed_source(storage)  # nodeA ACTIVE at epoch 1
    svc = MembershipService(store)
    keys_b = generate_peer_keypair()

    with pytest.raises(MembershipEpochError):
        await svc.admit_pending_candidate(Member(node_id="nodeB", public_key=keys_b.public_key), expected_epoch=99)
    v0 = await store.get_membership()
    assert v0.epoch == 1 and not any(m.node_id == "nodeB" for m in v0.members)  # fail closed

    v1 = await svc.admit_pending_candidate(Member(node_id="nodeB", public_key=keys_b.public_key), expected_epoch=1)
    assert v1.epoch == 2 and any(m.node_id == "nodeB" and m.status == MemberStatus.PENDING.value for m in v1.members)

    with pytest.raises(MembershipEpochError):
        await svc.promote_pending_to_active("nodeB", expected_epoch=99)
    v2 = await store.get_membership()
    assert v2.epoch == 2 and next(m for m in v2.members if m.node_id == "nodeB").status == MemberStatus.PENDING.value

    v3 = await svc.promote_pending_to_active("nodeB", expected_epoch=2)
    assert v3.epoch == 3 and next(m for m in v3.members if m.node_id == "nodeB").status == MemberStatus.ACTIVE.value

    v4 = await svc.promote_pending_to_active("nodeB", expected_epoch=99)  # already active -> no-op
    assert v4.epoch == 3


# ---------------------------------------------------------------------------
# Pairing-activation fence: operator epoch-advancing mutations (add_member,
# update_member_scopes) consult the fence under the membership lock; the
# pairing's OWN give-up paths (remove_pending_candidate, evict_member) do not, so
# a stuck pairing can always be cleared without self-blocking.
# ---------------------------------------------------------------------------


async def test_add_member_and_rescope_consult_activation_fence(storage) -> None:
    from live_mem.core.reservation_guard import (
        PairingActivationError,
        clear_pairing_activation_checker,
        register_pairing_activation_checker,
    )

    store, _ = await _seed_source(storage)  # nodeA ACTIVE at epoch 1, space "alpha"
    svc = MembershipService(store)

    async def refuse_alpha(space_id: str, ignore_pair_id) -> None:
        # Operator paths pass ignore_pair_id=None -> always fenced for "alpha".
        if space_id == "alpha" and ignore_pair_id is None:
            raise PairingActivationError(space_id)

    base_view = await store.get_membership()
    register_pairing_activation_checker(refuse_alpha)
    try:
        with pytest.raises(PairingActivationError):
            await svc.add_member(
                Member(node_id="nodeX", public_key=generate_peer_keypair().public_key)
            )
        with pytest.raises(PairingActivationError):
            await svc.update_member_scopes("nodeA", ["read"])
        # The bulk operator reconcile path is fenced too (it also bumps the epoch).
        with pytest.raises(PairingActivationError):
            async with svc._space_lock():  # apply_membership_plan presumes the lock
                await svc.apply_membership_plan(
                    add=[
                        Member(
                            node_id="nodeZ",
                            public_key=generate_peer_keypair().public_key,
                        )
                    ],
                    rescope=[],
                    revoke=[],
                    operator="op",
                    reason="",
                    base_view=base_view,
                )
    finally:
        clear_pairing_activation_checker()
    # Fenced BEFORE any mutation: membership is untouched at epoch 1.
    view = await store.get_membership()
    assert view.epoch == 1 and {m.node_id for m in view.members} == {"nodeA"}
    assert next(m for m in view.members if m.node_id == "nodeA").scopes is None


async def test_give_up_paths_fenced_by_default_bypass_only_for_owning_pairing(storage) -> None:
    """Caller-bound fence: the give-up primitives (remove_pending_candidate,
    evict_member) are fenced for an EXTERNAL caller during a pairing's activation,
    but the OWNING pairing bypasses via its own ``activation_pair_id`` so it can
    always converge/clear its own stuck pairing (no self-block, no external
    bypass)."""

    from live_mem.core.reservation_guard import (
        PairingActivationError,
        clear_pairing_activation_checker,
        register_pairing_activation_checker,
    )

    store, _ = await _seed_source(storage)
    svc = MembershipService(store)
    # Seed two PENDING candidates and two ACTIVE members BEFORE arming the fence.
    for nid in ("pend_ext", "pend_own"):
        await svc.admit_pending_candidate(
            Member(node_id=nid, public_key=generate_peer_keypair().public_key)
        )
    for nid in ("dead_ext", "dead_own"):
        await svc.add_member(
            Member(node_id=nid, public_key=generate_peer_keypair().public_key)
        )

    # A pairing "mypair" is mid-activation: refuse every OTHER caller, exempt only
    # the pairing that names itself (mirrors MeshPairingService's real checker).
    async def refuse_unless_own(space_id: str, ignore_pair_id) -> None:
        if ignore_pair_id != "mypair":
            raise PairingActivationError(space_id)

    register_pairing_activation_checker(refuse_unless_own)
    try:
        # EXTERNAL caller (no activation_pair_id) -> fenced.
        with pytest.raises(PairingActivationError):
            await svc.remove_pending_candidate("pend_ext", operator="op", confirm=True)
        with pytest.raises(PairingActivationError):
            await svc.evict_member("dead_ext", operator="op", confirm=True)
        # A DIFFERENT pairing's id is NOT a valid bypass -> still fenced.
        with pytest.raises(PairingActivationError):
            await svc.evict_member(
                "dead_ext", operator="op", confirm=True, activation_pair_id="otherpair"
            )
        # The OWNING pairing bypasses and converges its own give-up.
        await svc.remove_pending_candidate(
            "pend_own", operator="op", confirm=True, activation_pair_id="mypair"
        )
        await svc.evict_member(
            "dead_own", operator="op", confirm=True, activation_pair_id="mypair"
        )
    finally:
        clear_pairing_activation_checker()
    view = await store.get_membership()
    # External targets untouched (fenced); the owning pairing's targets evicted.
    assert next(m for m in view.members if m.node_id == "pend_ext").status == "pending"
    assert next(m for m in view.members if m.node_id == "dead_ext").status == "active"
    assert next(m for m in view.members if m.node_id == "pend_own").status == "evicted"
    assert next(m for m in view.members if m.node_id == "dead_own").status == "evicted"
