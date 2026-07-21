# -*- coding: utf-8 -*-
"""
P5-1 (#9) verification — close the two integrate/verify gaps the P5 readiness
review flagged on the already-delivered membership lifecycle (PR #17). This is a
TEST-ONLY hardening pass over `lifecycle.py`; it adds no runtime code.

The membership lifecycle (`MembershipService`/`ResyncService`/`BootstrapService`,
`resolve_hive_context`/`is_hivemind_space`) is delivered and closed (#9). Two AC
checks were under-pinned:

  * **AC2 — evict-triggered epoch fence (E2E).** The *add*-triggered epoch fence
    is already proven end-to-end (``test_epoch_change_invalidates_old_protocol_messages``,
    ``test_membership_epoch_mismatch_fails_closed``). The *evict*-triggered bump —
    eviction publishes ``epoch + 1`` and a message pinned to the prior epoch must be
    fenced with ``WRONG_MEMBERSHIP_EPOCH`` — had no dedicated end-to-end test.

  * **AC4 — fail-closed scope of ``is_hivemind_space()`` is deliberate.**
    ``resolve_hive_context`` reads only ``node/members/node_status.json`` (documented
    narrowed scope, ``lifecycle.py``). Corruption of a file it reads must
    RAISE ``CorruptedStateError`` (never a false ``not shared``); corruption of
    ``term/token/bank_version.json`` is invisible to *classification* by design — but
    any store read of those files still fails closed at the read layer. This pins both
    halves so a future change that silently widens/narrows the scope, or that swallows
    a corruption into a benign ``False``, goes RED.

Deterministic fake-storage + in-memory peer channel only; no real S3/network/LLM.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from live_mem.core.hivemind import (
    CorruptedStateError,
    EventEnvelope,
    EventType,
    HivemindPeerChannel,
    HivemindStateStore,
    Member,
    MembershipService,
    MembershipView,
    NodeIdentity,
    PeerChannelError,
    PeerErrorCode,
    generate_peer_keypair,
    is_hivemind_space,
    layout,
)
from tests.test_hivemind_state import FakeStorage

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()


async def _seed_hive(storage: FakeStorage, *, space_id: str = "alpha"):
    """A HEALTHY Hivemind space: node identity + one ACTIVE member at epoch 1."""
    keys = generate_peer_keypair()
    store = HivemindStateStore(storage=storage, space_id=space_id)  # type: ignore[arg-type]
    await store.set_node_identity(
        NodeIdentity(node_id="nodeA", display_name="A", public_key=keys.public_key)
    )
    await store.set_membership(
        MembershipView(
            epoch=1,
            members=[
                Member(node_id="nodeA", display_name="A", public_key=keys.public_key)
            ],
        )
    )
    await store.bump_term(2, updated_by_node_id="nodeA")
    return store, keys


# =============================================================================
# AC2 — eviction-triggered epoch bump fences prior-epoch messages (E2E)
# =============================================================================


async def test_eviction_triggered_epoch_fences_old_protocol_messages() -> None:
    """The evict path (distinct from the already-covered add path) must bump the
    epoch and invalidate an in-flight message pinned to the prior epoch."""
    storage = FakeStorage()
    keys_a = generate_peer_keypair()
    keys_b = generate_peer_keypair()
    keys_c = generate_peer_keypair()

    receiver = HivemindStateStore(storage=storage, space_id="alpha")  # type: ignore[arg-type]
    await receiver.set_node_identity(
        NodeIdentity(node_id="nodeA", public_key=keys_a.public_key)
    )
    await receiver.set_membership(
        MembershipView(
            epoch=1,
            members=[
                Member(node_id="nodeA", public_key=keys_a.public_key),
                Member(node_id="nodeB", public_key=keys_b.public_key),
                Member(node_id="nodeC", public_key=keys_c.public_key),
            ],
        )
    )
    await receiver.bump_term(2, updated_by_node_id="nodeA")

    # nodeC signs a protocol event at the CURRENT epoch (1).
    signer_store = HivemindStateStore(storage=FakeStorage(), space_id="alpha")  # type: ignore[arg-type]
    signer = HivemindPeerChannel(
        state=signer_store,
        local_node_id="nodeC",
        private_key=keys_c.private_key,
        clock=lambda: NOW,
    )
    event = EventEnvelope(
        event_id="evt-pre-evict",
        type=EventType.TOKEN_CLAIM,
        origin_node_id="nodeC",
        term=2,
        membership_epoch=1,
        created_at=NOW_ISO,
    )
    message = await signer.sign_event(event, signed_at=NOW_ISO)

    # The operator EVICTS nodeB -> epoch bumps 1 -> 2 (the evict-triggered bump).
    await MembershipService(receiver).evict_member(
        "nodeB", operator="ops", confirm=True
    )
    bumped = await receiver.get_membership()
    assert bumped.epoch == 2, "eviction must bump membership_epoch by exactly 1"
    assert "nodeB" not in {
        m.node_id for m in bumped.members if m.status == "active"
    }, "evicted peer must leave the active set"

    # nodeC's epoch-1 message is now fenced by the evict-triggered epoch bump.
    channel = HivemindPeerChannel(
        state=receiver,
        local_node_id="nodeA",
        private_key=keys_a.private_key,
        clock=lambda: NOW,
    )
    with pytest.raises(PeerChannelError) as exc:
        await channel.receive(message)
    assert exc.value.code == PeerErrorCode.WRONG_MEMBERSHIP_EPOCH


# =============================================================================
# AC4 — fail-closed classification; corruption of read files is never `False`
# =============================================================================


async def test_is_hivemind_space_raises_on_corrupt_node_never_false() -> None:
    """Corruption of ``node.json`` (a file the resolver reads) must propagate
    ``CorruptedStateError`` — NEVER a benign ``False`` that would let a shared
    write bypass the single-writer path."""
    storage = FakeStorage()
    await _seed_hive(storage)
    storage.objects[layout.node_key("alpha")] = "{not valid json"
    with pytest.raises(CorruptedStateError):
        await is_hivemind_space(storage, "alpha")  # type: ignore[arg-type]


async def test_is_hivemind_space_raises_on_corrupt_members_never_false() -> None:
    """Corruption of ``members.json`` must propagate ``CorruptedStateError``,
    never a false ``not shared``."""
    storage = FakeStorage()
    await _seed_hive(storage)
    storage.objects[layout.members_key("alpha")] = "{bad"
    with pytest.raises(CorruptedStateError):
        await is_hivemind_space(storage, "alpha")  # type: ignore[arg-type]


async def test_classification_scope_is_narrowed_term_corruption_is_invisible() -> None:
    """The resolver's scope is deliberately ``node/members/node_status`` only:
    corrupting ``term.json`` does NOT change the hive classification (it is never
    read at classification time), yet a direct store read of ``term.json`` still
    fails closed. This pins both halves of the narrowed-scope contract."""
    storage = FakeStorage()
    store, _ = await _seed_hive(storage)

    # Baseline: the space classifies as Hivemind.
    assert await is_hivemind_space(storage, "alpha") is True  # type: ignore[arg-type]

    # Corrupt term.json — invisible to classification (resolver never reads it).
    storage.objects[layout.term_key("alpha")] = "{not valid json"
    assert await is_hivemind_space(storage, "alpha") is True, (  # type: ignore[arg-type]
        "term.json corruption must not alter the hive classification "
        "(narrowed 3-file scope is deliberate)"
    )

    # …but any store read of the corrupt critical file fails closed at the read
    # layer (the dangerous path — a commit reads term — still cannot proceed).
    with pytest.raises(CorruptedStateError):
        await store.get_term()


async def test_classification_scope_ignores_token_and_bank_version_corruption() -> None:
    """Same narrowed-scope contract for ``token.json`` and ``bank_version.json``:
    invisible to classification, fail-closed on direct read."""
    storage = FakeStorage()
    store, _ = await _seed_hive(storage)

    storage.objects[layout.token_key("alpha")] = "{bad"
    storage.objects[layout.bank_version_key("alpha")] = "{bad"

    assert await is_hivemind_space(storage, "alpha") is True  # type: ignore[arg-type]

    with pytest.raises(CorruptedStateError):
        await store.get_token()
    with pytest.raises(CorruptedStateError):
        await store.get_bank_version_pointer()
