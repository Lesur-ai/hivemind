# -*- coding: utf-8 -*-
"""Durable Mesh pairing store tests (P10-3, issue #191)."""

from __future__ import annotations

import secrets

import pytest

from live_mem.mesh.identity import generate_mesh_identity
from live_mem.mesh.pairing_state import (
    BlockedRecoveryEvidence,
    MeshPairingRole,
    MeshPairingSession,
    MeshPairingState,
    SignedBlockedRecoveryEvidence,
)
from live_mem.mesh.pairing_store import MeshPairingStore, MeshPairingStoreError

_IDENTITY = generate_mesh_identity()


class InMemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, str] = {}

    async def put(self, key: str, content: str, content_type: str = "") -> None:
        self.objects[key] = content

    async def get(self, key: str) -> str | None:
        return self.objects.get(key)

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
        return [{"Key": k} for k in self.objects if k.startswith(prefix)]


def _prefix() -> str:
    return f"_system/mesh_pairing/hm1:{secrets.token_hex(32)}/"


def _store(storage=None):
    return MeshPairingStore(storage or InMemoryStorage(), prefix=_prefix())


def _session(pair_id: str, state=MeshPairingState.ISSUED) -> MeshPairingSession:
    return MeshPairingSession(
        pair_id=pair_id, role=MeshPairingRole.SOURCE.value, state=state.value,
        space_id="alpha", protocol_version=1,
        source_public_key=_IDENTITY.public_key, source_fingerprint=_IDENTITY.fingerprint,
        source_endpoint="https://a.example", target_public_key="", target_fingerprint="",
        target_endpoint="", granted_scopes=("read",), base_epoch=1,
        invitation_digest="", secret_digest="", claim_digest="", approval_digest="",
        bootstrap_manifest_digest="", bootstrap_bank_version=-1, activation_event_id="",
        last_error="", created_at_ms=1000, updated_at_ms=1000, expires_at_ms=1000 + 3_600_000,
    )


_P1 = "pair_" + "1" * 32
_P2 = "pair_" + "2" * 32


def test_prefix_must_be_valid() -> None:
    with pytest.raises(ValueError):
        MeshPairingStore(InMemoryStorage(), prefix="not/valid/")


async def test_session_roundtrip_and_list() -> None:
    st = _store()
    await st.put_session(_session(_P1))
    got = await st.get_session(_P1)
    assert got is not None and got.pair_id == _P1
    assert await st.get_session(_P2) is None
    sessions = await st.list_sessions()
    assert [s.pair_id for s in sessions] == [_P1]


async def test_reservation_is_exclusive_and_idempotent() -> None:
    storage = InMemoryStorage()
    st = _store(storage)
    await st.reserve("alpha", _P1, now_ms=1)
    # a different pairing cannot reserve the same space
    with pytest.raises(MeshPairingStoreError) as e:
        await st.reserve("alpha", _P2, now_ms=2)
    assert e.value.code == "space_reserved"
    # same pairing re-reserving is idempotent
    await st.reserve("alpha", _P1, now_ms=3)
    assert await st.get_reservation("alpha") == _P1


async def test_reservation_persists_across_restart() -> None:
    storage = InMemoryStorage()
    prefix = _prefix()
    st1 = MeshPairingStore(storage, prefix=prefix)
    await st1.reserve("alpha", _P1, now_ms=1)
    # "restart": a fresh store over the same storage must see the reservation
    st2 = MeshPairingStore(storage, prefix=prefix)
    assert await st2.get_reservation("alpha") == _P1
    with pytest.raises(MeshPairingStoreError):
        await st2.reserve("alpha", _P2, now_ms=2)


async def test_release_frees_the_space_only_for_owner() -> None:
    st = _store()
    await st.reserve("alpha", _P1, now_ms=1)
    await st.release("alpha", _P2)  # non-owner: no-op
    assert await st.get_reservation("alpha") == _P1
    await st.release("alpha", _P1)  # owner
    assert await st.get_reservation("alpha") is None
    await st.reserve("alpha", _P2, now_ms=3)  # now free for another pairing
    assert await st.get_reservation("alpha") == _P2


async def test_assert_space_not_reserved() -> None:
    st = _store()
    await st.assert_space_not_reserved("alpha")  # nothing reserved -> ok
    await st.reserve("alpha", _P1, now_ms=1)
    with pytest.raises(MeshPairingStoreError):
        await st.assert_space_not_reserved("alpha")
    await st.assert_space_not_reserved("beta")  # unrelated space unaffected


async def test_secret_burn_is_one_time_across_restart() -> None:
    storage = InMemoryStorage()
    prefix = _prefix()
    st1 = MeshPairingStore(storage, prefix=prefix)
    assert await st1.is_secret_burned(_P1) is False
    await st1.burn_secret(_P1, "a" * 64, now_ms=1)
    assert await st1.is_secret_burned(_P1) is True
    st2 = MeshPairingStore(storage, prefix=prefix)  # restart
    assert await st2.is_secret_burned(_P1) is True


async def test_nonce_dedup() -> None:
    st = _store()
    nonce = "nonce_" + "a" * 64
    assert await st.record_nonce(nonce, now_ms=1) is True
    assert await st.record_nonce(nonce, now_ms=2) is False


async def test_receipt_idempotency() -> None:
    st = _store()
    assert await st.has_receipt("tok1") is False
    await st.put_receipt("tok1", {"applied": True})
    assert await st.has_receipt("tok1") is True


async def test_evidence_roundtrip_and_verify() -> None:
    st = _store()
    ev = BlockedRecoveryEvidence(
        pair_id=_P1, space_id="alpha", epoch=2, phase="post_admit", next_action="resume",
        manifest_digest="c" * 64, candidate_view_digest="d" * 64, activation_event_id="", issued_at_ms=1,
    )
    signed = SignedBlockedRecoveryEvidence.sign(ev, _IDENTITY.private_key)
    await st.put_evidence(_P1, signed)
    got = await st.get_evidence(_P1)
    assert got is not None
    got.verify(_IDENTITY.public_key)  # signature still valid after storage round-trip
    assert got.evidence == ev


async def test_readback_mismatch_poisons_store() -> None:
    class TamperStorage(InMemoryStorage):
        async def get(self, key: str) -> str | None:
            # return corrupted bytes on read-back
            if key in self.objects:
                return self.objects[key] + " "
            return None

    st = MeshPairingStore(TamperStorage(), prefix=_prefix())
    with pytest.raises(MeshPairingStoreError) as e:
        await st.put_session(_session(_P1))
    assert e.value.code == "readback_mismatch"
    assert st.unsafe is True
    # once poisoned, further durable ops fail closed
    with pytest.raises(MeshPairingStoreError):
        await st.reserve("alpha", _P1, now_ms=1)
