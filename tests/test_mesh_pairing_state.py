# -*- coding: utf-8 -*-
"""Project Mesh pairing state-machine + durable session model tests (P10-3)."""

from __future__ import annotations

import pytest

from live_mem.mesh.identity import generate_mesh_identity
from live_mem.mesh.pairing_state import (
    BlockedRecoveryEvidence,
    MeshPairingError,
    MeshPairingRole,
    MeshPairingSession,
    MeshPairingState,
    SignedBlockedRecoveryEvidence,
    PRE_MUTATION_STATES,
    TERMINAL_STATES,
)

_IDENTITY = generate_mesh_identity()
_SRC_KEY = _IDENTITY.public_key
_SRC_FP = _IDENTITY.fingerprint
_PAIR = "pair_" + "a" * 32
_TTL_MS = 3_600_000


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
