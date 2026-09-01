# -*- coding: utf-8 -*-
"""Project Mesh pairing state-machine + durable session model tests (P10-3)."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import replace

import pytest

from live_mem.mesh.identity import decode_mesh_public_key, generate_mesh_identity
from live_mem.mesh.pairing_state import (
    BlockedRecoveryEvidence,
    MeshPairingError,
    MeshPairingRole,
    MeshPairingSession,
    MeshPairingState,
    ImportValidatedAuthority,
    SignedSourceActivationReceipt,
    SignedSourcePendingEvictionIntent,
    SignedSourcePreClaimCancelBarrier,
    SignedSourceTerminalDispositionReceipt,
    SignedSourceActivationMigrationAuthority,
    SignedBlockedRecoveryEvidence,
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
    SignedSourceBootstrapEvidence,
    SourcePreparationIntent,
    SourcePreparationState,
    TargetActivationReceipt,
    TargetPairingAdmissionAnchor,
    TargetPairingFenceAuthority,
    TargetTerminalConfirmationReceipt,
    PRE_MUTATION_STATES,
    TERMINAL_STATES,
)

_IDENTITY = generate_mesh_identity()
_SRC_KEY = _IDENTITY.public_key
_SRC_FP = _IDENTITY.fingerprint
_TARGET_IDENTITY = generate_mesh_identity()
_TARGET_FP = _TARGET_IDENTITY.fingerprint
_PAIR = "pair_" + "a" * 32
_TTL_MS = 3_600_000


def _membership_key() -> str:
    raw = decode_mesh_public_key(_SRC_KEY)
    return "ed25519:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _preparation(**over) -> SourcePreparationIntent:
    base = dict(
        preparation_id="prep_" + "b" * 32,
        protocol_version=1,
        state=SourcePreparationState.PREPARING.value,
        space_id="mesh-test-space",
        source_fingerprint=_SRC_FP,
        membership_public_key=_membership_key(),
        node_id=_SRC_FP.split(":", 1)[1],
        display_name="Mesh A",
        public_url="https://a.example",
        started_at_ms=1_000,
        started_at_iso="1970-01-01T00:00:01+00:00",
        completed_at_ms=0,
        expected_state_token="e" * 64,
    )
    base.update(over)
    return SourcePreparationIntent(**base)


def _source_bootstrap_evidence(**over) -> SourceBootstrapEvidence:
    base = dict(
        pair_id=_PAIR,
        protocol_version=1,
        space_id="mesh-test-space",
        source_fingerprint=_SRC_FP,
        target_fingerprint=_TARGET_FP,
        membership_epoch=2,
        membership_snapshot_digest="b" * 64,
        membership_view_digest="c" * 64,
        manifest_digest="a" * 64,
        bank_version=1,
        commit_id="commit-1",
        node_digest="d" * 64,
        term=2,
        term_digest="e" * 64,
        token_state="free",
        token_term=2,
        token_fencing_token=1,
        token_membership_epoch=2,
        token_bank_version=1,
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
        pair_id=_PAIR,
        protocol_version=1,
        space_id="mesh-test-space",
        source_fingerprint=_SRC_FP,
        target_fingerprint=_TARGET_FP,
        base_epoch=2,
        requires_terminal_confirmation=True,
        issued_at_ms=1_000,
    )
    base.update(over)
    return SourceActivationMigrationAuthority(**base)


def _import_validation(**over) -> ImportValidatedAuthority:
    base = dict(
        pair_id=_PAIR,
        protocol_version=1,
        space_id="mesh-test-space",
        source_fingerprint=_SRC_FP,
        target_fingerprint=_TARGET_FP,
        local_node_id=_TARGET_FP.split(":", 1)[1],
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
        pair_id=_PAIR,
        protocol_version=1,
        space_id="mesh-test-space",
        source_fingerprint=_SRC_FP,
        target_fingerprint=_TARGET_FP,
        # Source node IDs from pre-existing Hivemind spaces are not required to
        # be the mesh-fingerprint suffix.  The receipt binds the actual signed
        # e+2 event origin instead.
        source_node_id="source-node-a",
        target_node_id=_TARGET_FP.split(":", 1)[1],
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
        pair_id=_PAIR,
        protocol_version=1,
        disposition="pre_t1_cancel",
        space_id="mesh-test-space",
        source_public_key=_SRC_KEY,
        source_fingerprint=_SRC_FP,
        target_public_key=_TARGET_IDENTITY.public_key,
        target_fingerprint=_TARGET_FP,
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
        pair_id=_PAIR,
        protocol_version=1,
        space_id="mesh-test-space",
        source_public_key=_SRC_KEY,
        source_fingerprint=_SRC_FP,
        target_public_key=_TARGET_IDENTITY.public_key,
        target_fingerprint=_TARGET_FP,
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
        pair_id=_PAIR,
        protocol_version=1,
        space_id="mesh-test-space",
        source_public_key=_SRC_KEY,
        source_fingerprint=_SRC_FP,
        invitation_digest="a" * 64,
        base_epoch=1,
        membership_epoch=1,
        membership_view_digest="c" * 64,
        issued_at_ms=1_002,
    )
    base.update(over)
    return SourcePreClaimCancelBarrier(**base)


def _target_pairing_fence(**over) -> TargetPairingFenceAuthority:
    """Build a direct target-space fence, including a valid all-ACK chain.

    The terminal record deliberately builds all three signed receipts from one
    exact target receipt.  A structurally valid but differently signed receipt
    must not pass merely because its public fields look similar.
    """

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
                pair_id=_PAIR,
                protocol_version=1,
                space_id="mesh-test-space",
                source_fingerprint=_SRC_FP,
                target_fingerprint=_TARGET_FP,
                source_node_id="source-node-a",
                target_node_id=_TARGET_FP.split(":", 1)[1],
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
                pair_id=_PAIR,
                protocol_version=1,
                space_id="mesh-test-space",
                source_fingerprint=_SRC_FP,
                target_fingerprint=_TARGET_FP,
                base_epoch=1,
                membership_epoch=3,
                source_activation_receipt_digest=source_digest,
                target_activation_receipt_digest=target_digest,
                confirmed_at_ms=1_003,
            ),
            _TARGET_IDENTITY.private_key,
        )

    base = dict(
        pair_id=_PAIR,
        protocol_version=1,
        phase=phase,
        space_id="mesh-test-space",
        source_public_key=_SRC_KEY,
        source_fingerprint=_SRC_FP,
        target_public_key=_TARGET_IDENTITY.public_key,
        target_fingerprint=_TARGET_FP,
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


def _session(role: MeshPairingRole, state: MeshPairingState, **over) -> MeshPairingSession:
    base = dict(
        pair_id=_PAIR,
        role=role.value,
        state=state.value,
        space_id="mesh-test-space",
        protocol_version=1,
        source_public_key=_SRC_KEY,
        source_fingerprint=_SRC_FP,
        source_endpoint="https://a.example",
        target_public_key="",
        target_fingerprint="",
        target_endpoint="",
        granted_scopes=("read",),
        base_epoch=0,
        invitation_digest="",
        secret_digest="",
        claim_digest="",
        approval_digest="",
        bootstrap_manifest_digest="",
        bootstrap_bank_version=-1,
        activation_event_id="",
        last_error="",
        created_at_ms=1_000,
        updated_at_ms=1_000,
        expires_at_ms=1_000 + _TTL_MS,
    )
    base.update(over)
    return MeshPairingSession(**base)


def test_ten_states_are_exactly_frozen() -> None:
    assert {s.value for s in MeshPairingState} == {
        "issued", "claimed", "approved", "transferring", "awaiting_acks",
        "active", "expired", "cancelled", "refused", "blocked_recovery",
    }


def test_source_preparation_states_are_closed() -> None:
    assert {state.value for state in SourcePreparationState} == {
        "preparing",
        "complete",
    }


@pytest.mark.parametrize("space_id", ["_system", "../escape", "a/b", ""])
def test_session_space_id_rejects_internal_or_path_names(space_id: str) -> None:
    with pytest.raises(MeshPairingError) as exc:
        _session(MeshPairingRole.TARGET, MeshPairingState.TRANSFERRING, space_id=space_id)
    assert exc.value.code == "invalid_space_id"


def test_source_preparation_roundtrip_and_completion_are_exact() -> None:
    preparing = _preparation()
    restored = SourcePreparationIntent.from_dict(preparing.as_dict())
    assert restored == preparing
    assert restored.canonical_bytes() == preparing.canonical_bytes()

    complete = preparing.complete(2_000)
    assert complete.state_enum is SourcePreparationState.COMPLETE
    assert complete.completed_at_ms == 2_000
    assert {
        key: value
        for key, value in complete.as_dict().items()
        if key not in {"state", "completed_at_ms"}
    } == {
        key: value
        for key, value in preparing.as_dict().items()
        if key not in {"state", "completed_at_ms"}
    }
    with pytest.raises(MeshPairingError) as exc:
        complete.complete(3_000)
    assert exc.value.code == "illegal_transition"


def test_source_preparation_rejects_divergent_identity_time_and_shape() -> None:
    with pytest.raises(MeshPairingError) as exc:
        _preparation(node_id="0" * 64)
    assert exc.value.code == "invalid_node_id"

    other_raw = decode_mesh_public_key(generate_mesh_identity().public_key)
    other_membership_key = (
        "ed25519:"
        + base64.urlsafe_b64encode(other_raw).decode("ascii").rstrip("=")
    )
    with pytest.raises(MeshPairingError) as exc:
        _preparation(membership_public_key=other_membership_key)
    assert exc.value.code == "identity_mismatch"

    with pytest.raises(MeshPairingError) as exc:
        _preparation(started_at_iso="1970-01-01T00:00:02+00:00")
    assert exc.value.code == "invalid_timestamp"

    data = _preparation().as_dict()
    data["unexpected"] = "value"
    with pytest.raises(MeshPairingError) as exc:
        SourcePreparationIntent.from_dict(data)
    assert exc.value.code == "invalid_preparation"


def test_source_bootstrap_evidence_is_strict_and_canonical() -> None:
    evidence = _source_bootstrap_evidence()
    assert SourceBootstrapEvidence.from_bytes(evidence.canonical_bytes()) == evidence
    signed = SignedSourceBootstrapEvidence.sign(evidence, _IDENTITY.private_key)
    signed.verify(_SRC_KEY)
    assert SignedSourceBootstrapEvidence.from_bytes(signed.canonical_bytes()) == signed

    empty = _source_bootstrap_evidence(
        bank_version=-1,
        commit_id="",
        pointer_bank_version=-1,
        pointer_commit_id="",
        selected_commit_digest="",
    )
    assert SourceBootstrapEvidence.from_dict(empty.as_dict()) == empty

    with pytest.raises(MeshPairingError) as exc:
        _source_bootstrap_evidence(pointer_commit_id="other-commit")
    assert exc.value.code == "invalid_source_evidence"

    with pytest.raises(MeshPairingError) as exc:
        _source_bootstrap_evidence(token_state="unknown")
    assert exc.value.code == "invalid_source_evidence"

    data = evidence.as_dict()
    data.pop("term")
    with pytest.raises(MeshPairingError) as exc:
        SourceBootstrapEvidence.from_dict(data)
    assert exc.value.code == "invalid_source_evidence"


def test_import_validation_authority_is_pair_and_target_bound() -> None:
    authority = _import_validation()
    assert ImportValidatedAuthority.from_bytes(authority.canonical_bytes()) == authority

    empty = _import_validation(
        bank_version=-1, commit_id="", selected_commit_digest=""
    )
    assert ImportValidatedAuthority.from_dict(empty.as_dict()) == empty

    with pytest.raises(MeshPairingError) as exc:
        _import_validation(local_node_id=_SRC_FP.split(":", 1)[1])
    assert exc.value.code == "invalid_import_validation"

    with pytest.raises(MeshPairingError) as exc:
        _import_validation(commit_id="")
    assert exc.value.code == "invalid_import_validation"

    with pytest.raises(MeshPairingError) as exc:
        _import_validation(selected_commit_digest="")
    assert exc.value.code == "invalid_import_validation"

    data = replace(authority, manifest_digest="e" * 64).as_dict()
    data["unexpected"] = True
    with pytest.raises(MeshPairingError) as exc:
        ImportValidatedAuthority.from_dict(data)
    assert exc.value.code == "invalid_import_validation"


def test_target_activation_receipt_is_signed_and_epoch_bound() -> None:
    receipt = _target_activation_receipt()
    assert TargetActivationReceipt.from_bytes(receipt.canonical_bytes()) == receipt
    signed = SignedTargetActivationReceipt.sign(
        receipt, _TARGET_IDENTITY.private_key
    )
    signed.verify(_TARGET_IDENTITY.public_key)
    assert SignedTargetActivationReceipt.from_bytes(signed.canonical_bytes()) == signed

    with pytest.raises(MeshPairingError) as exc:
        _target_activation_receipt(membership_epoch=2)
    assert exc.value.code == "invalid_activation_receipt"


def test_source_activation_receipt_is_signed_and_exactly_bound() -> None:
    receipt = _source_activation_receipt()
    assert SourceActivationReceipt.from_bytes(receipt.canonical_bytes()) == receipt
    signed = SignedSourceActivationReceipt.sign(receipt, _IDENTITY.private_key)
    signed.verify(_IDENTITY.public_key)
    assert SignedSourceActivationReceipt.from_bytes(signed.canonical_bytes()) == signed

    with pytest.raises(MeshPairingError) as exc:
        _source_activation_receipt(membership_epoch=2)
    assert exc.value.code == "invalid_source_activation_receipt"

    with pytest.raises(MeshPairingError) as exc:
        _source_activation_receipt(target_node_id="different-target")
    assert exc.value.code == "invalid_source_activation_receipt"

    data = receipt.as_dict()
    data["unexpected"] = True
    with pytest.raises(MeshPairingError) as exc:
        SourceActivationReceipt.from_dict(data)
    assert exc.value.code == "invalid_source_activation_receipt"

    data = receipt.as_dict()
    data["unexpected"] = True
    with pytest.raises(MeshPairingError) as exc:
        TargetActivationReceipt.from_dict(data)
    assert exc.value.code == "invalid_activation_receipt"


def test_source_terminal_disposition_is_signed_and_epoch_bound() -> None:
    receipt = _source_terminal_disposition()
    assert (
        SourceTerminalDispositionReceipt.from_bytes(receipt.canonical_bytes())
        == receipt
    )
    signed = SignedSourceTerminalDispositionReceipt.sign(
        receipt, _IDENTITY.private_key
    )
    signed.verify(_SRC_KEY)
    assert (
        SignedSourceTerminalDispositionReceipt.from_bytes(signed.canonical_bytes())
        == signed
    )

    with pytest.raises(MeshPairingError) as exc:
        _source_terminal_disposition(membership_epoch=2)
    assert exc.value.code == "invalid_terminal_disposition"
    with pytest.raises(MeshPairingError) as exc:
        _source_terminal_disposition(
            disposition="pending_evicted", membership_epoch=1
        )
    assert exc.value.code == "invalid_terminal_disposition"

    data = receipt.as_dict()
    data["claim_digest"] = data["invitation_digest"]
    with pytest.raises(MeshPairingError) as exc:
        SourceTerminalDispositionReceipt.from_dict(data)
    assert exc.value.code == "invalid_terminal_disposition"

    tampered = SignedSourceTerminalDispositionReceipt(
        receipt=replace(receipt, invitation_digest="d" * 64),
        signature=signed.signature,
    )
    with pytest.raises(MeshPairingError) as exc:
        tampered.verify(_SRC_KEY)
    assert exc.value.code == "bad_signature"


def test_source_pending_eviction_intent_is_signed_and_t1_bound() -> None:
    intent = _source_pending_eviction_intent()
    assert SourcePendingEvictionIntent.from_bytes(intent.canonical_bytes()) == intent
    signed = SignedSourcePendingEvictionIntent.sign(intent, _IDENTITY.private_key)
    signed.verify(_SRC_KEY)
    assert SignedSourcePendingEvictionIntent.from_bytes(signed.canonical_bytes()) == signed

    with pytest.raises(MeshPairingError) as exc:
        _source_pending_eviction_intent(membership_epoch=3)
    assert exc.value.code == "invalid_pending_eviction_intent"
    with pytest.raises(MeshPairingError) as exc:
        _source_pending_eviction_intent(claim_digest="a" * 64)
    assert exc.value.code == "invalid_pending_eviction_intent"

    tampered = SignedSourcePendingEvictionIntent(
        intent=replace(intent, membership_view_digest="d" * 64),
        signature=signed.signature,
    )
    with pytest.raises(MeshPairingError) as exc:
        tampered.verify(_SRC_KEY)
    assert exc.value.code == "bad_signature"


def test_source_preclaim_cancel_barrier_is_signed_and_pre_t1_bound() -> None:
    barrier = _source_preclaim_cancel_barrier()
    assert (
        SourcePreClaimCancelBarrier.from_bytes(barrier.canonical_bytes()) == barrier
    )
    signed = SignedSourcePreClaimCancelBarrier.sign(
        barrier, _IDENTITY.private_key
    )
    signed.verify(_SRC_KEY)
    assert (
        SignedSourcePreClaimCancelBarrier.from_bytes(signed.canonical_bytes())
        == signed
    )

    with pytest.raises(MeshPairingError) as exc:
        _source_preclaim_cancel_barrier(membership_epoch=2)
    assert exc.value.code == "invalid_preclaim_cancel_barrier"
    with pytest.raises(MeshPairingError) as exc:
        _source_preclaim_cancel_barrier(invitation_digest="")
    assert exc.value.code == "empty_field"

    tampered = SignedSourcePreClaimCancelBarrier(
        barrier=replace(barrier, membership_view_digest="d" * 64),
        signature=signed.signature,
    )
    with pytest.raises(MeshPairingError) as exc:
        tampered.verify(_SRC_KEY)
    assert exc.value.code == "bad_signature"


@pytest.mark.parametrize(
    ("build", "parse"),
    (
        (
            lambda: SignedSourceTerminalDispositionReceipt.sign(
                _source_terminal_disposition(), _IDENTITY.private_key
            ),
            SignedSourceTerminalDispositionReceipt.from_dict,
        ),
        (
            lambda: SignedSourcePendingEvictionIntent.sign(
                _source_pending_eviction_intent(), _IDENTITY.private_key
            ),
            SignedSourcePendingEvictionIntent.from_dict,
        ),
        (
            lambda: SignedSourcePreClaimCancelBarrier.sign(
                _source_preclaim_cancel_barrier(), _IDENTITY.private_key
            ),
            SignedSourcePreClaimCancelBarrier.from_dict,
        ),
        (
            lambda: SignedTargetPairingAdmissionAnchor.sign(
                TargetPairingAdmissionAnchor(
                    protocol_version=1,
                    space_id="mesh-test-space",
                    target_public_key=_TARGET_IDENTITY.public_key,
                    target_fingerprint=_TARGET_FP,
                    issued_at_ms=1_002,
                ),
                _TARGET_IDENTITY.private_key,
            ),
            SignedTargetPairingAdmissionAnchor.from_dict,
        ),
    ),
    ids=("terminal-disposition", "pending-eviction", "preclaim-barrier", "target-anchor"),
)
def test_new_signed_authorities_reject_unparseable_verifier_and_signature_shape(
    build, parse
) -> None:
    """Each new detached authority fails closed before a malformed verifier/key.

    This is deliberately shared across the four new evidence forms: a parser
    must never downgrade a non-string signature, and a verifier must not leak
    a backend key-parsing exception into a caller-controlled recovery path.
    """

    signed = build()
    with pytest.raises(MeshPairingError) as exc:
        signed.verify("not-a-mesh-public-key")
    assert exc.value.code == "invalid_key"

    malformed = signed.as_dict()
    malformed["signature"] = 7
    with pytest.raises(MeshPairingError) as exc:
        parse(malformed)
    assert exc.value.code == "invalid_signature"


def test_source_activation_migration_authority_is_signed_and_fail_closed() -> None:
    authority = _source_activation_migration_authority()
    assert (
        SourceActivationMigrationAuthority.from_dict(authority.as_dict())
        == authority
    )
    signed = SignedSourceActivationMigrationAuthority.sign(
        authority, _IDENTITY.private_key
    )
    signed.verify(_SRC_KEY)
    assert (
        SignedSourceActivationMigrationAuthority.from_dict(signed.as_dict())
        == signed
    )

    with pytest.raises(MeshPairingError) as exc:
        _source_activation_migration_authority(
            requires_terminal_confirmation=False
        )
    assert exc.value.code == "invalid_source_activation_migration"

    tampered = SignedSourceActivationMigrationAuthority(
        authority=replace(authority, base_epoch=3),
        signature=signed.signature,
    )
    with pytest.raises(MeshPairingError) as exc:
        tampered.verify(_SRC_KEY)
    assert exc.value.code == "bad_signature"


def test_target_pairing_fence_is_target_signed_and_strict_for_each_phase() -> None:
    """Every durable target-fence phase stays bound to the target identity."""

    for phase in ("held", "terminal_confirmed", "released"):
        authority = _target_pairing_fence(phase=phase)
        assert TargetPairingFenceAuthority.from_dict(authority.as_dict()) == authority

        signed = SignedTargetPairingFenceAuthority.sign(
            authority, _TARGET_IDENTITY.private_key
        )
        signed.verify(_TARGET_IDENTITY.public_key)
        assert SignedTargetPairingFenceAuthority.from_dict(signed.as_dict()) == signed

        # The source can read this evidence but never becomes the signer for a
        # target-space ordinary-write fence.
        with pytest.raises(MeshPairingError) as exc:
            signed.verify(_SRC_KEY)
        assert exc.value.code == "bad_signature"

    held = _target_pairing_fence()
    with pytest.raises(MeshPairingError) as exc:
        _target_pairing_fence(target_fingerprint=_SRC_FP)
    assert exc.value.code == "invalid_target_pairing_fence"

    signed_held = SignedTargetPairingFenceAuthority.sign(
        held, _TARGET_IDENTITY.private_key
    )
    tampered = replace(signed_held, authority=replace(held, issued_at_ms=2_000))
    with pytest.raises(MeshPairingError) as exc:
        tampered.verify(_TARGET_IDENTITY.public_key)
    assert exc.value.code == "bad_signature"


def test_target_pairing_fence_terminal_chain_is_complete_and_exact() -> None:
    """A terminal fence is never authority with a missing or mismatched receipt."""

    terminal = _target_pairing_fence(phase="terminal_confirmed")
    with pytest.raises(MeshPairingError) as exc:
        replace(terminal, source_activation_receipt=None)
    assert exc.value.code == "invalid_target_pairing_fence"

    data = terminal.as_dict()
    assert data["target_activation_receipt"] is not None
    data["target_activation_receipt"]["signature"] = "A" * 86
    with pytest.raises(MeshPairingError) as exc:
        TargetPairingFenceAuthority.from_dict(data)
    assert exc.value.code == "invalid_target_pairing_fence"

    target_receipt = SignedTargetActivationReceipt.sign(
        _target_activation_receipt(), _TARGET_IDENTITY.private_key
    )
    target_digest = hashlib.sha256(target_receipt.canonical_bytes()).hexdigest()
    source_receipt = SignedSourceActivationReceipt.sign(
        SourceActivationReceipt(
            pair_id=_PAIR,
            protocol_version=1,
            space_id="mesh-test-space",
            source_fingerprint=_SRC_FP,
            target_fingerprint=_TARGET_FP,
            source_node_id="source-node-a",
            target_node_id=_TARGET_FP.split(":", 1)[1],
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
    mismatched_terminal = SignedTargetTerminalConfirmationReceipt.sign(
        TargetTerminalConfirmationReceipt(
            pair_id=_PAIR,
            protocol_version=1,
            space_id="mesh-test-space",
            source_fingerprint=_SRC_FP,
            target_fingerprint=_TARGET_FP,
            base_epoch=1,
            membership_epoch=3,
            source_activation_receipt_digest=hashlib.sha256(
                source_receipt.canonical_bytes()
            ).hexdigest(),
            target_activation_receipt_digest="0" * 64,
            confirmed_at_ms=1_003,
        ),
        _TARGET_IDENTITY.private_key,
    )
    with pytest.raises(MeshPairingError) as exc:
        _target_pairing_fence(
            phase="terminal_confirmed",
            target_activation_receipt=target_receipt.as_dict(),
            source_activation_receipt=source_receipt.as_dict(),
            target_terminal_confirmation=mismatched_terminal.as_dict(),
        )
    assert exc.value.code == "invalid_target_pairing_fence"

    # The terminal confirmation binds receipt digests, but the direct fence is
    # the ordinary-write authority and must itself require the two signed e+2
    # views to name the same membership projection.
    divergent_source = SignedSourceActivationReceipt.sign(
        SourceActivationReceipt(
            pair_id=_PAIR,
            protocol_version=1,
            space_id="mesh-test-space",
            source_fingerprint=_SRC_FP,
            target_fingerprint=_TARGET_FP,
            source_node_id="source-node-a",
            target_node_id=_TARGET_FP.split(":", 1)[1],
            base_epoch=1,
            membership_epoch=3,
            activation_event_id="a" * 64,
            membership_view_digest="e" * 64,
            target_activation_receipt_digest=target_digest,
            target_activation_receipt=target_receipt.as_dict(),
            confirmed_at_ms=1_002,
        ),
        _IDENTITY.private_key,
    )
    divergent_terminal = SignedTargetTerminalConfirmationReceipt.sign(
        TargetTerminalConfirmationReceipt(
            pair_id=_PAIR,
            protocol_version=1,
            space_id="mesh-test-space",
            source_fingerprint=_SRC_FP,
            target_fingerprint=_TARGET_FP,
            base_epoch=1,
            membership_epoch=3,
            source_activation_receipt_digest=hashlib.sha256(
                divergent_source.canonical_bytes()
            ).hexdigest(),
            target_activation_receipt_digest=target_digest,
            confirmed_at_ms=1_003,
        ),
        _TARGET_IDENTITY.private_key,
    )
    with pytest.raises(MeshPairingError) as exc:
        _target_pairing_fence(
            phase="terminal_confirmed",
            target_activation_receipt=target_receipt.as_dict(),
            source_activation_receipt=divergent_source.as_dict(),
            target_terminal_confirmation=divergent_terminal.as_dict(),
        )
    assert exc.value.code == "invalid_target_pairing_fence"


def test_source_activation_authorities_reject_malformed_signed_and_embedded_proofs() -> None:
    """Persisted activation proofs cannot become authority when shape-corrupted.

    The store can only retain a migration/floor after the service verifies its
    signature, while a source receipt carries the exact target-signed proof.
    Exercise the model boundary for both records so a parseable-looking raw
    rewrite cannot silently weaken either later gate.
    """

    authority = _source_activation_migration_authority()
    signed_authority = SignedSourceActivationMigrationAuthority.sign(
        authority, _IDENTITY.private_key
    )

    malformed = signed_authority.as_dict()
    malformed["authority"] = {"pair_id": _PAIR}
    with pytest.raises(MeshPairingError) as exc:
        SignedSourceActivationMigrationAuthority.from_dict(malformed)
    assert exc.value.code == "invalid_source_activation_migration"

    malformed = signed_authority.as_dict()
    malformed["signature"] = 7
    with pytest.raises(MeshPairingError) as exc:
        SignedSourceActivationMigrationAuthority.from_dict(malformed)
    assert exc.value.code == "invalid_signature"

    receipt = _source_activation_receipt()
    malformed_receipt = receipt.as_dict()
    malformed_receipt["target_activation_receipt"] = {"signature": "broken"}
    with pytest.raises(MeshPairingError) as exc:
        SourceActivationReceipt.from_dict(malformed_receipt)
    assert exc.value.code == "invalid_source_activation_receipt"

    wrong_digest = receipt.as_dict()
    wrong_digest["target_activation_receipt_digest"] = "0" * 64
    with pytest.raises(MeshPairingError) as exc:
        SourceActivationReceipt.from_dict(wrong_digest)
    assert exc.value.code == "invalid_source_activation_receipt"


@pytest.mark.parametrize(
    "kind",
    ["bootstrap", "target_activation", "source_activation", "terminal"],
)
def test_signed_activation_authorities_reject_invalid_keys_signatures_and_shapes(
    kind: str,
) -> None:
    """Every retained activation authority is strict at its signature boundary."""

    if kind == "bootstrap":
        signed = SignedSourceBootstrapEvidence.sign(
            _source_bootstrap_evidence(), _IDENTITY.private_key
        )
        parser = SignedSourceBootstrapEvidence.from_dict
    elif kind == "target_activation":
        signed = SignedTargetActivationReceipt.sign(
            _target_activation_receipt(), _TARGET_IDENTITY.private_key
        )
        parser = SignedTargetActivationReceipt.from_dict
    elif kind == "source_activation":
        signed = SignedSourceActivationReceipt.sign(
            _source_activation_receipt(), _IDENTITY.private_key
        )
        parser = SignedSourceActivationReceipt.from_dict
    else:
        terminal = TargetTerminalConfirmationReceipt(
            pair_id=_PAIR,
            protocol_version=1,
            space_id="mesh-test-space",
            source_fingerprint=_SRC_FP,
            target_fingerprint=_TARGET_FP,
            base_epoch=1,
            membership_epoch=3,
            source_activation_receipt_digest="a" * 64,
            target_activation_receipt_digest="b" * 64,
            confirmed_at_ms=1_003,
        )
        signed = SignedTargetTerminalConfirmationReceipt.sign(
            terminal, _TARGET_IDENTITY.private_key
        )
        parser = SignedTargetTerminalConfirmationReceipt.from_dict

    with pytest.raises(MeshPairingError) as exc:
        signed.verify("not-a-mesh-public-key")
    assert exc.value.code == "invalid_key"

    with pytest.raises(MeshPairingError) as exc:
        replace(signed, signature="A" * 86).verify(
            _SRC_KEY if kind in {"bootstrap", "source_activation"} else _TARGET_IDENTITY.public_key
        )
    assert exc.value.code == "bad_signature"

    malformed = signed.as_dict()
    malformed["signature"] = 7
    with pytest.raises(MeshPairingError) as exc:
        parser(malformed)
    assert exc.value.code == "invalid_signature"


def test_pre_mutation_and_terminal_partitions() -> None:
    assert PRE_MUTATION_STATES == {
        MeshPairingState.ISSUED, MeshPairingState.CLAIMED, MeshPairingState.APPROVED
    }
    assert TERMINAL_STATES == {
        MeshPairingState.ACTIVE, MeshPairingState.EXPIRED,
        MeshPairingState.CANCELLED, MeshPairingState.REFUSED,
    }
    # A terminal state and a pre-mutation state never overlap.
    assert PRE_MUTATION_STATES.isdisjoint(TERMINAL_STATES)


def test_target_pairing_admission_anchor_is_closed_and_target_signed() -> None:
    """The permanent #417 discriminator never carries tail permission."""

    anchor = TargetPairingAdmissionAnchor(
        protocol_version=1,
        space_id="mesh-test-space",
        target_public_key=_TARGET_IDENTITY.public_key,
        target_fingerprint=_TARGET_FP,
        issued_at_ms=1_000,
    )
    signed = SignedTargetPairingAdmissionAnchor.sign(
        anchor, _TARGET_IDENTITY.private_key
    )
    signed.verify(_TARGET_IDENTITY.public_key)
    assert (
        SignedTargetPairingAdmissionAnchor.from_bytes(signed.canonical_bytes())
        == signed
    )

    with pytest.raises(MeshPairingError) as exc:
        TargetPairingAdmissionAnchor.from_dict(
            {**anchor.as_dict(), "pair_id": _PAIR}
        )
    assert exc.value.code == "invalid_target_pairing_anchor"

    with pytest.raises(MeshPairingError) as exc:
        TargetPairingAdmissionAnchor(
            protocol_version=1,
            space_id="mesh-test-space",
            target_public_key=_TARGET_IDENTITY.public_key,
            target_fingerprint=_SRC_FP,
            issued_at_ms=1_000,
        )
    assert exc.value.code == "invalid_target_pairing_anchor"

    with pytest.raises(MeshPairingError) as exc:
        signed.verify(_SRC_KEY)
    assert exc.value.code == "bad_signature"


def test_source_happy_path_transitions_are_legal() -> None:
    s = _session(MeshPairingRole.SOURCE, MeshPairingState.ISSUED)
    s = s.transition(MeshPairingState.CLAIMED, now_ms=2_000)
    s = s.transition(MeshPairingState.APPROVED, now_ms=3_000)
    s = s.transition(MeshPairingState.TRANSFERRING, now_ms=4_000)
    s = s.transition(MeshPairingState.AWAITING_ACKS, now_ms=5_000)
    s = s.transition(MeshPairingState.ACTIVE, now_ms=6_000)
    assert s.state_enum is MeshPairingState.ACTIVE
    assert s.updated_at_ms == 6_000


def test_target_starts_claimed_and_cannot_be_issued() -> None:
    # target has no ISSUED edge at all
    s = _session(MeshPairingRole.TARGET, MeshPairingState.CLAIMED)
    s2 = s.transition(MeshPairingState.APPROVED, now_ms=2)
    assert s2.state_enum is MeshPairingState.APPROVED
    # target claimed cannot go to issued
    with pytest.raises(MeshPairingError):
        s.transition(MeshPairingState.ISSUED, now_ms=3)


def test_illegal_transition_fails_closed() -> None:
    s = _session(MeshPairingRole.SOURCE, MeshPairingState.ISSUED)
    # issued cannot jump straight to active
    with pytest.raises(MeshPairingError) as e:
        s.transition(MeshPairingState.ACTIVE, now_ms=2)
    assert e.value.code == "illegal_transition"


def test_post_mutation_failure_only_goes_to_blocked_recovery() -> None:
    s = _session(MeshPairingRole.SOURCE, MeshPairingState.TRANSFERRING)
    # cannot roll back a post-mutation state to a pre-mutation/terminal cancel
    with pytest.raises(MeshPairingError):
        s.transition(MeshPairingState.CANCELLED, now_ms=2)
    # the only failure edge is blocked_recovery
    b = s.transition(MeshPairingState.BLOCKED_RECOVERY, now_ms=2)
    assert b.state_enum is MeshPairingState.BLOCKED_RECOVERY
    # from blocked_recovery, resume can reach active
    assert b.transition(MeshPairingState.ACTIVE, now_ms=3).state_enum is MeshPairingState.ACTIVE


def test_target_resync_edge_blocked_recovery_to_transferring() -> None:
    # The target may re-drive a corrupt-import blocked_recovery back through
    # transferring (resync teardown + re-import); the source may NOT.
    t = _session(MeshPairingRole.TARGET, MeshPairingState.BLOCKED_RECOVERY)
    assert t.transition(MeshPairingState.TRANSFERRING, now_ms=2).state_enum is MeshPairingState.TRANSFERRING
    # still keeps the shared active/cancelled exits
    assert t.transition(MeshPairingState.ACTIVE, now_ms=2).state_enum is MeshPairingState.ACTIVE
    s = _session(MeshPairingRole.SOURCE, MeshPairingState.BLOCKED_RECOVERY)
    with pytest.raises(MeshPairingError) as e:
        s.transition(MeshPairingState.TRANSFERRING, now_ms=2)
    assert e.value.code == "illegal_transition"


def test_terminal_states_have_no_outgoing_edge() -> None:
    for term in TERMINAL_STATES:
        s = _session(MeshPairingRole.SOURCE, term)
        with pytest.raises(MeshPairingError):
            s.transition(MeshPairingState.CLAIMED, now_ms=2)


def test_expiry_only_applies_to_pre_mutation_states() -> None:
    issued = _session(MeshPairingRole.SOURCE, MeshPairingState.ISSUED)
    assert issued.is_expired(1_000 + _TTL_MS) is True  # exactly at boundary
    assert issued.is_expired(1_000 + _TTL_MS - 1) is False  # 1ms before
    transferring = _session(MeshPairingRole.SOURCE, MeshPairingState.TRANSFERRING)
    # a post-mutation session never "expires" (it blocks, never silently exits)
    assert transferring.is_expired(1_000 + _TTL_MS + 10**9) is False


def test_session_round_trips_through_canonical_bytes() -> None:
    s = _session(
        MeshPairingRole.SOURCE,
        MeshPairingState.APPROVED,
        secret_digest="a" * 64,
        invitation_digest="b" * 64,
        granted_scopes=("commit", "read"),
    )
    raw = s.canonical_bytes()
    restored = MeshPairingSession.from_bytes(raw)
    assert restored == s
    assert restored.canonical_bytes() == raw  # byte-stable


def test_scopes_must_include_read_and_be_sorted_unique() -> None:
    # missing the mandatory read floor
    with pytest.raises(MeshPairingError):
        _session(MeshPairingRole.SOURCE, MeshPairingState.ISSUED, granted_scopes=("propose",))
    # duplicate
    with pytest.raises(MeshPairingError):
        _session(MeshPairingRole.SOURCE, MeshPairingState.ISSUED, granted_scopes=("read", "read"))
    # unsorted ("read" > "propose")
    with pytest.raises(MeshPairingError):
        _session(MeshPairingRole.SOURCE, MeshPairingState.ISSUED, granted_scopes=("read", "propose"))
    # a valid sorted multi-scope set is accepted
    ok = _session(MeshPairingRole.SOURCE, MeshPairingState.ISSUED, granted_scopes=("propose", "read"))
    assert ok.granted_scopes == ("propose", "read")


def test_invalid_pair_id_and_fingerprint_rejected() -> None:
    with pytest.raises(MeshPairingError):
        _session(MeshPairingRole.SOURCE, MeshPairingState.ISSUED, pair_id="bad")
    with pytest.raises(MeshPairingError):
        _session(MeshPairingRole.SOURCE, MeshPairingState.ISSUED, source_fingerprint="hm1:zz")


def test_blocked_recovery_evidence_sign_verify_roundtrip() -> None:
    ev = BlockedRecoveryEvidence(
        pair_id=_PAIR,
        space_id="mesh-test-space",
        epoch=1,
        phase="post_admit_pre_activate",
        next_action="resume",
        manifest_digest="c" * 64,
        candidate_view_digest="d" * 64,
        activation_event_id="deadbeef",
        issued_at_ms=42,
    )
    signed = SignedBlockedRecoveryEvidence.sign(ev, _IDENTITY.private_key)
    signed.verify(_SRC_KEY)  # correct key: no raise
    restored = SignedBlockedRecoveryEvidence.from_bytes(signed.canonical_bytes())
    assert restored.evidence == ev
    restored.verify(_SRC_KEY)


def test_blocked_recovery_evidence_rejects_wrong_key_and_tamper() -> None:
    ev = BlockedRecoveryEvidence(
        pair_id=_PAIR, space_id="s", epoch=1, phase="p", next_action="resync",
        manifest_digest="", candidate_view_digest="", activation_event_id="", issued_at_ms=1,
    )
    signed = SignedBlockedRecoveryEvidence.sign(ev, _IDENTITY.private_key)
    other = generate_mesh_identity()
    with pytest.raises(MeshPairingError):
        signed.verify(other.public_key)  # wrong signer
    # tampered evidence (different epoch) must not verify under the same signature
    tampered = SignedBlockedRecoveryEvidence(
        evidence=BlockedRecoveryEvidence(
            pair_id=_PAIR, space_id="s", epoch=2, phase="p", next_action="resync",
            manifest_digest="", candidate_view_digest="", activation_event_id="", issued_at_ms=1,
        ),
        signature=signed.signature,
    )
    with pytest.raises(MeshPairingError):
        tampered.verify(_SRC_KEY)


def test_evidence_rejects_invalid_next_action() -> None:
    with pytest.raises(MeshPairingError):
        BlockedRecoveryEvidence(
            pair_id=_PAIR, space_id="s", epoch=0, phase="p", next_action="explode",
            manifest_digest="", candidate_view_digest="", activation_event_id="", issued_at_ms=0,
        )
