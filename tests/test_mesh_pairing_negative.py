# -*- coding: utf-8 -*-
"""Project Mesh pairing negative / shared-state-safety matrix (P10-3, issue #191).

Proves the acceptance invariants: a wrong secret, an expired invitation, a
populated target, or a non-pairing inbound event never mutates shared membership
and never becomes a success.
"""

from __future__ import annotations

import base64

import pytest

from live_mem.core.hivemind import (
    EventEnvelope,
    EventType,
    HivemindStateStore,
)
from live_mem.mesh.canonical import canonical_dumps
from live_mem.mesh.identity import MESH_PRIVATE_KEY_PREFIX
from live_mem.mesh.pairing_client import MeshPairingClient
from live_mem.mesh.pairing_service import MeshPairingService, MeshPairingServiceError
from live_mem.mesh.router import MeshNamespaceRouter
from live_mem.mesh.secret import generate_request_id
from live_mem.mesh.wire import MeshResponseCode, MeshResponseEnvelope
from tests.test_hivemind_state import FakeStorage
from tests.test_mesh_pairing_e2e import (
    A_URL,
    NOW_MS,
    SPACE,
    AsgiPeerSender,
    _config,
    _fallback,
    _seed_blank_target,
    _seed_source,
)
from tests.test_mesh_router import FakeProcessLock, FakeReplayLedger


async def _instances(a_seed: int, b_seed: int, *, clock=None, seed_target=True):
    a_priv = MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([a_seed]) * 32).decode().rstrip("=")
    b_priv = MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([b_seed]) * 32).decode().rstrip("=")
    a_config = _config(a_priv, A_URL)
    b_config = _config(b_priv, "https://b.mesh.test")
    a_storage, b_storage = FakeStorage(), FakeStorage()
    await _seed_source(a_storage, a_config)
    if seed_target:
        await _seed_blank_target(b_storage)
    clk = clock or (lambda: NOW_MS)
    peers: dict = {}
    a_service = MeshPairingService(a_config, a_storage, clock_ms=lambda: NOW_MS, sender_factory=lambda _e: AsgiPeerSender(peers, "B"))
    b_service = MeshPairingService(b_config, b_storage, clock_ms=clk, sender_factory=lambda _e: AsgiPeerSender(peers, "A"))
    a_router = MeshNamespaceRouter(_fallback(), config=a_config, process_lock=FakeProcessLock(), storage_factory=lambda: a_storage, replay_ledger=FakeReplayLedger(), clock_ms=lambda: NOW_MS, pairing_service=a_service)
    b_router = MeshNamespaceRouter(_fallback(), config=b_config, process_lock=FakeProcessLock(), storage_factory=lambda: b_storage, replay_ledger=FakeReplayLedger(), clock_ms=clk, pairing_service=b_service)
    peers["A"], peers["B"] = a_router, b_router
    return a_service, b_service, a_config, b_config, a_storage, b_storage, peers


async def _a_epoch(a_storage) -> int:
    m = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    return m.epoch


async def test_wrong_secret_is_refused_and_leaves_source_unchanged():
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers = await _instances(41, 42)
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read", "commit"))
    before = await _a_epoch(a_storage)
    with pytest.raises(MeshPairingServiceError):
        await b_service.accept_invitation(
            invite["invitation_bytes"], SPACE,
            secret="totally-wrong-secret", source_endpoint=A_URL,
            requested_scopes=("read", "commit"),
        )
    # Source membership is byte-unchanged: no pending admission on a bad secret.
    after = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert after.epoch == before == 1
    assert all(m.status == "active" for m in after.members)  # no pending member


async def test_replayed_claim_nonce_is_rejected():
    # A second claim reusing the same one-time secret after it is burned is a
    # replay: the source refuses and membership stays unchanged.
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers = await _instances(43, 44)
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read", "commit"))
    await b_service.accept_invitation(
        invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL,
        requested_scopes=("read", "commit"),
    )
    # A second accept of the SAME invitation replays the (now burned) secret.
    b2_storage = FakeStorage()
    await _seed_blank_target(b2_storage)
    b2 = MeshPairingService(b_config, b2_storage, clock_ms=lambda: NOW_MS, sender_factory=lambda _e: AsgiPeerSender(peers, "A"))
    with pytest.raises(MeshPairingServiceError):
        await b2.accept_invitation(
            invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL,
            requested_scopes=("read", "commit"),
        )


async def test_expired_invitation_is_refused():
    late = lambda: NOW_MS + 3_600_001  # 1ms past the 3600s TTL
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers = await _instances(45, 46, clock=late)
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    with pytest.raises(MeshPairingServiceError):
        await b_service.accept_invitation(
            invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL,
            requested_scopes=("read",),
        )
    assert await _a_epoch(a_storage) == 1


async def test_populated_target_is_refused():
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers = await _instances(47, 48, seed_target=False)
    await _seed_blank_target(b_storage)
    await b_storage.put(f"{SPACE}/bank/real.md", "# real content")  # populated
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    with pytest.raises(MeshPairingServiceError):
        await b_service.accept_invitation(
            invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL,
            requested_scopes=("read",),
        )


async def test_non_pairing_event_gets_local_unsafe_not_activation():
    # An inbound event to an instance with NO matching target pairing session must
    # re-emit the byte-identical LOCAL_UNSAFE refusal (the confined activation
    # branch must never turn a non-session event into a success).
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers = await _instances(49, 50)
    req = generate_request_id()
    event = EventEnvelope(
        event_id="strayevent1", request_id=req, type=EventType.MEMBERSHIP_UPDATED,
        origin_node_id="sourcenode0000000000000000000000", term=2, membership_epoch=3,
        payload={"node_id": "x", "epoch": 3, "status": "active", "candidate_view_digest": "e" * 64},
    )
    body = canonical_dumps(event.model_dump(mode="json"))
    client = MeshPairingClient(
        AsgiPeerSender(peers, "B"), source_public_key=a_config.public_key,
        source_fingerprint=a_config.fingerprint, private_key=a_config.private_key, clock_ms=lambda: NOW_MS,
    )
    resp = await client.deliver_event(space_id=SPACE, epoch=3, target_fingerprint=b_config.fingerprint, body=body, request_id=req)
    envelope, signature = MeshResponseEnvelope.from_headers(resp.headers)
    envelope.verify(signature)
    assert envelope.code == MeshResponseCode.LOCAL_UNSAFE
    assert envelope.acknowledged is False  # never a success


async def test_multi_member_source_pairing_is_refused():
    # V1 pairs a two-node mesh from a single-node source; a >1-active-member
    # source is refused fail-closed (its all-ACK would omit the other members).
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers = await _instances(51, 52)
    # Add a second active member to the source space.
    from live_mem.core.hivemind import Member, MembershipService, generate_peer_keypair
    store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    await MembershipService(store).add_member(Member(node_id="second", public_key=generate_peer_keypair().public_key))
    with pytest.raises(MeshPairingServiceError) as e:
        await a_service.create_invitation(SPACE, requested_scopes=("read", "commit"))
    assert e.value.code == "multi_member_source"


async def test_impostor_cannot_fetch_bootstrap_or_status():
    # After approval, a peer with a valid mesh keypair but NOT the enrolled target
    # cannot read the signed snapshot or approval/bootstrap metadata via pair_id.
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers = await _instances(53, 54)
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read", "commit"))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"))
    await a_service.approve(pair_id)  # source now transferring, bootstrap available

    imp_priv = MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([99]) * 32).decode().rstrip("=")
    imp_config = _config(imp_priv, "https://imp.mesh.test")
    impostor = MeshPairingClient(
        AsgiPeerSender(peers, "A"), source_public_key=imp_config.public_key,
        source_fingerprint=imp_config.fingerprint, private_key=imp_config.private_key, clock_ms=lambda: NOW_MS,
    )
    for resp in (
        await impostor.fetch_bootstrap(space_id=SPACE, epoch=1, target_fingerprint=a_config.fingerprint, pair_id=pair_id),
        await impostor.status(space_id=SPACE, epoch=1, target_fingerprint=a_config.fingerprint, pair_id=pair_id),
    ):
        env, sig = MeshResponseEnvelope.from_headers(resp.headers)
        env.verify(sig)
        assert env.code == MeshResponseCode.SOURCE_NOT_AUTHORIZED


async def test_concurrent_membership_change_before_ack_leaves_source_unpromoted():
    # If the source membership advances between admit (e+1) and the target's final
    # ACK, _handle_ack refuses to promote (shared state not half-applied).
    from live_mem.core.hivemind import MembershipService
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers = await _instances(55, 56)
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read", "commit"))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"))
    await a_service.approve(pair_id)  # target admitted PENDING at e+1
    # Concurrent membership mutation bumps the source epoch to e+2.
    a_store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    await MembershipService(a_store).update_member_scopes("sourcenode0000000000000000000000", ["read", "propose"])
    # The target drives to the ACK; the source refuses to promote.
    result = await b_service.run_target_enrollment(pair_id)
    assert result["state"] != "active"
    membership = await a_store.get_membership()
    tgt = b_config.fingerprint.split(":", 1)[1]
    assert any(m.node_id == tgt and m.status == "pending" for m in membership.members)  # not promoted


async def test_cancel_pre_mutation_releases_reservation():
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers = await _instances(57, 58)
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL, requested_scopes=("read",))
    assert await b_service.store.get_reservation(SPACE) == pair_id
    out = await b_service.cancel(pair_id)
    assert out["state"] == "cancelled"
    assert await b_service.store.get_reservation(SPACE) is None
