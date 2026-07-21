# -*- coding: utf-8 -*-
"""
Tests for issue #4 — Hivemind peer identity and authenticated transport.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

import pytest

from live_mem.core.hivemind import (
    EventEnvelope,
    EventType,
    HivemindPeerChannel,
    HivemindStateStore,
    InMemoryPeerTransport,
    Member,
    MembershipView,
    NodeIdentity,
    PeerChannelError,
    PeerErrorCode,
    PeerReceiveStatus,
    PeerTransport,
    SignedPeerEvent,
    canonical_event_payload_hash,
    generate_peer_keypair,
)
from tests.test_hivemind_state import FakeStorage


NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
async def node_a(storage: FakeStorage) -> tuple[HivemindStateStore, str]:
    keys_a = generate_peer_keypair()
    keys_b = generate_peer_keypair()
    store = HivemindStateStore(storage=storage, space_id="alpha")  # type: ignore[arg-type]
    await store.set_node_identity(
        NodeIdentity(node_id="nodeA", display_name="A", public_key=keys_a.public_key)
    )
    await store.set_membership(
        MembershipView(
            epoch=1,
            members=[
                Member(node_id="nodeA", display_name="A", public_key=keys_a.public_key),
                Member(
                    node_id="nodeB",
                    display_name="B",
                    endpoint="memory://nodeB",
                    public_key=keys_b.public_key,
                ),
            ],
        )
    )
    await store.bump_term(2, updated_by_node_id="nodeA")
    return store, keys_a.private_key


@pytest.fixture
async def node_b_message(
    storage: FakeStorage,
) -> tuple[HivemindStateStore, SignedPeerEvent, str]:
    keys_a = generate_peer_keypair()
    keys_b = generate_peer_keypair()
    receiver = HivemindStateStore(storage=storage, space_id="alpha")  # type: ignore[arg-type]
    await receiver.set_membership(
        MembershipView(
            epoch=1,
            members=[
                Member(node_id="nodeA", display_name="A", public_key=keys_a.public_key),
                Member(node_id="nodeB", display_name="B", public_key=keys_b.public_key),
            ],
        )
    )
    await receiver.bump_term(2, updated_by_node_id="nodeA")

    signer_store = HivemindStateStore(storage=FakeStorage(), space_id="alpha")  # type: ignore[arg-type]
    signer = HivemindPeerChannel(
        state=signer_store,
        local_node_id="nodeB",
        private_key=keys_b.private_key,
        clock=lambda: NOW,
    )
    event = EventEnvelope(
        event_id="evt-valid",
        request_id="req-1",
        type=EventType.TOKEN_CLAIM,
        origin_node_id="nodeB",
        term=2,
        membership_epoch=1,
        payload={"want": "token"},
        created_at=NOW_ISO,
    )
    return receiver, await signer.sign_event(event, signed_at=NOW_ISO), keys_b.private_key


@pytest.mark.asyncio
async def test_valid_signed_event_is_accepted_and_persisted_once(
    node_b_message: tuple[HivemindStateStore, SignedPeerEvent, str],
) -> None:
    receiver, message, _private_key = node_b_message
    channel = HivemindPeerChannel(
        state=receiver,
        local_node_id="nodeA",
        private_key=generate_peer_keypair().private_key,
        clock=lambda: NOW,
    )

    result = await channel.receive(message)
    assert result.status == PeerReceiveStatus.ACCEPTED.value
    assert result.persisted is True
    assert await receiver.get_event("evt-valid") == message.event

    replay = await channel.receive(message)
    assert replay.status == PeerReceiveStatus.DUPLICATE.value
    assert replay.persisted is False
    assert len(await receiver.list_events()) == 1


@pytest.mark.asyncio
async def test_invalid_signature_fails_closed(
    node_b_message: tuple[HivemindStateStore, SignedPeerEvent, str],
) -> None:
    receiver, message, _private_key = node_b_message
    channel = HivemindPeerChannel(
        state=receiver,
        local_node_id="nodeA",
        private_key=generate_peer_keypair().private_key,
        clock=lambda: NOW,
    )
    wrong_signature = base64.urlsafe_b64encode(b"\x00" * 64).decode().rstrip("=")
    tampered = message.model_copy(update={"signature": wrong_signature})

    with pytest.raises(PeerChannelError) as err:
        await channel.receive(tampered)
    assert err.value.code == PeerErrorCode.INVALID_SIGNATURE


@pytest.mark.asyncio
async def test_replay_conflict_is_rejected(
    node_b_message: tuple[HivemindStateStore, SignedPeerEvent, str],
) -> None:
    receiver, message, private_key = node_b_message
    channel = HivemindPeerChannel(
        state=receiver,
        local_node_id="nodeA",
        private_key=generate_peer_keypair().private_key,
        clock=lambda: NOW,
    )
    await channel.receive(message)

    signer = HivemindPeerChannel(
        state=HivemindStateStore(storage=FakeStorage(), space_id="alpha"),  # type: ignore[arg-type]
        local_node_id="nodeB",
        private_key=private_key,
        clock=lambda: NOW,
    )
    conflict_event = message.event.model_copy(
        update={"payload": {"want": "different"}}
    )
    conflict = await signer.sign_event(conflict_event, signed_at=NOW_ISO)

    with pytest.raises(PeerChannelError) as err:
        await channel.receive(conflict)
    assert err.value.code == PeerErrorCode.REPLAY_CONFLICT


@pytest.mark.asyncio
async def test_payload_hash_mismatch_is_rejected(
    node_b_message: tuple[HivemindStateStore, SignedPeerEvent, str],
) -> None:
    receiver, message, _private_key = node_b_message
    channel = HivemindPeerChannel(
        state=receiver,
        local_node_id="nodeA",
        private_key=generate_peer_keypair().private_key,
        clock=lambda: NOW,
    )
    tampered = message.model_copy(update={"payload_hash": "0" * 64})

    with pytest.raises(PeerChannelError) as err:
        await channel.receive(tampered)
    assert err.value.code == PeerErrorCode.PAYLOAD_HASH_MISMATCH


@pytest.mark.asyncio
async def test_wrong_membership_epoch_is_rejected(
    node_b_message: tuple[HivemindStateStore, SignedPeerEvent, str],
) -> None:
    receiver, message, _private_key = node_b_message
    channel = HivemindPeerChannel(
        state=receiver,
        local_node_id="nodeA",
        private_key=generate_peer_keypair().private_key,
        clock=lambda: NOW,
    )
    event = message.event.model_copy(update={"membership_epoch": 0})
    stale = message.model_copy(
        update={
            "membership_epoch": 0,
            "event": event,
            "payload_hash": canonical_event_payload_hash(event),
        }
    )

    with pytest.raises(PeerChannelError) as err:
        await channel.receive(stale)
    assert err.value.code == PeerErrorCode.WRONG_MEMBERSHIP_EPOCH


@pytest.mark.asyncio
async def test_stale_timestamp_is_rejected(
    node_b_message: tuple[HivemindStateStore, SignedPeerEvent, str],
) -> None:
    receiver, message, _private_key = node_b_message
    channel = HivemindPeerChannel(
        state=receiver,
        local_node_id="nodeA",
        private_key=generate_peer_keypair().private_key,
        replay_window_seconds=60,
        clock=lambda: NOW,
    )
    stale = message.model_copy(update={"signed_at": "2026-06-13T11:00:00+00:00"})

    with pytest.raises(PeerChannelError) as err:
        await channel.receive(stale)
    assert err.value.code == PeerErrorCode.STALE_TIMESTAMP


@pytest.mark.asyncio
async def test_incompatible_protocol_version_is_rejected(
    node_b_message: tuple[HivemindStateStore, SignedPeerEvent, str],
) -> None:
    receiver, message, _private_key = node_b_message
    channel = HivemindPeerChannel(
        state=receiver,
        local_node_id="nodeA",
        private_key=generate_peer_keypair().private_key,
        clock=lambda: NOW,
    )
    incompatible = message.model_copy(update={"protocol_version": 999})

    with pytest.raises(PeerChannelError) as err:
        await channel.receive(incompatible)
    assert err.value.code == PeerErrorCode.INCOMPATIBLE_PROTOCOL_VERSION


@pytest.mark.asyncio
async def test_unknown_peer_is_rejected(storage: FakeStorage) -> None:
    keys_known = generate_peer_keypair()
    keys_unknown = generate_peer_keypair()
    receiver = HivemindStateStore(storage=storage, space_id="alpha")  # type: ignore[arg-type]
    await receiver.set_membership(
        MembershipView(
            epoch=1,
            members=[Member(node_id="nodeA", public_key=keys_known.public_key)],
        )
    )
    signer = HivemindPeerChannel(
        state=HivemindStateStore(storage=FakeStorage(), space_id="alpha"),  # type: ignore[arg-type]
        local_node_id="nodeX",
        private_key=keys_unknown.private_key,
        clock=lambda: NOW,
    )
    event = EventEnvelope(
        event_id="evt-x",
        type=EventType.TOKEN_CLAIM,
        origin_node_id="nodeX",
        membership_epoch=1,
        created_at=NOW_ISO,
    )
    message = await signer.sign_event(event, signed_at=NOW_ISO)
    channel = HivemindPeerChannel(
        state=receiver,
        local_node_id="nodeA",
        private_key=keys_known.private_key,
        clock=lambda: NOW,
    )

    with pytest.raises(PeerChannelError) as err:
        await channel.receive(message)
    assert err.value.code == PeerErrorCode.UNKNOWN_PEER


@pytest.mark.asyncio
async def test_stale_term_is_rejected(
    node_b_message: tuple[HivemindStateStore, SignedPeerEvent, str],
) -> None:
    receiver, message, _private_key = node_b_message
    channel = HivemindPeerChannel(
        state=receiver,
        local_node_id="nodeA",
        private_key=generate_peer_keypair().private_key,
        clock=lambda: NOW,
    )
    event = message.event.model_copy(update={"term": 1})
    stale = message.model_copy(
        update={
            "term": 1,
            "event": event,
            "payload_hash": canonical_event_payload_hash(event),
        }
    )

    with pytest.raises(PeerChannelError) as err:
        await channel.receive(stale)
    assert err.value.code == PeerErrorCode.STALE_TERM


@pytest.mark.asyncio
async def test_transport_boundary_sends_without_user_tokens(
    node_a: tuple[HivemindStateStore, str],
) -> None:
    store, private_key = node_a
    transport = InMemoryPeerTransport()
    channel = HivemindPeerChannel(
        state=store,
        local_node_id="nodeA",
        private_key=private_key,
        transport=transport,
        clock=lambda: NOW,
    )
    event = EventEnvelope(
        event_id="evt-send",
        type=EventType.TOKEN_CLAIM,
        origin_node_id="nodeA",
        term=2,
        membership_epoch=1,
        created_at=NOW_ISO,
    )

    result = await channel.send(event, peer_node_id="nodeB")
    assert result.peer_node_id == "nodeB"
    assert transport.inboxes["nodeB"][0].event_id == "evt-send"


@pytest.mark.asyncio
async def test_transport_unavailable_is_machine_readable(
    node_a: tuple[HivemindStateStore, str],
) -> None:
    store, private_key = node_a
    channel = HivemindPeerChannel(
        state=store,
        local_node_id="nodeA",
        private_key=private_key,
        transport=InMemoryPeerTransport(unavailable_peers={"nodeB"}),
        clock=lambda: NOW,
    )
    event = EventEnvelope(
        event_id="evt-send",
        type=EventType.TOKEN_CLAIM,
        origin_node_id="nodeA",
        term=2,
        membership_epoch=1,
        created_at=NOW_ISO,
    )

    with pytest.raises(PeerChannelError) as err:
        await channel.send(event, peer_node_id="nodeB")
    assert err.value.code == PeerErrorCode.TRANSPORT_UNAVAILABLE


def test_peer_transport_interface_is_exported() -> None:
    assert PeerTransport is not None
