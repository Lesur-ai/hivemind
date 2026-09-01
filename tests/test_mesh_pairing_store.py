# -*- coding: utf-8 -*-
"""Durable Mesh pairing store tests (P10-3, issue #191)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from dataclasses import replace

import pytest

from live_mem.mesh.identity import decode_mesh_public_key, generate_mesh_identity
from live_mem.mesh.canonical import canonical_dumps
from live_mem.mesh.pairing_state import (
    BlockedRecoveryEvidence,
    ImportValidatedAuthority,
    MeshPairingError,
    MeshPairingRole,
    MeshPairingSession,
    MeshPairingState,
    SignedBlockedRecoveryEvidence,
    SignedSourceBootstrapEvidence,
    SignedSourceActivationReceipt,
    SignedSourceActivationMigrationAuthority,
    SignedSourcePendingEvictionIntent,
    SignedSourcePreClaimCancelBarrier,
    SignedSourceTerminalDispositionReceipt,
    SignedTargetActivationReceipt,
    SignedTargetPairingAdmissionAnchor,
    SignedTargetPairingFenceAuthority,
    SignedTargetTerminalConfirmationReceipt,
    SourceBootstrapEvidence,
    SourceActivationReceipt,
    SourceActivationMigrationAuthority,
    SourcePendingEvictionIntent,
    SourcePreClaimCancelBarrier,
    SourceTerminalDispositionReceipt,
    SourcePreparationIntent,
    SourcePreparationState,
    TargetActivationReceipt,
    TargetPairingAdmissionAnchor,
    TargetPairingFenceAuthority,
    TargetTerminalConfirmationReceipt,
)
from live_mem.mesh.pairing_store import (
    MAX_PAIRING_RESERVATIONS,
    MeshPairingStore,
    MeshPairingStoreError,
)

_IDENTITY = generate_mesh_identity()
_TARGET_IDENTITY = generate_mesh_identity()


class InMemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, str] = {}
        self.put_calls: list[str] = []
        self.list_calls = 0

    async def put(self, key: str, content: str, content_type: str = "") -> None:
        self.put_calls.append(key)
        self.objects[key] = content

    async def get(self, key: str) -> str | None:
        return self.objects.get(key)

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
        self.list_calls += 1
        objects = [
            {"Key": k, "Size": len(self.objects[k].encode("utf-8"))}
            for k in sorted(self.objects)
            if k.startswith(prefix)
        ]
        return objects[:max_keys] if max_keys else objects


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


def _preparation(space_id: str = "alpha") -> SourcePreparationIntent:
    raw = decode_mesh_public_key(_IDENTITY.public_key)
    membership_key = (
        "ed25519:"
        + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    )
    return SourcePreparationIntent(
        preparation_id="prep_" + "a" * 32,
        protocol_version=1,
        state=SourcePreparationState.PREPARING.value,
        space_id=space_id,
        source_fingerprint=_IDENTITY.fingerprint,
        membership_public_key=membership_key,
        node_id=_IDENTITY.fingerprint.split(":", 1)[1],
        display_name="Mesh A",
        public_url="https://a.example",
        started_at_ms=1_000,
        started_at_iso="1970-01-01T00:00:01+00:00",
        completed_at_ms=0,
        expected_state_token="e" * 64,
    )


def _source_bootstrap_evidence(**over) -> SourceBootstrapEvidence:
    base = dict(
        pair_id=_P1,
        protocol_version=1,
        space_id="alpha",
        source_fingerprint=_IDENTITY.fingerprint,
        target_fingerprint=_TARGET_IDENTITY.fingerprint,
        membership_epoch=2,
        membership_snapshot_digest="b" * 64,
        membership_view_digest="c" * 64,
        manifest_digest="a" * 64,
        bank_version=1,
        commit_id="commit-1",
        node_digest="d" * 64,
        term=2,
        term_digest="e" * 64,
        token_state="absent",
        token_term=0,
        token_fencing_token=0,
        token_membership_epoch=0,
        token_bank_version=0,
        token_digest="b" * 64,
        pointer_bank_version=1,
        pointer_commit_id="commit-1",
        pointer_digest="f" * 64,
        selected_commit_digest="c" * 64,
        preparation_digest="",
        health_digest="",
        recorded_at_ms=1_000,
    )
    base.update(over)
    return SourceBootstrapEvidence(**base)


def _source_activation_migration_authority(
    **over,
) -> SourceActivationMigrationAuthority:
    base = dict(
        pair_id=_P1,
        protocol_version=1,
        space_id="alpha",
        source_fingerprint=_IDENTITY.fingerprint,
        target_fingerprint=_TARGET_IDENTITY.fingerprint,
        base_epoch=1,
        requires_terminal_confirmation=True,
        issued_at_ms=1_000,
    )
    base.update(over)
    return SourceActivationMigrationAuthority(**base)


def _import_validation(**over) -> ImportValidatedAuthority:
    base = dict(
        pair_id=_P1,
        protocol_version=1,
        space_id="alpha",
        source_fingerprint=_IDENTITY.fingerprint,
        target_fingerprint=_TARGET_IDENTITY.fingerprint,
        local_node_id=_TARGET_IDENTITY.fingerprint.split(":", 1)[1],
        membership_epoch=2,
        membership_snapshot_digest="a" * 64,
        membership_view_digest="b" * 64,
        manifest_digest="d" * 64,
        bank_version=1,
        commit_id="commit-1",
        term_digest="c" * 64,
        token_digest="d" * 64,
        pointer_digest="e" * 64,
        selected_commit_digest="e" * 64,
        validated_at_ms=1_000,
    )
    base.update(over)
    return ImportValidatedAuthority(**base)


def _target_activation_receipt(**over) -> TargetActivationReceipt:
    base = dict(
        authority=_import_validation(),
        membership_epoch=3,
        membership_view_digest="f" * 64,
        activated_at_ms=1_001,
    )
    base.update(over)
    return TargetActivationReceipt(**base)


def _source_activation_receipt(**over) -> SourceActivationReceipt:
    target_receipt = SignedTargetActivationReceipt.sign(
        _target_activation_receipt(), _TARGET_IDENTITY.private_key
    )
    base = dict(
        pair_id=_P1,
        protocol_version=1,
        space_id="alpha",
        source_fingerprint=_IDENTITY.fingerprint,
        target_fingerprint=_TARGET_IDENTITY.fingerprint,
        source_node_id="source-node-a",
        target_node_id=_TARGET_IDENTITY.fingerprint.split(":", 1)[1],
        base_epoch=1,
        membership_epoch=3,
        activation_event_id="a" * 64,
        membership_view_digest="f" * 64,
        target_activation_receipt_digest=hashlib.sha256(
            target_receipt.canonical_bytes()
        ).hexdigest(),
        target_activation_receipt=target_receipt.as_dict(),
        confirmed_at_ms=1_002,
    )
    base.update(over)
    return SourceActivationReceipt(**base)


def _source_terminal_disposition(**over) -> SourceTerminalDispositionReceipt:
    base = dict(
        pair_id=_P1,
        protocol_version=1,
        disposition="pre_t1_cancel",
        space_id="alpha",
        source_public_key=_IDENTITY.public_key,
        source_fingerprint=_IDENTITY.fingerprint,
        target_public_key=_TARGET_IDENTITY.public_key,
        target_fingerprint=_TARGET_IDENTITY.fingerprint,
        invitation_digest="a" * 64,
        claim_digest="b" * 64,
        base_epoch=1,
        membership_epoch=1,
        membership_view_digest="c" * 64,
        issued_at_ms=1_002,
    )
    base.update(over)
    return SourceTerminalDispositionReceipt(**base)


def _source_pending_eviction_intent(**over) -> SourcePendingEvictionIntent:
    base = dict(
        pair_id=_P1,
        protocol_version=1,
        space_id="alpha",
        source_public_key=_IDENTITY.public_key,
        source_fingerprint=_IDENTITY.fingerprint,
        target_public_key=_TARGET_IDENTITY.public_key,
        target_fingerprint=_TARGET_IDENTITY.fingerprint,
        invitation_digest="a" * 64,
        claim_digest="b" * 64,
        base_epoch=1,
        membership_epoch=2,
        membership_view_digest="c" * 64,
        issued_at_ms=1_002,
    )
    base.update(over)
    return SourcePendingEvictionIntent(**base)


def _source_preclaim_cancel_barrier(**over) -> SourcePreClaimCancelBarrier:
    base = dict(
        pair_id=_P1,
        protocol_version=1,
        space_id="alpha",
        source_public_key=_IDENTITY.public_key,
        source_fingerprint=_IDENTITY.fingerprint,
        invitation_digest="a" * 64,
        base_epoch=1,
        membership_epoch=1,
        membership_view_digest="c" * 64,
        issued_at_ms=1_002,
    )
    base.update(over)
    return SourcePreClaimCancelBarrier(**base)


def _target_terminal_confirmation(**over) -> TargetTerminalConfirmationReceipt:
    target_receipt = SignedTargetActivationReceipt.sign(
        _target_activation_receipt(), _TARGET_IDENTITY.private_key
    )
    source_receipt = SignedSourceActivationReceipt.sign(
        _source_activation_receipt(), _IDENTITY.private_key
    )
    base = dict(
        pair_id=_P1,
        protocol_version=1,
        space_id="alpha",
        source_fingerprint=_IDENTITY.fingerprint,
        target_fingerprint=_TARGET_IDENTITY.fingerprint,
        base_epoch=1,
        membership_epoch=3,
        source_activation_receipt_digest=hashlib.sha256(
            source_receipt.canonical_bytes()
        ).hexdigest(),
        target_activation_receipt_digest=hashlib.sha256(
            target_receipt.canonical_bytes()
        ).hexdigest(),
        confirmed_at_ms=1_003,
    )
    base.update(over)
    return TargetTerminalConfirmationReceipt(**base)


def _target_pairing_fence(**over) -> TargetPairingFenceAuthority:
    """Build a direct target-space fence with an exact terminal receipt chain."""

    phase = over.get("phase", "held")
    target_receipt = None
    source_receipt = None
    terminal_confirmation = None
    if phase == "terminal_confirmed":
        target_receipt = SignedTargetActivationReceipt.sign(
            _target_activation_receipt(), _TARGET_IDENTITY.private_key
        )
        target_digest = hashlib.sha256(target_receipt.canonical_bytes()).hexdigest()
        source_receipt = SignedSourceActivationReceipt.sign(
            SourceActivationReceipt(
                pair_id=_P1,
                protocol_version=1,
                space_id="alpha",
                source_fingerprint=_IDENTITY.fingerprint,
                target_fingerprint=_TARGET_IDENTITY.fingerprint,
                source_node_id="source-node-a",
                target_node_id=_TARGET_IDENTITY.fingerprint.split(":", 1)[1],
                base_epoch=1,
                membership_epoch=3,
                activation_event_id="a" * 64,
                membership_view_digest="f" * 64,
                target_activation_receipt_digest=target_digest,
                target_activation_receipt=target_receipt.as_dict(),
                confirmed_at_ms=1_002,
            ),
            _IDENTITY.private_key,
        )
        source_digest = hashlib.sha256(source_receipt.canonical_bytes()).hexdigest()
        terminal_confirmation = SignedTargetTerminalConfirmationReceipt.sign(
            TargetTerminalConfirmationReceipt(
                pair_id=_P1,
                protocol_version=1,
                space_id="alpha",
                source_fingerprint=_IDENTITY.fingerprint,
                target_fingerprint=_TARGET_IDENTITY.fingerprint,
                base_epoch=1,
                membership_epoch=3,
                source_activation_receipt_digest=source_digest,
                target_activation_receipt_digest=target_digest,
                confirmed_at_ms=1_003,
            ),
            _TARGET_IDENTITY.private_key,
        )

    base = dict(
        pair_id=_P1,
        protocol_version=1,
        phase=phase,
        space_id="alpha",
        source_public_key=_IDENTITY.public_key,
        source_fingerprint=_IDENTITY.fingerprint,
        target_public_key=_TARGET_IDENTITY.public_key,
        target_fingerprint=_TARGET_IDENTITY.fingerprint,
        invitation_digest="a" * 64,
        requested_scopes_digest="b" * 64,
        base_epoch=1,
        target_activation_receipt=(
            None if target_receipt is None else target_receipt.as_dict()
        ),
        source_activation_receipt=(
            None if source_receipt is None else source_receipt.as_dict()
        ),
        target_terminal_confirmation=(
            None if terminal_confirmation is None else terminal_confirmation.as_dict()
        ),
        issued_at_ms=1_004,
    )
    base.update(over)
    return TargetPairingFenceAuthority(**base)


_P1 = "pair_" + "1" * 32
_P2 = "pair_" + "2" * 32


def _acceptance_intent(*, pair_id: str = _P1, space_id: str = "alpha") -> dict:
    return {
        "invitation_digest": "a" * 64,
        "pair_id": pair_id,
        "requested_scopes": ["read"],
        "source_fingerprint": _IDENTITY.fingerprint,
        "space_id": space_id,
        "target_fingerprint": _TARGET_IDENTITY.fingerprint,
    }


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


async def test_bootstrap_and_import_authorities_are_immutable_across_retries() -> None:
    storage = InMemoryStorage()
    st = _store(storage)
    evidence = _source_bootstrap_evidence()
    signed_evidence = SignedSourceBootstrapEvidence.sign(
        evidence, _IDENTITY.private_key
    )
    authority = _import_validation()
    receipt = _target_activation_receipt()
    signed_receipt = SignedTargetActivationReceipt.sign(
        receipt, _TARGET_IDENTITY.private_key
    )
    source_receipt = _source_activation_receipt()
    signed_source_receipt = SignedSourceActivationReceipt.sign(
        source_receipt, _IDENTITY.private_key
    )

    assert await st.get_source_bootstrap_evidence(_P1) is None
    assert await st.get_source_activation_marker("alpha") is None
    assert await st.get_import_validation(_P1) is None
    assert await st.get_target_activation_receipt(_P1) is None
    assert await st.get_source_activation_receipt(_P1) is None

    await st.put_source_bootstrap_evidence(signed_evidence)
    assert await st.get_source_bootstrap_evidence(_P1) == signed_evidence
    await st.put_source_bootstrap_evidence(signed_evidence)
    # A crash retry of the exact snapshot may have a later observation time,
    # but it must retain the original immutable record.
    await st.put_source_bootstrap_evidence(
        SignedSourceBootstrapEvidence.sign(
            replace(evidence, recorded_at_ms=2_000), _IDENTITY.private_key
        )
    )
    assert await st.get_source_bootstrap_evidence(_P1) == signed_evidence
    with pytest.raises(MeshPairingStoreError) as exc:
        await st.put_source_bootstrap_evidence(
            SignedSourceBootstrapEvidence.sign(
                replace(evidence, term=3), _IDENTITY.private_key
            )
        )
    assert exc.value.code == "source_evidence_conflict"

    # The per-space marker deliberately duplicates the signed export under a
    # distinct durable key. It is a tail fence discriminator, not a second
    # promotion authority, and therefore must survive restart and reject a
    # different pairing's snapshot for the same source space.
    await st.put_source_activation_marker(signed_evidence)
    assert await st.get_source_activation_marker("alpha") == signed_evidence
    await st.put_source_activation_marker(signed_evidence)
    with pytest.raises(MeshPairingStoreError) as exc:
        await st.put_source_activation_marker(
            SignedSourceBootstrapEvidence.sign(
                replace(evidence, pair_id=_P2), _IDENTITY.private_key
            )
        )
    assert exc.value.code == "source_activation_marker_conflict"

    await st.put_import_validation(authority)
    assert await st.get_import_validation(_P1) == authority
    await st.put_import_validation(authority)
    await st.put_import_validation(replace(authority, validated_at_ms=2_000))
    assert await st.get_import_validation(_P1) == authority
    with pytest.raises(MeshPairingStoreError) as exc:
        await st.put_import_validation(replace(authority, manifest_digest="e" * 64))
    assert exc.value.code == "import_validation_conflict"

    await st.put_target_activation_receipt(signed_receipt)
    assert await st.get_target_activation_receipt(_P1) == signed_receipt
    await st.put_target_activation_receipt(signed_receipt)
    await st.put_target_activation_receipt(
        SignedTargetActivationReceipt.sign(
            replace(receipt, activated_at_ms=2_000), _TARGET_IDENTITY.private_key
        )
    )
    assert await st.get_target_activation_receipt(_P1) == signed_receipt
    with pytest.raises(MeshPairingStoreError) as exc:
        await st.put_target_activation_receipt(
            SignedTargetActivationReceipt.sign(
                replace(receipt, membership_view_digest="a" * 64),
                _TARGET_IDENTITY.private_key,
            )
        )
    assert exc.value.code == "activation_receipt_conflict"

    await st.put_source_activation_receipt(signed_source_receipt)
    assert await st.get_source_activation_receipt(_P1) == signed_source_receipt
    await st.put_source_activation_receipt(signed_source_receipt)
    await st.put_source_activation_receipt(
        SignedSourceActivationReceipt.sign(
            replace(source_receipt, confirmed_at_ms=2_000), _IDENTITY.private_key
        )
    )
    assert await st.get_source_activation_receipt(_P1) == signed_source_receipt
    with pytest.raises(MeshPairingStoreError) as exc:
        await st.put_source_activation_receipt(
            SignedSourceActivationReceipt.sign(
                replace(source_receipt, activation_event_id="b" * 64),
                _IDENTITY.private_key,
            )
        )
    assert exc.value.code == "source_activation_receipt_conflict"

    restarted = MeshPairingStore(storage, prefix=st._prefix)
    assert await restarted.get_source_bootstrap_evidence(_P1) == signed_evidence
    assert await restarted.get_source_activation_marker("alpha") == signed_evidence
    assert await restarted.get_import_validation(_P1) == authority
    assert await restarted.get_target_activation_receipt(_P1) == signed_receipt
    assert await restarted.get_source_activation_receipt(_P1) == signed_source_receipt
    await restarted.release_source_activation_marker("alpha", _P1)
    assert await st.get_source_activation_marker("alpha") is None


async def test_pairing_authority_key_binding_and_clear_fail_closed() -> None:
    """Authority keys, readback clears, and legacy records never alias a pair."""

    evidence = _source_bootstrap_evidence()
    signed_evidence = SignedSourceBootstrapEvidence.sign(
        evidence, _IDENTITY.private_key
    )
    authority = _import_validation()

    source_storage = InMemoryStorage()
    source_store = _store(source_storage)
    source_storage.objects[
        source_store._source_bootstrap_evidence_key(_P2)
    ] = signed_evidence.canonical_bytes().decode("utf-8")
    with pytest.raises(MeshPairingStoreError) as exc:
        await source_store.get_source_bootstrap_evidence(_P2)
    assert exc.value.code == "corrupt_state"

    target_storage = InMemoryStorage()
    target_store = _store(target_storage)
    target_storage.objects[
        target_store._import_validation_key(_P2)
    ] = authority.canonical_bytes().decode("utf-8")
    with pytest.raises(MeshPairingStoreError) as exc:
        await target_store.get_import_validation(_P2)
    assert exc.value.code == "corrupt_state"

    class _StickyDeleteStorage(InMemoryStorage):
        async def delete(self, key: str) -> None:
            return None

    sticky_storage = _StickyDeleteStorage()
    sticky_store = _store(sticky_storage)
    await sticky_store.put_import_validation(authority)
    with pytest.raises(MeshPairingStoreError) as exc:
        await sticky_store.clear_import_validation_for_resync(_P1)
    assert exc.value.code == "readback_mismatch"
    assert sticky_store.unsafe is True


async def test_target_acceptance_intent_and_legacy_reservation_index_are_strict() -> None:
    """Pair-id admission intent is immutable while legacy collisions stay visible."""

    storage = InMemoryStorage()
    st = _store(storage)
    intent = _acceptance_intent()
    assert await st.get_target_acceptance_intent(_P1) is None
    await st.put_target_acceptance_intent(_P1, intent)
    assert await st.get_target_acceptance_intent(_P1) == intent
    await st.put_target_acceptance_intent(_P1, dict(intent))
    with pytest.raises(MeshPairingStoreError) as exc:
        await st.put_target_acceptance_intent(
            _P1, _acceptance_intent(space_id="beta")
        )
    assert exc.value.code == "acceptance_conflict"

    corrupt_storage = InMemoryStorage()
    corrupt_store = _store(corrupt_storage)
    corrupt_storage.objects[
        corrupt_store._target_acceptance_intent_key(_P1)
    ] = canonical_dumps({"pair_id": _P1}).decode("utf-8")
    with pytest.raises(MeshPairingStoreError) as exc:
        await corrupt_store.get_target_acceptance_intent(_P1)
    assert exc.value.code == "corrupt_state"

    await st.reserve("alpha", _P1, now_ms=1)
    await st.reserve("beta", _P1, now_ms=2)
    assert await st.find_reservations_by_pair_id(_P1) == ("alpha", "beta")
    with pytest.raises(MeshPairingStoreError) as exc:
        await st.find_reservation_by_pair_id(_P1)
    assert exc.value.code == "reservation_collision"


async def test_nonce_ledger_rejects_legacy_or_malformed_ownership() -> None:
    """A claim retry only inherits a nonce when its exact owner is durable."""

    storage = InMemoryStorage()
    st = _store(storage)
    nonce = "nonce_" + "b" * 64
    storage.objects[st._nonce_key(nonce)] = canonical_dumps(
        {"nonce": nonce, "seen_at_ms": 1}
    ).decode("utf-8")
    assert await st.record_nonce(
        nonce, pair_id=_P1, claim_digest="a" * 64, now_ms=2
    ) == "different"
    with pytest.raises(MeshPairingStoreError) as exc:
        await st.record_nonce(
            nonce, pair_id=_P1, claim_digest="not-a-digest", now_ms=3
        )
    assert exc.value.code == "invalid_claim_digest"


@pytest.mark.parametrize("bad_size", [None, -1, "1", 65_537])
async def test_diagnostic_session_inventory_rejects_bad_size_before_get(
    bad_size,
) -> None:
    class BadSizeStorage(InMemoryStorage):
        def __init__(self) -> None:
            super().__init__()
            self.get_keys: list[str] = []

        async def get(self, key: str) -> str | None:
            self.get_keys.append(key)
            return await super().get(key)

        async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
            objects = await super().list_objects(prefix, max_keys=max_keys)
            for obj in objects:
                if bad_size is None:
                    obj.pop("Size", None)
                else:
                    obj["Size"] = bad_size
            return objects

    storage = BadSizeStorage()
    writer = _store(storage)
    await writer.put_session(_session(_P1))
    storage.get_keys.clear()
    reader = MeshPairingStore(storage, prefix=writer._prefix)

    with pytest.raises(MeshPairingStoreError) as exc:
        await reader.list_sessions_diagnostic()
    assert exc.value.code == "corrupt_state"
    assert storage.get_keys == []


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


@pytest.mark.parametrize("bad_size", [None, -1, "1", 65_537])
async def test_reservation_inventory_rejects_bad_size_before_get(bad_size) -> None:
    class BadSizeStorage(InMemoryStorage):
        def __init__(self) -> None:
            super().__init__()
            self.bad_sizes = False
            self.get_keys: list[str] = []

        async def get(self, key: str) -> str | None:
            self.get_keys.append(key)
            return await super().get(key)

        async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
            objects = await super().list_objects(prefix, max_keys=max_keys)
            if self.bad_sizes:
                for obj in objects:
                    if bad_size is None:
                        obj.pop("Size", None)
                    else:
                        obj["Size"] = bad_size
            return objects

    storage = BadSizeStorage()
    writer = _store(storage)
    await writer.reserve("alpha", _P1, now_ms=1)
    storage.bad_sizes = True
    storage.get_keys.clear()
    reader = MeshPairingStore(storage, prefix=writer._prefix)

    with pytest.raises(MeshPairingStoreError) as exc:
        await reader.get_reservation("alpha")
    assert exc.value.code == "corrupt_state"
    assert storage.get_keys == []


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


async def test_reservation_hydration_cannot_overwrite_concurrent_reserve() -> None:
    class StaleListStorage(InMemoryStorage):
        def __init__(self) -> None:
            super().__init__()
            self.list_started = asyncio.Event()
            self.release_list = asyncio.Event()
            self.block_once = True

        async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
            objects = await super().list_objects(prefix, max_keys=max_keys)
            if prefix.endswith("reservations/") and self.block_once:
                self.block_once = False
                self.list_started.set()
                await self.release_list.wait()
            return objects

    storage = StaleListStorage()
    st = _store(storage)
    loading = asyncio.create_task(st.get_reservation("alpha"))
    await storage.list_started.wait()
    reserving = asyncio.create_task(st.reserve("alpha", _P1, now_ms=1))
    await asyncio.sleep(0)
    assert not reserving.done()

    storage.release_list.set()
    assert await loading is None
    await reserving
    assert await st.get_reservation("alpha") == _P1


@pytest.mark.parametrize(
    "damage",
    ["invalid_json", "missing_record", "invalid_shape", "invalid_fields", "key_alias"],
)
async def test_reservation_hydration_fails_closed_on_durable_corruption(
    damage: str,
) -> None:
    """Restart hydration never turns a malformed reservation into a free space."""

    storage = InMemoryStorage()
    prefix = _prefix()
    key = f"{prefix}reservations/alpha.json"
    storage.objects[key] = canonical_dumps(
        {"space_id": "alpha", "pair_id": _P1, "created_at_ms": 1}
    ).decode("utf-8")
    if damage == "invalid_json":
        storage.objects[key] = "{"
    elif damage == "invalid_shape":
        storage.objects[key] = canonical_dumps({"space_id": "alpha"}).decode(
            "utf-8"
        )
    elif damage == "invalid_fields":
        storage.objects[key] = canonical_dumps(
            {"space_id": "alpha", "pair_id": _P1, "created_at_ms": -1}
        ).decode("utf-8")
    elif damage == "key_alias":
        storage.objects.pop(key)
        storage.objects[f"{prefix}reservations/other.json"] = canonical_dumps(
            {"space_id": "alpha", "pair_id": _P1, "created_at_ms": 1}
        ).decode("utf-8")

    if damage == "missing_record":

        class MissingReadStorage(InMemoryStorage):
            async def get(self, candidate: str) -> str | None:
                if candidate == key:
                    return None
                return await super().get(candidate)

        missing_storage = MissingReadStorage()
        missing_storage.objects = dict(storage.objects)
        storage = missing_storage

    restarted = MeshPairingStore(storage, prefix=prefix)
    with pytest.raises(MeshPairingStoreError) as exc:
        await restarted.get_reservation("alpha")
    assert exc.value.code == "corrupt_state"
    assert restarted.unsafe


async def test_reservation_hydration_marks_backend_inventory_ambiguous() -> None:
    """A failed reservation listing is an unsafe persistence boundary, not absence."""

    class UnavailableStorage(InMemoryStorage):
        async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
            raise OSError("backend unavailable")

    store = MeshPairingStore(UnavailableStorage(), prefix=_prefix())
    with pytest.raises(MeshPairingStoreError) as exc:
        await store.get_reservation("alpha")
    assert exc.value.code == "io_ambiguous"
    assert store.unsafe


async def test_durable_artifact_blobs_are_byte_exact_and_fail_closed() -> None:
    """Artifacts cannot be silently decoded, replaced, or accepted after I/O loss."""

    storage = InMemoryStorage()
    store = _store(storage)
    payload = b"\x00signed-artifact\xff"
    await store.put_blob(_P1, "claim", payload)
    assert await store.get_blob(_P1, "claim") == payload
    with pytest.raises(MeshPairingStoreError) as exc:
        await store.put_blob(_P1, "claim", "not-bytes")  # type: ignore[arg-type]
    assert exc.value.code == "invalid_blob"
    with pytest.raises(MeshPairingStoreError) as exc:
        await store.get_blob(_P1, "invalid/blob")
    assert exc.value.code == "io_ambiguous"
    assert store.unsafe

    corrupt_storage = InMemoryStorage()
    corrupt_store = _store(corrupt_storage)
    corrupt_storage.objects[corrupt_store._blob_key(_P1, "claim")] = "a"
    with pytest.raises(MeshPairingStoreError) as exc:
        await corrupt_store.get_blob(_P1, "claim")
    assert exc.value.code == "corrupt_state"

    class SilentWriteStorage(InMemoryStorage):
        async def put(self, key: str, content: str, content_type: str = "") -> None:
            return None

    silent_store = _store(SilentWriteStorage())
    with pytest.raises(MeshPairingStoreError) as exc:
        await silent_store.put_blob(_P1, "claim", b"signed")
    assert exc.value.code == "readback_mismatch"
    assert silent_store.unsafe

    class UnavailableReadStorage(InMemoryStorage):
        async def get(self, key: str) -> str | None:
            raise OSError("backend unavailable")

    unavailable_store = _store(UnavailableReadStorage())
    with pytest.raises(MeshPairingStoreError) as exc:
        await unavailable_store.get_blob(_P1, "claim")
    assert exc.value.code == "io_ambiguous"
    assert unavailable_store.unsafe


async def test_reserve_refuses_inventory_overflow_before_write_and_restart() -> None:
    storage = InMemoryStorage()
    prefix = _prefix()
    st = MeshPairingStore(storage, prefix=prefix)
    for index in range(MAX_PAIRING_RESERVATIONS):
        await st.reserve(
            f"space-{index:03d}",
            f"pair_{index:032x}",
            now_ms=index,
        )

    # At the exact bound the current owner's retry remains valid.
    await st.reserve("space-255", f"pair_{255:032x}", now_ms=999)
    writes_before = list(storage.put_calls)
    snapshot_before = dict(storage.objects)
    with pytest.raises(MeshPairingStoreError) as exc:
        await st.reserve(
            "space-overflow",
            f"pair_{MAX_PAIRING_RESERVATIONS:032x}",
            now_ms=1_000,
        )
    assert exc.value.code == "too_many_reservations"
    assert storage.put_calls == writes_before
    assert storage.objects == snapshot_before

    restarted = MeshPairingStore(storage, prefix=prefix)
    assert await restarted.get_reservation("space-255") == f"pair_{255:032x}"
    assert restarted.unsafe is False


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


async def test_ordinary_write_reservation_guard_never_lists_global_inventory() -> None:
    """A cold ordinary write reads its one reservation key, not all history."""

    storage = InMemoryStorage()
    store = _store(storage)
    for index in range(MAX_PAIRING_RESERVATIONS + 1):
        space_id = f"historic-{index:03d}"
        storage.objects[store._reservation_key(space_id)] = canonical_dumps(
            {
                "space_id": space_id,
                "pair_id": f"pair_{index:032x}",
                "created_at_ms": index,
            }
        ).decode("utf-8")

    # Hydrating this deliberately oversized operational inventory would fail
    # closed.  An unrelated ordinary write instead has an O(1) direct read.
    await store.assert_space_not_reserved("unrelated")
    assert storage.list_calls == 0

    with pytest.raises(MeshPairingStoreError) as exc:
        await store.assert_space_not_reserved("historic-256")
    assert exc.value.code == "space_reserved"
    assert storage.list_calls == 0


async def test_source_preparation_roundtrip_transition_and_exact_retry() -> None:
    storage = InMemoryStorage()
    st = _store(storage)
    preparing = _preparation()

    await st.put_source_preparation(preparing)
    assert await st.get_source_preparation("alpha") == preparing
    writes_after_create = len(storage.put_calls)
    await st.put_source_preparation(preparing)
    assert len(storage.put_calls) == writes_after_create

    complete = preparing.complete(2_000)
    await st.put_source_preparation(complete)
    assert await st.get_source_preparation("alpha") == complete
    writes_after_complete = len(storage.put_calls)
    await st.put_source_preparation(complete)
    assert len(storage.put_calls) == writes_after_complete

    with pytest.raises(MeshPairingStoreError) as exc:
        await st.put_source_preparation(
            replace(complete, expected_state_token="f" * 64)
        )
    assert exc.value.code == "preparation_conflict"
    assert await st.get_source_preparation("alpha") == complete


async def test_preparing_fence_survives_restart_with_different_fingerprint() -> None:
    storage = InMemoryStorage()
    first = MeshPairingStore(storage, prefix=_prefix())
    changed_identity = MeshPairingStore(storage, prefix=_prefix())

    # Hydrate the second store's target-reservation cache negatively before the
    # preparation exists.  The stable preparation lookup must not reuse it.
    await changed_identity.assert_space_not_reserved("alpha")
    preparing = _preparation()
    await first.put_source_preparation(preparing)

    assert "_system/mesh_source_preparations/alpha.json" in storage.objects
    assert await changed_identity.get_source_preparation("alpha") == preparing
    with pytest.raises(MeshPairingStoreError) as exc:
        await changed_identity.assert_space_not_reserved("alpha")
    assert exc.value.code == "space_reserved"
    with pytest.raises(MeshPairingStoreError) as exc:
        await changed_identity.assert_direct_local_allowed("alpha")
    assert exc.value.code == "direct_local_forbidden"
    await changed_identity.assert_direct_local_allowed("beta")
    with pytest.raises(MeshPairingStoreError) as exc:
        await changed_identity.reserve("alpha", _P1, now_ms=2_000)
    assert exc.value.code == "space_reserved"

    await first.put_source_preparation(preparing.complete(3_000))
    # COMPLETE releases only the temporary ordinary-write reservation.  Its
    # durable provenance permanently forbids DIRECT_LOCAL and target reuse.
    await changed_identity.assert_space_not_reserved("alpha")
    with pytest.raises(MeshPairingStoreError) as exc:
        await changed_identity.assert_direct_local_allowed("alpha")
    assert exc.value.code == "direct_local_forbidden"
    with pytest.raises(MeshPairingStoreError) as exc:
        await changed_identity.reserve("alpha", _P1, now_ms=4_000)
    assert exc.value.code == "space_reserved"
    assert await changed_identity.get_reservation("alpha") is None


async def test_target_reservation_blocks_new_source_preparation() -> None:
    st = _store()
    await st.reserve("alpha", _P1, now_ms=1)
    with pytest.raises(MeshPairingStoreError) as exc:
        await st.put_source_preparation(_preparation())
    assert exc.value.code == "space_reserved"
    assert await st.get_source_preparation("alpha") is None


async def test_source_preparation_cannot_start_complete() -> None:
    st = _store()
    with pytest.raises(MeshPairingStoreError) as exc:
        await st.put_source_preparation(_preparation().complete(2_000))
    assert exc.value.code == "illegal_transition"
    assert await st.get_source_preparation("alpha") is None


async def test_store_rejects_session_key_payload_mismatch() -> None:
    storage = InMemoryStorage()
    prefix = _prefix()
    st = MeshPairingStore(storage, prefix=prefix)
    await st.put_session(_session(_P1))
    storage.objects[f"{prefix}sessions/{_P2}.json"] = storage.objects[
        f"{prefix}sessions/{_P1}.json"
    ]
    before = dict(storage.objects)

    with pytest.raises(MeshPairingStoreError) as exc:
        await st.get_session(_P2)
    assert exc.value.code == "corrupt_state"
    with pytest.raises(MeshPairingStoreError) as exc:
        await MeshPairingStore(storage, prefix=prefix).list_sessions()
    assert exc.value.code == "corrupt_state"
    assert storage.objects == before


async def test_store_rejects_preparation_key_payload_mismatch() -> None:
    storage = InMemoryStorage()
    st = _store(storage)
    storage.objects["_system/mesh_source_preparations/beta.json"] = canonical_dumps(
        _preparation("alpha").as_dict()
    ).decode("utf-8")
    before = dict(storage.objects)
    with pytest.raises(MeshPairingStoreError) as exc:
        await st.get_source_preparation("beta")
    assert exc.value.code == "corrupt_state"
    assert storage.objects == before


async def test_store_rejects_misnamed_or_unexpected_reservation_entry() -> None:
    storage = InMemoryStorage()
    prefix = _prefix()
    st = MeshPairingStore(storage, prefix=prefix)
    await st.reserve("alpha", _P1, now_ms=1)
    original_key = f"{prefix}reservations/alpha.json"
    wrong_key = f"{prefix}reservations/beta.json"
    storage.objects[wrong_key] = storage.objects.pop(original_key)
    before = dict(storage.objects)
    with pytest.raises(MeshPairingStoreError) as exc:
        await MeshPairingStore(storage, prefix=prefix).get_reservation("alpha")
    assert exc.value.code == "corrupt_state"
    assert storage.objects == before
    lists_after_failure = storage.list_calls
    with pytest.raises(MeshPairingStoreError) as exc:
        await MeshPairingStore(storage, prefix=prefix).get_reservation("alpha")
    assert exc.value.code == "local_unsafe"
    assert storage.list_calls == lists_after_failure

    prefix = _prefix()
    storage.objects = {f"{prefix}reservations/alpha.txt": "{}"}
    with pytest.raises(MeshPairingStoreError) as exc:
        await MeshPairingStore(storage, prefix=prefix).get_reservation("alpha")
    assert exc.value.code == "corrupt_state"


async def test_activation_fence_is_targeted_idempotent_and_restart_safe() -> None:
    storage = InMemoryStorage()
    prefix = _prefix()
    first = MeshPairingStore(storage, prefix=prefix)
    await first.put_activation_fence("alpha", _P1, now_ms=1)
    writes = len(storage.put_calls)
    await first.put_activation_fence("alpha", _P1, now_ms=2)
    assert len(storage.put_calls) == writes
    restarted = MeshPairingStore(storage, prefix=prefix)
    assert await restarted.get_activation_fence("alpha") == _P1
    assert await restarted.get_activation_fence_record("alpha") == (_P1, "activation")
    await restarted.put_activation_fence(
        "alpha", _P1, now_ms=3, phase="source_terminal_confirmation"
    )
    assert await restarted.get_activation_fence_record("alpha") == (
        _P1,
        "source_terminal_confirmation",
    )
    with pytest.raises(MeshPairingStoreError) as exc:
        await restarted.put_activation_fence("alpha", _P1, now_ms=4)
    assert exc.value.code == "invalid_activation_phase"
    with pytest.raises(MeshPairingStoreError) as exc:
        await restarted.put_activation_fence("alpha", _P2, now_ms=5)
    assert exc.value.code == "activation_conflict"
    await restarted.release_activation_fence("alpha", _P1)
    assert await first.get_activation_fence("alpha") is None


async def test_terminal_fence_releases_require_delete_readback() -> None:
    """A silent successful delete can never report all-ACK convergence."""

    reservation_storage = InMemoryStorage()
    reservation_store = _store(reservation_storage)
    await reservation_store.reserve("alpha", _P1, now_ms=1)

    async def silent_delete(_key: str) -> None:
        return None

    reservation_storage.delete = silent_delete  # type: ignore[method-assign]
    with pytest.raises(MeshPairingStoreError) as exc:
        await reservation_store.release("alpha", _P1)
    assert exc.value.code == "readback_mismatch"
    assert reservation_store.unsafe

    marker_storage = InMemoryStorage()
    marker_store = _store(marker_storage)
    marker = SignedSourceBootstrapEvidence.sign(
        _source_bootstrap_evidence(), _IDENTITY.private_key
    )
    await marker_store.put_source_activation_marker(marker)
    marker_storage.delete = silent_delete  # type: ignore[method-assign]
    with pytest.raises(MeshPairingStoreError) as exc:
        await marker_store.release_source_activation_marker("alpha", _P1)
    assert exc.value.code == "delete_unconfirmed"
    assert marker_store.unsafe


async def test_activation_migration_records_clear_or_one_legacy_owner() -> None:
    storage = InMemoryStorage()
    prefix = _prefix()
    first = MeshPairingStore(storage, prefix=prefix)
    assert await first.get_activation_migration("alpha") is None

    await first.put_activation_migration("alpha", _P1, now_ms=1)
    restarted = MeshPairingStore(storage, prefix=prefix)
    assert await restarted.get_activation_migration("alpha") == _P1
    with pytest.raises(MeshPairingStoreError) as exc:
        await restarted.put_activation_migration("alpha", _P2, now_ms=2)
    assert exc.value.code == "migration_conflict"

    await restarted.put_activation_migration("alpha", "", now_ms=3)
    assert await first.get_activation_migration("alpha") == ""
    writes = len(storage.put_calls)
    await first.put_activation_migration("alpha", "", now_ms=4)
    assert len(storage.put_calls) == writes
    with pytest.raises(MeshPairingStoreError) as exc:
        await first.put_activation_migration("alpha", _P1, now_ms=5)
    assert exc.value.code == "migration_conflict"


async def test_source_activation_migration_and_protocol_floor_are_signed_and_durable() -> None:
    storage = InMemoryStorage()
    store = MeshPairingStore(storage, prefix=_prefix())
    signed = SignedSourceActivationMigrationAuthority.sign(
        _source_activation_migration_authority(), _IDENTITY.private_key
    )

    await store.put_activation_migration(
        "alpha",
        _P1,
        now_ms=1,
        rearm_for_source_activation=True,
        source_activation_authority=signed,
    )
    await store.put_source_activation_protocol_floor(signed)

    restarted = MeshPairingStore(storage, prefix=store._prefix)
    entry = await restarted.get_activation_migration_entry("alpha")
    assert entry == (_P1, True, signed)
    assert await restarted.get_source_activation_protocol_floor("alpha") == signed

    # Exact crash retries are idempotent, but a signed #417 owner cannot be
    # replaced by an arbitrary legacy migration record through the store API.
    await restarted.put_activation_migration(
        "alpha",
        _P1,
        now_ms=2,
        rearm_for_source_activation=True,
        source_activation_authority=signed,
    )
    with pytest.raises(MeshPairingStoreError) as exc:
        await restarted.put_activation_migration("alpha", "", now_ms=3)
    assert exc.value.code == "migration_conflict"


async def test_source_activation_migration_v2_requires_explicit_replacement_and_keeps_floor() -> None:
    """A source tail is retry-idempotent but cannot be silently retargeted."""

    storage = InMemoryStorage()
    store = MeshPairingStore(storage, prefix=_prefix())
    first = SignedSourceActivationMigrationAuthority.sign(
        _source_activation_migration_authority(), _IDENTITY.private_key
    )
    second = SignedSourceActivationMigrationAuthority.sign(
        _source_activation_migration_authority(pair_id=_P2), _IDENTITY.private_key
    )

    await store.put_activation_migration(
        "alpha",
        _P1,
        now_ms=1,
        rearm_for_source_activation=True,
        source_activation_authority=first,
    )
    await store.put_source_activation_protocol_floor(first)
    writes_after_first_owner = len(storage.put_calls)

    # The exact crash retry neither mutates the V2 record nor its durable floor.
    await store.put_activation_migration(
        "alpha",
        _P1,
        now_ms=2,
        rearm_for_source_activation=True,
        source_activation_authority=first,
    )
    await store.put_source_activation_protocol_floor(second)
    assert len(storage.put_calls) == writes_after_first_owner
    assert await store.get_source_activation_protocol_floor("alpha") == first

    # A second owner needs the narrowly named, service-only settled-tail path;
    # normal retries must retain the current owner and fail closed.
    with pytest.raises(MeshPairingStoreError) as exc:
        await store.put_activation_migration(
            "alpha",
            _P2,
            now_ms=3,
            rearm_for_source_activation=True,
            source_activation_authority=second,
        )
    assert exc.value.code == "migration_conflict"
    assert await store.get_activation_migration_entry("alpha") == (_P1, True, first)

    await store.put_activation_migration(
        "alpha",
        _P2,
        now_ms=4,
        rearm_for_source_activation=True,
        source_activation_authority=second,
        replace_settled_source_activation=True,
    )
    assert await store.get_activation_migration_entry("alpha") == (_P2, True, second)
    # The floor identifies a source protocol generation, not a mutable pair
    # owner, and therefore deliberately keeps the first signed authority.
    assert await store.get_source_activation_protocol_floor("alpha") == first


async def test_source_activation_migration_and_floor_reject_corrupt_bindings() -> None:
    """Raw storage rewrites cannot alias a signed record to another key."""

    storage = InMemoryStorage()
    store = MeshPairingStore(storage, prefix=_prefix())
    signed = SignedSourceActivationMigrationAuthority.sign(
        _source_activation_migration_authority(), _IDENTITY.private_key
    )

    # A signed floor for alpha under beta's durable key is corrupt, even though
    # the signature itself would verify for the original source identity.
    storage.objects[store._source_activation_protocol_floor_key("beta")] = (
        signed.canonical_bytes().decode("utf-8")
    )
    with pytest.raises(MeshPairingStoreError) as exc:
        await store.get_source_activation_protocol_floor("beta")
    assert exc.value.code == "corrupt_state"

    # Version 2 must carry a complete signed authority and bind its pair key to
    # that authority; neither a malformed nested object nor a copied owner may
    # pass as an active source tail.
    storage.objects[store._activation_migration_key("alpha")] = canonical_dumps(
        {
            "authority": {"signature": signed.signature},
            "kind": "source_activation",
            "pair_id": _P1,
            "protocol_version": 2,
            "space_id": "alpha",
            "updated_at_ms": 1,
        }
    ).decode("utf-8")
    with pytest.raises(MeshPairingStoreError) as exc:
        await store.get_activation_migration_entry("alpha")
    assert exc.value.code == "corrupt_state"

    storage.objects[store._activation_migration_key("alpha")] = canonical_dumps(
        {
            "authority": signed.as_dict(),
            "kind": "source_activation",
            "pair_id": _P2,
            "protocol_version": 2,
            "space_id": "alpha",
            "updated_at_ms": 2,
        }
    ).decode("utf-8")
    with pytest.raises(MeshPairingStoreError) as exc:
        await store.get_activation_migration_entry("alpha")
    assert exc.value.code == "corrupt_state"


async def test_target_pairing_fence_and_protocol_floor_are_signed_direct_and_durable() -> None:
    """A target's floor/fence are per-space records with no inventory scan."""

    storage = InMemoryStorage()
    store = MeshPairingStore(storage, prefix=_prefix())
    held = SignedTargetPairingFenceAuthority.sign(
        _target_pairing_fence(), _TARGET_IDENTITY.private_key
    )

    await store.put_target_pairing_protocol_floor(held)
    await store.put_target_pairing_current_tail(held)
    await store.put_target_pairing_fence(held)

    restarted = MeshPairingStore(storage, prefix=store._prefix)
    assert await restarted.get_target_pairing_protocol_floor("alpha") == held
    assert await restarted.get_target_pairing_current_tail("alpha") == held
    assert await restarted.get_target_pairing_fence("alpha") == held
    assert storage.list_calls == 0

    other_target = generate_mesh_identity()
    other = SignedTargetPairingFenceAuthority.sign(
        _target_pairing_fence(
            target_public_key=other_target.public_key,
            target_fingerprint=other_target.fingerprint,
        ),
        other_target.private_key,
    )
    with pytest.raises(MeshPairingStoreError) as exc:
        await restarted.put_target_pairing_protocol_floor(other)
    assert exc.value.code == "target_pairing_protocol_conflict"
    with pytest.raises(MeshPairingStoreError) as exc:
        await restarted.put_target_pairing_current_tail(other)
    assert exc.value.code == "target_pairing_fence_conflict"
    with pytest.raises(MeshPairingStoreError) as exc:
        await restarted.put_target_pairing_fence(other)
    assert exc.value.code == "target_pairing_fence_conflict"


async def test_target_pairing_admission_anchor_is_direct_immutable_and_restart_safe() -> None:
    storage = InMemoryStorage()
    store = MeshPairingStore(storage, prefix=_prefix())
    anchor = SignedTargetPairingAdmissionAnchor.sign(
        TargetPairingAdmissionAnchor(
            protocol_version=1,
            space_id="alpha",
            target_public_key=_TARGET_IDENTITY.public_key,
            target_fingerprint=_TARGET_IDENTITY.fingerprint,
            issued_at_ms=1_000,
        ),
        _TARGET_IDENTITY.private_key,
    )
    await store.put_target_pairing_admission_anchor(anchor)
    assert await store.get_target_pairing_admission_anchor("alpha") == anchor
    assert storage.list_calls == 0

    # Timestamp is local observation only; the first signed bytes remain the
    # permanent target identity discriminator.
    timestamp_retry = SignedTargetPairingAdmissionAnchor.sign(
        TargetPairingAdmissionAnchor(
            protocol_version=1,
            space_id="alpha",
            target_public_key=_TARGET_IDENTITY.public_key,
            target_fingerprint=_TARGET_IDENTITY.fingerprint,
            issued_at_ms=2_000,
        ),
        _TARGET_IDENTITY.private_key,
    )
    await store.put_target_pairing_admission_anchor(timestamp_retry)
    assert await store.get_target_pairing_admission_anchor("alpha") == anchor

    other = generate_mesh_identity()
    conflicting = SignedTargetPairingAdmissionAnchor.sign(
        TargetPairingAdmissionAnchor(
            protocol_version=1,
            space_id="alpha",
            target_public_key=other.public_key,
            target_fingerprint=other.fingerprint,
            issued_at_ms=1_000,
        ),
        other.private_key,
    )
    with pytest.raises(MeshPairingStoreError) as exc:
        await store.put_target_pairing_admission_anchor(conflicting)
    assert exc.value.code == "target_pairing_protocol_conflict"

    restarted = MeshPairingStore(storage, prefix=store._prefix)
    assert await restarted.get_target_pairing_admission_anchor("alpha") == anchor


async def test_target_acceptance_intent_migration_inventory_is_bounded_and_marked() -> None:
    storage = InMemoryStorage()
    store = MeshPairingStore(storage, prefix=_prefix())
    intent = {
        "pair_id": _P1,
        "space_id": "alpha",
        "invitation_digest": "a" * 64,
        "source_fingerprint": _IDENTITY.fingerprint,
        "target_fingerprint": _TARGET_IDENTITY.fingerprint,
        "requested_scopes": ["read"],
    }
    await store.put_target_acceptance_intent(_P1, intent)
    assert await store.target_pairing_admission_anchor_migration_complete() is False
    assert await store.list_target_acceptance_intents_for_migration() == [(_P1, intent)]
    await store.mark_target_pairing_admission_anchor_migration_complete()
    assert await store.target_pairing_admission_anchor_migration_complete() is True


async def test_target_pairing_fence_transitions_are_monotonic_and_owner_bound() -> None:
    """Only a held owner can settle, and later acceptance is explicit."""

    storage = InMemoryStorage()
    store = MeshPairingStore(storage, prefix=_prefix())
    held = SignedTargetPairingFenceAuthority.sign(
        _target_pairing_fence(), _TARGET_IDENTITY.private_key
    )
    await store.put_target_pairing_fence(held)
    await store.put_target_pairing_current_tail(held)

    # An exact crash retry may observe a later local timestamp, but cannot
    # replace the first signed authority bytes.
    timestamp_retry = SignedTargetPairingFenceAuthority.sign(
        replace(held.authority, issued_at_ms=2_000), _TARGET_IDENTITY.private_key
    )
    await store.put_target_pairing_fence(timestamp_retry)
    assert await store.get_target_pairing_fence("alpha") == held

    with pytest.raises(MeshPairingStoreError) as exc:
        await store.put_target_pairing_fence(
            SignedTargetPairingFenceAuthority.sign(
                _target_pairing_fence(pair_id=_P2), _TARGET_IDENTITY.private_key
            )
        )
    assert exc.value.code == "target_pairing_fence_conflict"

    with pytest.raises(MeshPairingStoreError) as exc:
        await store.put_target_pairing_fence(
            SignedTargetPairingFenceAuthority.sign(
                _target_pairing_fence(invitation_digest="c" * 64),
                _TARGET_IDENTITY.private_key,
            )
        )
    assert exc.value.code == "target_pairing_fence_conflict"

    terminal = SignedTargetPairingFenceAuthority.sign(
        _target_pairing_fence(phase="terminal_confirmed"),
        _TARGET_IDENTITY.private_key,
    )
    await store.put_target_pairing_fence(terminal)
    await store.put_target_pairing_current_tail(terminal)
    assert await store.get_target_pairing_fence("alpha") == terminal

    with pytest.raises(MeshPairingStoreError) as exc:
        await store.put_target_pairing_fence(
            SignedTargetPairingFenceAuthority.sign(
                _target_pairing_fence(phase="released"), _TARGET_IDENTITY.private_key
            )
        )
    assert exc.value.code == "target_pairing_fence_conflict"

    next_held = SignedTargetPairingFenceAuthority.sign(
        _target_pairing_fence(pair_id=_P2), _TARGET_IDENTITY.private_key
    )
    # Completion means the target joined the mesh: a later invitation must not
    # replace that terminal authority merely because its local workflow record
    # is absent.  Only a proven pre-mutation release can make the space
    # available to a different target pair.
    with pytest.raises(MeshPairingStoreError) as exc:
        await store.put_target_pairing_fence(next_held, replace_settled=True)
    assert exc.value.code == "target_pairing_fence_conflict"
    assert await store.get_target_pairing_fence("alpha") == terminal
    with pytest.raises(MeshPairingStoreError) as exc:
        await store.put_target_pairing_current_tail(next_held, replace_settled=True)
    assert exc.value.code == "target_pairing_fence_conflict"
    assert await store.get_target_pairing_current_tail("alpha") == terminal

    released_storage = InMemoryStorage()
    released_store = MeshPairingStore(released_storage, prefix=_prefix())
    await released_store.put_target_pairing_fence(held)
    await released_store.put_target_pairing_current_tail(held)
    released = SignedTargetPairingFenceAuthority.sign(
        _target_pairing_fence(phase="released"), _TARGET_IDENTITY.private_key
    )
    await released_store.put_target_pairing_fence(released)
    await released_store.put_target_pairing_current_tail(released)
    assert await released_store.get_target_pairing_fence("alpha") == released
    await released_store.put_target_pairing_fence(
        next_held, replace_settled=True
    )
    await released_store.put_target_pairing_current_tail(
        next_held, replace_settled=True
    )
    assert await released_store.get_target_pairing_fence("alpha") == next_held


async def test_target_pairing_fence_store_rejects_alias_and_missing_terminal_chain() -> None:
    """A copied or truncated durable fence never becomes target write authority."""

    storage = InMemoryStorage()
    store = MeshPairingStore(storage, prefix=_prefix())
    terminal = SignedTargetPairingFenceAuthority.sign(
        _target_pairing_fence(phase="terminal_confirmed"),
        _TARGET_IDENTITY.private_key,
    )

    storage.objects[store._target_pairing_fence_key("beta")] = (
        terminal.canonical_bytes().decode("utf-8")
    )
    with pytest.raises(MeshPairingStoreError) as exc:
        await store.get_target_pairing_fence("beta")
    assert exc.value.code == "corrupt_state"

    malformed = terminal.as_dict()
    malformed["authority"]["source_activation_receipt"] = None
    storage.objects[store._target_pairing_fence_key("alpha")] = canonical_dumps(
        malformed
    ).decode("utf-8")
    with pytest.raises(MeshPairingError) as exc:
        await store.get_target_pairing_fence("alpha")
    assert exc.value.code == "invalid_target_pairing_fence"


async def test_restore_source_activation_receipt_replaces_only_timestamp_variant() -> None:
    """Target-confirmed bytes may replace a timestamp variant, never authority."""

    storage = InMemoryStorage()
    store = MeshPairingStore(storage, prefix=_prefix())
    original_receipt = _source_activation_receipt()
    original = SignedSourceActivationReceipt.sign(
        original_receipt, _IDENTITY.private_key
    )
    target_confirmed = SignedSourceActivationReceipt.sign(
        replace(original_receipt, confirmed_at_ms=2_000), _IDENTITY.private_key
    )
    await store.put_source_activation_receipt(original)

    await store.restore_source_activation_receipt(target_confirmed)
    assert await store.get_source_activation_receipt(_P1) == target_confirmed
    writes_after_restore = len(storage.put_calls)
    await store.restore_source_activation_receipt(target_confirmed)
    assert len(storage.put_calls) == writes_after_restore

    incompatible = SignedSourceActivationReceipt.sign(
        replace(original_receipt, activation_event_id="b" * 64),
        _IDENTITY.private_key,
    )
    with pytest.raises(MeshPairingStoreError) as exc:
        await store.restore_source_activation_receipt(incompatible)
    assert exc.value.code == "source_activation_receipt_conflict"
    assert await store.get_source_activation_receipt(_P1) == target_confirmed


async def test_source_terminal_disposition_is_timestamp_idempotent_and_strict() -> None:
    """Target-release evidence has one immutable source authority per pair."""

    store = _store()
    first_receipt = _source_terminal_disposition()
    first = SignedSourceTerminalDispositionReceipt.sign(
        first_receipt, _IDENTITY.private_key
    )
    await store.put_source_terminal_disposition(first)
    assert await store.get_source_terminal_disposition(_P1) == first

    timestamp_retry = SignedSourceTerminalDispositionReceipt.sign(
        replace(first_receipt, issued_at_ms=2_000), _IDENTITY.private_key
    )
    await store.put_source_terminal_disposition(timestamp_retry)
    assert await store.get_source_terminal_disposition(_P1) == first

    conflicting = SignedSourceTerminalDispositionReceipt.sign(
        replace(first_receipt, claim_digest="d" * 64), _IDENTITY.private_key
    )
    with pytest.raises(MeshPairingStoreError) as exc:
        await store.put_source_terminal_disposition(conflicting)
    assert exc.value.code == "terminal_disposition_conflict"
    assert await store.get_source_terminal_disposition(_P1) == first

    with pytest.raises(MeshPairingStoreError) as exc:
        await store.put_source_terminal_disposition(first.receipt)  # type: ignore[arg-type]
    assert exc.value.code == "invalid_terminal_disposition"


async def test_source_pending_eviction_intent_is_timestamp_idempotent_and_strict() -> None:
    """The source cannot mint a second removal authorization for one pair."""

    store = _store()
    first_intent = _source_pending_eviction_intent()
    first = SignedSourcePendingEvictionIntent.sign(
        first_intent, _IDENTITY.private_key
    )
    await store.put_source_pending_eviction_intent(first)
    assert await store.get_source_pending_eviction_intent(_P1) == first

    timestamp_retry = SignedSourcePendingEvictionIntent.sign(
        replace(first_intent, issued_at_ms=2_000), _IDENTITY.private_key
    )
    await store.put_source_pending_eviction_intent(timestamp_retry)
    assert await store.get_source_pending_eviction_intent(_P1) == first

    conflicting = SignedSourcePendingEvictionIntent.sign(
        replace(first_intent, membership_view_digest="d" * 64),
        _IDENTITY.private_key,
    )
    with pytest.raises(MeshPairingStoreError) as exc:
        await store.put_source_pending_eviction_intent(conflicting)
    assert exc.value.code == "pending_eviction_intent_conflict"
    assert await store.get_source_pending_eviction_intent(_P1) == first

    with pytest.raises(MeshPairingStoreError) as exc:
        await store.put_source_pending_eviction_intent(first.intent)  # type: ignore[arg-type]
    assert exc.value.code == "invalid_pending_eviction_intent"


async def test_source_preclaim_cancel_barrier_is_timestamp_idempotent_and_strict() -> None:
    """An ISSUED abort has one immutable pre-claim authority per pair."""

    store = _store()
    first_barrier = _source_preclaim_cancel_barrier()
    first = SignedSourcePreClaimCancelBarrier.sign(
        first_barrier, _IDENTITY.private_key
    )
    await store.put_source_preclaim_cancel_barrier(first)
    assert await store.get_source_preclaim_cancel_barrier(_P1) == first

    timestamp_retry = SignedSourcePreClaimCancelBarrier.sign(
        replace(first_barrier, issued_at_ms=2_000), _IDENTITY.private_key
    )
    await store.put_source_preclaim_cancel_barrier(timestamp_retry)
    assert await store.get_source_preclaim_cancel_barrier(_P1) == first

    conflicting = SignedSourcePreClaimCancelBarrier.sign(
        replace(first_barrier, membership_view_digest="d" * 64),
        _IDENTITY.private_key,
    )
    with pytest.raises(MeshPairingStoreError) as exc:
        await store.put_source_preclaim_cancel_barrier(conflicting)
    assert exc.value.code == "preclaim_cancel_barrier_conflict"
    assert await store.get_source_preclaim_cancel_barrier(_P1) == first

    with pytest.raises(MeshPairingStoreError) as exc:
        await store.put_source_preclaim_cancel_barrier(first.barrier)  # type: ignore[arg-type]
    assert exc.value.code == "invalid_preclaim_cancel_barrier"


@pytest.mark.parametrize(
    ("build", "put_name", "get_name"),
    (
        (
            lambda: SignedSourceTerminalDispositionReceipt.sign(
                _source_terminal_disposition(), _IDENTITY.private_key
            ),
            "put_source_terminal_disposition",
            "get_source_terminal_disposition",
        ),
        (
            lambda: SignedSourcePendingEvictionIntent.sign(
                _source_pending_eviction_intent(), _IDENTITY.private_key
            ),
            "put_source_pending_eviction_intent",
            "get_source_pending_eviction_intent",
        ),
        (
            lambda: SignedSourcePreClaimCancelBarrier.sign(
                _source_preclaim_cancel_barrier(), _IDENTITY.private_key
            ),
            "put_source_preclaim_cancel_barrier",
            "get_source_preclaim_cancel_barrier",
        ),
    ),
    ids=("terminal-disposition", "pending-eviction", "preclaim-barrier"),
)
async def test_source_terminal_authority_exact_retry_has_no_second_write(
    build, put_name: str, get_name: str
) -> None:
    """An exact retry preserves the first authority bytes without another write."""

    storage = InMemoryStorage()
    store = MeshPairingStore(storage, prefix=_prefix())
    signed = build()
    put = getattr(store, put_name)
    get = getattr(store, get_name)

    await put(signed)
    write_count = len(storage.put_calls)
    await put(signed)

    assert len(storage.put_calls) == write_count
    assert await get(_P1) == signed


@pytest.mark.parametrize(
    ("build", "key_name", "get_name"),
    (
        (
            lambda: SignedSourceTerminalDispositionReceipt.sign(
                _source_terminal_disposition(), _IDENTITY.private_key
            ),
            "_source_terminal_disposition_key",
            "get_source_terminal_disposition",
        ),
        (
            lambda: SignedSourcePendingEvictionIntent.sign(
                _source_pending_eviction_intent(), _IDENTITY.private_key
            ),
            "_source_pending_eviction_intent_key",
            "get_source_pending_eviction_intent",
        ),
        (
            lambda: SignedSourcePreClaimCancelBarrier.sign(
                _source_preclaim_cancel_barrier(), _IDENTITY.private_key
            ),
            "_source_preclaim_cancel_barrier_key",
            "get_source_preclaim_cancel_barrier",
        ),
    ),
    ids=("terminal-disposition", "pending-eviction", "preclaim-barrier"),
)
async def test_source_terminal_authority_key_record_mismatch_fails_closed(
    build, key_name: str, get_name: str
) -> None:
    """A valid signed record under another pair key is corrupt, never adopted."""

    storage = InMemoryStorage()
    store = MeshPairingStore(storage, prefix=_prefix())
    signed = build()
    key = getattr(store, key_name)(_P2)
    await storage.put(key, canonical_dumps(signed.as_dict()).decode("utf-8"))

    with pytest.raises(MeshPairingStoreError) as exc:
        await getattr(store, get_name)(_P2)
    assert exc.value.code == "corrupt_state"


async def test_target_terminal_confirmation_is_timestamp_idempotent_and_strict() -> None:
    """A terminal all-ACK proof has one authority despite retry timestamps."""

    store = _store()
    first_receipt = _target_terminal_confirmation()
    first = SignedTargetTerminalConfirmationReceipt.sign(
        first_receipt, _TARGET_IDENTITY.private_key
    )
    await store.put_target_terminal_confirmation(first)

    timestamp_retry = SignedTargetTerminalConfirmationReceipt.sign(
        replace(first_receipt, confirmed_at_ms=2_000), _TARGET_IDENTITY.private_key
    )
    await store.put_target_terminal_confirmation(timestamp_retry)
    assert await store.get_target_terminal_confirmation(_P1) == first

    conflicting = SignedTargetTerminalConfirmationReceipt.sign(
        replace(first_receipt, source_activation_receipt_digest="f" * 64),
        _TARGET_IDENTITY.private_key,
    )
    with pytest.raises(MeshPairingStoreError) as exc:
        await store.put_target_terminal_confirmation(conflicting)
    assert exc.value.code == "terminal_confirmation_conflict"
    assert await store.get_target_terminal_confirmation(_P1) == first

    with pytest.raises(MeshPairingStoreError) as exc:
        await store.put_target_terminal_confirmation(first.receipt)  # type: ignore[arg-type]
    assert exc.value.code == "invalid_terminal_confirmation"


async def test_secret_burn_is_one_time_across_restart() -> None:
    storage = InMemoryStorage()
    prefix = _prefix()
    st1 = MeshPairingStore(storage, prefix=prefix)
    assert await st1.is_secret_burned(_P1) is False
    await st1.burn_secret(_P1, "a" * 64, now_ms=1)
    assert await st1.is_secret_burned(_P1) is True
    assert await st1.is_secret_burned(_P1, secret_digest="a" * 64) is True
    with pytest.raises(MeshPairingStoreError) as exc:
        await st1.is_secret_burned(_P1, secret_digest="b" * 64)
    assert exc.value.code == "secret_conflict"
    with pytest.raises(MeshPairingStoreError) as exc:
        await st1.burn_secret(_P1, "b" * 64, now_ms=2)
    assert exc.value.code == "secret_conflict"
    st2 = MeshPairingStore(storage, prefix=prefix)  # restart
    assert await st2.is_secret_burned(_P1) is True


async def test_nonce_dedup() -> None:
    st = _store()
    nonce = "nonce_" + "a" * 64
    assert await st.record_nonce(
        nonce, pair_id=_P1, claim_digest="a" * 64, now_ms=1
    ) == "new"
    assert await st.record_nonce(
        nonce, pair_id=_P1, claim_digest="a" * 64, now_ms=2
    ) == "same"
    assert await st.record_nonce(
        nonce, pair_id=_P2, claim_digest="b" * 64, now_ms=3
    ) == "different"


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
