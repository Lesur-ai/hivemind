# -*- coding: utf-8 -*-
"""Project Mesh pairing negative / shared-state-safety matrix (P10-3, issue #191).

Proves the acceptance invariants: a wrong secret, an expired invitation, a
populated target, or a non-pairing inbound event never mutates shared membership
and never becomes a success.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace

import pytest

from live_mem.core.hivemind import (
    EventEnvelope,
    EventType,
    HivemindStateStore,
    MemberStatus,
    MembershipView,
)
from live_mem.mesh.canonical import canonical_dumps, canonical_loads
from live_mem.mesh.artifacts import (
    MESH_INVITATION_TTL_MILLISECONDS,
    MeshArtifactKind,
    MeshInvitation,
    MeshJoinClaim,
    SignedMeshArtifact,
)
from live_mem.mesh.identity import MESH_PRIVATE_KEY_PREFIX
from live_mem.mesh.pairing_client import MeshPairingClient
from live_mem.mesh.pairing_service import MeshPairingService, MeshPairingServiceError
from live_mem.mesh.pairing_state import MeshPairingState
from live_mem.mesh.router import MeshNamespaceRouter
from live_mem.mesh.secret import generate_pairing_nonce, generate_request_id
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


def _signed_claim_for_invitation(
    invitation_payload: dict,
    target_config,
    *,
    nonce: str,
    membership_epoch: int | None = None,
) -> SignedMeshArtifact:
    """Build the exact target-signed join claim an inbound peer would send."""

    signed_invitation = SignedMeshArtifact.from_bytes(
        invitation_payload["invitation_bytes"]
    )
    invitation = signed_invitation.artifact
    assert type(invitation) is MeshInvitation
    claim = MeshJoinClaim(
        protocol_version=1,
        kind=MeshArtifactKind.JOIN_CLAIM,
        pair_id=invitation.pair_id,
        space_id=invitation.space_id,
        source_public_key=invitation.source_public_key,
        source_fingerprint=invitation.source_fingerprint,
        target_public_key=target_config.public_key,
        target_fingerprint=target_config.fingerprint,
        membership_epoch=(
            invitation.membership_epoch
            if membership_epoch is None
            else membership_epoch
        ),
        issued_at_ms=NOW_MS,
        nonce=nonce,
        invitation_digest=signed_invitation.digest(),
        requested_scopes=("read",),
    )
    return SignedMeshArtifact.sign(claim, target_config.private_key)


def _claim_body(
    signed_claim: SignedMeshArtifact,
    *,
    secret: str,
    target_endpoint: str,
) -> bytes:
    return canonical_dumps(
        {
            "claim": canonical_loads(signed_claim.canonical_bytes()),
            "secret": secret,
            "target_endpoint": target_endpoint,
        }
    )


async def _send_claim_to_source(
    peers: dict,
    source_config,
    target_config,
    *,
    pair_id: str,
    membership_epoch: int,
    body: bytes,
):
    client = MeshPairingClient(
        AsgiPeerSender(peers, "A"),
        source_public_key=target_config.public_key,
        source_fingerprint=target_config.fingerprint,
        private_key=target_config.private_key,
        clock_ms=lambda: NOW_MS,
    )
    return await client.claim(
        space_id=SPACE,
        epoch=membership_epoch,
        target_fingerprint=source_config.fingerprint,
        pair_id=pair_id,
        body=body,
    )


def _verified_pair_response(response) -> MeshResponseEnvelope:
    envelope, signature = MeshResponseEnvelope.from_headers(response.headers)
    envelope.verify(signature)
    return envelope


async def test_wrong_secret_is_refused_and_leaves_source_unchanged():
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers = await _instances(41, 42)
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read", "commit"))
    before = await _a_epoch(a_storage)
    with pytest.raises(MeshPairingServiceError):
        await b_service.accept_invitation(
            invite["invitation_bytes"], SPACE,
            secret="totally-wrong-secret", source_endpoint=A_URL,
            requested_scopes=("read", "commit"), quiesced=True,
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
        requested_scopes=("read", "commit"), quiesced=True,
    )
    # A second accept of the SAME invitation replays the (now burned) secret.
    b2_storage = FakeStorage()
    await _seed_blank_target(b2_storage)
    b2 = MeshPairingService(b_config, b2_storage, clock_ms=lambda: NOW_MS, sender_factory=lambda _e: AsgiPeerSender(peers, "A"))
    with pytest.raises(MeshPairingServiceError):
        await b2.accept_invitation(
            invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL,
            requested_scopes=("read", "commit"), quiesced=True,
        )


async def test_expired_invitation_is_refused():
    late = lambda: NOW_MS + 3_600_001  # 1ms past the 3600s TTL
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers = await _instances(45, 46, clock=late)
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    with pytest.raises(MeshPairingServiceError):
        await b_service.accept_invitation(
            invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL,
            requested_scopes=("read",), quiesced=True,
        )
    assert await _a_epoch(a_storage) == 1


async def test_invalid_source_endpoint_has_zero_target_mutation_and_canonicalizes() -> None:
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers = await _instances(46, 47)
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    before = b_storage.snapshot()
    calls = 0

    class NeverSend:
        async def send(self, method, path, *, headers, body):
            nonlocal calls
            calls += 1
            raise AssertionError("invalid endpoint must not reach a peer sender")

    b_service._sender_factory = lambda _endpoint: NeverSend()  # type: ignore[attr-defined]

    for endpoint in ("not-a-url", "https://127.0.0.1"):
        with pytest.raises(MeshPairingServiceError) as exc:
            await b_service.accept_invitation(
                invite["invitation_bytes"],
                SPACE,
                secret=invite["secret"],
                source_endpoint=endpoint,
                requested_scopes=("read",), quiesced=True,
            )
        assert exc.value.code == "invalid_source_endpoint"
        assert b_storage.snapshot() == before
        assert await b_service.store.get_reservation(SPACE) is None
        assert await b_service.store.get_session(pair_id) is None
        assert calls == 0

    b_service._sender_factory = lambda _endpoint: AsgiPeerSender(peers, "A")  # type: ignore[attr-defined]
    accepted = await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint="https://a.mesh.test:443/",
        requested_scopes=("read",), quiesced=True,
    )
    assert accepted["state"] == "claimed"
    session = await b_service.store.get_session(pair_id)
    assert session is not None and session.source_endpoint == A_URL

    before_session = session
    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.set_target_source_endpoint(pair_id, "https://127.0.0.1")
    assert exc.value.code == "invalid_source_endpoint"
    assert await b_service.store.get_session(pair_id) == before_session

    await b_service.set_target_source_endpoint(pair_id, "https://a.mesh.test:443/")
    assert (await b_service.store.get_session(pair_id)).source_endpoint == A_URL


async def test_invalid_local_endpoint_has_zero_target_mutation() -> None:
    a_service, _b_service, _a_config, b_config, _a_storage, b_storage, peers = (
        await _instances(47, 48)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    before = b_storage.snapshot()
    calls = 0

    class NeverSend:
        async def send(self, method, path, *, headers, body):
            nonlocal calls
            calls += 1
            raise AssertionError("invalid local endpoint must not reach a peer sender")

    # MeshConfig validates this at process configuration time.  Construct the
    # service directly with a deliberately bad retained configuration to prove
    # the Action-2 boundary reports the local fault before durable mutation.
    invalid_local = MeshPairingService(
        replace(b_config, public_url="https://b.mesh.test/not-an-origin"),
        b_storage,
        clock_ms=lambda: NOW_MS,
        sender_factory=lambda _endpoint: NeverSend(),
    )

    with pytest.raises(MeshPairingServiceError) as exc:
        await invalid_local.accept_invitation(
            invite["invitation_bytes"],
            SPACE,
            secret=invite["secret"],
            source_endpoint=A_URL,
            requested_scopes=("read",),
            quiesced=True,
        )
    assert exc.value.code == "invalid_local_endpoint"
    assert "configured public URL" in str(exc.value)
    assert b_storage.snapshot() == before
    assert await invalid_local.store.get_reservation(SPACE) is None
    assert await invalid_local.store.get_session(invite["pair_id"]) is None
    assert calls == 0


async def test_target_endpoint_update_keeps_accept_retry_idempotent() -> None:
    """Transport endpoint repair is mutable; the pair identity fence is not."""

    a_service, b_service, _a_config, _b_config, _a_storage, _b_storage, _peers = (
        await _instances(146, 147)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )
    repaired = "https://alternate-source.mesh.test:443/"
    await b_service.set_target_source_endpoint(pair_id, repaired)

    retried = await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint="https://alternate-source.mesh.test",
        requested_scopes=("read",), quiesced=True,
    )
    assert retried["state"] == "claimed"
    session = await b_service.store.get_session(pair_id)
    assert session is not None
    assert session.source_endpoint == "https://alternate-source.mesh.test"
    intent = await b_service.store.get_target_acceptance_intent(pair_id)
    assert intent is not None and "source_endpoint" not in intent


async def test_accept_retry_canonicalizes_legacy_https_endpoint_fields() -> None:
    a_service, b_service, _a_config, _b_config, _a_storage, _b_storage, _peers = (
        await _instances(148, 149)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )
    session = await b_service.store.get_session(pair_id)
    assert session is not None
    await b_service.store.put_session(
        replace(
            session,
            source_endpoint="https://a.mesh.test:443/",
            target_endpoint="https://b.mesh.test:443/",
        )
    )

    retried = await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )
    assert retried["state"] == "claimed"
    migrated = await b_service.store.get_session(pair_id)
    assert migrated is not None
    assert migrated.source_endpoint == A_URL
    assert migrated.target_endpoint == "https://b.mesh.test"


async def test_accept_retry_does_not_reclassify_legacy_active_history() -> None:
    """A retry never retrofits #417 pre-reservation provenance onto ACTIVE data."""

    a_service, b_service, _a_config, _b_config, _a_storage, _b_storage, _peers = (
        await _instances(150, 151)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )
    session = await b_service.store.get_session(pair_id)
    assert session is not None
    await b_service.store._storage.delete(
        b_service.store._target_acceptance_intent_key(pair_id)
    )
    # A true pre-#417 record has neither of the new direct per-space target
    # fence records.  Remove them too; deleting only the mutable reservation
    # from a #417 tail must now remain fail-closed.
    await b_service.store._storage.delete(
        b_service.store._target_pairing_fence_key(SPACE)
    )
    await b_service.store._storage.delete(
        b_service.store._target_pairing_current_tail_key(SPACE)
    )
    await b_service.store._storage.delete(
        b_service.store._target_pairing_protocol_floor_key(SPACE)
    )
    await b_service.store._storage.delete(
        b_service.store._target_pairing_admission_anchor_key(SPACE)
    )
    await b_service.store.release(SPACE, pair_id)
    # Simulate a terminal pre-#417 target record: it has valid historic
    # invitation/claim artifacts but no independently retained terminal proof.
    legacy_active = session.with_fields(
        now_ms=session.updated_at_ms + 1,
        state="active",
    )
    await b_service.store.put_session(legacy_active)
    assert await b_service.store.get_target_acceptance_intent(pair_id) is None

    retried = await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )

    assert retried["state"] == "active"
    assert await b_service.store.get_target_acceptance_intent(pair_id) is None
    await b_service.assert_space_not_reserved(SPACE)


async def test_source_claim_retry_canonicalizes_legacy_target_endpoint() -> None:
    """A lost response remains retryable across canonical-endpoint upgrade."""

    a_service, b_service, _a_config, _b_config, _a_storage, _b_storage, _peers = (
        await _instances(149, 150)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )
    source_session = await a_service.store.get_session(pair_id)
    assert source_session is not None and source_session.state == "claimed"
    await a_service.store.put_session(
        replace(source_session, target_endpoint="https://b.mesh.test:443/")
    )

    retried = await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )

    assert retried["state"] == "claimed"
    migrated = await a_service.store.get_session(pair_id)
    assert migrated is not None
    assert migrated.target_endpoint == "https://b.mesh.test"


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("invalid_json", MeshResponseCode.INVALID_EVENT),
        ("wrong_shape", MeshResponseCode.INVALID_EVENT),
        ("bad_signature", MeshResponseCode.SOURCE_NOT_AUTHORIZED),
        ("wrong_artifact", MeshResponseCode.INVALID_EVENT),
        ("empty_endpoint", MeshResponseCode.INVALID_EVENT),
        ("unsafe_endpoint", MeshResponseCode.INVALID_EVENT),
    ],
)
async def test_inbound_claim_refusals_leave_source_issued_state_unchanged(
    case: str,
    expected: MeshResponseCode,
) -> None:
    """Malformed peer claims cannot consume an invitation or create a prefix."""

    a_service, _b_service, a_config, b_config, a_storage, _b_storage, peers = (
        await _instances(163, 164)
    )
    invitation = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invitation["pair_id"]
    issued = await a_service.store.get_session(pair_id)
    assert issued is not None and issued.state == "issued"
    signed_claim = _signed_claim_for_invitation(
        invitation,
        b_config,
        nonce="nonce_" + "a" * 64,
    )

    if case == "invalid_json":
        body = b"{"
    elif case == "wrong_shape":
        body = canonical_dumps(
            {
                "claim": canonical_loads(signed_claim.canonical_bytes()),
                "secret": invitation["secret"],
                "target_endpoint": b_config.public_url,
                "unexpected": True,
            }
        )
    elif case == "bad_signature":
        wire_claim = canonical_loads(signed_claim.canonical_bytes())
        assert type(wire_claim) is dict
        wire_claim["signature"] = "A" * 86
        body = canonical_dumps(
            {
                "claim": wire_claim,
                "secret": invitation["secret"],
                "target_endpoint": b_config.public_url,
            }
        )
    elif case == "wrong_artifact":
        body = canonical_dumps(
            {
                "claim": canonical_loads(invitation["invitation_bytes"]),
                "secret": invitation["secret"],
                "target_endpoint": b_config.public_url,
            }
        )
    elif case == "empty_endpoint":
        body = _claim_body(
            signed_claim,
            secret=invitation["secret"],
            target_endpoint="",
        )
    else:
        assert case == "unsafe_endpoint"
        body = _claim_body(
            signed_claim,
            secret=invitation["secret"],
            target_endpoint="https://127.0.0.1",
        )

    before = a_storage.snapshot()
    response = await _send_claim_to_source(
        peers,
        a_config,
        b_config,
        pair_id=pair_id,
        membership_epoch=signed_claim.artifact.membership_epoch,
        body=body,
    )

    envelope = _verified_pair_response(response)
    assert envelope.code is expected
    assert envelope.acknowledged is False
    assert a_storage.snapshot() == before
    assert await a_service.store.get_session(pair_id) == issued
    assert await a_service.store.get_blob(pair_id, "claim") is None
    assert not await a_service.store.is_secret_burned(pair_id)


@pytest.mark.parametrize("interruption", ["after_nonce", "after_claim_blob"])
async def test_source_claim_prefix_crash_retries_only_the_exact_signed_claim(
    interruption: str,
) -> None:
    """The nonce ledger permits only an exact crash-retry to finish Action 2."""

    a_service, b_service, _a_config, _b_config, _a_storage, _b_storage, _peers = (
        await _instances(165, 166)
    )
    invitation = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invitation["pair_id"]
    crashed = False

    if interruption == "after_nonce":
        real_put_blob = a_service.store.put_blob

        async def crash_before_claim_blob(stored_pair_id, name, data):
            nonlocal crashed
            if not crashed and stored_pair_id == pair_id and name == "claim":
                crashed = True
                raise OSError("simulated source crash after nonce record")
            await real_put_blob(stored_pair_id, name, data)

        a_service.store.put_blob = crash_before_claim_blob  # type: ignore[method-assign]
        restore = lambda: setattr(a_service.store, "put_blob", real_put_blob)
    else:
        assert interruption == "after_claim_blob"
        real_burn_secret = a_service.store.burn_secret

        async def crash_before_secret_burn(stored_pair_id, secret_digest, *, now_ms):
            nonlocal crashed
            if not crashed and stored_pair_id == pair_id:
                crashed = True
                raise OSError("simulated source crash after claim blob")
            await real_burn_secret(stored_pair_id, secret_digest, now_ms=now_ms)

        a_service.store.burn_secret = crash_before_secret_burn  # type: ignore[method-assign]
        restore = lambda: setattr(a_service.store, "burn_secret", real_burn_secret)

    try:
        with pytest.raises(MeshPairingServiceError) as caught:
            await b_service.accept_invitation(
                invitation["invitation_bytes"],
                SPACE,
                secret=invitation["secret"],
                source_endpoint=A_URL,
                requested_scopes=("read",), quiesced=True,
            )
    finally:
        restore()

    assert caught.value.code == "claim_rejected"
    issued = await a_service.store.get_session(pair_id)
    assert issued is not None and issued.state == "issued"
    assert not await a_service.store.is_secret_burned(pair_id)
    if interruption == "after_nonce":
        assert await a_service.store.get_blob(pair_id, "claim") is None
    else:
        assert await a_service.store.get_blob(pair_id, "claim") is not None

    resumed = await b_service.accept_invitation(
        invitation["invitation_bytes"],
        SPACE,
        secret=invitation["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )

    assert resumed["state"] == "claimed"
    recovered = await a_service.store.get_session(pair_id)
    assert recovered is not None and recovered.state == "claimed"
    assert await a_service.store.is_secret_burned(pair_id)
    assert await a_service.store.get_blob(pair_id, "claim") == await b_service.store.get_blob(
        pair_id, "claim"
    )


async def test_nonce_collision_between_pair_ids_cannot_poison_the_second_source_session() -> None:
    """A nonce is globally bound to one pair and its exact signed claim bytes."""

    a_service, _b_service, a_config, b_config, a_storage, _b_storage, peers = (
        await _instances(167, 168)
    )
    first = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    second = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    shared_nonce = "nonce_" + "b" * 64
    first_claim = _signed_claim_for_invitation(
        first,
        b_config,
        nonce=shared_nonce,
    )
    first_response = await _send_claim_to_source(
        peers,
        a_config,
        b_config,
        pair_id=first["pair_id"],
        membership_epoch=first_claim.artifact.membership_epoch,
        body=_claim_body(
            first_claim,
            secret=first["secret"],
            target_endpoint=b_config.public_url,
        ),
    )
    assert _verified_pair_response(first_response).code is MeshResponseCode.OK

    second_claim = _signed_claim_for_invitation(
        second,
        b_config,
        nonce=shared_nonce,
    )
    second_issued = await a_service.store.get_session(second["pair_id"])
    assert second_issued is not None and second_issued.state == "issued"
    before_second = a_storage.snapshot()
    second_response = await _send_claim_to_source(
        peers,
        a_config,
        b_config,
        pair_id=second["pair_id"],
        membership_epoch=second_claim.artifact.membership_epoch,
        body=_claim_body(
            second_claim,
            secret=second["secret"],
            target_endpoint=b_config.public_url,
        ),
    )

    envelope = _verified_pair_response(second_response)
    assert envelope.code is MeshResponseCode.REPLAY_REJECTED
    assert envelope.acknowledged is False
    assert a_storage.snapshot() == before_second
    assert await a_service.store.get_session(second["pair_id"]) == second_issued
    assert await a_service.store.get_blob(second["pair_id"], "claim") is None
    assert not await a_service.store.is_secret_burned(second["pair_id"])


@pytest.mark.parametrize("damage", ["missing_claim", "corrupt_claim", "unsafe_endpoint"])
async def test_claimed_source_recovery_refuses_corrupt_persisted_prefix(
    damage: str,
) -> None:
    """A claimed source re-acks only its exact durable claim and safe endpoint."""

    a_service, b_service, _a_config, _b_config, a_storage, _b_storage, _peers = (
        await _instances(169, 170)
    )
    invitation = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invitation["pair_id"]
    await b_service.accept_invitation(
        invitation["invitation_bytes"],
        SPACE,
        secret=invitation["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )
    source_session = await a_service.store.get_session(pair_id)
    assert source_session is not None and source_session.state == "claimed"
    if damage == "missing_claim":
        await a_storage.delete(a_service.store._blob_key(pair_id, "claim"))
        expected_session = source_session
    elif damage == "corrupt_claim":
        await a_storage.put(
            a_service.store._blob_key(pair_id, "claim"),
            base64.urlsafe_b64encode(b"tampered-source-claim").decode("ascii"),
        )
        expected_session = source_session
    else:
        assert damage == "unsafe_endpoint"
        expected_session = replace(
            source_session,
            target_endpoint="https://127.0.0.1",
        )
        await a_service.store.put_session(expected_session)

    before = a_storage.snapshot()
    with pytest.raises(MeshPairingServiceError) as caught:
        await b_service.accept_invitation(
            invitation["invitation_bytes"],
            SPACE,
            secret=invitation["secret"],
            source_endpoint=A_URL,
            requested_scopes=("read",), quiesced=True,
        )

    assert caught.value.code == "claim_rejected"
    assert a_storage.snapshot() == before
    assert await a_service.store.get_session(pair_id) == expected_session


async def test_legacy_bare_reservation_refuses_cross_space_reuse_and_is_recoverable() -> None:
    """A pre-intent reserve-only prefix cannot be rebound from caller input."""

    a_service, b_service, _a_config, _b_config, _a_storage, b_storage, _peers = (
        await _instances(150, 151)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    legacy_space = "legacy-target"
    for suffix, payload in (
        ("_meta.json", '{"space_id":"legacy-target","version":1}'),
        ("_rules.md", ""),
        ("live/.keep", ""),
        ("bank/.keep", ""),
    ):
        await b_storage.put(f"{legacy_space}/{suffix}", payload)
    await b_service.store.reserve(legacy_space, pair_id, now_ms=NOW_MS)
    before = b_storage.snapshot()

    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.accept_invitation(
            invite["invitation_bytes"],
            SPACE,
            secret=invite["secret"],
            source_endpoint=A_URL,
            requested_scopes=("read",), quiesced=True,
        )
    assert exc.value.code == "pair_conflict"
    assert b_storage.snapshot() == before
    assert await b_service.store.get_target_acceptance_intent(pair_id) is None
    assert await b_service.store.get_session(pair_id) is None
    assert await b_service.store.get_reservation(legacy_space) == pair_id

    recovered = await b_service.recover_orphaned_target_reservation(
        pair_id, space_id=legacy_space, operator="op"
    )
    assert recovered["state"] == "orphaned_reservation_released"
    assert await b_service.store.get_reservation(legacy_space) is None


async def test_tampered_acceptance_intent_cannot_add_a_second_target_reservation() -> None:
    """A rewritten local intent cannot redirect one pair id from X to Y."""

    a_service, b_service, _a_config, b_config, _a_storage, b_storage, _peers = (
        await _instances(150, 152)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    signed_invitation = SignedMeshArtifact.from_bytes(invite["invitation_bytes"])
    invitation = signed_invitation.artifact
    pair_id = invite["pair_id"]
    # Model the dangerous valid-schema rewrite I(X) -> I(Y): target storage
    # claims this invitation is bound to SPACE while the durable reservation is
    # still held by a different target.  There is no session/artifact yet, so
    # accept must reject before blank-check/reserve/blob/session or peer I/O.
    await b_service.store.put_target_acceptance_intent(
        pair_id,
        {
            "pair_id": pair_id,
            "space_id": SPACE,
            "invitation_digest": signed_invitation.digest(),
            "source_fingerprint": invitation.source_fingerprint,
            "target_fingerprint": b_config.fingerprint,
            "requested_scopes": ["read"],
        },
    )
    foreign_space = "other-target"
    await b_service.store.reserve(foreign_space, pair_id, now_ms=NOW_MS)
    before = b_storage.snapshot()

    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.accept_invitation(
            invite["invitation_bytes"],
            SPACE,
            secret=invite["secret"],
            source_endpoint=A_URL,
            requested_scopes=("read",), quiesced=True,
        )

    assert exc.value.code == "pair_conflict"
    assert b_storage.snapshot() == before
    assert await b_service.store.get_reservation(foreign_space) == pair_id
    assert await b_service.store.get_reservation(SPACE) is None
    assert await b_service.store.get_session(pair_id) is None
    assert await b_service.store.get_blob(pair_id, "invitation") is None
    assert await b_service.store.get_blob(pair_id, "claim") is None


async def test_orphaned_intent_reservation_after_expiry_is_operator_recoverable() -> None:
    """A crash between intent/reserve and artifacts cannot fence a space forever."""

    clock = {"now": NOW_MS}
    a_service, b_service, _a_config, _b_config, _a_storage, _b_storage, _peers = (
        await _instances(151, 152, clock=lambda: clock["now"])
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    real_put_blob = b_service.store.put_blob

    async def crash_before_first_artifact(stored_pair_id, name, data):
        if stored_pair_id == pair_id and name == "invitation":
            raise OSError("simulated crash after target reservation")
        await real_put_blob(stored_pair_id, name, data)

    b_service.store.put_blob = crash_before_first_artifact  # type: ignore[method-assign]
    with pytest.raises(Exception):
        await b_service.accept_invitation(
            invite["invitation_bytes"],
            SPACE,
            secret=invite["secret"],
            source_endpoint=A_URL,
            requested_scopes=("read",), quiesced=True,
        )
    b_service.store.put_blob = real_put_blob  # type: ignore[method-assign]
    assert await b_service.store.get_reservation(SPACE) == pair_id
    assert await b_service.store.get_target_acceptance_intent(pair_id) is not None
    assert await b_service.store.get_session(pair_id) is None

    clock["now"] = NOW_MS + MESH_INVITATION_TTL_MILLISECONDS + 1
    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.accept_invitation(
            invite["invitation_bytes"],
            SPACE,
            secret=invite["secret"],
            source_endpoint=A_URL,
            requested_scopes=("read",), quiesced=True,
        )
    assert exc.value.code == "expired"

    recovered = await b_service.recover_orphaned_target_reservation(
        pair_id, space_id=SPACE, operator="op"
    )
    assert recovered["state"] == "orphaned_reservation_released"
    assert await b_service.store.get_reservation(SPACE) is None


async def test_orphan_recovery_refuses_a_lost_target_session_after_source_admission() -> None:
    """A missing local workflow record cannot prove a claim was never delivered.

    The source has already admitted this target at e+1.  Losing only the target
    session must therefore preserve the held target fence: the explicit orphan
    tool is for prefixes that are provably pre-claim, not a cross-peer rollback
    primitive.
    """

    from live_mem.mesh.pairing_store import MeshPairingStoreError

    a_service, b_service, _a_config, _b_config, _a_storage, b_storage, _peers = (
        await _instances(152, 153)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",),
        quiesced=True,
    )
    await a_service.approve(pair_id)
    assert (await a_service.store.get_session(pair_id)).state == "transferring"
    assert await b_service.store.get_reservation(SPACE) == pair_id

    # Model a single critical-record loss after the real outbound claim: the
    # target is still blank locally, but the source now owns PENDING admission.
    await b_storage.delete(b_service.store._session_key(pair_id))
    assert await b_service.store.get_session(pair_id) is None

    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.recover_orphaned_target_reservation(
            pair_id, space_id=SPACE, operator="op"
        )
    assert exc.value.code == "not_orphaned"
    assert await b_service.store.get_reservation(SPACE) == pair_id
    with pytest.raises(MeshPairingStoreError) as guard:
        await b_service.assert_space_not_reserved(SPACE)
    assert guard.value.code == "space_reserved"


async def test_floor_only_target_acceptance_prefix_requires_blank_proof_to_recover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after the permanent floor cannot strand a blank target forever.

    The target writes its protocol floor before the mutable direct fence and
    raw reservation.  A power loss in that exact interval must still fence
    ordinary writes, then let an operator convert the verified blank prefix to
    the signed ``released`` fence.  A newly populated target remains a hard
    refusal rather than being silently freed.
    """

    from live_mem.mesh.pairing_store import MeshPairingStoreError

    a_service, b_service, _a_config, _b_config, _a_storage, b_storage, _peers = (
        await _instances(157, 158)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    real_put_fence = b_service.store.put_target_pairing_fence

    async def crash_before_first_target_fence(*_args, **_kwargs):
        raise OSError("simulated crash after target protocol floor")

    monkeypatch.setattr(
        b_service.store, "put_target_pairing_fence", crash_before_first_target_fence
    )
    with pytest.raises(OSError):
        await b_service.accept_invitation(
            invite["invitation_bytes"],
            SPACE,
            secret=invite["secret"],
            source_endpoint=A_URL,
            requested_scopes=("read",), quiesced=True,
        )
    monkeypatch.setattr(b_service.store, "put_target_pairing_fence", real_put_fence)

    floor = await b_service.store.get_target_pairing_protocol_floor(SPACE)
    assert floor is not None and floor.authority.pair_id == pair_id
    assert await b_service.store.get_target_pairing_fence(SPACE) is None
    assert await b_service.store.get_reservation(SPACE) is None
    assert await b_service.store.get_session(pair_id) is None
    with pytest.raises(MeshPairingStoreError) as exc:
        await b_service.assert_space_not_reserved(SPACE)
    assert exc.value.code == "space_reserved"

    # Recovery has no permission to free a target that gained local data after
    # the interrupted acceptance prefix.  The direct floor stays fail-closed.
    nonblank_key = f"{SPACE}/bank/operator-content.md"
    await b_storage.put(nonblank_key, "must preserve")
    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.recover_orphaned_target_reservation(
            pair_id, space_id=SPACE, operator="op"
        )
    assert exc.value.code == "populated_target"
    assert await b_service.store.get_target_pairing_fence(SPACE) is None
    assert await b_service.store.get_reservation(SPACE) is None

    await b_storage.delete(nonblank_key)
    recovered = await b_service.recover_orphaned_target_reservation(
        pair_id, space_id=SPACE, operator="op"
    )
    assert recovered["state"] == "orphaned_reservation_released"
    released = await b_service.store.get_target_pairing_fence(SPACE)
    assert released is not None
    assert released.authority.pair_id == pair_id
    assert released.authority.phase == "released"
    assert await b_service.store.get_reservation(SPACE) is None
    await b_service.assert_space_not_reserved(SPACE)


async def test_orphan_recovery_repairs_released_fence_before_current_tail_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact orphan retry completes fence->current-tail release ordering.

    The operational fence is written before its independent current-tail copy.
    A crash in that narrow interval must not try to re-arm the same released
    owner as ``held`` (which the monotonic store correctly rejects).  The
    operator retry instead reconciles the exact released evidence and only
    then releases the raw reservation.
    """

    a_service, b_service, _a_config, _b_config, _a_storage, _b_storage, _peers = (
        await _instances(159, 160)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    real_put_blob = b_service.store.put_blob

    async def crash_before_first_artifact(stored_pair_id, name, data):
        if stored_pair_id == pair_id and name == "invitation":
            raise OSError("simulated acceptance-prefix crash")
        await real_put_blob(stored_pair_id, name, data)

    monkeypatch.setattr(b_service.store, "put_blob", crash_before_first_artifact)
    with pytest.raises(OSError):
        await b_service.accept_invitation(
            invite["invitation_bytes"],
            SPACE,
            secret=invite["secret"],
            source_endpoint=A_URL,
            requested_scopes=("read",), quiesced=True,
        )
    monkeypatch.setattr(b_service.store, "put_blob", real_put_blob)

    real_put_current = b_service.store.put_target_pairing_current_tail
    fail_once = {"value": True}

    async def crash_after_released_fence(signed, **kwargs):
        if fail_once["value"] and signed.authority.phase == "released":
            fail_once["value"] = False
            raise OSError("simulated released-fence tail crash")
        await real_put_current(signed, **kwargs)

    monkeypatch.setattr(
        b_service.store, "put_target_pairing_current_tail", crash_after_released_fence
    )
    with pytest.raises(OSError):
        await b_service.recover_orphaned_target_reservation(
            pair_id, space_id=SPACE, operator="op"
        )
    monkeypatch.setattr(
        b_service.store, "put_target_pairing_current_tail", real_put_current
    )

    fence = await b_service.store.get_target_pairing_fence(SPACE)
    current = await b_service.store.get_target_pairing_current_tail(SPACE)
    assert fence is not None and fence.authority.phase == "released"
    assert current is not None and current.authority.phase == "held"
    assert await b_service.store.get_reservation(SPACE) == pair_id

    recovered = await b_service.recover_orphaned_target_reservation(
        pair_id, space_id=SPACE, operator="op"
    )
    assert recovered["state"] == "orphaned_reservation_released"
    current = await b_service.store.get_target_pairing_current_tail(SPACE)
    assert current == await b_service.store.get_target_pairing_fence(SPACE)
    assert current is not None and current.authority.phase == "released"
    assert await b_service.store.get_reservation(SPACE) is None
    await b_service.assert_space_not_reserved(SPACE)


async def test_cancel_does_not_downgrade_corrupt_intent_after_triple_direct_loss() -> None:
    """A known #417 target cannot become legacy while cancelling its own tail."""

    from live_mem.mesh.pairing_store import MeshPairingStoreError

    a_service, b_service, _a_config, _b_config, _a_storage, b_storage, _peers = (
        await _instances(161, 162)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )

    # Simulate loss of the three direct records and a valid-schema rewrite of
    # the retained immutable intent.  Only a truly absent intent may select
    # legacy cleanup; an unreadable or mismatched one must leave the raw
    # reservation in place.
    for key in (
        b_service.store._target_pairing_protocol_floor_key(SPACE),
        b_service.store._target_pairing_current_tail_key(SPACE),
        b_service.store._target_pairing_fence_key(SPACE),
    ):
        await b_storage.delete(key)
    intent = await b_service.store.get_target_acceptance_intent(pair_id)
    assert intent is not None
    tampered = dict(intent)
    tampered["invitation_digest"] = "0" * 64
    await b_storage.put(
        b_service.store._target_acceptance_intent_key(pair_id),
        canonical_dumps(tampered).decode("utf-8"),
    )

    # Give the target the only legitimate pre-T1 release authorization.  The
    # corrupted local #417 provenance must still prevent it from downgrading to
    # a legacy release path.
    await a_service.cancel(pair_id)
    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.cancel(pair_id)
    assert exc.value.code == "target_fence_invalid"
    assert await b_service.store.get_reservation(SPACE) == pair_id
    with pytest.raises(MeshPairingStoreError) as guard:
        await b_service.assert_space_not_reserved(SPACE)
    assert guard.value.code == "space_reserved"


@pytest.mark.parametrize(
    ("last_artifact", "recoverable"),
    [("invitation", True), ("claim", False)],
)
async def test_orphan_recovery_allows_only_a_provably_preclaim_artifact_prefix(
    last_artifact: str, recoverable: bool
) -> None:
    """A retained claim could outlive a lost target session after send."""

    clock = {"now": NOW_MS}
    a_service, b_service, _a_config, _b_config, _a_storage, _b_storage, _peers = (
        await _instances(154, 155, clock=lambda: clock["now"])
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    real_put_blob = b_service.store.put_blob

    async def crash_after_artifact(stored_pair_id, name, data):
        await real_put_blob(stored_pair_id, name, data)
        if stored_pair_id == pair_id and name == last_artifact:
            raise OSError("simulated target artifact-prefix crash")

    b_service.store.put_blob = crash_after_artifact  # type: ignore[method-assign]
    with pytest.raises(Exception):
        await b_service.accept_invitation(
            invite["invitation_bytes"],
            SPACE,
            secret=invite["secret"],
            source_endpoint=A_URL,
            requested_scopes=("read",), quiesced=True,
        )
    b_service.store.put_blob = real_put_blob  # type: ignore[method-assign]
    assert await b_service.store.get_session(pair_id) is None
    assert await b_service.store.get_reservation(SPACE) == pair_id
    assert await b_service.store.get_blob(pair_id, last_artifact) is not None

    clock["now"] = NOW_MS + MESH_INVITATION_TTL_MILLISECONDS + 1
    if recoverable:
        recovered = await b_service.recover_orphaned_target_reservation(
            pair_id, space_id=SPACE, operator="op"
        )
        assert recovered["state"] == "orphaned_reservation_released"
        assert await b_service.store.get_reservation(SPACE) is None
    else:
        with pytest.raises(MeshPairingServiceError) as exc:
            await b_service.recover_orphaned_target_reservation(
                pair_id, space_id=SPACE, operator="op"
            )
        assert exc.value.code == "not_orphaned"
        assert await b_service.store.get_reservation(SPACE) == pair_id
    assert await b_service.store.get_session(pair_id) is None


@pytest.mark.parametrize("last_artifact", ["invitation", "claim"])
async def test_unexpired_partial_target_acceptance_retries_exactly(
    last_artifact: str,
) -> None:
    """An interrupted local Action 2 prefix resumes the exact signed claim.

    No peer request happens before the target session write.  Therefore an
    unexpired retry may complete the exact invitation/claim prefix, but must not
    manufacture a different pair binding or silently discard its reservation.
    """

    a_service, b_service, _a_config, _b_config, _a_storage, _b_storage, _peers = (
        await _instances(155, 156)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    real_put_blob = b_service.store.put_blob
    crashed = False

    async def crash_after_artifact(stored_pair_id, name, data):
        nonlocal crashed
        await real_put_blob(stored_pair_id, name, data)
        if (
            not crashed
            and stored_pair_id == pair_id
            and name == last_artifact
        ):
            crashed = True
            raise OSError("simulated target artifact-prefix crash")

    b_service.store.put_blob = crash_after_artifact  # type: ignore[method-assign]
    with pytest.raises(OSError):
        await b_service.accept_invitation(
            invite["invitation_bytes"],
            SPACE,
            secret=invite["secret"],
            source_endpoint=A_URL,
            requested_scopes=("read",), quiesced=True,
        )
    b_service.store.put_blob = real_put_blob  # type: ignore[method-assign]

    assert await b_service.store.get_reservation(SPACE) == pair_id
    assert await b_service.store.get_session(pair_id) is None
    assert await b_service.store.get_blob(pair_id, "invitation") is not None
    if last_artifact == "claim":
        assert await b_service.store.get_blob(pair_id, "claim") is not None

    resumed = await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )
    assert resumed["state"] == "claimed"
    session = await b_service.store.get_session(pair_id)
    assert session is not None and session.state == "claimed"
    assert await b_service.store.get_reservation(SPACE) == pair_id


@pytest.mark.parametrize(
    "prefix",
    ["claim_without_invitation", "invalid_invitation", "other_invitation"],
)
async def test_partial_target_acceptance_rejects_unbound_or_conflicting_artifacts(
    prefix: str,
) -> None:
    """A partial Action 2 prefix is resumable only from its exact signed bytes."""

    a_service, b_service, _a_config, _b_config, _a_storage, b_storage, _peers = (
        await _instances(157, 158)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    if prefix == "claim_without_invitation":
        await b_service.store.put_blob(pair_id, "claim", b"not-a-claim")
    elif prefix == "invalid_invitation":
        await b_service.store.put_blob(pair_id, "invitation", b"not-an-invitation")
    else:
        other = await a_service.create_invitation(SPACE, requested_scopes=("read",))
        await b_service.store.put_blob(
            pair_id, "invitation", other["invitation_bytes"]
        )

    before = b_storage.snapshot()
    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.accept_invitation(
            invite["invitation_bytes"],
            SPACE,
            secret=invite["secret"],
            source_endpoint=A_URL,
            requested_scopes=("read",), quiesced=True,
        )

    assert exc.value.code == "pair_conflict"
    assert b_storage.snapshot() == before
    assert await b_service.store.get_session(pair_id) is None
    assert await b_service.store.get_reservation(SPACE) is None


@pytest.mark.parametrize("damage", ["missing_claim", "lost_reservation"])
async def test_accept_retry_refuses_incomplete_or_unfenced_target_state(
    damage: str,
) -> None:
    """A retry must not repair a session whose immutable artifacts or fence are lost."""

    a_service, b_service, _a_config, _b_config, _a_storage, b_storage, _peers = (
        await _instances(159, 160)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )
    if damage == "missing_claim":
        await b_storage.delete(b_service.store._blob_key(pair_id, "claim"))
    else:
        await b_service.store.release(SPACE, pair_id)

    before = b_storage.snapshot()
    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.accept_invitation(
            invite["invitation_bytes"],
            SPACE,
            secret=invite["secret"],
            source_endpoint=A_URL,
            requested_scopes=("read",), quiesced=True,
        )

    assert exc.value.code == "pair_conflict"
    assert b_storage.snapshot() == before


async def test_accept_retry_rejects_unsafe_persisted_source_endpoint() -> None:
    """Legacy raw endpoint data is a conflict, never a transport escape hatch."""

    a_service, b_service, _a_config, _b_config, _a_storage, b_storage, _peers = (
        await _instances(161, 162)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )
    session = await b_service.store.get_session(pair_id)
    assert session is not None
    await b_service.store.put_session(
        replace(session, source_endpoint="https://127.0.0.1")
    )

    before = b_storage.snapshot()
    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.accept_invitation(
            invite["invitation_bytes"],
            SPACE,
            secret=invite["secret"],
            source_endpoint=A_URL,
            requested_scopes=("read",), quiesced=True,
        )

    assert exc.value.code == "pair_conflict"
    assert b_storage.snapshot() == before


async def test_orphan_recovery_refuses_ambiguous_legacy_collision_bare_space() -> None:
    """A legacy pair-id collision cannot prove that its bare target was never sent."""

    a_service, b_service, _a_config, _b_config, _a_storage, b_storage, _peers = (
        await _instances(152, 153)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )
    surviving_space = "legacy-bound"
    bare_space = "legacy-bare"
    for space_id in (surviving_space, bare_space):
        for suffix, payload in (
            ("_meta.json", json.dumps({"space_id": space_id, "version": 1})),
            ("_rules.md", ""),
            ("live/.keep", ""),
            ("bank/.keep", ""),
        ):
            await b_storage.put(f"{space_id}/{suffix}", payload)
    session = await b_service.store.get_session(pair_id)
    assert session is not None
    await b_service.store.put_session(replace(session, space_id=surviving_space))
    await b_service.store.release(SPACE, pair_id)
    await b_service.store.reserve(surviving_space, pair_id, now_ms=NOW_MS)
    await b_service.store.reserve(bare_space, pair_id, now_ms=NOW_MS)
    assert await b_service.store.find_reservations_by_pair_id(pair_id) == (
        bare_space,
        surviving_space,
    )

    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.recover_orphaned_target_reservation(
            pair_id, space_id=bare_space, operator="op"
        )

    assert exc.value.code == "not_orphaned"
    assert await b_service.store.get_reservation(bare_space) == pair_id
    assert await b_service.store.get_reservation(surviving_space) == pair_id
    retained = await b_service.store.get_session(pair_id)
    assert retained is not None and retained.space_id == surviving_space


async def test_orphan_recovery_refuses_source_session_legacy_collision() -> None:
    """A legacy SOURCE record can also have overwritten an already-sent target."""

    a_service, _b_service, _a_config, _b_config, a_storage, _b_storage, _peers = (
        await _instances(153, 154)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    bare_space = "legacy-source-collision"
    for suffix, payload in (
        ("_meta.json", json.dumps({"space_id": bare_space, "version": 1})),
        ("_rules.md", ""),
        ("live/.keep", ""),
        ("bank/.keep", ""),
    ):
        await a_storage.put(f"{bare_space}/{suffix}", payload)
    await a_service.store.reserve(bare_space, pair_id, now_ms=NOW_MS)
    source_before = await a_service.store.get_session(pair_id)
    assert source_before is not None and source_before.role == "source"

    with pytest.raises(MeshPairingServiceError) as exc:
        await a_service.recover_orphaned_target_reservation(
            pair_id, space_id=bare_space, operator="op"
        )

    assert exc.value.code == "not_orphaned"
    assert await a_service.store.get_reservation(bare_space) == pair_id
    assert await a_service.store.get_session(pair_id) == source_before


async def test_populated_target_is_refused():
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers = await _instances(47, 48, seed_target=False)
    await _seed_blank_target(b_storage)
    await b_storage.put(f"{SPACE}/bank/real.md", "# real content")  # populated
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    with pytest.raises(MeshPairingServiceError):
        await b_service.accept_invitation(
            invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL,
            requested_scopes=("read",), quiesced=True,
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
    await b_service.accept_invitation(invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"), quiesced=True)
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
    await b_service.accept_invitation(invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"), quiesced=True)
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
    await b_service.accept_invitation(invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL, requested_scopes=("read",), quiesced=True)
    assert await b_service.store.get_reservation(SPACE) == pair_id
    # The source records a signed pre-T1 disposition first.  The target never
    # releases its fence based on its own mutable CLAIMED session alone.
    await a_service.cancel(pair_id)
    out = await b_service.cancel(pair_id)
    assert out["state"] == "cancelled"
    assert await b_service.store.get_reservation(SPACE) is None


async def test_cancel_issued_source_does_not_require_unbound_target_identity():
    """An unclaimed invitation has no target fingerprint to inspect yet."""

    a_service, _b_service, _a_config, _b_config, _a_storage, _b_storage, _peers = (
        await _instances(183, 184)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))

    out = await a_service.cancel(invite["pair_id"])
    assert out["state"] == "cancelled"
    assert await a_service.store.get_source_terminal_disposition(invite["pair_id"]) is None
    barrier = await a_service.store.get_source_preclaim_cancel_barrier(
        invite["pair_id"]
    )
    assert barrier is not None
    barrier.verify(a_service._config.public_key)
    # The durable barrier is an abort monotone even if parseable operational
    # state is rewritten back into an approval-looking state after a crash.
    source = await a_service.store.get_session(invite["pair_id"])
    assert source is not None
    await a_service.store.put_session(
        source.with_fields(now_ms=NOW_MS + 1, state=MeshPairingState.CLAIMED.value)
    )
    with pytest.raises(MeshPairingServiceError) as exc:
        await a_service.approve(invite["pair_id"])
    assert exc.value.code == "not_cancellable"


@pytest.mark.parametrize(
    "replayed_state",
    (
        MeshPairingState.ISSUED.value,
        MeshPairingState.CLAIMED.value,
        MeshPairingState.APPROVED.value,
    ),
)
async def test_preclaim_cancel_barrier_dominates_replayed_source_claim_state(
    replayed_state: str,
) -> None:
    """A durable ISSUED abort cannot be turned back into a normal claim.

    The pairing session is intentionally mutable operational state.  Once the
    signed pre-claim barrier exists, a replayed ISSUED/CLAIMED value must take
    the cleanup-only late-claim route, not revive admission or strand B's
    reservation behind ``source_still_enrolling``.
    """

    a_service, b_service, _a_config, _b_config, _a_storage, _b_storage, _peers = (
        await _instances(193, 194)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await a_service.cancel(pair_id)

    cancelled = await a_service.store.get_session(pair_id)
    assert cancelled is not None
    await a_service.store.put_session(
        cancelled.with_fields(now_ms=NOW_MS + 1, state=replayed_state)
    )

    accepted = await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",),
        quiesced=True,
    )
    assert accepted["state"] == MeshPairingState.CLAIMED.value
    source = await a_service.store.get_session(pair_id)
    disposition = await a_service.store.get_source_terminal_disposition(pair_id)
    assert source is not None and source.state == MeshPairingState.CANCELLED.value
    assert disposition is not None
    assert disposition.receipt.disposition == "pre_t1_cancel"
    assert (await b_service.cancel(pair_id))["state"] == MeshPairingState.CANCELLED.value
    assert await b_service.store.get_reservation(SPACE) is None


async def test_preclaim_cancel_barrier_normalizes_replayed_cancelled_fields() -> None:
    """A bare CANCELLED state cannot carry mutable admission residue forward."""

    a_service, b_service, _a_config, _b_config, _a_storage, _b_storage, _peers = (
        await _instances(197, 198)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await a_service.cancel(pair_id)

    cancelled = await a_service.store.get_session(pair_id)
    assert cancelled is not None
    await a_service.store.put_session(
        cancelled.with_fields(
            now_ms=NOW_MS + 1,
            approval_digest="a" * 64,
            bootstrap_manifest_digest="b" * 64,
            bootstrap_bank_version=7,
        )
    )

    accepted = await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",),
        quiesced=True,
    )
    assert accepted["state"] == MeshPairingState.CLAIMED.value
    source = await a_service.store.get_session(pair_id)
    assert source is not None
    assert source.state == MeshPairingState.CANCELLED.value
    assert not source.approval_digest
    assert not source.bootstrap_manifest_digest
    assert source.bootstrap_bank_version == -1
    assert await a_service.store.get_source_terminal_disposition(pair_id) is not None
    assert (await b_service.cancel(pair_id))["state"] == MeshPairingState.CANCELLED.value
    assert await b_service.store.get_reservation(SPACE) is None


async def test_late_claim_recovers_barrier_before_source_cancelled_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented barrier -> session-write crash prefix remains retryable."""

    a_service, b_service, _a_config, _b_config, _a_storage, _b_storage, _peers = (
        await _instances(195, 196)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    real_put = a_service.store.put_session
    failed = {"value": False}

    async def crash_cancelled_once(session):
        if (
            session.pair_id == pair_id
            and session.state == MeshPairingState.CANCELLED.value
            and not failed["value"]
        ):
            failed["value"] = True
            raise OSError("simulated crash after pre-claim barrier")
        await real_put(session)

    monkeypatch.setattr(a_service.store, "put_session", crash_cancelled_once)
    with pytest.raises(OSError, match="pre-claim barrier"):
        await a_service.cancel(pair_id)
    source_prefix = await a_service.store.get_session(pair_id)
    assert source_prefix is not None
    assert source_prefix.state == MeshPairingState.ISSUED.value
    assert await a_service.store.get_source_preclaim_cancel_barrier(pair_id) is not None

    monkeypatch.setattr(a_service.store, "put_session", real_put)
    # The crash prefix is operationally indistinguishable from a parseable
    # ISSUED/CLAIMED replay until the signed barrier is consulted.  A retry of
    # cancel must normalize it safely instead of deriving an empty target
    # fingerprint or leaving the target to discover the suffix by itself.
    await a_service.store.put_session(
        source_prefix.with_fields(
            now_ms=NOW_MS + 1, state=MeshPairingState.CLAIMED.value
        )
    )
    assert (await a_service.cancel(pair_id))["state"] == MeshPairingState.CANCELLED.value
    normalized = await a_service.store.get_session(pair_id)
    assert normalized is not None and normalized.state == MeshPairingState.CANCELLED.value

    accepted = await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",),
        quiesced=True,
    )
    assert accepted["state"] == MeshPairingState.CLAIMED.value
    source = await a_service.store.get_session(pair_id)
    assert source is not None and source.state == MeshPairingState.CANCELLED.value
    assert await a_service.store.get_source_terminal_disposition(pair_id) is not None
    assert (await b_service.cancel(pair_id))["state"] == MeshPairingState.CANCELLED.value
    assert await b_service.store.get_reservation(SPACE) is None


async def test_late_claim_after_issued_cancel_materializes_release_disposition() -> None:
    """A claim already in transport cannot strand a held blank target.

    The source cancellation is linearized while still ISSUED, before it knows
    the target identity.  Once the exact signed claim reaches it, the signed
    source-only barrier permits one target-bound *release* disposition — never
    admission — and the target can use ordinary abandon/cancel cleanup.
    """

    a_service, b_service, _a_config, b_config, a_storage, _b_storage, peers = (
        await _instances(187, 188)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    claim_entered = asyncio.Event()
    release_claim = asyncio.Event()

    class _PauseClaim(AsgiPeerSender):
        async def send(self, method, path, *, headers, body):
            if path == "/mesh/v1/pair/claim":
                claim_entered.set()
                await release_claim.wait()
            return await super().send(method, path, headers=headers, body=body)

    b_service._sender_factory = lambda _endpoint: _PauseClaim(peers, "A")  # type: ignore[attr-defined]
    accepting = asyncio.create_task(
        b_service.accept_invitation(
            invite["invitation_bytes"],
            SPACE,
            secret=invite["secret"],
            source_endpoint=A_URL,
            requested_scopes=("read",),
            quiesced=True,
        )
    )
    await asyncio.wait_for(claim_entered.wait(), timeout=1)
    assert await b_service.store.get_reservation(SPACE) == pair_id
    target = await b_service.store.get_session(pair_id)
    assert target is not None and target.state == MeshPairingState.CLAIMED.value

    assert (await a_service.cancel(pair_id))["state"] == MeshPairingState.CANCELLED.value
    assert await a_service.store.get_source_terminal_disposition(pair_id) is None
    assert await a_service.store.get_source_preclaim_cancel_barrier(pair_id) is not None

    # The target formed its signed claim while the invitation was valid, but
    # transport may deliver it only at the expiry boundary.  It remains
    # cleanup-only: a cancelled/barrier-authorized source may bind the terminal
    # disposition, never revive the enrollment.
    source_before_claim = await a_service.store.get_session(pair_id)
    assert source_before_claim is not None
    a_service._clock_ms = lambda: source_before_claim.expires_at_ms  # type: ignore[attr-defined]
    release_claim.set()
    accepted = await accepting
    assert accepted["state"] == MeshPairingState.CLAIMED.value
    disposition = await a_service.store.get_source_terminal_disposition(pair_id)
    assert disposition is not None
    assert disposition.receipt.disposition == "pre_t1_cancel"

    cancelled = await b_service.cancel(pair_id)
    assert cancelled["state"] == MeshPairingState.CANCELLED.value
    assert await b_service.store.get_reservation(SPACE) is None
    source_membership = await HivemindStateStore(
        storage=a_storage, space_id=SPACE
    ).get_membership()  # type: ignore[arg-type]
    assert source_membership is not None
    target_node_id = b_config.fingerprint.split(":", 1)[1]
    assert not any(
        member.node_id == target_node_id
        and member.status in (MemberStatus.PENDING.value, MemberStatus.ACTIVE.value)
        for member in source_membership.members
    )


async def test_late_claim_retries_after_disposition_write_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact late claim completes the sole crash prefix after binding."""

    a_service, b_service, _a_config, _b_config, _a_storage, _b_storage, _peers = (
        await _instances(189, 190)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await a_service.cancel(pair_id)

    real_put = a_service.store.put_source_terminal_disposition
    failed = {"value": False}

    async def crash_once(signed):
        if not failed["value"]:
            failed["value"] = True
            raise OSError("simulated crash after late claim binding")
        await real_put(signed)

    monkeypatch.setattr(a_service.store, "put_source_terminal_disposition", crash_once)
    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.accept_invitation(
            invite["invitation_bytes"],
            SPACE,
            secret=invite["secret"],
            source_endpoint=A_URL,
            requested_scopes=("read",),
            quiesced=True,
        )
    assert exc.value.code == "claim_rejected"
    source = await a_service.store.get_session(pair_id)
    assert source is not None and source.state == MeshPairingState.CANCELLED.value
    assert source.target_fingerprint
    assert await a_service.store.get_source_terminal_disposition(pair_id) is None

    monkeypatch.setattr(a_service.store, "put_source_terminal_disposition", real_put)
    # The source can now finish the signed release proof without relying on a
    # second target claim delivery.  This is the crash prefix after the exact
    # target binding became durable but before the terminal receipt did.
    assert (await a_service.cancel(pair_id))["state"] == MeshPairingState.CANCELLED.value
    assert await a_service.store.get_source_terminal_disposition(pair_id) is not None
    retried = await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",),
        quiesced=True,
    )
    assert retried["state"] == MeshPairingState.CLAIMED.value
    assert await a_service.store.get_source_terminal_disposition(pair_id) is not None
    assert (await b_service.cancel(pair_id))["state"] == MeshPairingState.CANCELLED.value
    assert await b_service.store.get_reservation(SPACE) is None


async def test_source_cancel_does_not_sign_bound_late_claim_without_secret_burn() -> None:
    """A mutable binding/blob alone cannot prove source secret validation."""

    a_service, _b_service, _a_config, b_config, _a_storage, _b_storage, _peers = (
        await _instances(199, 200)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await a_service.cancel(pair_id)
    source = await a_service.store.get_session(pair_id)
    assert source is not None
    signed_claim = _signed_claim_for_invitation(
        invite,
        b_config,
        nonce=generate_pairing_nonce(),
    )
    await a_service.store.put_blob(pair_id, "claim", signed_claim.canonical_bytes())
    await a_service.store.put_session(
        source.with_fields(
            now_ms=NOW_MS + 1,
            target_public_key=b_config.public_key,
            target_fingerprint=b_config.fingerprint,
            target_endpoint="https://b.mesh.test",
            claim_digest=signed_claim.digest(),
        )
    )
    assert not await a_service.store.is_secret_burned(pair_id)

    with pytest.raises(MeshPairingServiceError) as exc:
        await a_service.cancel(pair_id)
    assert exc.value.code == "cancel_unproven"
    assert await a_service.store.get_source_terminal_disposition(pair_id) is None
    # A valid-schema burn record for another digest is not proof either.
    await a_service.store.burn_secret(pair_id, "0" * 64, now_ms=NOW_MS + 2)
    with pytest.raises(MeshPairingServiceError) as exc:
        await a_service.cancel(pair_id)
    assert exc.value.code == "cancel_unproven"
    assert await a_service.store.get_source_terminal_disposition(pair_id) is None
    rewritten = await a_service.store.get_session(pair_id)
    assert rewritten is not None
    await a_service.store.put_session(
        rewritten.with_fields(now_ms=NOW_MS + 3, secret_digest="0" * 64)
    )
    with pytest.raises(MeshPairingServiceError) as exc:
        await a_service.cancel(pair_id)
    assert exc.value.code == "cancel_unproven"
    assert await a_service.store.get_source_terminal_disposition(pair_id) is None


async def test_late_claim_retries_after_claim_blob_prefix_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blob-only late-claim prefix is recoverable only by that exact claim."""

    a_service, b_service, _a_config, _b_config, _a_storage, _b_storage, _peers = (
        await _instances(191, 192)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await a_service.cancel(pair_id)

    real_burn = a_service.store.burn_secret
    failed = {"value": False}

    async def crash_after_claim_blob(stored_pair_id, secret_digest, *, now_ms):
        if stored_pair_id == pair_id and not failed["value"]:
            failed["value"] = True
            raise OSError("simulated crash after late claim blob")
        await real_burn(stored_pair_id, secret_digest, now_ms=now_ms)

    monkeypatch.setattr(a_service.store, "burn_secret", crash_after_claim_blob)
    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.accept_invitation(
            invite["invitation_bytes"],
            SPACE,
            secret=invite["secret"],
            source_endpoint=A_URL,
            requested_scopes=("read",),
            quiesced=True,
        )
    assert exc.value.code == "claim_rejected"
    source = await a_service.store.get_session(pair_id)
    assert source is not None and source.state == MeshPairingState.CANCELLED.value
    assert not source.target_fingerprint and not source.claim_digest
    assert await a_service.store.get_blob(pair_id, "claim") is not None
    assert await a_service.store.get_source_terminal_disposition(pair_id) is None

    monkeypatch.setattr(a_service.store, "burn_secret", real_burn)
    retried = await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",),
        quiesced=True,
    )
    assert retried["state"] == MeshPairingState.CLAIMED.value
    assert await a_service.store.get_source_terminal_disposition(pair_id) is not None
    assert (await b_service.cancel(pair_id))["state"] == MeshPairingState.CANCELLED.value
    assert await b_service.store.get_reservation(SPACE) is None


async def test_accept_requires_quiescence_before_any_target_prefix_write():
    """The operational target-write precondition is enforced by the service."""

    a_service, b_service, _a_config, _b_config, _a_storage, _b_storage, _peers = (
        await _instances(185, 186)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]

    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.accept_invitation(
            invite["invitation_bytes"],
            SPACE,
            secret=invite["secret"],
            source_endpoint=A_URL,
            requested_scopes=("read",),
            quiesced=False,
        )
    assert exc.value.code == "quiescence_required"
    assert await b_service.store.get_session(pair_id) is None
    assert await b_service.store.get_reservation(SPACE) is None
    assert await b_service.store.get_target_acceptance_intent(pair_id) is None
    assert await b_service.store.get_target_pairing_protocol_floor(SPACE) is None
    assert await b_service.store.get_target_pairing_current_tail(SPACE) is None
    assert await b_service.store.get_target_pairing_fence(SPACE) is None


async def test_pre_t1_disposition_is_an_abort_monotone_after_source_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after the signed abort cannot let stale approval admit PENDING."""

    a_service, b_service, _a_config, b_config, a_storage, _b_storage, _peers = (
        await _instances(169, 170)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )
    real_put_session = a_service.store.put_session
    failed = {"value": False}

    async def crash_before_source_cancelled(session):
        if session.pair_id == pair_id and session.state == "cancelled" and not failed["value"]:
            failed["value"] = True
            raise OSError("simulated crash after source disposition")
        await real_put_session(session)

    monkeypatch.setattr(a_service.store, "put_session", crash_before_source_cancelled)
    with pytest.raises(OSError):
        await a_service.cancel(pair_id)
    disposition = await a_service.store.get_source_terminal_disposition(pair_id)
    assert disposition is not None
    assert disposition.receipt.disposition == "pre_t1_cancel"

    # The target can safely consume the durable abort even while source
    # bookkeeping is still CLAIMED, but a stale approval cannot cross T1.
    assert (await b_service.abandon(pair_id))["state"] == "cancelled"
    with pytest.raises(MeshPairingServiceError) as exc:
        await a_service.approve(pair_id)
    assert exc.value.code == "not_cancellable"
    membership = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    target_node = b_config.fingerprint.split(":", 1)[1]
    assert membership is not None
    assert not any(
        member.node_id == target_node and member.status == MemberStatus.PENDING.value
        for member in membership.members
    )

    monkeypatch.setattr(a_service.store, "put_session", real_put_session)
    assert (await a_service.cancel(pair_id))["state"] == "cancelled"


async def test_target_cancel_rejects_mutable_source_terminal_state_without_disposition():
    """A source session enum alone cannot release a target's held fence."""

    a_service, b_service, _a_config, _b_config, _a_storage, _b_storage, _peers = (
        await _instances(171, 172)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )
    source = await a_service.store.get_session(pair_id)
    assert source is not None
    await a_service.store.put_session(
        source.with_fields(now_ms=NOW_MS + 1, state="cancelled")
    )

    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.cancel(pair_id)
    assert exc.value.code in {"source_still_enrolling", "source_unverified"}
    assert await b_service.store.get_reservation(SPACE) == pair_id
    fence = await b_service.store.get_target_pairing_fence(SPACE)
    assert fence is not None and fence.authority.phase == "held"


async def test_target_abandon_rejects_rewritten_evicted_membership_without_disposition():
    """A valid-schema EVICTED rewrite is not the source's signed removal proof."""

    a_service, b_service, _a_config, b_config, a_storage, _b_storage, _peers = (
        await _instances(173, 174)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )
    await a_service.approve(pair_id)
    # Leave the target in its imported/awaiting state while the source is e+1.
    class _BlackHole:
        async def send(self, *args, **kwargs):
            from live_mem.mesh.pairing_client import PeerResponse

            return PeerResponse(503, [], b"")

    a_service._sender_factory = lambda _e: _BlackHole()  # type: ignore[attr-defined]
    await b_service.run_target_enrollment(pair_id)
    source = await a_service.store.get_session(pair_id)
    assert source is not None and source.state == "blocked_recovery"
    source_store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    membership = await source_store.get_membership()
    assert membership is not None
    target_node_id = b_config.fingerprint.split(":", 1)[1]
    rewritten = MembershipView(
        epoch=membership.epoch,
        members=[
            member.model_copy(update={"status": MemberStatus.EVICTED.value})
            if member.node_id == target_node_id
            else member
            for member in membership.members
        ],
    )
    await source_store.set_membership(rewritten)
    await a_service.store.put_session(
        source.with_fields(now_ms=NOW_MS + 1, state="cancelled")
    )

    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.abandon(pair_id)
    assert exc.value.code in {"source_still_enrolling", "source_unverified"}
    assert await b_service.store.get_reservation(SPACE) == pair_id
    fence = await b_service.store.get_target_pairing_fence(SPACE)
    assert fence is not None and fence.authority.phase == "held"


async def test_evict_does_not_sign_disposition_from_rewritten_evicted_membership():
    """Only an intent written before real PENDING removal may complete a retry."""

    a_service, b_service, _a_config, b_config, a_storage, _b_storage, _peers = (
        await _instances(175, 176)
    )
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )
    await a_service.approve(pair_id)

    class _BlackHole:
        async def send(self, *args, **kwargs):
            from live_mem.mesh.pairing_client import PeerResponse

            return PeerResponse(503, [], b"")

    a_service._sender_factory = lambda _e: _BlackHole()  # type: ignore[attr-defined]
    await b_service.run_target_enrollment(pair_id)
    source = await a_service.store.get_session(pair_id)
    assert source is not None and source.state == "blocked_recovery"
    source_store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    membership = await source_store.get_membership()
    assert membership is not None
    target_node_id = b_config.fingerprint.split(":", 1)[1]
    await source_store.set_membership(
        MembershipView(
            epoch=membership.epoch + 1,
            members=[
                member.model_copy(update={"status": MemberStatus.EVICTED.value})
                if member.node_id == target_node_id
                else member
                for member in membership.members
            ],
        )
    )

    with pytest.raises(MeshPairingServiceError) as exc:
        await a_service.evict(pair_id, operator="op", reason="tampered view")
    assert exc.value.code == "eviction_unproven"
    assert await a_service.store.get_source_pending_eviction_intent(pair_id) is None
    assert await a_service.store.get_source_terminal_disposition(pair_id) is None
    assert await b_service.store.get_reservation(SPACE) == pair_id
    fence = await b_service.store.get_target_pairing_fence(SPACE)
    assert fence is not None and fence.authority.phase == "held"
